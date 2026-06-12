#!/usr/bin/env python3
"""CSV to nested JSON converter with grouping and aggregation support."""

import argparse
import csv
import json
import sys
import warnings
from collections import defaultdict


SUPPORTED_AGG_FUNCS = ('sum', 'avg', 'count', 'min', 'max')


def _deep_set(d, keys, value):
    for k in keys[:-1]:
        if k not in d:
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _aggregate(values, agg_func):
    if not values:
        return None
    numeric_values = []
    for v in values:
        if v is None or v == '':
            continue
        try:
            numeric_values.append(float(v))
        except (ValueError, TypeError):
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
        from io import StringIO
        reader = csv.DictReader(StringIO(content))
        return list(reader)
    else:
        try:
            with open(input_source, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                return list(reader)
        except FileNotFoundError:
            print(f"Error: File '{input_source}' not found.", file=sys.stderr)
            sys.exit(1)


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


def resolve_value_cols(value_cols, agg_map):
    resolved = list(value_cols) if value_cols else []
    for col in agg_map:
        if col not in resolved:
            resolved.append(col)
    return resolved


def dry_run_check(input_source, group_cols, value_cols, agg_map):
    rows = _read_csv(input_source)

    if not rows:
        print("CSV is empty (no data rows).", file=sys.stderr)
        return

    fieldnames = list(rows[0].keys())
    print(f"[Preview] CSV columns ({len(fieldnames)}): {', '.join(fieldnames)}")
    print(f"[Preview] Group columns ({len(group_cols)}): {' -> '.join(group_cols)}")
    print(f"[Preview] Value columns ({len(value_cols)}): {', '.join(value_cols)}")

    if agg_map:
        agg_info = ', '.join(f"{col}({func})" for col, func in agg_map.items())
        print(f"[Preview] Aggregations: {agg_info}")
    else:
        print("[Preview] Aggregations: none (raw values)")

    missing_group = [c for c in group_cols if c not in fieldnames]
    missing_values = [c for c in value_cols if c not in fieldnames]

    if missing_group:
        print(f"[Error] Missing group columns in CSV: {', '.join(missing_group)}", file=sys.stderr)
    if missing_values:
        print(f"[Error] Missing value columns in CSV: {', '.join(missing_values)}", file=sys.stderr)

    if missing_group or missing_values:
        sys.exit(1)

    grouped_counts = defaultdict(int)
    missing_group_rows = 0
    missing_value_rows = defaultdict(int)

    for row_idx, row in enumerate(rows, start=2):
        group_key_parts = []
        has_missing_group = False
        for col in group_cols:
            val = row.get(col, '').strip()
            if val == '' or val is None:
                has_missing_group = True
            group_key_parts.append(val)

        if has_missing_group:
            missing_group_rows += 1

        group_key = tuple(group_key_parts)
        grouped_counts[group_key] += 1

        for col in value_cols:
            val = row.get(col, '')
            if val == '' or val is None:
                missing_value_rows[col] += 1

    unique_groups = len(grouped_counts)
    dup_groups = sum(1 for k, v in grouped_counts.items() if v > 1)

    print(f"[Preview] Total rows: {len(rows)}")
    print(f"[Preview] Unique group keys: {unique_groups}")
    if dup_groups > 0:
        print(f"[Preview] Groups with multiple rows: {dup_groups} (aggregation recommended)")
    if missing_group_rows > 0:
        print(f"[Warning] Rows with missing group values: {missing_group_rows}")
    if missing_value_rows:
        for col, cnt in missing_value_rows.items():
            if cnt > 0:
                print(f"[Warning] Missing values in column '{col}': {cnt} rows")

    structure = group_cols + ['leaf']
    print(f"[Preview] Nested structure: {' → '.join(structure)}")
    print("[Preview] Configuration looks valid.")


def csv_to_nested_json(input_source, group_cols, value_cols, agg_map=None,
                       missing_value=None, warn_missing=True, warn_duplicates=True):
    if agg_map is None:
        agg_map = {}

    for col in group_cols:
        if col in agg_map:
            raise ValueError(f"Group column '{col}' cannot also be an aggregation column")

    for col in value_cols:
        if col not in agg_map:
            agg_map[col] = None

    rows = _read_csv(input_source)

    if not rows:
        return {}

    fieldnames = rows[0].keys()
    all_cols = group_cols + value_cols
    for col in all_cols:
        if col not in fieldnames:
            print(f"Error: Column '{col}' not found in CSV. Available columns: {list(fieldnames)}", file=sys.stderr)
            sys.exit(1)

    if not group_cols:
        print("Error: At least one group column must be specified.", file=sys.stderr)
        sys.exit(1)

    if not value_cols:
        print("Error: At least one value column must be specified.", file=sys.stderr)
        sys.exit(1)

    grouped = defaultdict(list)

    for row_idx, row in enumerate(rows, start=2):
        group_key_parts = []
        has_missing_group = False
        for col in group_cols:
            val = row.get(col, '').strip()
            if val == '' or val is None:
                has_missing_group = True
                if warn_missing:
                    warnings.warn(f"Row {row_idx}: missing value in group column '{col}'")
                val = missing_value if missing_value is not None else ''
            group_key_parts.append(val)

        if has_missing_group and missing_value is None:
            continue

        group_key = tuple(group_key_parts)
        grouped[group_key].append(row)

    result = {}
    leaf_values_seen = set()

    for group_key, group_rows in grouped.items():
        key_path = list(group_key)
        leaf_dict = {}

        for val_col in value_cols:
            agg_func = agg_map.get(val_col)
            if agg_func:
                values = [row.get(val_col, '') for row in group_rows]
                agg_result = _aggregate(values, agg_func)
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

        if len(value_cols) == 1:
            final_leaf = leaf_dict[value_cols[0]]
        else:
            final_leaf = leaf_dict

        key_tuple = tuple(key_path)
        if key_tuple in leaf_values_seen and warn_duplicates:
            warnings.warn(f"Duplicate group key {key_path} encountered")
        leaf_values_seen.add(key_tuple)

        _deep_set(result, key_path, final_leaf)

    return result


def build_parser():
    parser = argparse.ArgumentParser(
        description='Convert CSV to nested JSON with grouping and aggregation.'
    )
    parser.add_argument('input', help='Input CSV file path (use "-" for stdin)')
    parser.add_argument('-o', '--output', default=None,
                        help='Output JSON file path (default: stdout)')
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
                        help='Preview mode: validate columns and rules, do not write output')
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.config:
        config = load_config(args.config)
        args = apply_config_to_args(args, config)

    warnings.simplefilter('always')

    agg_map = validate_and_build_agg_map(args.agg)
    value_cols = resolve_value_cols(args.values, agg_map)
    group_cols = args.group or []

    if not group_cols:
        print("Error: --group is required (can be specified via config file).", file=sys.stderr)
        sys.exit(1)

    if not value_cols:
        print("Error: At least one value column is required (via --values or --agg).", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        dry_run_check(args.input, group_cols, value_cols, agg_map)
        return

    result = csv_to_nested_json(
        input_source=args.input,
        group_cols=group_cols,
        value_cols=value_cols,
        agg_map=agg_map,
        missing_value=args.missing_value,
        warn_missing=not args.no_warn_missing,
        warn_duplicates=not args.no_warn_duplicates,
    )

    indent = None if args.no_indent else args.indent
    json_str = json.dumps(result, indent=indent, sort_keys=args.sort_keys, ensure_ascii=False)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(json_str)
            f.write('\n')
        print(f"JSON written to {args.output}", file=sys.stderr)
    else:
        print(json_str)


if __name__ == '__main__':
    main()
