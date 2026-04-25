#!/usr/bin/env python3
"""
Call-graph builder. Walks every function's MIPS body, extracts `jal <imm>`
targets, and emits callgraph.json:

    {
      "functions": {
        "entry_xxx_0xADDR": {
          "start":     "0xADDR",
          "end":       "0xEND",
          "callers":   ["entry_yyy_0xYYY", ...],
          "callees":   ["entry_zzz_0xZZZ", ...],
          "call_sites": [
            {
              "pc":"0xPC", "target":"0xTGT", "target_name":"...",
              "args": {"a0":"0xVAL", "a1":"0xVAL"}   # T15: when resolvable
            },
            ...
          ]
        }
      }
    }

T15: Each `jal`/`jalr` call site records the KNOWN values of $a0..$a3 (and
$t0 for 5th arg) at the moment of the call, using the same lui+addi/ori/
sll+addu constant folding as build_xref. This surfaces things like the
different module IDs passed to the module dispatcher, or the specific state
function addresses passed to setGameState. The delay-slot instruction IS
folded in before the call values are captured, which is critical on MIPS
where the delay slot often sets $a0.

Usage:
    python tools/xref/build_callgraph.py \\
        --elf SLUS_202.68 \\
        --functions src/generated \\
        --output build/xref/callgraph.json

Then:
    python tools/xref/query.py --callers FUN_002fddf8_0x2fddf8
    python tools/xref/query.py --args-to FUN_002fddf8_0x2fddf8   # T15
    python tools/xref/query.py --callees entry_2fe7c0_0x2fe800
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

# Reuse function discovery + ELF loader from build_xref.
sys.path.insert(0, str(Path(__file__).parent))
from build_xref import (  # type: ignore
    Function, discover_functions, load_elf_segments, bytes_at_va,
    OP_J, OP_JAL, OP_SPECIAL, OP_LUI, OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI,
    OP_ORI, OP_ANDI, OP_XORI, SPECIAL_JR, SPECIAL_JALR,
    LOAD_OPS,
    sign_ext16,
)


# Argument registers we care about in call sites. MIPS convention:
# $a0..$a3 (GPRs 4..7) carry the first four integer args, $t0 (GPR 8) the fifth
# (some toolchains). We log all five when known.
ARG_REGS = [4, 5, 6, 7, 8]
ARG_NAMES = {4: "a0", 5: "a1", 6: "a2", 7: "a3", 8: "t0"}


def build_address_map(functions: list[Function]) -> dict[int, Function]:
    return {f.start: f for f in functions}


def _apply_instruction(reg: list, word: int, pc: int) -> None:
    """Apply one MIPS instruction's effect to the register-state list.
    Register state is: int | ("indexed", base, shift) | None.

    Mirrors the folding logic in build_xref.analyze_function. Kept local
    so build_callgraph can be run independently."""
    op = (word >> 26) & 0x3F
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    sa = (word >> 6)  & 0x1F
    fn = word & 0x3F
    imm = word & 0xFFFF

    def inval(r: int) -> None:
        if r != 0:
            reg[r] = None

    if op == OP_LUI:
        if rt != 0: reg[rt] = (imm & 0xFFFF) << 16
    elif op in (OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI):
        base = reg[rs]
        if isinstance(base, int):
            reg[rt] = (base + sign_ext16(imm)) & 0xFFFFFFFF
        elif isinstance(base, tuple) and base and base[0] == "indexed":
            reg[rt] = ("indexed", (base[1] + sign_ext16(imm)) & 0xFFFFFFFF, base[2])
        else:
            inval(rt)
    elif op == OP_ORI:
        base = reg[rs]
        reg[rt] = ((base | imm) & 0xFFFFFFFF) if isinstance(base, int) else None
    elif op == OP_ANDI:
        base = reg[rs]
        reg[rt] = ((base & imm) & 0xFFFFFFFF) if isinstance(base, int) else None
    elif op == OP_XORI:
        base = reg[rs]
        reg[rt] = ((base ^ imm) & 0xFFFFFFFF) if isinstance(base, int) else None
    elif op == OP_SPECIAL:
        if fn in (0x21, 0x2D):   # addu / daddu
            a, b = reg[rs], reg[rt]
            if isinstance(a, int) and isinstance(b, int):
                reg[rd] = (a + b) & 0xFFFFFFFF
            elif isinstance(a, int) and not isinstance(b, int):
                shift = b[2] if isinstance(b, tuple) and b[0] == "indexed" else 0
                reg[rd] = ("indexed", a, shift)
            elif isinstance(b, int) and not isinstance(a, int):
                shift = a[2] if isinstance(a, tuple) and a[0] == "indexed" else 0
                reg[rd] = ("indexed", b, shift)
            else:
                inval(rd)
        elif fn == 0x25:  # or
            a, b = reg[rs], reg[rt]
            if isinstance(a, int) and isinstance(b, int):
                reg[rd] = (a | b) & 0xFFFFFFFF
            else:
                inval(rd)
        elif fn == 0x00 and rd != 0:  # sll $rd, $rt, sa
            src = reg[rt]
            if isinstance(src, int):
                reg[rd] = (src << sa) & 0xFFFFFFFF
            else:
                reg[rd] = ("indexed", 0, sa)
        elif fn in (SPECIAL_JR, SPECIAL_JALR):
            # Indirect jump / return. Conservatively invalidate most state.
            for r in range(1, 32):
                reg[r] = None
        else:
            inval(rd)
    elif op == 3:  # jal: clobber caller-saved registers
        for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
            inval(r)
    elif op in LOAD_OPS:
        inval(rt)
    else:
        # Unknown: invalidate rt and rd conservatively.
        inval(rt)
        inval(rd)


def _capture_args(reg: list) -> dict:
    """Capture currently-known values of the arg registers ($a0..$a3, $t0).
    Returns a {"a0": "0xVAL", ...} dict with only the resolvable entries."""
    out: dict[str, str] = {}
    for r in ARG_REGS:
        v = reg[r]
        if isinstance(v, int):
            out[ARG_NAMES[r]] = f"0x{v:x}"
        elif isinstance(v, tuple) and v and v[0] == "indexed":
            # Indexed base: show as "<base>+<<shift>>" so the caller can see
            # it was an index-scaled register.
            out[ARG_NAMES[r]] = f"0x{v[1]:x}+<<{v[2]}>>"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--functions", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    elf_path = Path(args.elf)
    gen_dir  = Path(args.functions)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[callgraph] Loading ELF: {elf_path}")
    overlays_toml = Path("config") / "racer_revenge.toml"
    segments = load_elf_segments(elf_path, overlays_toml)
    for base, data in segments:
        print(f"[callgraph] Segment: 0x{base:08x} - 0x{base + len(data):08x} ({len(data):,} bytes)")

    print(f"[callgraph] Discovering functions in {gen_dir}")
    functions = discover_functions(gen_dir)
    print(f"[callgraph] {len(functions):,} functions")

    by_start = build_address_map(functions)

    results: dict[str, dict] = {
        f.name: {
            "start":      f"0x{f.start:08x}",
            "end":        f"0x{f.end:08x}",
            "callers":    set(),
            "callees":    set(),
            "call_sites": [],
        } for f in functions
    }

    unresolved_jalr = 0
    unresolved_jal_to_noncode = 0
    args_recorded = 0

    for i, fn in enumerate(functions):
        if i and i % 500 == 0:
            print(f"[callgraph]   progress: {i:,}/{len(functions):,}")

        # Register-state for constant folding, reset per function.
        reg: list = [0] + [None] * 31

        pc = fn.start
        insts: list[tuple[int, int]] = []
        while pc < fn.end:
            buf = bytes_at_va(segments, pc, 4)
            if buf is None:
                break
            word = struct.unpack_from("<I", buf, 0)[0]
            insts.append((pc, word))
            pc += 4

        idx = 0
        while idx < len(insts):
            pc_i, word = insts[idx]
            op = (word >> 26) & 0x3F

            if op == OP_JAL:
                target26 = word & 0x03FFFFFF
                target = ((pc_i + 4) & 0xF0000000) | (target26 << 2)

                # T15: fold delay-slot instruction into state BEFORE
                # capturing args, since the delay slot often sets $a0/etc.
                if idx + 1 < len(insts):
                    _apply_instruction(reg, insts[idx + 1][1], insts[idx + 1][0])

                call_args = _capture_args(reg)
                if call_args:
                    args_recorded += 1

                callee = by_start.get(target)
                site = {
                    "pc":          f"0x{pc_i:08x}",
                    "target":      f"0x{target:08x}",
                    "target_name": callee.name if callee else None,
                    "kind":        "jal" if callee else "jal_unresolved",
                }
                if call_args:
                    site["args"] = call_args
                results[fn.name]["call_sites"].append(site)
                if callee is None:
                    unresolved_jal_to_noncode += 1
                else:
                    results[fn.name]["callees"].add(callee.name)
                    results[callee.name]["callers"].add(fn.name)

                # JAL clobbers caller-saved (including $a0-$a3, $t0-$t9, $v0-$v1)
                # because the callee may overwrite them. Invalidate.
                for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                    if r != 0: reg[r] = None

                # Skip delay slot — we already applied it.
                idx += 2
                continue

            if op == OP_SPECIAL:
                fn_f = word & 0x3F
                if fn_f == SPECIAL_JALR:
                    unresolved_jalr += 1
                    # Fold delay slot in, capture args, then invalidate.
                    if idx + 1 < len(insts):
                        _apply_instruction(reg, insts[idx + 1][1], insts[idx + 1][0])
                    call_args = _capture_args(reg)
                    if call_args:
                        args_recorded += 1
                    site = {
                        "pc":          f"0x{pc_i:08x}",
                        "target":      None,
                        "target_name": None,
                        "kind":        "jalr",
                    }
                    if call_args:
                        site["args"] = call_args
                    results[fn.name]["call_sites"].append(site)
                    # Same invalidation — callee may clobber caller-saved.
                    for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                        reg[r] = None
                    idx += 2
                    continue

            # Non-call instruction: fold into state and move on.
            _apply_instruction(reg, word, pc_i)
            idx += 1

    # Convert sets to sorted lists for stable JSON.
    for entry in results.values():
        entry["callers"] = sorted(entry["callers"])
        entry["callees"] = sorted(entry["callees"])

    total_call_sites = sum(len(e["call_sites"]) for e in results.values())
    resolved_jal = total_call_sites - unresolved_jalr - unresolved_jal_to_noncode

    print(f"[callgraph] Call sites: {total_call_sites:,} total")
    print(f"[callgraph]   resolved jal:        {resolved_jal:,}")
    print(f"[callgraph]   unresolved jalr:     {unresolved_jalr:,}")
    print(f"[callgraph]   jal to non-function: {unresolved_jal_to_noncode:,}")
    print(f"[callgraph]   sites with arg values (T15): {args_recorded:,}")

    doc = {
        "stats": {
            "functions":                len(functions),
            "total_call_sites":         total_call_sites,
            "resolved_jal":             resolved_jal,
            "unresolved_jalr":          unresolved_jalr,
            "unresolved_jal_to_noncode": unresolved_jal_to_noncode,
            "sites_with_args":          args_recorded,
        },
        "functions": results,
    }
    print(f"[callgraph] Writing {out_path}")
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
    print("[callgraph] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
