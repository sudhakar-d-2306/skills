#!/usr/bin/env python
"""
attribute_sql_by_event.py -- attribute SQL spans in an OTel/Grafana trace to the
nearest ancestor span that marks a logical sub-operation (e.g. a nested
workflow/event/rule call inside one outer request).

Usage:
    python attribute_sql_by_event.py trace.json
        [--event-attr http.request.header.eventname]
        [--events NAME1 NAME2 ...]
        [--top N]

Without --events, reports every sub-operation span found. With --events, filters
to just the named ones. Use this when a flat per-database SQL total hides which
inner step of a multi-stage request actually owns the time.

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


def span_ms(span):
    return (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("trace", help="OTel/Grafana trace JSON file")
    ap.add_argument(
        "--event-attr", default="http.request.header.eventname",
        help="attribute key that marks a sub-operation span (default: %(default)s)",
    )
    ap.add_argument(
        "--events", nargs="*", default=None,
        help="only report these sub-operation names (default: all found)",
    )
    ap.add_argument("--top", type=int, default=6, help="statements to show per sub-operation (default: 6)")
    args = ap.parse_args()

    data = json.load(open(args.trace, encoding="utf-8"))
    spans = list(iter_spans(data))
    by_id = {s["spanId"]: s for s in spans if "spanId" in s}
    attrs = {sid: span_attrs(s) for sid, s in by_id.items()}

    def nearest_event_ancestor(span):
        cur = span.get("parentSpanId")
        hops = 0
        while cur and cur in by_id and hops < 500:
            if args.event_attr in attrs[cur]:
                return cur
            cur = by_id[cur].get("parentSpanId")
            hops += 1
        return None

    groups = defaultdict(list)
    for s in spans:
        a = span_attrs(s)
        if not a.get("db.statement"):
            continue
        groups[nearest_event_ancestor(s)].append((s, a))

    events = [s for s in spans if args.event_attr in span_attrs(s)]
    events.sort(key=lambda s: int(s["startTimeUnixNano"]))

    print(f"FILE {args.trace}   traceId {spans[0].get('traceId') if spans else '?'}")
    for e in events:
        ea = span_attrs(e)
        name = str(ea.get(args.event_attr, "")).strip('[]"')
        if args.events and name not in args.events:
            continue
        items = groups.get(e["spanId"], [])
        total_span_ms = span_ms(e)
        sql_ms = sum(span_ms(s) for s, a in items)
        print("\n" + "=" * 100)
        print(f"{name}")
        share = (100 * sql_ms / total_span_ms) if total_span_ms else 0
        print(
            f"span {total_span_ms:.1f} ms   SQL in this span: {len(items)} statements, "
            f"{sql_ms:.1f} ms ({share:.0f}% of span)"
        )

        by_db = defaultdict(lambda: [0, 0.0])
        stmts = OrderedDict()
        for s, a in items:
            db = a.get("db.name", "(unknown)")
            by_db[db][0] += 1
            by_db[db][1] += span_ms(s)
            key = (db, re.sub(r"\s+", " ", a["db.statement"]).strip())
            g = stmts.setdefault(key, {"calls": 0, "total": 0.0, "max": 0.0})
            d = span_ms(s)
            g["calls"] += 1
            g["total"] += d
            g["max"] = max(g["max"], d)
        if by_db:
            print(
                "by database: "
                + ", ".join(
                    f"{db} {c}/{t:.1f}ms"
                    for db, (c, t) in sorted(by_db.items(), key=lambda kv: -kv[1][1])
                )
            )

        ranked = sorted(stmts.items(), key=lambda kv: -kv[1]["total"])
        for i, ((db, q), g) in enumerate(ranked[: args.top], 1):
            print(f"  #{i}  total {g['total']:8.1f} ms  calls {g['calls']:4}  max {g['max']:7.1f} ms  [{db}]")
            print(f"      {q[:300]}")

    orphan = groups.get(None, [])
    if orphan and not args.events:
        print("\n" + "=" * 100)
        print(f"SQL with no matching ancestor ({len(orphan)} statements)")
        tot = sum(span_ms(s) for s, a in orphan)
        print(f"  total {tot:.1f} ms")
        for s, a in orphan[: args.top]:
            print(f"  {span_ms(s):.1f} ms  [{a.get('db.name')}]  {a['db.statement'][:200]}")


if __name__ == "__main__":
    main()
