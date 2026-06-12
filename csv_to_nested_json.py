#!/usr/bin/env python3
"""CSV to nested JSON converter — production-ready with batch, audit, checks, and incremental."""

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

_UNSET = object()

_APPEND_FIELDS = {'agg', 'agg_as'}


# ---------- helpers ----------

def _deep_set(d, keys, value):
    for k in keys[:-1]:
        if k not in d:
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _deep_merge(d, keys, value):
    """Like _deep_set but intermediates can be non-dict — overwrite leaf if conflict."""
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
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


def _file_signature(file_path):
    """Return (mtime, size) tuple for a file, or None if it doesn't exist."""
    if file_path == '-' or not os.path.isfile(file_path):
        return None
    st = os.stat(file_path)
    return (st.st_mtime, st.st_size)


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
    """Map each input file to a safe output path, avoiding overwrites."""
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

    for fp in files:
        if fp == '-':
            preferred = '(stdin).json'
        else:
            preferred = os.path.splitext(os.path.basename(fp))[0] + '.json'
        seen[preferred].append(fp)

    for preferred, fplist in seen.items():
        if len(fplist) == 1:
            fp = fplist[0]
            out_map[fp] = os.path.join(output_dir, preferred) if output_dir else preferred
        else:
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
        'merge_nested': False,
        'recursive': False,
        'preview_examples': 5,
        'audit_file': None,
        'check': False,
        'fail_fast': False,
        'incremental': False,
        'dry_run': False,
        'settings_summary': False,
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


def _cli_provided(args, field):
    """Check if a field was explicitly provided via CLI.

    For append-action fields (agg, agg_as), non-empty means CLI provided.
    For others, _UNSET sentinel is used.
    """
    if field in _APPEND_FIELDS:
        val = getattr(args, field, None)
        return isinstance(val, list) and len(val) > 0
    return getattr(args, field, _UNSET) is not _UNSET


def apply_config_tracked(args, config, defaults):
    """Apply config settings, tracking origin (cli > config > default).

    Returns (args, origins, effective) where origins[field] = CLI/CONFIG/DEFAULT
    and effective[field] holds the final resolved value.
    """
    origins = {}
    effective = {}

    def resolve(field, config_key=None, default=None):
        ckey = config_key or field

        if field in ('agg', 'agg_as'):
            if _cli_provided(args, field):
                cli_val = getattr(args, field)
                origins[field] = ORIGIN_CLI
                effective[field] = cli_val
                return cli_val
            if ckey in config:
                if field == 'agg':
                    parsed = _parse_agg_from_config(config[ckey])
                    origins[field] = ORIGIN_CONFIG
                    effective[field] = parsed
                    return parsed
                if field == 'agg_as':
                    parsed = _parse_agg_as_from_config(config[ckey])
                    origins[field] = ORIGIN_CONFIG
                    effective[field] = parsed
                    return parsed
            origins[field] = ORIGIN_DEFAULT
            effective[field] = default or []
            return default or []

        if _cli_provided(args, field):
            val = getattr(args, field)
            origins[field] = ORIGIN_CLI
            effective[field] = val
            return val
        if ckey in config:
            val = config[ckey]
            origins[field] = ORIGIN_CONFIG
            effective[field] = val
            return val
        val = default
        origins[field] = ORIGIN_DEFAULT
        effective[field] = val
        return val

    args.group = resolve('group', default=None)
    args.values = resolve('values', default=None)
    args.agg = resolve('agg', default=[])
    args.agg_as = resolve('agg_as', default=[])

    scalar_fields = [
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
        ('merge_nested', False),
        ('recursive', False),
        ('preview_examples', 5),
        ('audit_file', None),
        ('check', False),
        ('fail_fast', False),
        ('incremental', False),
        ('dry_run', False),
        ('settings_summary', False),
    ]
    for fname, fdefault in scalar_fields:
        setattr(args, fname, resolve(fname, default=fdefault))

    if args.merge_key not in MERGE_KEY_STRATEGIES:
        print(
            f"Error: Unknown --merge-key strategy '{args.merge_key}'. "
            f"Supported: {', '.join(MERGE_KEY_STRATEGIES)}",
            file=sys.stderr
        )
        sys.exit(1)
    if args.leaf_as not in LEAF_MODES:
        print(
            f"Error: Unknown --leaf-as mode '{args.leaf_as}'. "
            f"Supported: {', '.join(LEAF_MODES)}",
            file=sys.stderr
        )
        sys.exit(1)

    return args, origins, effective


def print_settings_summary(origins, effective, config_path=None):
    """Print a detailed summary of each setting, its origin, and its value."""
    tag = {ORIGIN_CLI: 'CLI', ORIGIN_CONFIG: 'config', ORIGIN_DEFAULT: 'default'}
    print("=" * 68, file=sys.stderr)
    print("[Settings] Effective configuration:", file=sys.stderr)
    if config_path:
        print(f"  Config file: {config_path}", file=sys.stderr)
    print(file=sys.stderr)

    for field in sorted(origins.keys()):
        origin = origins[field]
        val = effective.get(field)
        val_str = _format_setting_value(field, val)
        print(f"  {field:<22} = {val_str:<30} [{tag[origin]}]", file=sys.stderr)

    print("=" * 68, file=sys.stderr)


def _format_setting_value(field, val):
    if field == 'agg' and val:
        return ', '.join(f"{c}:{f}" for c, f in val)
    if field == 'agg_as' and val:
        return ', '.join(f"{c}→{n}" for c, n in val)
    if field == 'group' and val:
        return ' → '.join(val)
    if field == 'values' and val:
        return ', '.join(val)
    if val is None:
        return 'null'
    if isinstance(val, bool):
        return 'yes' if val else 'no'
    if isinstance(val, int):
        return str(val)
    return str(val)


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

    for k in ('missing_value_rows', 'non_numeric_values'):
        if isinstance(stats.get(k), defaultdict):
            stats[k] = dict(stats[k])

    return result, stats, None


# ---------- dry-run / preview / check ----------

def _collect_file_checks(fp, group_cols, value_cols, agg_map, missing_value, preview_examples):
    """Run all checks on a single file and return a result dict."""
    result = {
        'file': fp,
        'status': 'ok',
        'errors': [],
        'warnings': [],
        'info': {},
    }
    try:
        rows, fieldnames = _read_csv(fp)
    except FileNotFoundError:
        result['status'] = 'error'
        result['errors'].append('File not found')
        return result
    except Exception as e:
        result['status'] = 'error'
        result['errors'].append(f'Cannot read: {e}')
        return result

    result['info']['columns'] = list(fieldnames) if fieldnames else []
    result['info']['total_rows'] = len(rows)

    if not rows and not fieldnames:
        result['info']['empty_completely'] = True
        result['warnings'].append('File is completely empty (no header, no data)')
        return result
    if not rows:
        result['info']['header_only'] = True
        result['warnings'].append('Header only, no data rows')
        return result

    missing_group = [c for c in group_cols if c not in fieldnames]
    missing_values = [c for c in value_cols if c not in fieldnames]
    if missing_group:
        result['errors'].append(f'Missing group columns: {", ".join(missing_group)}')
    if missing_values:
        result['errors'].append(f'Missing value columns: {", ".join(missing_values)}')
    if missing_group or missing_values:
        result['status'] = 'error'
        return result

    grouped, stats = preflight_and_group_rows(
        rows, fieldnames, group_cols, value_cols, agg_map,
        missing_value, warn_missing=False, warn_duplicates=False
    )
    result['info'].update({
        'unique_group_keys': stats['unique_group_keys'],
        'missing_group_rows': stats['missing_group_rows'],
        'missing_value_rows': dict(stats['missing_value_rows']),
    })

    if stats['missing_group_rows'] > 0:
        result['warnings'].append(f"{stats['missing_group_rows']} rows with missing group values")
    for col, cnt in stats['missing_value_rows'].items():
        if cnt > 0:
            result['warnings'].append(f"Missing values in column '{col}': {cnt} rows")

    agg_cols = [c for c in value_cols if c in agg_map]
    dup_keys = [(k, v) for k, v in grouped.items() if len(v) > 1]
    result['info']['aggregated_groups'] = len(dup_keys)
    if dup_keys and agg_cols:
        agg_detail = []
        for gk, grows in dup_keys[:preview_examples]:
            path_str = ' → '.join(str(p) for p in list(gk))
            per_col = []
            for col in agg_cols:
                valid = 0
                for r in grows:
                    v = r.get(col, '')
                    if v and v != '':
                        try:
                            float(v)
                            valid += 1
                        except (ValueError, TypeError):
                            continue
                per_col.append(f"{col}({valid}/{len(grows)} valid)")
            agg_detail.append((path_str, len(grows), per_col))
        result['info']['agg_detail_examples'] = agg_detail
        result['info']['agg_detail_remaining'] = max(0, len(dup_keys) - preview_examples)

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
    result['info']['non_numeric_values'] = dict(non_num)
    for col, cnt in non_num.items():
        if cnt > 0:
            result['warnings'].append(
                f"Non-numeric values in aggregated column '{col}': {cnt} (will be skipped)"
            )

    if not grouped:
        result['warnings'].append('No valid groups after filtering')
        result['info']['no_output_groups'] = True

    if result['warnings'] and result['status'] == 'ok':
        result['status'] = 'warn'

    return result


def run_check_report(input_paths, group_cols, value_cols, agg_map, missing_value,
                     recursive, preview_examples, fail_fast=False):
    """Run check mode: validate all files, optionally fail fast on errors."""
    files = discover_inputs(input_paths, recursive=recursive)
    if not files:
        print("[Check] No CSV files found.", file=sys.stderr)
        sys.exit(1)

    print("=" * 68)
    print("[Check] Validation Report")
    print("=" * 68)
    print(f"[Check] Files to validate: {len(files)}")
    for f in files:
        print(f"  - {f}")
    print()

    errors_total = 0
    warnings_total = 0
    ok_count = 0
    warn_count = 0
    error_count = 0

    for fp in files:
        label = fp if fp != '-' else '<stdin>'
        print("-" * 60)
        print(f"[Check] File: {label}")

        check_result = _collect_file_checks(
            fp, group_cols, value_cols, agg_map, missing_value, preview_examples
        )

        info = check_result['info']
        if 'columns' in info:
            print(f"  Columns ({len(info['columns'])}): {', '.join(info['columns'][:8])}"
                  + ('...' if len(info['columns']) > 8 else ''))
            print(f"  Total rows: {info.get('total_rows', 0)}")
            if 'unique_group_keys' in info:
                print(f"  Unique group keys: {info['unique_group_keys']}")
                print(f"  Aggregated groups: {info.get('aggregated_groups', 0)}")
                if info.get('agg_detail_examples'):
                    print("  Aggregated group examples:")
                    for path_str, nrows, per_col in info['agg_detail_examples']:
                        print(f"    * {path_str}  x{nrows} rows — {', '.join(per_col)}")
                    if info.get('agg_detail_remaining', 0) > 0:
                        print(f"    ... and {info['agg_detail_remaining']} more")

        for w in check_result['warnings']:
            print(f"  [Warning] {w}")
            warnings_total += 1
        for e in check_result['errors']:
            print(f"  [Error]   {e}")
            errors_total += 1

        status = check_result['status']
        if status == 'ok':
            ok_count += 1
            print(f"  Status: OK")
        elif status == 'warn':
            warn_count += 1
            print(f"  Status: OK (with warnings)")
        elif status == 'error':
            error_count += 1
            print(f"  Status: FAILED")
            if fail_fast:
                print(file=sys.stderr)
                print("[Check] --fail-fast: stopping on first error.", file=sys.stderr)
                print("=" * 68)
                sys.exit(1)

    print("-" * 60)
    print("=" * 68)
    print(f"[Check] Summary: {len(files)} files — "
          f"{ok_count} OK, {warn_count} with warnings, {error_count} errors")
    if warnings_total > 0:
        print(f"[Check] Total warnings: {warnings_total}")
    if errors_total > 0:
        print(f"[Check] Total errors: {errors_total}")
        print("[Check] Issues found — please review the errors above.")
        print("=" * 68)
        sys.exit(1)
    print("[Check] All files passed validation. Ready to convert.")
    print("=" * 68)


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
                    print(f"    * {path_str}  x{len(grows)} rows — {', '.join(agg_in_group)}")
                else:
                    print(f"    * {path_str}  x{len(grows)} rows")
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

def build_audit_entry(file_path, output_path, stats, status, error=None, merge_key=None,
                      skip_reason=None):
    entry = {
        'input': '(stdin)' if file_path == '-' else os.path.abspath(file_path),
        'output': output_path,
        'status': status,  # ok / empty / fail / skipped
        'error': error,
        'skip_reason': skip_reason,
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
            'skipped': sum(1 for e in audit_entries if e['status'] == 'skipped'),
        },
        'files': audit_entries,
    }
    with open(audit_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write('\n')


# ---------- incremental helpers ----------

def should_skip_incremental(input_path, output_path):
    """Check if we can skip conversion because output is newer than input.

    Returns (skip: bool, reason: str or None).
    """
    if input_path == '-':
        return False, None
    if not output_path or not os.path.isfile(output_path):
        return False, None
    in_sig = _file_signature(input_path)
    out_sig = _file_signature(output_path)
    if not in_sig or not out_sig:
        return False, None
    in_mtime, _ = in_sig
    out_mtime, _ = out_sig
    if out_mtime >= in_mtime:
        return True, 'output newer than source (mtime)'
    return False, None


# ---------- argument parser ----------

def build_parser():
    parser = argparse.ArgumentParser(
        description='Convert CSV to nested JSON with grouping, aggregation, batch, check, audit, and incremental.'
    )
    parser.add_argument('input', nargs='+',
                        help='Input CSV file(s), directory, or "-" for stdin')
    parser.add_argument('-o', '--output', default=_UNSET,
                        help='Output JSON file path (single file only — use --output-dir for batch)')
    parser.add_argument('--output-dir', default=_UNSET,
                        help='Directory for per-file batch outputs')
    parser.add_argument('--merge', action='store_true', default=_UNSET,
                        help='Merge all results into a single JSON')
    parser.add_argument('--merge-key', default=_UNSET, choices=MERGE_KEY_STRATEGIES,
                        help='Merge key strategy: name (basename), path (relative, default), fullpath (absolute)')
    parser.add_argument('--merge-nested', action='store_true', default=_UNSET,
                        help='Nest merged results by directory path instead of flat keys')
    parser.add_argument('-r', '--recursive', action='store_true', default=_UNSET,
                        help='Recursively scan directories for CSV files')
    parser.add_argument('-c', '--config', default=None,
                        help='JSON config file with group/values/agg/options')
    parser.add_argument('--group', nargs='+', default=_UNSET,
                        help='Columns to group by (in order of nesting)')
    parser.add_argument('--values', nargs='+', default=_UNSET,
                        help='Columns to use as leaf values')
    parser.add_argument('--agg', nargs=2, action='append', default=[],
                        metavar=('COLUMN', 'FUNC'),
                        help='Aggregate a column: --agg sales sum. '
                             f'Supported: {", ".join(SUPPORTED_AGG_FUNCS)}. '
                             'Aggregated columns are automatically included in values.')
    parser.add_argument('--agg-as', nargs=2, action='append', default=[],
                        metavar=('COLUMN', 'OUTPUT_NAME'),
                        help='Rename an output field, e.g. --agg-as sales sales_total')
    parser.add_argument('--no-agg-suffix', action='store_true', default=_UNSET,
                        help='Do not add function suffix to aggregated field names')
    parser.add_argument('--leaf-as', default=_UNSET, choices=LEAF_MODES,
                        help='Leaf structure: auto (default), object, array, scalar')
    parser.add_argument('--indent', type=int, default=_UNSET,
                        help='Indentation spaces for pretty JSON (default: 2)')
    parser.add_argument('--no-indent', action='store_true', default=_UNSET,
                        help='Output compact JSON without indentation')
    parser.add_argument('--missing-value', default=_UNSET,
                        help='Value to use for missing data (default: null/None)')
    parser.add_argument('--no-warn-missing', action='store_true', default=_UNSET,
                        help='Disable warnings for missing values')
    parser.add_argument('--no-warn-duplicates', action='store_true', default=_UNSET,
                        help='Disable warnings for duplicate group keys')
    parser.add_argument('--sort-keys', action='store_true', default=_UNSET,
                        help='Sort keys in output JSON')
    parser.add_argument('--dry-run', '--preview', action='store_true', dest='dry_run', default=_UNSET,
                        help='Preview mode: validate, show aggregated groups & examples, do not write output')
    parser.add_argument('--check', action='store_true', default=_UNSET,
                        help='Validation check mode: verify all inputs, show pass/warn/fail per file')
    parser.add_argument('--fail-fast', action='store_true', default=_UNSET,
                        help='With --check: stop on first error found')
    parser.add_argument('--incremental', action='store_true', default=_UNSET,
                        help='Skip files where output JSON is newer than source CSV')
    parser.add_argument('--preview-examples', type=int, default=_UNSET,
                        help='Number of example paths / aggregated groups in preview (default: 5)')
    parser.add_argument('--settings-summary', action='store_true', default=_UNSET,
                        help='Print a detailed summary of effective settings with origins and values')
    parser.add_argument('--audit-file', default=_UNSET,
                        help='Write a JSON audit file with per-file stats and status')
    return parser


# ---------- main ----------

def main():
    parser = build_parser()
    args = parser.parse_args()
    origins = None
    effective = None

    if args.config:
        config = load_config(args.config)
        defaults = default_args_dict()
        args, origins, effective = apply_config_tracked(args, config, defaults)
    else:
        defaults = default_args_dict()
        for fname, fdefault in defaults.items():
            if not _cli_provided(args, fname):
                setattr(args, fname, fdefault)
        origins = _build_default_origins(args)
        effective = {k: getattr(args, k) for k in defaults}

    if args.settings_summary:
        print_settings_summary(origins, effective, args.config)

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

    if args.check:
        run_check_report(
            args.input, group_cols, value_cols, agg_map, args.missing_value,
            args.recursive, args.preview_examples, fail_fast=args.fail_fast,
        )
        return

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

    if not args.merge and args.output and len(files) > 1:
        print(
            "Error: Multiple input files with a single -o/--output is not allowed. "
            "Use --output-dir to place per-file results, or --merge to combine into one JSON.",
            file=sys.stderr
        )
        sys.exit(1)

    indent = None if args.no_indent else args.indent
    merged_results = {}
    successes = []
    failures = []
    empties = []
    skips = []
    audit_entries = []
    input_roots = [os.path.abspath(p) if os.path.isdir(p) else os.getcwd() for p in args.input]

    safe_outputs = None
    if not args.merge and (len(files) > 1 or args.output_dir):
        try:
            safe_outputs = build_safe_batch_output_paths(files, args.output_dir, args.output)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    is_batch = (len(files) > 1) or args.merge or args.output_dir

    merge_skip_all = False
    merge_skip_reason = None
    if args.merge and args.incremental and args.output:
        real_files = [fp for fp in files if fp != '-']
        if real_files:
            latest_mtime = 0
            latest_size = 0
            for fp in real_files:
                mtime, size = _file_signature(fp)
                if mtime and mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_size = size
            out_mtime, out_size = _file_signature(args.output)
            if out_mtime and latest_mtime and out_mtime >= latest_mtime:
                merge_skip_all = True
                merge_skip_reason = f'merge output "{args.output}" is newer than all source files'

    for fp in files:
        label = '(stdin)' if fp == '-' else fp

        out_path = None
        if args.merge:
            out_path = args.output
        elif is_batch:
            if safe_outputs:
                out_path = safe_outputs.get(fp)
            elif args.output and len(files) == 1:
                out_path = args.output
            else:
                out_path = f"{os.path.splitext(os.path.basename(fp if fp != '-' else 'stdin'))[0]}.json"
                if args.output_dir:
                    out_path = os.path.join(args.output_dir, out_path)
        else:
            out_path = args.output if args.output else None

        if args.incremental and fp != '-' and out_path:
            if args.merge and merge_skip_all:
                skip = True
                reason = merge_skip_reason
            else:
                skip, reason = should_skip_incremental(fp, out_path)
            if skip:
                print(f"[SKIP] {label}: {reason}", file=sys.stderr)
                skips.append((label, reason))
                audit_entries.append(build_audit_entry(
                    fp, out_path, {}, 'skipped', skip_reason=reason
                ))
                continue

        result, stats, err = convert_file(
            fp, group_cols, value_cols, dict(agg_map), agg_rename_map,
            args.leaf_as, args.missing_value,
            not args.no_warn_missing, not args.no_warn_duplicates,
            agg_func_suffix,
        )

        if err is not None:
            print(f"[FAIL] {label}: {err}", file=sys.stderr)
            failures.append((label, err))
            audit_entries.append(build_audit_entry(fp, out_path, {}, 'fail', error=err))
            continue

        is_empty_result = stats.get('empty_result', False)
        if is_empty_result and stats.get('empty_csv'):
            if not stats.get('header_only'):
                msg = 'completely empty (no header, no data)'
            else:
                msg = 'header only, no data rows'
            print(f"[EMPTY] {label}: {msg}", file=sys.stderr)
            empties.append((label, msg))
            audit_entries.append(build_audit_entry(fp, out_path, stats, 'empty', error=msg))
            continue

        if is_empty_result:
            msg = 'no output groups after grouping/filtering'
            print(f"[WARN]  {label}: {msg}", file=sys.stderr)

        non_num = stats.get('non_numeric_values', {})
        for col, cnt in non_num.items():
            if cnt > 0 and not args.no_warn_missing:
                warnings.warn(
                    f"{label}: {cnt} non-numeric values skipped in aggregated column '{col}'"
                )

        mk = None
        if args.merge:
            mk = compute_merge_key(fp, args.merge_key, roots=input_roots)
            if args.merge_nested:
                key_parts = mk.split('/') if mk != '(stdin)' else [mk]
                _deep_merge(merged_results, key_parts, result)
            else:
                if mk in merged_results:
                    i = 2
                    while f"{mk}_{i}" in merged_results:
                        i += 1
                    warnings.warn(f"Duplicate merge key '{mk}' for '{fp}' — renamed to '{mk}_{i}'")
                    mk = f"{mk}_{i}"
                merged_results[mk] = result
            actual_out_path = None
        elif is_batch:
            actual_out_path = out_path
            try:
                with open(actual_out_path, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False))
                    f.write('\n')
                print(f"[OK]   {label} → {actual_out_path}", file=sys.stderr)
            except Exception as e:
                print(f"[FAIL] {label}: write error: {e}", file=sys.stderr)
                failures.append((label, f'write error: {e}'))
                audit_entries.append(build_audit_entry(fp, actual_out_path, stats, 'fail', error=f'write error: {e}'))
                continue
        else:
            output_str = json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)
            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(output_str)
                    f.write('\n')
                print(f"[OK]   {label} → {args.output}", file=sys.stderr)
                actual_out_path = args.output
            else:
                print(output_str)
                actual_out_path = None

        successes.append((label, stats, is_empty_result, actual_out_path, mk))
        audit_entries.append(build_audit_entry(fp, actual_out_path, stats,
                                               'empty' if is_empty_result else 'ok',
                                               merge_key=mk))

    if args.merge and not merge_skip_all:
        output_str = json.dumps(merged_results, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_str)
                f.write('\n')
            print(f"\n[MERGED] All results → {args.output}", file=sys.stderr)
        else:
            print(output_str)

    if is_batch:
        total = len(files)
        ok = len([s for s in successes if not s[2]])
        empty = len([s for s in successes if s[2]]) + len(empties)
        fail = len(failures)
        skip = len(skips)
        print(file=sys.stderr)
        print("=" * 58, file=sys.stderr)
        parts = [f"{ok} OK"]
        if skip:
            parts.append(f"{skip} skipped")
        if empty:
            parts.append(f"{empty} empty")
        if fail:
            parts.append(f"{fail} failed")
        print(f"Summary: {total} file(s) — " + ", ".join(parts), file=sys.stderr)
        if failures:
            print("Failures:", file=sys.stderr)
            for label, err in failures:
                print(f"  - {label}: {err}", file=sys.stderr)
        if skips:
            print("Skipped:", file=sys.stderr)
            for label, reason in skips:
                print(f"  - {label}: {reason}", file=sys.stderr)
        print("=" * 58, file=sys.stderr)

    if args.audit_file:
        if args.merge and args.output:
            for e in audit_entries:
                if e['status'] in ('ok', 'empty') and e.get('merge_key'):
                    e['output'] = args.output
        write_audit_file(args.audit_file, audit_entries)
        print(f"[Audit] Report written to {args.audit_file}", file=sys.stderr)

    if failures:
        sys.exit(2)


def _build_default_origins(args):
    """Build origins dict when no config file is used."""
    defaults = default_args_dict()
    origins = {}
    for field in defaults:
        if _cli_provided(args, field):
            origins[field] = ORIGIN_CLI
        else:
            origins[field] = ORIGIN_DEFAULT
    return origins


if __name__ == '__main__':
    main()
