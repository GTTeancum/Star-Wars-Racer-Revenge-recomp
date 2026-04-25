#!/usr/bin/env python3
"""
Function classifier. Uses globals.json + callgraph.json + a small set of
hand-curated rules to assign a behavioural domain tag to each function.

Domains (initial set):
  - "state_machine"      touches 0x384670 or 0x384678 (game state fn / syscall table)
  - "kernel_install"     calls SetSyscall-style wrappers or touches 0x3846A0-0x3846F0 table
  - "gs_state"           touches 0x443870 or 0x442B70 (GS state pointer / init flag)
  - "gs_regs"            writes to IO 0x12000000+ range (GS privileged regs) via MMIO
  - "dma"                writes to IO 0x1000A000-0x1000F000 (DMA channel registers)
  - "thread"             calls CreateThread / StartThread / ExitThread etc. (via syscall)
  - "memcpy_overlay"     calls one of the overlay memcpy wrappers 0x2F68A0/0x2FE810/0x2FDEE0
  - "hot_state"          writes to one of the top-10 write hotspots
  - "trivial"            <= 3 instructions (wrappers/stubs)

One function can get multiple tags. Output: functions_classified.json
keyed by function name, and a summary to stdout.

Usage:
    python tools/xref/classify.py \\
        --xref      build/xref/globals.json \\
        --callgraph build/xref/callgraph.json \\
        --output    build/xref/functions_classified.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


# Address-range rules: (tag, predicate_on_addr_hex_string)
RANGE_RULES: list[tuple[str, int, int]] = [
    # (domain,        lo,              hi (exclusive))
    ("state_machine", 0x00384670,      0x00384700),
    ("kernel_install",0x003846A0,      0x003846F0),
    ("gs_state",      0x00442B70,      0x00442B74),
    ("gs_state",      0x00443870,      0x00443874),
    ("gs_regs",       0x12000000,      0x14000000),  # GS privileged regs
    ("dma",           0x1000A000,      0x1000F000),  # DMA channel regs
    ("dma",           0x10008000,      0x1000A000),  # VIF1
]


# Specific-callee rules: if a function calls any of these, tag it.
# (These are syscall wrappers + kernel entry-point functions we know.)
CALLEE_RULES: dict[str, list[str]] = {
    "memcpy_overlay": [
        # syscall 0x5A memcpy wrappers
        "entry_2f68a0_0x2f68e8",
        "entry_2fe810_0x2fe858",
        "entry_2fdee0_0x2fdf28",
    ],
    "kernel_install": [
        "entry_2fe790_0x2fe7c0",  # SetSyscall (syscall 0x74)
        "entry_2fe800_0x2fe810",
        "entry_2f5850_0x2f5860",  # syscall 0x0D (custom state-handler install)
    ],
}


def load(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"Required input not found: {path}")
    with open(path) as f:
        return json.load(f)


def classify(xref: dict, callgraph: dict) -> tuple[dict[str, list[str]], Counter[str]]:
    """Walk every function in the callgraph and assign tags based on:
      - globals it writes/reads (via xref accesses)
      - functions it calls (via callgraph callees)
      - instruction count (very small functions → 'trivial')
    Returns: {func_name: [tags...]}  plus  summary_counter.
    """
    # Build reverse index: function → set of (addr, kind)
    touched: dict[str, list[tuple[int, str]]] = {}
    for addr_s, entry in xref["addresses"].items():
        addr = int(addr_s, 16)
        for w in entry["writers"]:
            touched.setdefault(w["function"], []).append((addr, "write"))
        for r in entry["readers"]:
            touched.setdefault(r["function"], []).append((addr, "read"))

    # Top-10 write hotspot addresses (so we can tag functions that touch them)
    write_hotspots: list[int] = sorted(
        xref["addresses"].items(),
        key=lambda kv: -kv[1]["writer_count"],
    )[:10]
    hotspot_addrs = {int(k, 16) for k, _ in write_hotspots}

    assignments: dict[str, list[str]] = {}

    for func_name, info in callgraph["functions"].items():
        tags: set[str] = set()

        # Range-based tags from accesses
        for addr, _ in touched.get(func_name, []):
            for (tag, lo, hi) in RANGE_RULES:
                if lo <= addr < hi:
                    tags.add(tag)
            if addr in hotspot_addrs:
                tags.add("hot_state")

        # Callee-based tags
        callees = set(info["callees"])
        for tag, callee_list in CALLEE_RULES.items():
            if any(c in callees for c in callee_list):
                tags.add(tag)

        # Trivial (very short functions)
        start = int(info["start"], 16)
        end = int(info["end"],   16)
        inst_count = max(0, (end - start) // 4)
        if inst_count <= 3:
            tags.add("trivial")

        if tags:
            assignments[func_name] = sorted(tags)

    # Build summary
    counter = Counter()
    for tags in assignments.values():
        for t in tags:
            counter[t] += 1
    return assignments, counter


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xref",      default="build/xref/globals.json")
    ap.add_argument("--callgraph", default="build/xref/callgraph.json")
    ap.add_argument("--output",    default="build/xref/functions_classified.json")
    ap.add_argument("--top-per-domain", type=int, default=5,
                    help="When printing summary, show top-N functions per domain.")
    args = ap.parse_args()

    xref = load(Path(args.xref))
    cg   = load(Path(args.callgraph))

    assignments, summary = classify(xref, cg)

    print("Domain tag summary:")
    for tag, count in summary.most_common():
        print(f"  {count:5d}  {tag}")

    # Print a few examples per domain.
    print(f"\nExamples per domain (up to {args.top_per_domain}):")
    by_domain: dict[str, list[str]] = {}
    for fn, tags in assignments.items():
        for t in tags:
            by_domain.setdefault(t, []).append(fn)
    for tag in sorted(by_domain.keys()):
        fns = sorted(by_domain[tag])[:args.top_per_domain]
        print(f"\n  {tag}:")
        for fn in fns:
            print(f"    {fn}")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "summary": dict(summary),
            "functions": assignments,
        }, f, indent=2)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
