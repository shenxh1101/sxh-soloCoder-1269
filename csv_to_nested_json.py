#!/usr/bin/env python3
"""CSV to nested JSON converter with grouping, aggregation, batch processing, and more."""

import argparse
import csv
import json
import os
import sys
import warnings
from collections import defaultdict
from io import StringIO


SUPPORTED_AGG_FUNCS = ('sum', 'avg', 'count', 'min', 'max')
LEAF_MODES = ('auto', 'object', 'array', 'scalar')


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


def default_output_path(csv_path, output_dir=None, ext='.json'):
    if csv_path == '-':
        return None
    base = os.path.splitext(os.path.basename(csv_path))[0]
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        return os.path.join(output_dir, base + ext)
    return base + ext


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


def apply_config_to_args(args, config):
    if 'group' in config and args.group is None:
        args.group = config['group']
    if 'values' in config and args.values is None:
        args.values = config['values']
    if 'agg' in config and not args.agg:
        agg_list = []
        for col, func in config['agg'].items():
            agg_list.append([col, func])
        args.agg = agg_list
    if 'agg_as' in config and not args.agg_as:
        agg_as_list = []
        for col, name in config['agg_as'].items():
            agg_as_list.append([col, name])
        args.agg_as = agg_as_list
    if 'missing_value' in config and args.missing_value is None:
        args.missing_value = config['missing_value']
    if 'no_warn_missing' in config and not args.no_warn_missing:
        args.no_warn_missing = config['no_warn_missing']
    if 'no_warn_duplicates' in config and not args.no_warn_duplicates:
        args.no_warn_duplicates = config['no_warn_duplicates']
    if 'indent' in config and args.indent == 2:
        args.indent = config['indent']
    if 'no_indent' in config and not args.no_indent:
        args.no_indent = config['no_indent']
    if 'sort_keys' in config and not args.sort_keys:
        args.sort_keys = config['sort_keys']
    if 'output' in config and args.output is None:
        args.output = config['output']
    if 'output_dir' in config and args.output_dir is None:
        args.output_dir = config['output_dir']
    if 'merge' in config and not args.merge:
        args.merge = config['merge']
    if 'leaf_as' in config and args.leaf_as == 'auto':
        args.leaf_as = config['leaf_as']
    if 'recursive' in config and not args.recursive:
        args.recursive = config['recursive']
    return args


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
    """Map each internal value column to its output field name.

    Rules:
    - If column has an explicit --agg-as rename, use that.
    - Else if column is aggregated and agg_func_suffix is True, use "{col}_{func}" by default.
    - Else use the column name itself.
    """
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
    """Convert leaf_dict {col: val} into final leaf based on leaf_mode."""
    ordered_values = [leaf_dict[c] for c in value_cols]

    if leaf_mode == 'scalar':
        if len(value_cols) != 1:
            raise ValueError(
                f"leaf_as='scalar' requires exactly 1 value column, got {len(value_cols)}"
            )
        return ordered_values[0]
    elif leaf_mode == 'array':
        return ordered_values
    elif leaf_mode == 'object':
        return {out_field_map[c]: leaf_dict[c] for c in value_cols}
    else:
        if len(value_cols) == 1:
            return ordered_values[0]
        return {out_field_map[c]: leaf_dict[c] for c in value_cols}


def preflight_and_group_rows(rows, fieldnames, group_cols, value_cols, agg_map,
                              missing_value, warn_missing, warn_duplicates):
    """Validate columns, group rows, and collect stats. Returns (grouped, stats)."""
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
                if col in agg_map and warn_missing:
                    pass

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
    """Core conversion. Returns (result_dict, stats_dict)."""
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


def print_file_stats(label, rows, fieldnames, stats):
    print(f"[{label}] Total rows: {len(rows)}")
    if not rows and not fieldnames:
        print(f"  [Empty] File is completely empty (no header, no data)")
        return
    if not rows:
        print(f"  [Empty] Header only — no data rows. Columns: {list(fieldnames or [])}")
        return
    print(f"  Data columns: {', '.join(fieldnames or [])}")
    if stats.get('empty_result'):
        print(f"  [Empty] After grouping/filtering, no output groups were produced")


def convert_file(input_source, group_cols, value_cols, agg_map, agg_rename_map,
                 leaf_mode, missing_value, warn_missing, warn_duplicates,
                 agg_func_suffix, label=None):
    """Convert a single CSV file. Returns (result_or_None, stats, error_message)."""
    display_label = label or input_source
    try:
        rows, fieldnames = _read_csv(input_source)
    except FileNotFoundError:
        return None, {}, f"File not found"
    except Exception as e:
        return None, {}, f"Read error: {e}"

    empty_case = (not rows)

    try:
        result, stats = convert_rows(
            rows, fieldnames or [], group_cols, value_cols, agg_map, agg_rename_map,
            leaf_mode, missing_value, warn_missing, warn_duplicates, agg_func_suffix,
        )
    except ValueError as e:
        return None, {}, str(e)
    except Exception as e:
        return None, {}, f"Conversion error: {e}"

    if empty_case:
        stats['empty_result'] = True
        stats['total_rows'] = 0
        stats['empty_csv'] = True
        stats['header_only'] = bool(fieldnames)

    return result, stats, None


def dry_run_report(input_paths, group_cols, value_cols, agg_map, agg_rename_map,
                   leaf_mode, missing_value, recursive, agg_func_suffix, preview_examples=5):
    files = discover_inputs(input_paths, recursive=recursive)
    if not files:
        print("[Preview] No CSV files found.", file=sys.stderr)
        sys.exit(1)

    out_field_map = build_output_field_map(value_cols, agg_map, agg_rename_map, agg_func_suffix)

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
            print(f"  [Error] File not found", file=sys.stderr)
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

        dup = stats['duplicate_group_rows']
        if dup > 0:
            print(f"  Groups with multiple rows: {dup} (will be aggregated)")
        else:
            print(f"  Groups with multiple rows: 0")

        if stats['missing_group_rows'] > 0:
            print(f"  [Warning] Rows with missing group values: {stats['missing_group_rows']}")
        for col, cnt in stats['missing_value_rows'].items():
            if cnt > 0:
                print(f"  [Warning] Missing values in column '{col}': {cnt} rows")

        non_num = defaultdict(int)
        for gk, grows in grouped.items():
            for col in [c for c in value_cols if c in agg_map]:
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
            print(f"  [Empty] No valid groups after filtering")
            overall_empty += 1

    print("-" * 50)
    print("=" * 60)
    if errors:
        print("[Preview] Issues found — please review the errors above.")
        sys.exit(1)
    if overall_empty or overall_header_only:
        parts = []
        if overall_empty:
            parts.append(f"{overall_empty} completely empty")
        if overall_header_only:
            parts.append(f"{overall_header_only} header-only")
        print(f"[Preview] Note: {', '.join(parts)} file(s) will produce no output.")
    print("[Preview] Configuration looks valid. Ready to convert.")
    print("=" * 60)


def build_parser():
    parser = argparse.ArgumentParser(
        description='Convert CSV to nested JSON with grouping, aggregation, and batch processing.'
    )
    parser.add_argument('input', nargs='+',
                        help='Input CSV file(s), directory, or "-" for stdin')
    parser.add_argument('-o', '--output', default=None,
                        help='Output JSON file path (default: stdout for single, per-file for batch)')
    parser.add_argument('--output-dir', default=None,
                        help='Directory for per-file batch outputs (default: current dir)')
    parser.add_argument('--merge', action='store_true',
                        help='Merge all CSV results into a single JSON keyed by filename')
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
                        help='Do not add function suffix to aggregated field names (use raw col name)')
    parser.add_argument('--leaf-as', default='auto', choices=LEAF_MODES,
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
                        help='Preview mode: validate columns, show example paths, do not write output')
    parser.add_argument('--preview-examples', type=int, default=5,
                        help='Number of example paths to show in preview (default: 5)')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.config:
        config = load_config(args.config)
        args = apply_config_to_args(args, config)

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

    indent = None if args.no_indent else args.indent
    merged_results = {}
    successes = []
    failures = []
    empties = []

    is_batch = (len(files) > 1) or args.merge or args.output_dir

    for fp in files:
        label = '(stdin)' if fp == '-' else fp
        result, stats, err = convert_file(
            fp, group_cols, value_cols, dict(agg_map), agg_rename_map,
            args.leaf_as, args.missing_value,
            not args.no_warn_missing, not args.no_warn_duplicates,
            agg_func_suffix, label=label,
        )

        if err is not None:
            print(f"[FAIL] {label}: {err}", file=sys.stderr)
            failures.append((label, err))
            continue

        if stats.get('empty_result'):
            if stats.get('empty_csv'):
                if not stats.get('header_only'):
                    msg = 'completely empty (no header, no data)'
                else:
                    msg = 'header only, no data rows'
                print(f"[EMPTY] {label}: {msg}", file=sys.stderr)
                empties.append((label, msg))
                continue
            else:
                msg = 'no output groups after grouping/filtering'
                print(f"[WARN]  {label}: {msg}", file=sys.stderr)

        if stats.get('non_numeric_values'):
            for col, cnt in stats['non_numeric_values'].items():
                if cnt > 0 and not args.no_warn_missing:
                    warnings.warn(
                        f"{label}: {cnt} non-numeric values skipped in aggregated column '{col}'"
                    )

        if args.merge:
            key = '(stdin)' if fp == '-' else os.path.splitext(os.path.basename(fp))[0]
            merged_results[key] = result
            successes.append((label, stats, stats.get('empty_result', False)))
        elif is_batch:
            out_path = args.output or default_output_path(fp, args.output_dir)
            if fp == '-' and not args.output and not args.output_dir:
                print(json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False))
                successes.append((label, stats, stats.get('empty_result', False)))
                continue
            try:
                with open(out_path, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False))
                    f.write('\n')
                print(f"[OK]   {label} → {out_path}", file=sys.stderr)
                successes.append((label, stats, stats.get('empty_result', False)))
            except Exception as e:
                print(f"[FAIL] {label}: write error: {e}", file=sys.stderr)
                failures.append((label, f"write error: {e}"))
        else:
            output_str = json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)
            if args.output:
                with open(args.output, 'w', encoding='utf-8') as f:
                    f.write(output_str)
                    f.write('\n')
                print(f"[OK]   {label} → {args.output}", file=sys.stderr)
            else:
                print(output_str)
            successes.append((label, stats, stats.get('empty_result', False)))

    if args.merge:
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
        print(file=sys.stderr)
        print("=" * 50, file=sys.stderr)
        print(f"Summary: {total} file(s) — {ok} OK, {empty} empty, {fail} failed", file=sys.stderr)
        if failures:
            print("Failures:", file=sys.stderr)
            for label, err in failures:
                print(f"  - {label}: {err}", file=sys.stderr)
        print("=" * 50, file=sys.stderr)

    if failures:
        sys.exit(2)


if __name__ == '__main__':
    main()
