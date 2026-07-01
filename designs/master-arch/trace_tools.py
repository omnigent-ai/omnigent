#!/usr/bin/env python3
"""Omnigent trace extraction helpers (Jaeger query API at :16686).

Usage:
  python trace_tools.py services
  python trace_tools.py recent [minutes=10]      # recent traces, grouped, with session.id
  python trace_tools.py conv <conv_id> [minutes=60]   # all spans for a session.id, summarized
  python trace_tools.py tree <trace_id>          # parent/child span tree for one trace
  python trace_tools.py raw <conv_id> <outfile>  # dump all traces for a conv to a json file
"""
import json, sys, urllib.request, collections

BASE = "http://localhost:16686"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.load(r)

def services():
    return get("/api/services")["data"] or []

def all_traces(minutes=60, limit=200):
    """Return {traceID: trace} across all omni-* services."""
    lookback = f"{minutes}m" if minutes < 60 else f"{minutes//60}h"
    out = {}
    for s in services():
        if not s.lower().startswith("omni"):
            continue
        try:
            d = get(f"/api/traces?service={s}&limit={limit}&lookback={lookback}")["data"]
        except Exception:
            d = []
        for t in d:
            out[t["traceID"]] = t
    return out

def _svc(t):
    return {pid: p["serviceName"] for pid, p in t["processes"].items()}

def _sids(t):
    s = set()
    for sp in t["spans"]:
        for tag in sp.get("tags", []):
            if tag["key"] == "session.id":
                s.add(tag["value"])
    return s

def _parent(sp, spanids):
    for r in sp.get("references", []):
        if r["refType"] == "CHILD_OF":
            return r["spanID"]
    return None

def cmd_recent(minutes=10):
    traces = all_traces(int(minutes))
    rows = []
    for tid, t in traces.items():
        procs = _svc(t)
        svcset = sorted({procs.get(sp["processID"]) for sp in t["spans"]})
        rows.append((min(sp["startTime"] for sp in t["spans"]), tid, len(t["spans"]), svcset, _sids(t)))
    rows.sort()
    for st, tid, n, svcset, sids in rows:
        print(f"{tid}  spans={n:4d}  services={svcset}  session.id={sorted(sids) or '-'}")

def cmd_conv(conv_id, minutes=60):
    traces = [t for t in all_traces(int(minutes)).values() if conv_id in _sids(t)]
    print(f"conv {conv_id}: {len(traces)} traces")
    hist = collections.Counter()
    edges = collections.Counter()
    roots = []
    for t in traces:
        procs = _svc(t)
        spans = {sp["spanID"]: sp for sp in t["spans"]}
        for sp in t["spans"]:
            hist[(procs.get(sp["processID"]), sp["operationName"])] += 1
            p = _parent(sp, spans)
            if p is None or p not in spans:
                roots.append((procs.get(sp["processID"]), sp["operationName"], t["traceID"]))
            elif procs.get(spans[p]["processID"]) != procs.get(sp["processID"]):
                edges[(procs.get(spans[p]["processID"]), procs.get(sp["processID"]), sp["operationName"])] += 1
    print("\n=== root spans (per trace) ===")
    for r in roots[:40]:
        print(f"  {r[0]:12s} {r[1]}   [{r[2][:12]}]")
    print("\n=== span histogram (service, op): count ===")
    for (sv, op), c in hist.most_common(60):
        print(f"  {c:4d}  {str(sv):12s} {op}")
    print("\n=== cross-service edges ===")
    for (a, b, op), c in edges.most_common(30):
        print(f"  {a} -> {b}  [{op}] x{c}")

def cmd_tree(trace_id):
    t = get(f"/api/traces/{trace_id}")["data"][0]
    procs = _svc(t)
    spans = {sp["spanID"]: sp for sp in t["spans"]}
    children = collections.defaultdict(list)
    roots = []
    for sp in t["spans"]:
        p = _parent(sp, spans)
        (children[p] if (p and p in spans) else roots).append(sp)
    for lst in children.values():
        lst.sort(key=lambda s: s["startTime"])
    roots.sort(key=lambda s: s["startTime"])
    def walk(sp, depth):
        tags = {tg["key"]: tg["value"] for tg in sp.get("tags", [])}
        kind = tags.get("openinference.span.kind", "")
        extra = f" [{kind}]" if kind else ""
        print(f"{'  '*depth}{procs.get(sp['processID']):11s} {sp['operationName']}{extra} ({sp['duration']//1000}ms)")
        for c in children.get(sp["spanID"], []):
            walk(c, depth + 1)
    for r in roots:
        walk(r, 0)

def cmd_raw(conv_id, outfile):
    traces = [t for t in all_traces(180).values() if conv_id in _sids(t)]
    with open(outfile, "w") as f:
        json.dump(traces, f)
    print(f"wrote {len(traces)} traces to {outfile}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "recent"
    args = sys.argv[2:]
    {"services": lambda: print(services()),
     "recent": cmd_recent, "conv": cmd_conv, "tree": cmd_tree, "raw": cmd_raw}[cmd](*args)
