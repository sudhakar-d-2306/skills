#!/usr/bin/env python
"""
sql_timing_report.py -- rank SQL execution time inside an OTel/Grafana trace export.

Usage:
    python sql_timing_report.py trace1.json [trace2.json ...] [--top N] [--min-ms X]

For each file: prints total SQL time and call count per db.name, then for each
database lists distinct statements ranked by total time (calls / total ms / max ms).
Bind placeholders and literal values are normalized so repeated calls of "the same
query" (modulo ? vs literal substitution) group together under one entry.

Standard library only -- no dependencies to install.
"""
import argparse
import json
import re
from collections import OrderedDict, defaultdict


def iter_spans(node):
    if isinstance(node, dict):
        if "startTimeUnixNano" in node and "attributes" in node:
            yield node
        for v in node.values():
            yield from iter_spans(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_spans(v)


def span_attrs(span):
    out = {}
    for a in span.get("attributes", []):
        v = a.get("value", {})
        out[a["key"]] = next(iter(v.values()), None) if v else None
    return out


def normalize(sql):
    """Collapse literals/binds to ? so repeated calls of 'the same query' group together."""
    s = re.sub(r"'[^']*'|\"[^\"]*\"", "?", sql)
    s = re.sub(r"\b\d+(\.\d+)?\b", "?", s)
    return re.sub(r"\s+", " ", s).strip()


def span_ms(span):
    return (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("traces", nargs="+", help="OTel/Grafana trace JSON file(s)")
    ap.add_argument(
        "--top", type=int, default=0,
        help="show only the top N statements per database (0 = all)",
    )
    ap.add_argument(
        "--min-ms", type=float, default=0.0,
        help="hide statements with total time below this (ms)",
    )
    args = ap.parse_args()

    for path in args.traces:
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            print(f"FILE {path}\n  could not parse: {e}\n")
            continue

        spans = list(iter_spans(data))
        sql_spans = []
        for s in spans:
            a = span_attrs(s)
            if a.get("db.statement"):
                sql_spans.append((s, a))

        print("=" * 100)
        print(f"FILE {path}   spans {len(spans)}   SQL spans {len(sql_spans)}")
        if not sql_spans:
            print("  no spans with a db.statement attribute found")
            continue

        by_db = defaultdict(list)
        for s, a in sql_spans:
            by_db[a.get("db.name", "(unknown)")].append((s, a))

        grand_total = sum(span_ms(s) for s, a in sql_spans)
        print(f"  total SQL time across all databases: {grand_total:.1f} ms ({len(sql_spans)} calls)\n")

        for db, items in sorted(
            by_db.items(), key=lambda kv: -sum(span_ms(s) for s, a in kv[1])
        ):
            stmts = OrderedDict()
            for s, a in items:
                key = normalize(a["db.statement"])
                g = stmts.setdefault(
                    key, {"calls": 0, "total": 0.0, "max": 0.0, "example": a["db.statement"]}
                )
                d = span_ms(s)
                g["calls"] += 1
                g["total"] += d
                g["max"] = max(g["max"], d)

            db_total = sum(g["total"] for g in stmts.values())
            print(f"--- {db}  ({len(items)} calls, {len(stmts)} distinct statements, {db_total:.1f} ms total)")

            ranked = sorted(stmts.items(), key=lambda kv: -kv[1]["total"])
            shown = 0
            for _key, g in ranked:
                if g["total"] < args.min_ms:
                    continue
                if args.top and shown >= args.top:
                    break
                shown += 1
                print(f"  #{shown}  total {g['total']:8.1f} ms  calls {g['calls']:4}  max {g['max']:8.1f} ms")
                print(f"      {g['example'][:300]}")
            print()


if __name__ == "__main__":
    main()
