#!/usr/bin/env python3
"""
Diff two globals.json snapshots — T10.

Catches silent regressions when the xref schema, constant-folder, or ELF
changes. Useful patterns:

  # Before making a xref-related change, snapshot the current state:
  python tools/xref/build_xref.py --elf SLUS_202.68 --functions src/generated \\
      --output /tmp/xref_before.json

  # After the change, re-run:
  python tools/xref/build_xref.py --elf SLUS_202.68 --functions src/generated \\
      --output /tmp/xref_after.json

  # See what moved:
  python tools/xref/diff.py /tmp/xref_before.json /tmp/xref_after.json

Output:
  - Totals delta (addresses, writers, readers).
  - Addresses that gained or lost writers/readers.
  - Writes whose resolved value changed (useful when constant folding
    regresses or improves).

Ignores cosmetic re-orderings (writers/readers are treated as sets).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def write_key(w: dict) -> tuple:
    """Canonical identity for a writer record (ignores op name casing)."""
    return (w["function"], w["pc"], w.get("op", ""), w.get("width", 0))


def read_key(r: dict) -> tuple:
    return (r["function"], r["pc"], r.get("op", ""), r.get("width", 0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("before", help="older globals.json")
    ap.add_argument("after",  help="newer globals.json")
    ap.add_argument("--max-changes", type=int, default=50,
                    help="Cap per category (default 50)")
    ap.add_argument("--address-filter",
                    help="Only report addresses whose hex key matches this prefix "
                         "(e.g. 0x00384 for state-machine region)")
    args = ap.parse_args()

    a = load(args.before)
    b = load(args.after)

    print(f"Before: {args.before}")
    print(f"  addresses={a['stats']['unique_addresses']:,}  "
          f"writes={a['stats']['total_writes']:,}  "
          f"reads={a['stats']['total_reads']:,}")
    print(f"After:  {args.after}")
    print(f"  addresses={b['stats']['unique_addresses']:,}  "
          f"writes={b['stats']['total_writes']:,}  "
          f"reads={b['stats']['total_reads']:,}")
    print()

    a_addrs = a["addresses"]
    b_addrs = b["addresses"]
    a_keys = set(a_addrs.keys())
    b_keys = set(b_addrs.keys())

    def passes_filter(k: str) -> bool:
        return args.address_filter is None or k.startswith(args.address_filter)

    only_after  = sorted(k for k in b_keys - a_keys if passes_filter(k))
    only_before = sorted(k for k in a_keys - b_keys if passes_filter(k))
    common      = sorted(k for k in a_keys & b_keys if passes_filter(k))

    print(f"Addresses added (only in AFTER):   {len(only_after):,}")
    for k in only_after[:args.max_changes]:
        e = b_addrs[k]
        print(f"  + {k}  W={e['writer_count']} R={e['reader_count']}")
    if len(only_after) > args.max_changes:
        print(f"  ... +{len(only_after) - args.max_changes} more")
    print()

    print(f"Addresses removed (only in BEFORE): {len(only_before):,}")
    for k in only_before[:args.max_changes]:
        e = a_addrs[k]
        print(f"  - {k}  W={e['writer_count']} R={e['reader_count']}")
    if len(only_before) > args.max_changes:
        print(f"  ... +{len(only_before) - args.max_changes} more")
    print()

    # Deep diff for common addresses.
    writer_changes: list[str] = []
    reader_changes: list[str] = []
    value_changes: list[str] = []
    for k in common:
        ea, eb = a_addrs[k], b_addrs[k]
        wa = {write_key(w) for w in ea["writers"]}
        wb = {write_key(w) for w in eb["writers"]}
        ra = {read_key(r) for r in ea["readers"]}
        rb = {read_key(r) for r in eb["readers"]}

        if wa != wb:
            added   = wb - wa
            removed = wa - wb
            writer_changes.append(
                f"  {k}  writers: "
                f"{len(ea['writers'])}→{len(eb['writers'])}  "
                f"+{len(added)}, -{len(removed)}"
            )

        if ra != rb:
            added   = rb - ra
            removed = ra - rb
            reader_changes.append(
                f"  {k}  readers: "
                f"{len(ea['readers'])}→{len(eb['readers'])}  "
                f"+{len(added)}, -{len(removed)}"
            )

        # Compare the VALUE field on writes that are the same (function, pc).
        by_key_a = {write_key(w): w for w in ea["writers"]}
        by_key_b = {write_key(w): w for w in eb["writers"]}
        for key in by_key_a.keys() & by_key_b.keys():
            va = by_key_a[key].get("value")
            vb = by_key_b[key].get("value")
            if va != vb:
                value_changes.append(
                    f"  {k}  writer {key[0]} @ {key[1]}: value {va} → {vb}"
                )

    print(f"Writer-set changes (common addrs): {len(writer_changes):,}")
    for line in writer_changes[:args.max_changes]:
        print(line)
    if len(writer_changes) > args.max_changes:
        print(f"  ... +{len(writer_changes) - args.max_changes} more")
    print()

    print(f"Reader-set changes (common addrs): {len(reader_changes):,}")
    for line in reader_changes[:args.max_changes]:
        print(line)
    if len(reader_changes) > args.max_changes:
        print(f"  ... +{len(reader_changes) - args.max_changes} more")
    print()

    print(f"Resolved-value changes (common writers): {len(value_changes):,}")
    for line in value_changes[:args.max_changes]:
        print(line)
    if len(value_changes) > args.max_changes:
        print(f"  ... +{len(value_changes) - args.max_changes} more")
    print()

    total_changes = (len(only_after) + len(only_before) +
                     len(writer_changes) + len(reader_changes) +
                     len(value_changes))
    if total_changes == 0:
        print("OK: no changes detected.")
        return 0
    print(f"Total changes: {total_changes:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
