#!/usr/bin/env python3
"""
Prune stale .cpp files in src/generated/ that aren't referenced from the
latest ps2_recompiled_functions.h.

Background: when ps2_recomp regenerates with different function boundaries
(e.g. after editing extra_entry_points or Ghidra CSV input), old .cpp files
from the previous run stay on disk. If some of them are still referenced by
NEW files whose boundaries happened to change, link-time errors fire with
"identifier not found" for symbols the new header doesn't declare.

This script walks src/generated/*.cpp, cross-references each file's
void-declared entry against the forward declarations in
ps2_recompiled_functions.h, and deletes any .cpp whose symbol isn't in the
header.

Intended to run as a regen.sh post-step.
"""

from __future__ import annotations
import re
import sys
from pathlib import Path


DECL_RE = re.compile(r"^void\s+([A-Za-z_0-9]+)\s*\(", re.MULTILINE)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    gen = root / "src" / "generated"
    header = gen / "ps2_recompiled_functions.h"
    if not header.exists():
        print(f"[prune] {header} not found — skipping (regen never ran?)")
        return 0

    declared: set[str] = set(DECL_RE.findall(header.read_text(encoding="utf-8")))
    print(f"[prune] {len(declared):,} functions declared in ps2_recompiled_functions.h")

    kept = 0
    dropped = 0
    for cpp in sorted(gen.glob("*.cpp")):
        # register_functions.cpp etc. don't export one symbol — always keep.
        if cpp.name in ("register_functions.cpp",):
            kept += 1
            continue
        # Find any void foo(uint8_t* rdram, ...) definition in the file.
        # First few KB are enough (function bodies come later anyway).
        head = cpp.read_text(encoding="utf-8", errors="replace")[:4096]
        m = re.search(r"^void\s+([A-Za-z_0-9]+)\s*\(uint8_t\*", head, re.MULTILINE)
        if not m:
            kept += 1
            continue
        sym = m.group(1)
        if sym in declared:
            kept += 1
        else:
            print(f"[prune] drop {cpp.name} (symbol {sym!r} not in header)")
            cpp.unlink()
            dropped += 1

    print(f"[prune] kept={kept:,}  dropped={dropped:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
