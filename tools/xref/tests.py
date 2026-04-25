#!/usr/bin/env python3
"""
Regression tests for the xref database. Run after every recompile or any
change to `build_xref.py` / `build_callgraph.py`.

These are SPECIFIC, MANUALLY VERIFIED facts — if they regress, the
constant-folder in build_xref.py has a bug or the ELF changed
meaningfully.

Usage:
    python tools/xref/tests.py
    python tools/xref/tests.py --json build/xref/globals.json

Exits 0 on all-pass, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Each assertion is a lambda over the address entry. Name is human-readable
# for the failure message.
ASSERTIONS: list[tuple[str, str, str, callable]] = [
    # (address, test_name, description, check_fn(entry) -> (ok, actual_str))

    # 0x00384670 (game state function pointer): written by setGameState
    ("0x00384670", "gsf.writer_count_exact_2",
        "writer_count should be 2 (FUN_002fddf8 + its fused parent sub_002FDCE8)",
        lambda e: (e["writer_count"] == 2, f"actual={e['writer_count']}")),

    ("0x00384670", "gsf.reader_count_exact_1",
        "reader_count should be exactly 1 (the frame dispatch at 0x2FE404)",
        lambda e: (e["reader_count"] == 1, f"actual={e['reader_count']}")),

    ("0x00384670", "gsf.first_writer_is_setGameState",
        "first writer should be sub_002FDCE8 (which contains 0x2FDDF8)",
        lambda e: (
            e["first_writer_function"] in ("sub_002FDCE8_0x2fdce8", "FUN_002fddf8_0x2fddf8"),
            f"actual={e['first_writer_function']}"
        )),

    # 0x00443870 (GS state pointer): single writer = GS allocator
    ("0x00443870", "gsp.writer_count_exact_1",
        "writer_count should be 1 (the GS allocator)",
        lambda e: (e["writer_count"] == 1, f"actual={e['writer_count']}")),

    ("0x00443870", "gsp.writer_is_13fda0",
        "writer must be entry_13fda0_0x140230 (the GS allocator)",
        lambda e: (
            e["first_writer_function"] == "entry_13fda0_0x140230",
            f"actual={e['first_writer_function']}"
        )),

    ("0x00443870", "gsp.readers_exist",
        "reader_count should be substantial (>50 expected based on GS code usage)",
        lambda e: (e["reader_count"] >= 50, f"actual={e['reader_count']}")),

    # 0x00442B70 (GS init flag): set by the GS init function
    ("0x00442b70", "gif.writer_starts_with_251a20",
        "first writer should start with entry_251a20 (containing the GS init)",
        lambda e: (
            e["first_writer_function"].startswith("entry_251a20"),
            f"actual={e['first_writer_function']}"
        )),

    # 0x00384678 (syscall dispatch table): table-indexed access picked up by #2a
    ("0x00384678", "sdt.has_writer",
        "syscall dispatch table should have at least 1 writer (index-add pattern resolved)",
        lambda e: (e["writer_count"] >= 1, f"actual={e['writer_count']}")),

    ("0x00384678", "sdt.reader_is_ee_dispatcher",
        "reader should include entry_2fe100_0x2fe660 (the EE exception dispatcher)",
        lambda e: (
            any(r["function"] == "entry_2fe100_0x2fe660" for r in e["readers"]),
            f"readers={[r['function'] for r in e['readers']]}"
        )),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", default="build/xref/globals.json")
    args = ap.parse_args()

    path = Path(args.json)
    if not path.exists():
        sys.exit(f"xref not found: {path}")

    with open(path) as f:
        doc = json.load(f)
    addresses = doc["addresses"]

    passed = 0
    failed = 0
    failures: list[str] = []
    for addr_s, name, desc, check in ASSERTIONS:
        entry = addresses.get(addr_s)
        if entry is None:
            failed += 1
            failures.append(f"FAIL {name}: address {addr_s} not in xref at all")
            continue
        try:
            ok, detail = check(entry)
        except Exception as e:  # pragma: no cover
            failed += 1
            failures.append(f"FAIL {name}: check raised: {e}")
            continue
        if ok:
            passed += 1
            print(f"  ok   {name}  ({addr_s})")
        else:
            failed += 1
            failures.append(f"FAIL {name}: {desc}  [{detail}]")
            print(f"  FAIL {name}  ({addr_s})  {detail}")

    print()
    print(f"{passed} passed, {failed} failed")
    if failures:
        print()
        print("Failures:")
        for line in failures:
            print(f"  {line}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
