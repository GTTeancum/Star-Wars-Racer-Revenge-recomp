#!/usr/bin/env python3
"""
Dead-function detector — T13.

A function is "suspected-dead" if no reachability path can be built from
a set of known roots (program entry + any address the game installs as a
syscall/exception handler + overlay entries + any address referenced by a
resolved jalr target) through the direct `jal` callgraph.

Because the callgraph only resolves direct `jal`, suspected-dead is
NECESSARILY an overestimate: a function that's called only via jalr whose
target we haven't resolved will look dead here. That's USEFUL — those
are exactly the jalr patterns we want to know about.

Output categories:
  - truly dead:   0 direct callers, not reachable from any root, not a
                  resolved jalr target. Safe to stub/skip.
  - indirect-only: 0 direct callers, but it's a resolved jalr target OR
                   it's an overlay entry OR it's an extra_entry_point.
                   Reached by indirect dispatch.
  - reachable:    everything else.

Usage:
    python tools/xref/dead_functions.py \\
        --callgraph  build/xref/callgraph.json \\
        --indirect   build/xref/indirect_targets.json \\
        --roots      entry_0x100008_0x100008,overlay_kernel_74_0x80074000,...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path


DEFAULT_ROOTS = [
    "entry_0x100008_0x100008",            # _start
    "overlay_kernel_74_0x80074000",       # RR custom kernel overlays
    "overlay_kernel_75_0x80075000",
    "overlay_kernel_76_0x80076000",
]


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def reachable_from(cg: dict, roots: set[str]) -> set[str]:
    seen = set(r for r in roots if r in cg["functions"])
    q = deque(seen)
    while q:
        cur = q.popleft()
        for callee in cg["functions"][cur]["callees"]:
            if callee not in seen:
                seen.add(callee)
                q.append(callee)
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--callgraph", default="build/xref/callgraph.json")
    ap.add_argument("--indirect",  default="build/xref/indirect_targets.json")
    ap.add_argument("--roots", default=",".join(DEFAULT_ROOTS),
                    help=f"Comma-separated root functions (default: {','.join(DEFAULT_ROOTS)})")
    ap.add_argument("--max-list", type=int, default=30,
                    help="Max suspected-dead functions to print (default 30)")
    ap.add_argument("--output",
                    help="Optional JSON output with full lists")
    args = ap.parse_args()

    cg = load(args.callgraph)
    indirect = load(args.indirect) if Path(args.indirect).exists() else {"sites": []}

    all_fns = set(cg["functions"].keys())
    roots = set(r.strip() for r in args.roots.split(",") if r.strip())

    # Gather addresses of resolved jalr targets — these are indirectly reachable.
    # The callgraph uses function names (e.g. "entry_2f7430_0x2f7550"); indirect
    # records give us absolute target addresses. Build a name index keyed by
    # start-address so we can convert addresses → names.
    name_by_start: dict[int, str] = {}
    for fn, info in cg["functions"].items():
        try:
            s = int(info["start"], 16)
            name_by_start[s] = fn
        except (KeyError, ValueError):
            pass

    indirect_target_names: set[str] = set()
    for site in indirect.get("sites", []):
        for c in site.get("candidates", []):
            try:
                t = int(c["target"], 16)
            except (KeyError, ValueError):
                continue
            n = name_by_start.get(t)
            if n:
                indirect_target_names.add(n)

    # Direct reachability from roots.
    reachable = reachable_from(cg, roots)
    # Extend with indirect targets: if a resolved jalr points at function F,
    # treat F as reachable (plus anything F transitively calls).
    indirect_reachable = reachable_from(cg, indirect_target_names) - reachable
    # All functions reachable (directly or via known indirect).
    all_reachable = reachable | indirect_reachable

    dead: list[str] = []
    indirect_only: list[str] = []
    for fn, info in cg["functions"].items():
        if fn in reachable:
            continue
        callers = info.get("callers", [])
        if fn in indirect_reachable:
            indirect_only.append(fn)
            continue
        if callers:
            # Has a direct caller that's itself dead — downstream-dead. Mark
            # as dead too; it just happens to live in a dead island.
            dead.append(fn)
            continue
        # No direct callers, not indirectly reachable from the resolved jalrs.
        dead.append(fn)

    print("Dead-function analysis")
    print(f"  total functions:             {len(all_fns):,}")
    print(f"  reachable from roots (direct): {len(reachable):,}")
    print(f"  reachable only via indirect:   {len(indirect_reachable):,}")
    print(f"  suspected dead:                {len(dead):,}")
    print()
    if dead:
        print(f"Top {min(args.max_list, len(dead))} suspected-dead functions:")
        # Deterministic order: by name.
        for fn in sorted(dead)[:args.max_list]:
            info = cg["functions"][fn]
            callers = info.get("callers", [])
            print(f"  {fn:<48}  start={info['start']}  direct_callers={len(callers)}")
        if len(dead) > args.max_list:
            print(f"  ... +{len(dead) - args.max_list} more")
    print()

    if args.output:
        out = {
            "roots":           sorted(roots),
            "stats": {
                "total":            len(all_fns),
                "reachable_direct": len(reachable),
                "reachable_indirect": len(indirect_reachable),
                "suspected_dead":   len(dead),
            },
            "suspected_dead":   sorted(dead),
            "indirect_only":    sorted(indirect_only),
        }
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2)
        print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
