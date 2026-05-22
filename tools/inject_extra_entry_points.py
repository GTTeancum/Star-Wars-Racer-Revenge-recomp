#!/usr/bin/env python3
"""inject_extra_entry_points.py

Patches the recompiler's generated .cpp files to honour `[[extra_entry_points]]`
entries in the TOML config.

PS2Recomp v0.4 does not parse `[[extra_entry_points]]` from the TOML — the
key is silently ignored. The game (and CLAUDE.md, smoke_test_golden, etc.)
assume those addresses produce both:
  1. a `case 0xAAAAAAu: goto label_AAAAAA;` entry in the containing
     function's `switch (ctx->pc)` dispatch prologue, and
  2. a `label_AAAAAA:` marker immediately before the instruction at that
     address, and
  3. a `runtime.registerFunction(0xAAAAAA, sub_...)` alias so the runtime
     dispatcher resolves the interior address to the containing function.

This script reads `config/racer_revenge.toml`, locates each extra entry
point's containing generated .cpp file by parsing the `// Address: 0xstart
- 0xend` header, and patches the file in-place. It also emits a new file
`src/generated/extra_entry_points_register.cpp` exposing
`registerExtraEntryPoints(PS2Runtime&)`, which main.cpp calls right after
`registerAllFunctions`.

Idempotent. Safe to run multiple times. Run after `ps2_recomp`.

Usage:
    python tools/inject_extra_entry_points.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TOML_PATH = REPO_ROOT / "config" / "racer_revenge.toml"
GENERATED_DIR = REPO_ROOT / "src" / "generated"
REGISTER_OUTPUT = GENERATED_DIR / "extra_entry_points_register.cpp"


ADDRESS_LINE_RE = re.compile(r"^//\s*Address:\s*0x([0-9a-fA-F]+)\s*-\s*0x([0-9a-fA-F]+)\s*$", re.M)
TOML_ENTRY_RE = re.compile(
    r"\[\[extra_entry_points\]\]\s*\n\s*address\s*=\s*\"0x([0-9a-fA-F]+)\"",
    re.M,
)


def parse_extra_entry_points(toml_path: Path) -> list[int]:
    """Return sorted list of unique extra entry-point addresses."""
    text = toml_path.read_text(encoding="utf-8")
    addrs = {int(m.group(1), 16) for m in TOML_ENTRY_RE.finditer(text)}
    return sorted(addrs)


def index_generated_functions(generated_dir: Path) -> list[tuple[int, int, Path]]:
    """Scan src/generated/*.cpp for `// Address: 0xs - 0xe` headers.

    Returns list of (start, end, path) sorted by start. `end` is the
    function's last instruction address + 4 (i.e. exclusive upper bound)
    as encoded in the recompiler's header line.
    """
    entries: list[tuple[int, int, Path]] = []
    for cpp in sorted(generated_dir.glob("sub_*.cpp")):
        text = cpp.read_text(encoding="utf-8")
        m = ADDRESS_LINE_RE.search(text)
        if not m:
            continue
        start = int(m.group(1), 16)
        end = int(m.group(2), 16)
        entries.append((start, end, cpp))
    entries.sort(key=lambda t: t[0])
    return entries


def find_containing(entries: list[tuple[int, int, Path]], addr: int) -> tuple[int, Path] | None:
    """Linear scan — N ≈ 3400, K ≈ 12, so this is fine. Returns
    (function_start, file_path) for the function whose [start, end] range
    contains `addr`. The end value from the recompiler's header is the
    last instruction's address (inclusive), so we test `addr <= end`.
    """
    for start, end, path in entries:
        if start <= addr <= end:
            return (start, path)
    return None


def patch_file(path: Path, fn_start: int, extra_addrs: list[int]) -> bool:
    """Insert case + label entries for each address in `extra_addrs`.

    Returns True if the file was modified.
    """
    original = path.read_text(encoding="utf-8")
    text = original

    # --- 1. Switch case insertion ---
    # Existing form:
    #     switch (ctx->pc) {
    #         case 0xXXXu: goto label_XXX;
    #         default: break;
    #     }
    #
    # If the switch doesn't exist (no internal targets discovered), insert
    # one right before `ctx->pc = 0xNNNu;` (the start-of-function PC reset).
    needs_new_switch = "switch (ctx->pc)" not in text

    for addr in extra_addrs:
        case_line = f"        case 0x{addr:x}u: goto label_{addr:x};"
        if case_line in text:
            continue  # already present (idempotent)

        if needs_new_switch:
            # Build the entire switch block once, with all extra addrs.
            block = ["    switch (ctx->pc) {"]
            for a in extra_addrs:
                block.append(f"        case 0x{a:x}u: goto label_{a:x};")
            block.append("        default: break;")
            block.append("    }")
            block.append("")
            switch_block = "\n".join(block) + "\n"

            # Insert just before `    ctx->pc = 0xNNNu;` (the function's
            # start-PC reset, which the recompiler always emits).
            anchor = f"    ctx->pc = 0x{fn_start:x}u;\n"
            if anchor not in text:
                print(f"  WARN: anchor '{anchor.strip()}' not found in {path.name}; skipping switch insert",
                      file=sys.stderr)
                continue
            text = text.replace(anchor, switch_block + anchor, 1)
            needs_new_switch = False  # we just made it
            continue

        # Switch exists — insert before the `default: break;` line.
        default_line = "        default: break;"
        if default_line not in text:
            print(f"  WARN: 'default: break;' not found in {path.name}; cannot insert case",
                  file=sys.stderr)
            continue
        text = text.replace(default_line, case_line + "\n" + default_line, 1)

    # --- 2. Label insertion ---
    # Each instruction has a comment like `    // 0xAAAA: 0xRAW  disasm`.
    # We want `label_AAAA:` on its own line immediately before it. If a
    # `label_AAAA:` already exists in the file, skip (idempotent).
    for addr in extra_addrs:
        label_line = f"label_{addr:x}:"
        if label_line in text:
            continue

        # The recompiler emits the instruction comment with at least one
        # space after `0x` and the address has no leading zeros.
        # Form: `    // 0xAAAA: 0xRRRR  disasm`
        # Match the WHOLE line so we can prepend the label on its own line.
        instr_re = re.compile(rf"^(    // 0x{addr:x}: )", re.M)
        m = instr_re.search(text)
        if not m:
            # Fallback: maybe the address is the function start itself.
            # The recompiler doesn't emit label_<fn_start>: in that case,
            # but the function entry IS reachable via fn_start, so the
            # trampoline can call ctx->pc = fn_start which the
            # registerFunction lookup resolves directly. Skip the label.
            if addr == fn_start:
                continue
            print(f"  WARN: no instruction line for 0x{addr:x} in {path.name}; "
                  f"label not inserted (may be alignment/data, not code)",
                  file=sys.stderr)
            continue
        text = instr_re.sub(f"{label_line}\n" + r"\1", text, count=1)

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def emit_register_file(addr_to_owner: dict[int, int], output: Path) -> None:
    """Generate src/generated/extra_entry_points_register.cpp.

    Calls `runtime.registerFunction(0xAAAA, sub_NNNN_0xNNNN)` for each
    extra entry point, aliasing it to the containing function. The
    containing function's switch (patched above) routes to the right
    label based on ctx->pc.
    """
    lines = [
        "// AUTO-GENERATED by tools/inject_extra_entry_points.py — do not edit.",
        "// Source: config/racer_revenge.toml `[[extra_entry_points]]`.",
        "// PS2Recomp v0.4 ignores that TOML key; this file plugs the gap.",
        "",
        "#include \"ps2_runtime.h\"",
        "#include \"ps2_runtime_macros.h\"",
        "#include \"ps2_recompiled_functions.h\"",
        "",
        "void registerExtraEntryPoints(PS2Runtime& runtime)",
        "{",
    ]
    for addr in sorted(addr_to_owner):
        owner = addr_to_owner[addr]
        owner_name = f"sub_{owner:08X}_0x{owner:x}"
        if addr == owner:
            # Function start — already registered by registerAllFunctions.
            continue
        # Trampoline: set ctx->pc to the interior address, call the
        # owner function. Its switch (patched by this script) jumps to
        # the right label.
        lines.append(f"    runtime.registerFunction(0x{addr:x},")
        lines.append(f"        +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {{")
        lines.append(f"            ctx->pc = 0x{addr:x}u;")
        lines.append(f"            {owner_name}(rdram, ctx, runtime);")
        lines.append(f"        }});")
    lines.append("}")
    lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    if not TOML_PATH.exists():
        print(f"missing TOML: {TOML_PATH}", file=sys.stderr)
        return 1
    if not GENERATED_DIR.exists():
        print(f"missing generated dir: {GENERATED_DIR}", file=sys.stderr)
        return 1

    addrs = parse_extra_entry_points(TOML_PATH)
    if not addrs:
        print("no [[extra_entry_points]] in TOML; nothing to do")
        REGISTER_OUTPUT.unlink(missing_ok=True)
        return 0
    print(f"found {len(addrs)} [[extra_entry_points]] entries")

    entries = index_generated_functions(GENERATED_DIR)
    print(f"indexed {len(entries)} generated function files")

    # Group by containing function, then patch each containing file once.
    by_owner: dict[int, list[int]] = {}
    addr_to_owner: dict[int, int] = {}
    unmatched: list[int] = []
    for addr in addrs:
        match = find_containing(entries, addr)
        if match is None:
            unmatched.append(addr)
            continue
        owner_start, _ = match
        addr_to_owner[addr] = owner_start
        by_owner.setdefault(owner_start, []).append(addr)

    if unmatched:
        # Addresses that don't map to any function — e.g. kseg0 overlay
        # internal labels (0x80075038 etc.) where the "owner" is the
        # overlay function generated under a different path. Those are
        # already separately handled by the overlay registration logic
        # in main.cpp / register_functions.cpp, so we just log and skip.
        print(f"unmatched addresses (skipped — likely overlays or BSS):")
        for a in unmatched:
            print(f"  0x{a:x}")

    patched = 0
    for owner_start, addr_list in by_owner.items():
        # Look up the path again.
        path = next(p for s, _, p in entries if s == owner_start)
        if patch_file(path, owner_start, sorted(addr_list)):
            patched += 1
            print(f"  patched {path.name}: {[hex(a) for a in addr_list]}")

    emit_register_file(addr_to_owner, REGISTER_OUTPUT)
    print(f"wrote {REGISTER_OUTPUT.relative_to(REPO_ROOT)} "
          f"({len(addr_to_owner)} trampoline(s))")
    print(f"patched {patched} file(s); {len(by_owner) - patched} already up-to-date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
