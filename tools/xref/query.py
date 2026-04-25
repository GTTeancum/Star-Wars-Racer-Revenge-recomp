#!/usr/bin/env python3
"""
Query the global-variable cross-reference database produced by build_xref.py.

Usage:
    # Look up a single address:
    python tools/xref/query.py 0x384670

    # Multiple addresses:
    python tools/xref/query.py 0x384670 0x443870 0x442b70

    # Range:
    python tools/xref/query.py --range 0x384000 0x385000

    # Show functions that write to addresses in a range (writer-frequency):
    python tools/xref/query.py --writers-in 0x443000 0x444000

    # Show the addresses touched by a specific function:
    python tools/xref/query.py --function entry_13fda0_0x140230

    # Show hottest addresses (most readers, most writers):
    python tools/xref/query.py --top-readers 20
    python tools/xref/query.py --top-writers 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path


DEFAULT_JSON = Path("build") / "xref" / "globals.json"
DEFAULT_CALLGRAPH = Path("build") / "xref" / "callgraph.json"
DEFAULT_MEMORY_MAP = Path("config") / "memory_map.toml"


def load_memory_map(path: Path) -> dict[int, dict]:
    """Return {addr_int → {name, width/end, desc, tags}} parsed from TOML."""
    if not path.exists():
        return {}
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # Fallback
    except ImportError:
        return {}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    out: dict[int, dict] = {}
    for entry in data.get("entries", []):
        addr_s = entry.get("address", "")
        try:
            addr = int(addr_s, 0) if isinstance(addr_s, str) else int(addr_s)
        except (TypeError, ValueError):
            continue
        out[addr] = entry
    return out


def mmap_name(mmap: dict[int, dict], addr: int) -> str | None:
    """Return a symbolic name if the address is inside a mapped entry."""
    for base, entry in mmap.items():
        end_s = entry.get("end")
        if end_s:
            try:
                end = int(end_s, 0) if isinstance(end_s, str) else int(end_s)
            except (TypeError, ValueError):
                continue
        else:
            width = entry.get("width", 4)
            if isinstance(width, str):
                try:
                    width = int(width, 0)
                except ValueError:
                    width = 4
            end = base + width
        if base <= addr < end:
            name = entry.get("name") or entry.get("desc") or ""
            off = addr - base
            return f"{name}+0x{off:x}" if off else name
    return None


def parse_addr(s: str) -> int:
    return int(s, 0)


def load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"xref file not found: {path}\n"
                 f"Run: python tools/xref/build_xref.py "
                 f"--elf SLUS_202.68 --functions src/generated --output {path}")
    with open(path) as f:
        return json.load(f)


def fmt_access(a: dict) -> str:
    return f"{a['function']:<40} @ {a['pc']}  {a['op']:<6} ({a['width']}B)"


def print_address(addr_s: str, entry: dict, *, max_list: int = 0,
                  mmap: dict[int, dict] | None = None) -> None:
    header = f"=== {addr_s}"
    if mmap:
        name = mmap_name(mmap, int(addr_s, 16))
        if name:
            header += f"  [{name}]"
    header += " ==="
    print(f"\n{header}")
    if mmap:
        entry_md = mmap.get(int(addr_s, 16))
        if entry_md and entry_md.get("desc"):
            print(f"  desc: {entry_md['desc']}")
    print(f"  writers: {entry['writer_count']}   readers: {entry['reader_count']}")
    if entry.get("first_writer_function"):
        print(f"  first writer: {entry['first_writer_function']}")
    writers = entry["writers"]
    readers = entry["readers"]
    if max_list > 0:
        writers = writers[:max_list]
        readers = readers[:max_list]
    if writers:
        print(f"  WRITERS:")
        for w in writers:
            print(f"    {fmt_access(w)}")
        if max_list and len(entry["writers"]) > max_list:
            print(f"    ... {len(entry['writers']) - max_list} more")
    if readers:
        print(f"  READERS:")
        for r in readers:
            print(f"    {fmt_access(r)}")
        if max_list and len(entry["readers"]) > max_list:
            print(f"    ... {len(entry['readers']) - max_list} more")


def cmd_lookup(doc: dict, addrs: list[str], max_list: int,
               mmap: dict[int, dict] | None = None) -> int:
    addrs_db = doc["addresses"]
    for a in addrs:
        a_n = parse_addr(a)
        key = f"0x{a_n:08x}"
        if key in addrs_db:
            print_address(key, addrs_db[key], max_list=max_list, mmap=mmap)
        else:
            # Try nearby addresses (for struct field access patterns)
            nearby = []
            for offset in (-0x20, -0x10, -0x08, -0x04, 0x04, 0x08, 0x10, 0x20):
                near_key = f"0x{(a_n + offset) & 0xFFFFFFFF:08x}"
                if near_key in addrs_db:
                    nearby.append((offset, near_key))
            print(f"\n=== {key} ===")
            print(f"  (no writes or reads recorded)")
            if nearby:
                print(f"  Nearby addresses with activity:")
                for off, k in nearby:
                    e = addrs_db[k]
                    print(f"    {k} ({off:+#x}): W={e['writer_count']} R={e['reader_count']}")
    return 0


def cmd_range(doc: dict, lo_s: str, hi_s: str, max_list: int) -> int:
    lo, hi = parse_addr(lo_s), parse_addr(hi_s)
    addrs_db = doc["addresses"]
    hits = 0
    for key in sorted(addrs_db.keys()):
        a = int(key, 16)
        if lo <= a < hi:
            print_address(key, addrs_db[key], max_list=max_list)
            hits += 1
    if hits == 0:
        print(f"No activity recorded in range {lo_s}..{hi_s}")
    else:
        print(f"\n({hits} addresses in range)")
    return 0


def cmd_writers_in(doc: dict, lo_s: str, hi_s: str, top: int) -> int:
    lo, hi = parse_addr(lo_s), parse_addr(hi_s)
    counts: Counter[str] = Counter()
    for key, entry in doc["addresses"].items():
        a = int(key, 16)
        if not (lo <= a < hi):
            continue
        for w in entry["writers"]:
            counts[w["function"]] += 1
    print(f"\nWriter functions touching {lo_s}..{hi_s} (by write count):")
    for func, n in counts.most_common(top):
        print(f"  {n:4d}  {func}")
    return 0


def cmd_function(doc: dict, func_name: str, max_list: int) -> int:
    writes: list[tuple[str, dict]] = []
    reads:  list[tuple[str, dict]] = []
    for key, entry in doc["addresses"].items():
        for w in entry["writers"]:
            if w["function"] == func_name:
                writes.append((key, w))
        for r in entry["readers"]:
            if r["function"] == func_name:
                reads.append((key, r))
    print(f"\n=== Function: {func_name} ===")
    print(f"  {len(writes)} distinct write accesses, {len(reads)} distinct read accesses")
    if writes:
        print(f"  WRITES (sorted by pc):")
        writes.sort(key=lambda x: int(x[1]["pc"], 16))
        for addr, w in writes[:max_list if max_list else len(writes)]:
            print(f"    {addr:12}  @ {w['pc']}  {w['op']:<6} ({w['width']}B)")
    if reads:
        print(f"  READS (sorted by pc):")
        reads.sort(key=lambda x: int(x[1]["pc"], 16))
        for addr, r in reads[:max_list if max_list else len(reads)]:
            print(f"    {addr:12}  @ {r['pc']}  {r['op']:<6} ({r['width']}B)")
    return 0


def load_callgraph(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def cmd_callers(cg: dict, func: str, max_list: int) -> int:
    if func not in cg["functions"]:
        print(f"Function not found in callgraph: {func}")
        return 1
    callers = cg["functions"][func]["callers"]
    print(f"\n{func} has {len(callers)} direct callers:")
    for c in callers[:max_list if max_list else len(callers)]:
        print(f"  {c}")
    return 0


def cmd_callees(cg: dict, func: str, max_list: int) -> int:
    if func not in cg["functions"]:
        print(f"Function not found in callgraph: {func}")
        return 1
    callees = cg["functions"][func]["callees"]
    print(f"\n{func} calls {len(callees)} distinct functions:")
    for c in callees[:max_list if max_list else len(callees)]:
        print(f"  {c}")
    return 0


def cmd_args_to(cg: dict, dst: str, max_list: int) -> int:
    """T15: list every call site that targets `dst`, with its resolved arg values.
    Useful for questions like "what state function addresses are ever passed
    to setGameState?" and "which module IDs does the dispatcher receive?"."""
    rows: list[tuple[str, str, dict | None, str]] = []
    for caller_name, info in cg["functions"].items():
        for site in info["call_sites"]:
            if site.get("target_name") == dst:
                rows.append((caller_name, site["pc"], site.get("args"), site.get("kind", "jal")))
    if not rows:
        print(f"No direct call sites found targeting {dst}.")
        return 1
    # Histogram of arg values across all call sites.
    arg_histos: dict[str, dict[str, int]] = {}
    for _, _, args, _ in rows:
        if not args: continue
        for k, v in args.items():
            arg_histos.setdefault(k, {}).setdefault(v, 0)
            arg_histos[k][v] += 1
    print(f"\nCall sites targeting {dst}: {len(rows)}")
    for caller, pc, args, kind in rows[:max_list]:
        arg_str = ""
        if args:
            arg_str = "  " + ", ".join(f"{k}={v}" for k, v in args.items())
        print(f"  {caller:<44} @ {pc}  [{kind}]{arg_str}")
    if len(rows) > max_list:
        print(f"  ... +{len(rows) - max_list} more")
    if arg_histos:
        print(f"\nDistinct argument values:")
        for argname in sorted(arg_histos):
            buckets = arg_histos[argname]
            total = sum(buckets.values())
            print(f"  ${argname}: {len(buckets)} distinct values across {total} sites")
            for val, count in sorted(buckets.items(), key=lambda kv: -kv[1]):
                print(f"    {count:4d}x  {val}")
    return 0


def cmd_args_from(cg: dict, src: str, max_list: int) -> int:
    """T15: list every call this function makes, with the resolved arg values.
    Useful for "what does this dispatcher actually dispatch to?"."""
    if src not in cg["functions"]:
        print(f"function not in callgraph: {src}")
        return 1
    sites = cg["functions"][src]["call_sites"]
    print(f"\nCall sites from {src}: {len(sites)}")
    for site in sites[:max_list]:
        target = site.get("target_name") or site.get("target") or "<jalr>"
        arg_str = ""
        args = site.get("args")
        if args:
            arg_str = "  " + ", ".join(f"{k}={v}" for k, v in args.items())
        print(f"  @ {site['pc']}  -> {target}  [{site.get('kind','jal')}]{arg_str}")
    if len(sites) > max_list:
        print(f"  ... +{len(sites) - max_list} more")
    return 0


def cmd_path(cg: dict, src: str, dst: str, max_depth: int) -> int:
    """BFS from src forward along callees, looking for a path to dst.
    Useful for 'how does boot reach setGameState?' queries."""
    if src not in cg["functions"]:
        print(f"source function not in callgraph: {src}")
        return 1
    if dst not in cg["functions"]:
        print(f"destination function not in callgraph: {dst}")
        return 1
    from collections import deque
    seen: dict[str, str | None] = {src: None}
    q = deque([(src, 0)])
    while q:
        cur, depth = q.popleft()
        if cur == dst:
            # Reconstruct path.
            chain: list[str] = []
            node: str | None = cur
            while node is not None:
                chain.append(node)
                node = seen[node]
            chain.reverse()
            print(f"\nCall path ({len(chain)-1} hops):")
            for step, fn in enumerate(chain):
                print(f"  {step:2d}. {fn}")
            return 0
        if depth >= max_depth:
            continue
        for callee in cg["functions"][cur]["callees"]:
            if callee not in seen:
                seen[callee] = cur
                q.append((callee, depth + 1))
    print(f"No call path from {src} to {dst} within depth {max_depth}.")
    return 1


def cmd_dot(cg: dict, src: str, dst: str, max_depth: int, outfile: str | None) -> int:
    """T8: emit a Graphviz DOT graph of ALL paths from src to dst within
    depth N. Useful for understanding indirect-heavy boot flows when --path
    only shows one of many.

    Strategy: BFS from src, keeping every edge; then BFS from dst backward
    (via reverse-callers); intersect the reachable sets; emit edges that
    are on SOME path from src to dst within depth."""
    if src not in cg["functions"]:
        print(f"source function not in callgraph: {src}")
        return 1
    if dst not in cg["functions"]:
        print(f"destination function not in callgraph: {dst}")
        return 1

    from collections import deque
    # Forward distances from src.
    fwd: dict[str, int] = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        d = fwd[u]
        if d >= max_depth:
            continue
        for v in cg["functions"][u]["callees"]:
            if v not in fwd:
                fwd[v] = d + 1
                q.append(v)
    # Backward distances from dst via callers.
    bwd: dict[str, int] = {dst: 0}
    q = deque([dst])
    while q:
        u = q.popleft()
        d = bwd[u]
        if d >= max_depth:
            continue
        for v in cg["functions"][u].get("callers", []):
            if v not in bwd:
                bwd[v] = d + 1
                q.append(v)
    # A node is "on some path from src to dst within depth max_depth"
    # iff fwd[u] + bwd[u] <= max_depth.
    on_path = {u for u in fwd if u in bwd and fwd[u] + bwd[u] <= max_depth}
    if not on_path:
        print(f"No call paths from {src} to {dst} within depth {max_depth}.")
        return 1
    # Collect the edges (u -> v) where both endpoints are on some path
    # and fwd[u] + 1 + bwd[v] <= max_depth.
    edges: list[tuple[str, str]] = []
    for u in on_path:
        d_u = fwd.get(u, 10**9)
        for v in cg["functions"][u]["callees"]:
            if v not in on_path: continue
            if d_u + 1 + bwd.get(v, 10**9) > max_depth: continue
            edges.append((u, v))
    # Emit DOT.
    lines: list[str] = []
    lines.append("digraph callpaths {")
    lines.append(f"  rankdir=LR;")
    lines.append(f"  labelloc=t;")
    lines.append(f"  label=\"paths: {src} -> {dst} (depth <= {max_depth})\";")
    lines.append(f"  \"{src}\" [style=filled,fillcolor=lightgreen];")
    lines.append(f"  \"{dst}\" [style=filled,fillcolor=lightcoral];")
    for u, v in edges:
        lines.append(f"  \"{u}\" -> \"{v}\";")
    lines.append("}")
    out = "\n".join(lines) + "\n"
    if outfile:
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"Wrote DOT graph to {outfile}  ({len(on_path)} nodes, {len(edges)} edges)")
    else:
        print(out)
    return 0


def cmd_top(doc: dict, kind: str, n: int) -> int:
    rows: list[tuple[int, str]] = []
    for key, entry in doc["addresses"].items():
        count = entry["writer_count"] if kind == "writers" else entry["reader_count"]
        rows.append((count, key))
    rows.sort(reverse=True)
    print(f"\nTop {n} addresses by {kind}:")
    for count, key in rows[:n]:
        if count == 0:
            break
        entry = doc["addresses"][key]
        # Deduplicate function names so very-reused addresses look like
        # "(5 unique funcs)" rather than flooding the output.
        funcs = set()
        access_list = entry["writers"] if kind == "writers" else entry["readers"]
        for a in access_list:
            funcs.add(a["function"])
        hint = f"{len(funcs)} unique fn{'s' if len(funcs)!=1 else ''}"
        first_fn = sorted(funcs)[0] if funcs else "-"
        if len(funcs) == 1:
            hint_str = first_fn
        else:
            hint_str = f"{first_fn} + {len(funcs)-1} others"
        print(f"  {count:4d}  {key}  ({hint_str})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("addresses", nargs="*", help="Addresses to look up (hex, e.g. 0x384670)")
    ap.add_argument("--json", default=str(DEFAULT_JSON),
                    help=f"Path to globals.json (default: {DEFAULT_JSON})")
    ap.add_argument("--range", nargs=2, metavar=("LO", "HI"),
                    help="Print all addresses in a range")
    ap.add_argument("--writers-in", nargs=2, metavar=("LO", "HI"),
                    help="Rank writer functions for a range")
    ap.add_argument("--function", help="List all accesses from a given function")
    ap.add_argument("--top-readers", type=int, metavar="N",
                    help="Top N addresses by reader count")
    ap.add_argument("--top-writers", type=int, metavar="N",
                    help="Top N addresses by writer count")
    ap.add_argument("--callers", metavar="FN",
                    help="Show direct callers of FN (requires callgraph.json)")
    ap.add_argument("--callees", metavar="FN",
                    help="Show direct callees of FN (requires callgraph.json)")
    ap.add_argument("--args-to", metavar="FN",
                    help="T15: show every call site targeting FN with its resolved arg values")
    ap.add_argument("--args-from", metavar="FN",
                    help="T15: show every call FN makes, with resolved arg values")
    ap.add_argument("--path", nargs=2, metavar=("SRC", "DST"),
                    help="Find a call path from SRC to DST via BFS")
    ap.add_argument("--paths-dot", nargs=2, metavar=("SRC", "DST"),
                    help="T8: emit Graphviz DOT of ALL paths from SRC to DST "
                         "within --max-depth hops")
    ap.add_argument("--dot-out", default=None,
                    help="Write DOT output to this file instead of stdout")
    ap.add_argument("--max-depth", type=int, default=6,
                    help="Max BFS depth for --path (default 6)")
    ap.add_argument("--callgraph", default=str(DEFAULT_CALLGRAPH),
                    help=f"Path to callgraph.json (default: {DEFAULT_CALLGRAPH})")
    ap.add_argument("--memory-map", default=str(DEFAULT_MEMORY_MAP),
                    help=f"Path to memory_map.toml (default: {DEFAULT_MEMORY_MAP})")
    ap.add_argument("--max-list", type=int, default=12,
                    help="Max accesses per list (default 12; 0 = unlimited)")
    ap.add_argument("--top", type=int, default=25,
                    help="Default N for --writers-in rankings (default 25)")
    args = ap.parse_args()

    doc = load(Path(args.json))

    if args.range:
        return cmd_range(doc, args.range[0], args.range[1], args.max_list)
    if args.writers_in:
        return cmd_writers_in(doc, args.writers_in[0], args.writers_in[1], args.top)
    if args.function:
        return cmd_function(doc, args.function, args.max_list)
    if args.top_readers is not None:
        return cmd_top(doc, "readers", args.top_readers)
    if args.top_writers is not None:
        return cmd_top(doc, "writers", args.top_writers)
    if args.callers or args.callees or args.path or args.paths_dot or \
       args.args_to or args.args_from:
        cg = load_callgraph(Path(args.callgraph))
        if cg is None:
            sys.exit(f"callgraph not found: {args.callgraph}\n"
                     f"Run: python tools/xref/build_callgraph.py "
                     f"--elf SLUS_202.68 --functions src/generated --output {args.callgraph}")
        if args.callers:
            return cmd_callers(cg, args.callers, args.max_list)
        if args.callees:
            return cmd_callees(cg, args.callees, args.max_list)
        if args.args_to:
            return cmd_args_to(cg, args.args_to, args.max_list)
        if args.args_from:
            return cmd_args_from(cg, args.args_from, args.max_list)
        if args.path:
            return cmd_path(cg, args.path[0], args.path[1], args.max_depth)
        if args.paths_dot:
            return cmd_dot(cg, args.paths_dot[0], args.paths_dot[1],
                           args.max_depth, args.dot_out)
    if args.addresses:
        mmap = load_memory_map(Path(args.memory_map)) if args.memory_map else {}
        return cmd_lookup(doc, args.addresses, args.max_list, mmap)

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
