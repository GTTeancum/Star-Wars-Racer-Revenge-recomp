#!/usr/bin/env python3
"""
fix_chunk_recompiler_bugs.py — Post-process generated_chunks/*.cpp to fix
two pre-existing recompiler bugs that cause clang-cl compile failures.

Bug #1: ERET pattern missing braces
  The recompiler emits this for the `eret` instruction:

    if (ctx->cop0_status & 0x4) {
        ctx->pc = ctx->cop0_errorepc;
        ctx->cop0_status &= ~0x4;
        ctx->pc = ctx->cop0_epc;
        ctx->cop0_status &= ~0x2;

  But the closing `} else { ... }` is missing.  Fix: insert `} else {`
  before `ctx->pc = ctx->cop0_epc;` and `}` after the `&= ~0x2;` line.

Bug #2: Source file truncation
  The original sub_0031D200_0x31d200.cpp is truncated mid-line (ps2_recomp
  ran out of resources on the 2.2GB file).  The final chunk inherits this
  — its last line is `case 0x...u: goto lab` with no newline, followed by
  the function-closing `}` from the splitter.  Fix: drop the truncated
  line entirely and ensure the function closes cleanly.

Usage:
    python tools/fix_chunk_recompiler_bugs.py [--dir src/generated_chunks]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys


# Pattern to detect the broken eret body (one full block at a time).
# Matches:
#     if (ctx->cop0_status & 0x4) {
#         ctx->pc = ctx->cop0_errorepc;
#         ctx->cop0_status &= ~0x4;
#         ctx->pc = ctx->cop0_epc;
#         ctx->cop0_status &= ~0x2;
# Note: lines in the recompiler output start with `    ` (4-space indent)
# and may have trailing spaces; some intervening blank lines possible.
ERET_BROKEN_RE = re.compile(
    r'(\s*if\s*\(ctx->cop0_status\s*&\s*0x4\)\s*\{[^\n]*\n'
    r'\s*ctx->pc\s*=\s*ctx->cop0_errorepc;\s*\n'
    r'\s*ctx->cop0_status\s*&=\s*~0x4;\s*\n)'
    r'(\s*ctx->pc\s*=\s*ctx->cop0_epc;\s*\n'
    r'\s*ctx->cop0_status\s*&=\s*~0x2;\s*\n)',
    re.MULTILINE,
)

# Pattern to detect a truncated final line (no trailing semicolon, no newline
# before the splitter-added function-closing brace).
TRUNCATED_TAIL_RE = re.compile(
    r'(case\s+0x[0-9A-Fa-f]+u:\s*goto\s+lab)(\}\s*\n*)\Z',
    re.MULTILINE,
)


def fix_eret(content: str) -> tuple[str, int]:
    """Wrap broken eret blocks with the missing } else { ... }."""
    def repl(m: re.Match[str]) -> str:
        first = m.group(1)
        second = m.group(2)
        # Insert close-of-true-branch + else + open
        return f"{first}    }} else {{\n{second}    }}\n"
    new, count = ERET_BROKEN_RE.subn(repl, content)
    return new, count


def fix_truncated_tail(content: str) -> tuple[str, int]:
    """Remove the truncated final line and ensure the function closes."""
    m = TRUNCATED_TAIL_RE.search(content)
    if not m:
        return content, 0
    # Replace truncated tail with proper closes.  Count the brace depth at
    # the truncation point and add enough `}` to return to depth 0.
    truncated = content[:m.start()]
    depth = 0
    for line in truncated.splitlines():
        # Strip C++ // comments
        code = re.sub(r'//.*', '', line)
        depth += code.count('{') - code.count('}')
    # Need `depth` more closes plus a trailing newline
    closes = "}\n" * depth
    new = truncated + closes
    return new, 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="src/generated_chunks",
                   help="Directory containing chunk .cpp files")
    p.add_argument("--dry-run", action="store_true",
                   help="Report fixes without writing")
    args = p.parse_args()

    pattern = os.path.join(args.dir, "sub_0031D200_chunk_*.cpp")
    chunks = sorted(glob.glob(pattern))
    if not chunks:
        print(f"No chunks found at {pattern}", file=sys.stderr)
        return 1

    total_eret_fixes = 0
    total_trunc_fixes = 0
    files_changed = 0
    for chunk_path in chunks:
        with open(chunk_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        new, eret_n = fix_eret(content)
        new, trunc_n = fix_truncated_tail(new)
        if eret_n > 0 or trunc_n > 0:
            files_changed += 1
            total_eret_fixes += eret_n
            total_trunc_fixes += trunc_n
            name = os.path.basename(chunk_path)
            print(f"  {name}: eret={eret_n} trunc={trunc_n}")
            if not args.dry_run:
                with open(chunk_path, "w", encoding="utf-8") as f:
                    f.write(new)

    print()
    print(f"Files changed:        {files_changed}")
    print(f"Total eret fixes:     {total_eret_fixes}")
    print(f"Total trunc fixes:    {total_trunc_fixes}")
    if args.dry_run:
        print("(dry-run, no files written)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
