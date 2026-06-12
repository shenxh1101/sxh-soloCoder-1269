#!/usr/bin/env python3
"""CSV to nested JSON converter with grouping and aggregation support."""

import argparse
import csv
import json
import sys
import warnings
from collections import defaultdict


def _deep_get(d, keys):
    for k in keys:
        d = d[k]
    return d


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


def csv_to_nested_json(csv_file, group_cols, value_cols, agg_map=None, 
                       missing_value=None, warn_missing=True, warn_duplicates=True):
    if agg_map is None:
        agg_map = {}

    for col in group_cols:
        if col in agg_map:
            raise ValueError(f"Group column '{col}' cannot also be an aggregation column")

    for col in value_cols:
        if col not in agg_map:
            agg_map[col] = None

    try:
        with open(csv_file, 'r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except FileNotFoundError:
        print(f"Error: File '{csv_file}' not found.", file=sys.stderr)
        sys.exit(1)

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
    leaf_values_seen = {}

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
        leaf_values_seen[key_tuple] = True

        _deep_set(result, key_path, final_leaf)

    return result


def main():
    parser = argparse.ArgumentParser(
        description='Convert CSV to nested JSON with grouping and aggregation.'
    )
    parser.add_argument('input', help='Input CSV file path')
    parser.add_argument('-o', '--output', help='Output JSON file path (optional)')
    parser.add_argument('--group', nargs='+', required=True,
                        help='Columns to group by (in order of nesting)')
    parser.add_argument('--values', nargs='+', required=True,
                        help='Columns to use as leaf values')
    parser.add_argument('--agg', nargs=2, action='append', default=[],
                        metavar=('COLUMN', 'FUNC'),
                        help='Aggregate a column: --agg sales sum. '
                             'Supported: sum, avg, count, min, max')
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

    args = parser.parse_args()

    warnings.simplefilter('always')

    agg_map = {}
    for col, func in args.agg:
        func_lower = func.lower()
        if func_lower not in ('sum', 'avg', 'count', 'min', 'max'):
            print(f"Error: Unknown aggregation function '{func}'. "
                  f"Supported: sum, avg, count, min, max", file=sys.stderr)
            sys.exit(1)
        agg_map[col] = func_lower

    for col in args.values:
        if col not in agg_map and len(args.values) == 1:
            pass
        elif col not in agg_map:
            pass

    result = csv_to_nested_json(
        csv_file=args.input,
        group_cols=args.group,
        value_cols=args.values,
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
