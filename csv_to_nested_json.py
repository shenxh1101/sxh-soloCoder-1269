#!/usr/bin/env python3
"""CSV to nested JSON converter — production-ready with batch, audit, and more."""

import argparse
import csv
import datetime
import json
import os
import sys
import warnings
from collections import defaultdict
from io import StringIO


SUPPORTED_AGG_FUNCS = ('sum', 'avg', 'count', 'min', 'max')
LEAF_MODES = ('auto', 'object', 'array', 'scalar')
MERGE_KEY_STRATEGIES = ('name', 'path', 'fullpath')


# ---------- helpers ----------

def _deep_set(d, keys, value):
    for k in keys[:-1]:
        if k not in d:
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _aggregate(values, agg_func, non_numeric_counter=None, col_name=None):
    if not values:
        return None
    numeric_values = []
    for v in values:
        if v is None or v == '':
            continue
        try:
            numeric_values.append(float(v))
        except (ValueError, TypeError):
            if non_numeric_counter is not None and col_name is not None:
                non_numeric_counter[col_name] += 1
            continue

    if not numeric_values:
        return None

    if agg_func == 'sum':
        result = sum(numeric_values)
    elif agg_func == 'avg':
        result = sum(numeric_values) / len(numeric_values)
    elif agg_func == 'count':
        result = len(numeric_values)
    elif agg_func == 'min':
        result = min(numeric_values)
    elif agg_func == 'max':
        result = max(numeric_values)
    else:
        raise ValueError(f"Unknown aggregation function: {agg_func}")

    if result == int(result):
        return int(result)
    return result


def _read_csv(input_source):
    if input_source == '-':
        content = sys.stdin.read()
        reader = csv.DictReader(StringIO(content))
        return list(reader), reader.fieldnames
    else:
        try:
            with open(input_source, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                return rows, reader.fieldnames
        except FileNotFoundError:
            raise


# ---------- input discovery & output path safety ----------

def discover_inputs(paths, recursive=False):
    files = []
    for path in paths:
        if path == '-':
            files.append('-')
            continue
        if os.path.isdir(path):
            if recursive:
                for root, _, filenames in os.walk(path):
                    for fn in filenames:
                        if fn.lower().endswith('.csv'):
                            files.append(os.path.join(root, fn))
            else:
                for fn in os.listdir(path):
                    full = os.path.join(path, fn)
                    if os.path.isfile(full) and fn.lower().endswith('.csv'):
                        files.append(full)
        elif os.path.isfile(path):
            files.append(path)
        else:
            warnings.warn(f"Path not found, skipping: {path}")
    return files


def compute_relative_path(path, roots=None):
    """Return the shortest relative representation of path, for merge keys."""
    if path == '-':
        return '(stdin)'
    roots = list(roots) if roots else [os.getcwd()]
    abspath = os.path.abspath(path)
    best = abspath
    for root in roots:
        try:
            rel = os.path.relpath(abspath, os.path.abspath(root))
            if len(rel) < len(best):
                best = rel
        except ValueError:
            continue
    return best.replace('\\', '/')


def compute_merge_key(file_path, strategy, roots=None):
    """Build a merge key from a file path using the selected strategy."""
    if file_path == '-':
        return '(stdin)'
    if strategy == 'name':
        return os.path.splitext(os.path.basename(file_path))[0]
    if strategy == 'path':
        return os.path.splitext(compute_relative_path(file_path, roots))[0]
    if strategy == 'fullpath':
        return os.path.splitext(os.path.abspath(file_path).replace('\\', '/'))[0]
    raise ValueError(f"Unknown merge key strategy: {strategy}")


def build_safe_batch_output_paths(files, output_dir=None, explicit_output=None):
    """Map each input file to a safe output path, avoiding overwrites.

    - If explicit_output is set and multiple files: raise ValueError (user error).
    - Else output paths are based on basename (or unique path if collisions exist).
    - Collision is resolved by embedding the relative path, then adding _1, _2...
    """
    if explicit_output is not None and len(files) > 1:
        raise ValueError(
            "Multiple input files with a single -o/--output is not allowed. "
            "Use --output-dir instead, or pass --merge to combine into one JSON."
        )

    output_dir = output_dir or '.'
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    out_map = {}
    seen = defaultdict(list)

    # First pass: compute "preferred" name, detect collisions
    for fp in files:
        if fp == '-':
            preferred = '(stdin).json'
        else:
            preferred = os.path.splitext(os.path.basename(fp))[0] + '.json'
        seen[preferred].append(fp)

    # Second pass: assign final paths, resolving collisions
    for preferred, fplist in seen.items():
        if len(fplist) == 1:
            fp = fplist[0]
            out_map[fp] = os.path.join(output_dir, preferred) if output_dir else preferred
        else:
            # Collision — use relative path, then numeric disambiguation for any remaining
            path_map = {}
            for fp in fplist:
                if fp == '-':
                    rel = '(stdin)'
                else:
                    roots = [os.getcwd()] + [
                        r for r in ['testdata', 'reports', 'data'] if os.path.isdir(r)
                    ]
                    rel = os.path.splitext(compute_relative_path(fp, roots))[0]
                    rel = rel.replace('/', '__').replace('\\', '__')
                path_map[fp] = rel

            # Check if the rel-based names still collide
            rel_seen = defaultdict(list)
            for fp, rel in path_map.items():
                rel_seen[rel + '.json'].append(fp)

            for preferred2, fplist2 in rel_seen.items():
                if len(fplist2) == 1:
                    fp = fplist2[0]
                    out_map[fp] = os.path.join(output_dir, preferred2) if output_dir else preferred2
                else:
                    for i, fp in enumerate(fplist2, 1):
                        with_suffix = f"{os.path.splitext(preferred2)[0]}_{i}.json"
                        out_map[fp] = os.path.join(output_dir, with_suffix) if output_dir else with_suffix

    return out_map


# ---------- config loading + settings origin tracking ----------

ORIGIN_DEFAULT = 'default'
ORIGIN_CONFIG = 'config'
ORIGIN_CLI = 'cli'


def default_args_dict():
    return {
        'group': None,
        'values': None,
        'agg': [],
        'agg_as': [],
        'no_agg_suffix': False,
        'leaf_as': 'auto',
        'missing_value': None,
        'no_warn_missing': False,
        'no_warn_duplicates': False,
        'indent': 2,
        'no_indent': False,
        'sort_keys': False,
        'output': None,
        'output_dir': None,
        'merge': False,
        'merge_key': 'path',
        'recursive': False,
        'preview_examples': 5,
        'audit_file': None,
    }


def load_config(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Error: Config file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}", file=sys.stderr)
        sys.exit(1)
    return config


def _parse_agg_from_config(agg_cfg):
    agg_list = []
    for col, func in agg_cfg.items():
        agg_list.append([col, func])
    return agg_list


def _parse_agg_as_from_config(agg_as_cfg):
    agg_as_list = []
    for col, name in agg_as_cfg.items():
        agg_as_list.append([col, name])
    return agg_as_list


def apply_config_tracked(args, config, defaults):
    """Apply config settings, tracking origin (cli > config > default).

    Returns (args, origins) where origins[field] = ORIGIN_CLI | CONFIG | DEFAULT.
    """
    origins = {}

    def resolve(field, cli_val, config_key=None, default=None):
        ckey = config_key or field
        if field in ('agg', 'agg_as'):
            if cli_val:
                origins[field] = ORIGIN_CLI
                return cli_val
            if ckey in config:
                if field == 'agg':
                    origins[field] = ORIGIN_CONFIG
                    return _parse_agg_from_config(config[ckey])
                if field == 'agg_as':
                    origins[field] = ORIGIN_CONFIG
                    return _parse_agg_as_from_config(config[ckey])
            origins[field] = ORIGIN_DEFAULT
            return default or []

        if cli_val is not None and cli_val != defaults.get(field):
            if field == 'indent' and cli_val != 2:
                origins[field] = ORIGIN_CLI
                return cli_val
            if field != 'indent':
                origins[field] = ORIGIN_CLI
                return cli_val
        if ckey in config:
            origins[field] = ORIGIN_CONFIG
            return config[ckey]
        origins[field] = ORIGIN_DEFAULT
        return cli_val if cli_val is not None else default

    defaults_d = defaults
    args.group = resolve('group', args.group, default=None) or args.group
    args.values = resolve('values', args.values, default=None) or args.values
    args.agg = resolve('agg', args.agg, default=[])
    args.agg_as = resolve('agg_as', args.agg_as, default=[])

    # boolean & scalar fields:
    fields_to_resolve = [
        ('no_agg_suffix', False),
        ('leaf_as', 'auto'),
        ('missing_value', None),
        ('no_warn_missing', False),
        ('no_warn_duplicates', False),
        ('indent', 2),
        ('no_indent', False),
        ('sort_keys', False),
        ('output', None),
        ('output_dir', None),
        ('merge', False),
        ('merge_key', 'path'),
        ('recursive', False),
        ('preview_examples', 5),
        ('audit_file', None),
    ]
    for fname, fdefault in fields_to_resolve:
        cli_val = getattr(args, fname)
        setattr(args, fname, resolve(fname, cli_val, default=fdefault))

    # Also merge_key needs to be checked
    if not hasattr(args, 'merge_key') or args.merge_key is None:
        args.merge_key = 'path'

    if args.merge_key not in MERGE_KEY_STRATEGIES:
        print(
            f"Error: Unknown --merge-key strategy '{args.merge_key}'. "
            f"Supported: {', '.join(MERGE_KEY_STRATEGIES)}",
            file=sys.stderr
        )
        sys.exit(1)

    return args, origins


def print_settings_summary(origins, config_path=None, verbose=False):
    """Print a short summary of which settings came from where."""
    tag = {ORIGIN_CLI: 'CLI', ORIGIN_CONFIG: 'config', ORIGIN_DEFAULT: 'default'}
    print("=" * 60, file=sys.stderr)
    print("[Settings] Effective configuration:", file=sys.stderr)
    if config_path:
        print(f"  Config file: {config_path}", file=sys.stderr)

    cli_settings = []
    cfg_settings = []
    for field, origin in sorted(origins.items()):
        if origin == ORIGIN_CLI:
            cli_settings.append(field)
        elif origin == ORIGIN_CONFIG:
            cfg_settings.append(field)

    if cli_settings:
        print(f"  From CLI    : {', '.join(cli_settings)}", file=sys.stderr)
    if cfg_settings:
        print(f"  From config : {', '.join(cfg_settings)}", file=sys.stderr)
    defaults_only = [f for f in origins if origins[f] == ORIGIN_DEFAULT]
    if verbose and defaults_only:
        print(f"  Defaults    : {len(defaults_only)} settings", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


# ---------- value column / output field resolution ----------

def validate_and_build_agg_map(agg_pairs):
    agg_map = {}
    for col, func in agg_pairs:
        func_lower = func.lower()
        if func_lower not in SUPPORTED_AGG_FUNCS:
            print(
                f"Error: Unknown aggregation function '{func}'. "
                f"Supported: {', '.join(SUPPORTED_AGG_FUNCS)}",
                file=sys.stderr
            )
            sys.exit(1)
        agg_map[col] = func_lower
    return agg_map


def build_agg_rename_map(agg_as_pairs):
    rename_map = {}
    for col, name in agg_as_pairs:
        rename_map[col] = name
    return rename_map


def resolve_value_cols(value_cols, agg_map):
    resolved = list(value_cols) if value_cols else []
    for col in agg_map:
        if col not in resolved:
            resolved.append(col)
    return resolved


def build_output_field_map(value_cols, agg_map, agg_rename_map, agg_func_suffix=True):
    out_map = {}
    for col in value_cols:
        if col in agg_rename_map:
            out_map[col] = agg_rename_map[col]
        elif col in agg_map:
            if agg_func_suffix:
                out_map[col] = f"{col}_{agg_map[col]}"
            else:
                out_map[col] = col
        else:
            out_map[col] = col
    return out_map


def format_leaf(leaf_dict, value_cols, out_field_map, leaf_mode):
    ordered_values = [leaf_dict[c] for c in value_cols]

    if leaf_mode == 'scalar':
        if len(value_cols) != 1:
            raise ValueError(
                f"leaf_as='scalar' requires exactly 1 value column, got {len(value_cols)}"
            )
        return ordered_values[0]
    if leaf_mode == 'array':
        return ordered_values
    if leaf_mode == 'object':
        return {out_field_map[c]: leaf_dict[c] for c in value_cols}
    # auto
    if len(value_cols) == 1:
        return ordered_values[0]
    return {out_field_map[c]: leaf_dict[c] for c in value_cols}


# ---------- grouping + core conversion ----------

def preflight_and_group_rows(rows, fieldnames, group_cols, value_cols, agg_map,
                              missing_value, warn_missing, warn_duplicates):
    stats = {
        'total_rows': len(rows),
        'missing_group_rows': 0,
        'missing_value_rows': defaultdict(int),
        'non_numeric_values': defaultdict(int),
        'duplicate_group_rows': 0,
        'unique_group_keys': 0,
    }
    grouped = defaultdict(list)

    for row_idx, row in enumerate(rows, start=2):
        group_key_parts = []
        has_missing_group = False
        for col in group_cols:
            val = (row.get(col) or '').strip()
            if val == '':
                has_missing_group = True
                if warn_missing:
                    warnings.warn(f"Row {row_idx}: missing value in group column '{col}'")
                val = missing_value if missing_value is not None else ''
            group_key_parts.append(val)

        if has_missing_group:
            stats['missing_group_rows'] += 1
            if missing_value is None:
                continue

        for col in value_cols:
            val = row.get(col, '')
            if val == '' or val is None:
                stats['missing_value_rows'][col] += 1

        group_key = tuple(group_key_parts)
        grouped[group_key].append(row)

    stats['unique_group_keys'] = len(grouped)
    stats['duplicate_group_rows'] = sum(
        1 for k, v in grouped.items() if len(v) > 1
    )
    return grouped, stats


def convert_rows(rows, fieldnames, group_cols, value_cols, agg_map, agg_rename_map,
                 leaf_mode, missing_value=None, warn_missing=True, warn_duplicates=True,
                 agg_func_suffix=True):
    stats = {
        'total_rows': len(rows),
        'missing_group_rows': 0,
        'missing_value_rows': defaultdict(int),
        'non_numeric_values': defaultdict(int),
        'duplicate_group_rows': 0,
        'unique_group_keys': 0,
        'output_groups': 0,
        'empty_result': False,
    }

    if not rows:
        stats['empty_result'] = True
        return {}, stats

    for col in group_cols:
        if col in agg_map:
            raise ValueError(f"Group column '{col}' cannot also be an aggregation column")

    all_cols = group_cols + value_cols
    missing_in_csv = [c for c in all_cols if c not in fieldnames]
    if missing_in_csv:
        raise ValueError(
            f"Columns not found in CSV: {', '.join(missing_in_csv)}. "
            f"Available: {list(fieldnames)}"
        )

    out_field_map = build_output_field_map(value_cols, agg_map, agg_rename_map, agg_func_suffix)

    for col in value_cols:
        if col not in agg_map:
            agg_map = dict(agg_map)
            agg_map[col] = None

    grouped, pre_stats = preflight_and_group_rows(
        rows, fieldnames, group_cols, value_cols, agg_map,
        missing_value, warn_missing, warn_duplicates
    )
    stats.update(pre_stats)

    if not grouped:
        stats['empty_result'] = True
        return {}, stats

    result = {}
    for group_key, group_rows in grouped.items():
        key_path = list(group_key)
        leaf_dict = {}
        for val_col in value_cols:
            agg_func = agg_map.get(val_col)
            if agg_func:
                values = [row.get(val_col, '') for row in group_rows]
                agg_result = _aggregate(values, agg_func, stats['non_numeric_values'], val_col)
                leaf_dict[val_col] = agg_result
            else:
                if len(group_rows) > 1:
                    if warn_duplicates:
                        warnings.warn(
                            f"Duplicate group key {key_path} for column '{val_col}': "
                            f"{len(group_rows)} rows match. Using the last value."
                        )
                    val = group_rows[-1].get(val_col, '')
                else:
                    val = group_rows[0].get(val_col, '')

                if val == '' or val is None:
                    if warn_missing:
                        warnings.warn(f"Missing value for column '{val_col}' at key {key_path}")
                    val = missing_value
                leaf_dict[val_col] = val

        final_leaf = format_leaf(leaf_dict, value_cols, out_field_map, leaf_mode)
        _deep_set(result, key_path, final_leaf)
        stats['output_groups'] += 1

    stats['empty_result'] = (stats['output_groups'] == 0)
    return result, stats


def convert_file(input_source, group_cols, value_cols, agg_map, agg_rename_map,
                 leaf_mode, missing_value, warn_missing, warn_duplicates,
                 agg_func_suffix):
    display_label = '(stdin)' if input_source == '-' else input_source
    try:
        rows, fieldnames = _read_csv(input_source)
    except FileNotFoundError:
        return None, {}, 'File not found'
    except Exception as e:
        return None, {}, f'Read error: {e}'

    empty_case = (not rows)
    try:
        result, stats = convert_rows(
            rows, fieldnames or [], group_cols, value_cols, agg_map, agg_rename_map,
            leaf_mode, missing_value, warn_missing, warn_duplicates, agg_func_suffix,
        )
    except ValueError as e:
        return None, {}, str(e)
    except Exception as e:
        return None, {}, f'Conversion error: {e}'

    if empty_case:
        stats['empty_result'] = True
        stats['total_rows'] = 0
        stats['empty_csv'] = True
        stats['header_only'] = bool(fieldnames)

    # convert defaultdicts to plain dicts for JSON serialization
    for k in ('missing_value_rows', 'non_numeric_values'):
        if isinstance(stats.get(k), defaultdict):
            stats[k] = dict(stats[k])

    return result, stats, None


# ---------- dry-run / preview ----------

def dry_run_report(input_paths, group_cols, value_cols, agg_map, agg_rename_map,
                   leaf_mode, missing_value, recursive, agg_func_suffix, preview_examples=5):
    files = discover_inputs(input_paths, recursive=recursive)
    if not files:
        print("[Preview] No CSV files found.", file=sys.stderr)
        sys.exit(1)

    out_field_map = build_output_field_map(value_cols, agg_map, agg_rename_map, agg_func_suffix)
    agg_cols = [c for c in value_cols if c in agg_map]

    print("=" * 60)
    print("[Preview] Conversion Report")
    print("=" * 60)
    print(f"[Preview] Files to process: {len(files)}")
    for f in files:
        print(f"  - {f}")
    print()

    agg_display = []
    for col in value_cols:
        if col in agg_map:
            out_name = out_field_map.get(col, col)
            agg_display.append(f"{col} → {out_name} ({agg_map[col]})")
        else:
            out_name = out_field_map.get(col, col)
            if out_name != col:
                agg_display.append(f"{col} → {out_name} (raw)")
            else:
                agg_display.append(f"{col} (raw)")

    print(f"[Preview] Group columns ({len(group_cols)}): {' → '.join(group_cols)}")
    print(f"[Preview] Value columns ({len(value_cols)}): {', '.join(agg_display)}")
    print(f"[Preview] Leaf mode: {leaf_mode}")
    structure = group_cols + (['leaf[]'] if leaf_mode == 'array' else
                              ['leaf{}'] if leaf_mode == 'object' else
                              ['leaf'] if leaf_mode == 'scalar' else
                              ['leaf'])
    print(f"[Preview] Target structure: {' → '.join(structure)}")
    print()

    errors = False
    overall_empty = 0
    overall_header_only = 0

    for fp in files:
        file_label = fp if fp != '-' else '<stdin>'
        print("-" * 50)
        print(f"[Preview] File: {file_label}")
        try:
            rows, fieldnames = _read_csv(fp)
        except FileNotFoundError:
            print("  [Error] File not found", file=sys.stderr)
            errors = True
            continue
        except Exception as e:
            print(f"  [Error] Cannot read: {e}", file=sys.stderr)
            errors = True
            continue

        if not rows and not fieldnames:
            print("  [Empty] File is completely empty (no header, no data)")
            overall_empty += 1
            continue

        if not rows:
            print(f"  [Empty] Header only, no data rows. Columns: {list(fieldnames)}")
            overall_header_only += 1
            continue

        missing_group = [c for c in group_cols if c not in fieldnames]
        missing_values = [c for c in value_cols if c not in fieldnames]

        if missing_group:
            print(f"  [Error] Missing group columns: {', '.join(missing_group)}", file=sys.stderr)
            errors = True
        if missing_values:
            print(f"  [Error] Missing value columns: {', '.join(missing_values)}", file=sys.stderr)
            errors = True
        if missing_group or missing_values:
            continue

        grouped, stats = preflight_and_group_rows(
            rows, fieldnames, group_cols, value_cols, agg_map,
            missing_value, warn_missing=False, warn_duplicates=False
        )

        print(f"  CSV columns ({len(fieldnames)}): {', '.join(fieldnames)}")
        print(f"  Total rows: {len(rows)}")
        print(f"  Unique group keys: {stats['unique_group_keys']}")

        dup_keys = [(k, v) for k, v in grouped.items() if len(v) > 1]
        dup = len(dup_keys)
        if dup > 0:
            print(f"  Groups with multiple rows: {dup} (will be aggregated)")
            if agg_cols:
                print(f"  Aggregated columns: {', '.join(agg_cols)}")
            for gk, grows in dup_keys[:preview_examples]:
                path_str = ' → '.join(str(p) for p in list(gk))
                agg_in_group = []
                for col in agg_cols:
                    valid_vals = 0
                    for r in grows:
                        v = r.get(col, '')
                        if v and v != '':
                            try:
                                float(v)
                                valid_vals += 1
                            except (ValueError, TypeError):
                                continue
                    agg_in_group.append(f"{col}({valid_vals}/{len(grows)} valid)")
                if agg_in_group:
                    print(f"    ▶ {path_str}  x{len(grows)} rows — {', '.join(agg_in_group)}")
                else:
                    print(f"    ▶ {path_str}  x{len(grows)} rows")
            remaining = dup - len(dup_keys[:preview_examples])
            if remaining > 0:
                print(f"    ... and {remaining} more aggregated groups")
        else:
            print("  Groups with multiple rows: 0")

        if stats['missing_group_rows'] > 0:
            print(f"  [Warning] Rows with missing group values: {stats['missing_group_rows']}")
        for col, cnt in stats['missing_value_rows'].items():
            if cnt > 0:
                print(f"  [Warning] Missing values in column '{col}': {cnt} rows")

        non_num = defaultdict(int)
        for gk, grows in grouped.items():
            for col in agg_cols:
                for row in grows:
                    v = row.get(col, '')
                    if v == '' or v is None:
                        continue
                    try:
                        float(v)
                    except (ValueError, TypeError):
                        non_num[col] += 1
        for col, cnt in non_num.items():
            if cnt > 0:
                print(f"  [Warning] Non-numeric values in aggregated column '{col}': {cnt} (will be skipped)")

        if grouped:
            print(f"  Example nested paths (up to {preview_examples}):")
            shown = 0
            for gk in list(grouped.keys())[:preview_examples]:
                path_str = ' → '.join(str(p) for p in (list(gk) + ['leaf']))
                print(f"    • {path_str}")
                shown += 1
            remaining = len(grouped) - shown
            if remaining > 0:
                print(f"    ... and {remaining} more")

        if not grouped:
            print("  [Empty] No valid groups after filtering")
            overall_empty += 1

    print("-" * 50)
    print("=" * 60)
    if errors:
        print("[Preview] Issues found — please review the errors above.")
        sys.exit(1)
    if overall_empty or overall_header_only:
        parts = []
        if overall_empty:
            parts.append(f"{overall_empty} completely empty/no-output")
        if overall_header_only:
            parts.append(f"{overall_header_only} header-only")
        print(f"[Preview] Note: {', '.join(parts)} file(s) will produce no output.")
    print("[Preview] Configuration looks valid. Ready to convert.")
    print("=" * 60)


# ---------- audit file ----------

def build_audit_entry(file_path, output_path, stats, status, error=None, merge_key=None):
    entry = {
        'input': '(stdin)' if file_path == '-' else os.path.abspath(file_path),
        'output': output_path,
        'status': status,  # ok / empty / fail
        'error': error,
        'merge_key': merge_key,
        'total_rows': stats.get('total_rows', 0),
        'output_groups': stats.get('output_groups', 0),
        'missing_group_rows': stats.get('missing_group_rows', 0),
        'missing_value_rows': dict(stats.get('missing_value_rows', {})),
        'non_numeric_values': dict(stats.get('non_numeric_values', {})),
        'empty_csv': stats.get('empty_csv', False),
        'header_only': stats.get('header_only', False),
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
    }
    return entry


def write_audit_file(audit_path, audit_entries):
    audit_path_dir = os.path.dirname(audit_path) or '.'
    os.makedirs(audit_path_dir, exist_ok=True)
    payload = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'total_files': len(audit_entries),
        'summary': {
            'ok': sum(1 for e in audit_entries if e['status'] == 'ok'),
            'empty': sum(1 for e in audit_entries if e['status'] == 'empty'),
            'fail': sum(1 for e in audit_entries if e['status'] == 'fail'),
        },
        'files': audit_entries,
    }
    with open(audit_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write('\n')


# ---------- argument parser ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description='Convert CSV to nested JSON with grouping, aggregation, batch processing, and audit.'
    )
    parser.add_argument('input', nargs='+',
                        help='Input CSV file(s), directory, or "-" for stdin')
    parser.add_argument('-o', '--output', default=None,
                        help='Output JSON file path (single file only — use --output-dir for batch)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory for per-file batch outputs')
    parser.add_argument('--merge', action='store_true',
                        help='Merge all results into a single JSON')
    parser.add_argument('--merge-key', default=None, choices=MERGE_KEY_STRATEGIES,
                        help='Merge key strategy: name (basename), path (relative, default), fullpath (absolute)')
    parser.add_argument('-r', '--recursive', action='store_true',
                        help='Recursively scan directories for CSV files')
    parser.add_argument('-c', '--config', default=None,
                        help='JSON config file with group/values/agg/options')
    parser.add_argument('--group', nargs='+', default=None,
                        help='Columns to group by (in order of nesting)')
    parser.add_argument('--values', nargs='+', default=None,
                        help='Columns to use as leaf values')
    parser.add_argument('--agg', nargs=2, action='append', default=[],
                        metavar=('COLUMN', 'FUNC'),
                        help='Aggregate a column: --agg sales sum. '
                             f'Supported: {", ".join(SUPPORTED_AGG_FUNCS)}. '
                             'Aggregated columns are automatically included in values.')
    parser.add_argument('--agg-as', nargs=2, action='append', default=[],
                        metavar=('COLUMN', 'OUTPUT_NAME'),
                        help='Rename an output field, e.g. --agg-as sales sales_total')
    parser.add_argument('--no-agg-suffix', action='store_true',
                        help='Do not add function suffix to aggregated field names')
    parser.add_argument('--leaf-as', default=None, choices=LEAF_MODES,
                        help='Leaf structure: auto (default), object, array, scalar')
    parser.add_argument('--indent', type=int, default=2,
                        help='Indentation spaces for pretty JSON (default: 2)')
    parser.add_argument('--no-indent', action='store_true',
                        help='Output compact JSON without indentation')
    parser.add_argument('--missing-value', default=None,
                        help='Value to use for missing data (default: null/None)')
    parser.add_argument('--no-warn-missing', action='store_true',
                        help='Disable warnings for missing values')
    parser.add_argument('--no-warn-duplicates', action='store_true',
                        help='Disable warnings for duplicate group keys')
    parser.add_argument('--sort-keys', action='store_true',
                        help='Sort keys in output JSON')
    parser.add_argument('--dry-run', '--preview', action='store_true', dest='dry_run',
                        help='Preview mode: validate, show aggregated groups & examples, do not write output')
    parser.add_argument('--preview-examples', type=int, default=5,
                        help='Number of example paths / aggregated groups in preview')
    parser.add_argument('--settings-summary', action='store_true',
                        help='Print a summary of effective settings (CLI vs config vs defaults)')
    parser.add_argument('--audit-file', default=None,
                        help='Write a JSON audit file with per-file stats and status')
    return parser


# ---------- main ----------

def main():
    parser = build_parser()
    args = parser.parse_args()
    origins = None

    if args.config:
        config = load_config(args.config)
        defaults = default_args_dict()
        args, origins = apply_config_tracked(args, config, defaults)
    else:
        # Fill defaults for tracking fields that the parser has raw defaults on
        if args.leaf_as is None:
            args.leaf_as = 'auto'
        if args.merge_key is None:
            args.merge_key = 'path'
        if args.audit_file is None:
            args.audit_file = None

    if args.settings_summary:
        if origins is None:
            # Build a minimal origins dict from CLI presence check
            origins = build_default_origins(args)
        print_settings_summary(origins, args.config, verbose=True)

    warnings.simplefilter('always')

    agg_map = validate_and_build_agg_map(args.agg)
    agg_rename_map = build_agg_rename_map(args.agg_as)
    value_cols = resolve_value_cols(args.values, agg_map)
    group_cols = args.group or []
    agg_func_suffix = not args.no_agg_suffix

    if not group_cols:
        print("Error: --group is required (can be specified via config file).", file=sys.stderr)
        sys.exit(1)

    if not value_cols:
        print("Error: At least one value column is required (via --values or --agg).", file=sys.stderr)
        sys.exit(1)

    if args.leaf_as == 'scalar' and len(value_cols) != 1:
        print(
            f"Error: --leaf-as scalar requires exactly 1 value column, got {len(value_cols)}",
            file=sys.stderr
        )
        sys.exit(1)

    if args.dry_run:
        dry_run_report(
            args.input, group_cols, value_cols, agg_map, agg_rename_map,
            args.leaf_as, args.missing_value, args.recursive, agg_func_suffix,
            preview_examples=args.preview_examples,
        )
        return

    files = discover_inputs(args.input, recursive=args.recursive)
    if not files:
        print("Error: No CSV files found to process.", file=sys.stderr)
        sys.exit(1)

    # Safety check: multi-file + single -o (and not --merge) is error
    if not args.merge and args.output and len(files) > 1:
        print(
            "Error: Multiple input files with a single -o/--output is not allowed. "
            "Use --output-dir to place per-file results, or --merge to combine into one JSON.",
            file=sys.stderr
        )
        sys.exit(1)

    indent = None if args.no_indent else args.indent
    merged_results = {}
    successes = []  # (label, stats, is_empty, output_path, merge_key)
    failures = []
    empties = []
    audit_entries = []
    input_roots = [os.path.abspath(p) if os.path.isdir(p) else os.getcwd() for p in args.input]

    # Build safe output paths for non-merge batch mode
    safe_outputs = None
    if not args.merge and (len(files) > 1 or args.output_dir):
        try:
            safe_outputs = build_safe_batch_output_paths(files, args.output_dir, args.output)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    is_batch = (len(files) > 1) or args.merge or args.output_dir

    for fp in files:
        label = '(stdin)' if fp == '-' else fp
        result, stats, err = convert_file(
            fp, group_cols, value_cols, dict(agg_map), agg_rename_map,
            args.leaf_as, args.missing_value,
            not args.no_warn_missing, not args.no_warn_duplicates,
            agg_func_suffix,
        )

        # --- failure ---
        if err is not None:
            print(f"[FAIL] {label}: {err}", file=sys.stderr)
            failures.append((label, err))
            audit_entries.append(build_audit_entry(fp, None, {}, 'fail', error=err))
            continue

        # --- empty inputs ---
        is_empty_result = stats.get('empty_result', False)
        if is_empty_result and stats.get('empty_csv'):
            if not stats.get('header_only'):
                msg = 'completely empty (no header, no data)'
            else:
                msg = 'header only, no data rows'
            print(f"[EMPTY] {label}: {msg}", file=sys.stderr)
            empties.append((label, msg))
            audit_entries.append(build_audit_entry(fp, None, stats, 'empty', error=msg))
            continue

        if is_empty_result:
            msg = 'no output groups after grouping/filtering'
            print(f"[WARN]  {label}: {msg}", file=sys.stderr)

        # --- non-numeric warnings ---
        non_num = stats.get('non_numeric_values', {})
        for col, cnt in non_num.items():
            if cnt > 0 and not args.no_warn_missing:
                warnings.warn(
                    f"{label}: {cnt} non-numeric values skipped in aggregated column '{col}'"
                )

        # --- output ---
        mk = None
        if args.merge:
            mk = compute_merge_key(fp, args.merge_key, roots=input_roots)
            if mk in merged_results:
                existing = merged_results[mk]
                i = 2
                while f"{mk}_{i}" in merged_results:
                    i += 1
                warnings.warn(f"Duplicate merge key '{mk}' for '{fp}' — renamed to '{mk}_{i}'")
                mk = f"{mk}_{i}"
            merged_results[mk] = result
            out_path = None
        elif is_batch:
            if fp == '-' and not args.output_dir and not args.output:
                print(json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False))
                out_path = None
            else:
                out_path = (args.output if args.output else
                            (safe_outputs.get(fp) if safe_outputs else None))
                if out_path is None:
                    out_path = f"{os.path.splitext(os.path.basename(fp if fp != '-' else 'stdin'))[0]}.json"
                    if args.output_dir:
                        out_path = os.path.join(args.output_dir, out_path)
                try:
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False))
                        f.write('\n')
                    print(f"[OK]   {label} → {out_path}", file=sys.stderr)
                except Exception as e:
                    print(f"[FAIL] {label}: write error: {e}", file=sys.stderr)
                    failures.append((label, f'write error: {e}'))
                    audit_entries.append(build_audit_entry(fp, out_path, stats, 'fail', error=f'write error: {e}'))
                    continue
        else:
            # single file, non-batch
            output_str = json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)
            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(output_str)
                    f.write('\n')
                print(f"[OK]   {label} → {args.output}", file=sys.stderr)
                out_path = args.output
            else:
                print(output_str)
                out_path = None

        successes.append((label, stats, is_empty_result, out_path, mk))
        audit_entries.append(build_audit_entry(fp, out_path, stats,
                                               'empty' if is_empty_result else 'ok',
                                               merge_key=mk))

    # --- merged output ---
    if args.merge:
        output_str = json.dumps(merged_results, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_str)
                f.write('\n')
            print(f"\n[MERGED] All results → {args.output}", file=sys.stderr)
        else:
            print(output_str)

    # --- summary ---
    if is_batch:
        total = len(files)
        ok = len([s for s in successes if not s[2]])
        empty = len([s for s in successes if s[2]]) + len(empties)
        fail = len(failures)
        print(file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"Summary: {total} file(s) — {ok} OK, {empty} empty, {fail} failed", file=sys.stderr)
        if failures:
            print("Failures:", file=sys.stderr)
            for label, err in failures:
                print(f"  - {label}: {err}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)

    # --- audit file ---
    if args.audit_file:
        # Update merge outputs' output_path in audit entries
        if args.merge and args.output:
            for e in audit_entries:
                if e['status'] in ('ok', 'empty') and e.get('merge_key'):
                    e['output'] = args.output
        write_audit_file(args.audit_file, audit_entries)
        print(f"[Audit] Report written to {args.audit_file}", file=sys.stderr)

    if failures:
        sys.exit(2)


def build_default_origins(args):
    """Fallback origins generator when no config file is used."""
    origins = {}
    origins['group'] = ORIGIN_CLI if args.group else ORIGIN_DEFAULT
    origins['values'] = ORIGIN_CLI if args.values else ORIGIN_DEFAULT
    origins['agg'] = ORIGIN_CLI if args.agg else ORIGIN_DEFAULT
    origins['agg_as'] = ORIGIN_CLI if args.agg_as else ORIGIN_DEFAULT
    origins['no_agg_suffix'] = ORIGIN_CLI if args.no_agg_suffix else ORIGIN_DEFAULT
    origins['leaf_as'] = ORIGIN_CLI if args.leaf_as and args.leaf_as != 'auto' else ORIGIN_DEFAULT
    origins['missing_value'] = ORIGIN_CLI if args.missing_value is not None else ORIGIN_DEFAULT
    origins['no_warn_missing'] = ORIGIN_CLI if args.no_warn_missing else ORIGIN_DEFAULT
    origins['no_warn_duplicates'] = ORIGIN_CLI if args.no_warn_duplicates else ORIGIN_DEFAULT
    origins['indent'] = ORIGIN_CLI if args.indent != 2 else ORIGIN_DEFAULT
    origins['no_indent'] = ORIGIN_CLI if args.no_indent else ORIGIN_DEFAULT
    origins['sort_keys'] = ORIGIN_CLI if args.sort_keys else ORIGIN_DEFAULT
    origins['output'] = ORIGIN_CLI if args.output else ORIGIN_DEFAULT
    origins['output_dir'] = ORIGIN_CLI if args.output_dir else ORIGIN_DEFAULT
    origins['merge'] = ORIGIN_CLI if args.merge else ORIGIN_DEFAULT
    origins['merge_key'] = ORIGIN_CLI if args.merge_key and args.merge_key != 'path' else ORIGIN_DEFAULT
    origins['recursive'] = ORIGIN_CLI if args.recursive else ORIGIN_DEFAULT
    origins['preview_examples'] = ORIGIN_CLI if args.preview_examples != 5 else ORIGIN_DEFAULT
    origins['audit_file'] = ORIGIN_CLI if args.audit_file else ORIGIN_DEFAULT
    return origins


if __name__ == '__main__':
    main()
