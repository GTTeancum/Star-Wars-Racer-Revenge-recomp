#!/usr/bin/env python3
"""
strip_inline_dispatch.py — Remove redundant inline JR/JALR dispatch tables
from sub_0031D200_chunk_*.cpp files.

The recompiler emits two dispatchers for every indirect jump:

  switch (jumpTarget) {                          // <-- the bloat
      case 0xX1: { ctx->pc=0xX1; sub_chunk_N(...); return; }
      ... 13,882 cases per mega-label ...
      default: break;
  }
  {
      auto targetFn = runtime->lookupFunction(jumpTarget);    // <-- the truth
      const uint32_t __entryPc = ctx->pc;
      targetFn(rdram, ctx, runtime);
      if (ctx->pc == __entryPc) { ctx->pc = NEXT; }
      if (ctx->pc != NEXT) { return; }
  }

The switch is a fast-path shortcut: it skips the hash lookup for known
targets.  But:
  1) sub_31D200 is rarely called (cycle 27 trace), so the perf win is moot
  2) Each switch adds ~1.2MB of source per mega-label (240 mega-labels)
  3) Total impact: ~1.4GB of chunked source, ~900MB of binary, mostly
     from these tables

Removing the switch is semantically equivalent — the lookupFunction
handles every target the switch would have handled, plus targets the
switch didn't enumerate.  Indirect jumps slow by one hash lookup
(negligible for cold code).

Strategy: scan each chunk line-by-line, find `switch (jumpTarget`, count
braces to find the matching `}`, drop those lines.

Usage:
    python tools/strip_inline_dispatch.py [--dir src/generated_chunks] [--dry-run]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys


SWITCH_START_RE = re.compile(r'\bswitch\s*\(\s*jumpTarget\b')


def strip_chunk(content: str) -> tuple[str, int, int]:
    """Strip all `switch (jumpTarget)` blocks.

    Returns (new_content, n_switches_stripped, bytes_removed).
    """
    lines = content.splitlines(keepends=True)
    output: list[str] = []
    n_stripped = 0
    bytes_before = sum(len(line) for line in lines)

    i = 0
    while i < len(lines):
        line = lines[i]
        if SWITCH_START_RE.search(line):
            # Found a switch.  Find matching close-brace by counting depth.
            # The switch's own `{` is on this line (or sometimes the next).
            # Standard recompiler output: `switch (jumpTarget) {` is on one line.
            start_i = i
            # Count braces ONLY in code (strip C++ // comments).
            def brace_delta(s: str) -> int:
                code = re.sub(r'//.*', '', s)
                return code.count('{') - code.count('}')

            depth = brace_delta(line)
            if depth <= 0:
                # Malformed — bail and keep the line as-is
                output.append(line)
                i += 1
                continue
            j = i
            while depth > 0 and j + 1 < len(lines):
                j += 1
                depth += brace_delta(lines[j])
            # Lines i..j (inclusive) are the switch.  Skip them.
            n_stripped += 1
            i = j + 1
        else:
            output.append(line)
            i += 1

    new = "".join(output)
    bytes_after = len(new)
    return new, n_stripped, bytes_before - bytes_after


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="src/generated_chunks")
    p.add_argument("--dry-run", action="store_true",
                   help="Report stats without writing")
    args = p.parse_args()

    pattern = os.path.join(args.dir, "sub_0031D200_chunk_*.cpp")
    chunks = sorted(glob.glob(pattern))
    if not chunks:
        print(f"No chunks found at {pattern}", file=sys.stderr)
        return 1

    total_switches = 0
    total_bytes_saved = 0
    files_changed = 0
    for chunk_path in chunks:
        with open(chunk_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        new, n_switches, bytes_saved = strip_chunk(content)
        if n_switches > 0:
            files_changed += 1
            total_switches += n_switches
            total_bytes_saved += bytes_saved
            if not args.dry_run:
                with open(chunk_path, "w", encoding="utf-8") as f:
                    f.write(new)
            if n_switches > 1 or bytes_saved > 1024 * 1024:
                name = os.path.basename(chunk_path)
                print(f"  {name}: stripped {n_switches} switch(es), "
                      f"-{bytes_saved/1024/1024:.1f}MB")

    print()
    print(f"Files changed:        {files_changed}")
    print(f"Switches stripped:    {total_switches}")
    print(f"Total bytes removed:  {total_bytes_saved/1024/1024:.1f} MB")
    if args.dry_run:
        print("(dry-run, no files written)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
