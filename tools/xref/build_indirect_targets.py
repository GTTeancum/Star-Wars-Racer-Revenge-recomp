#!/usr/bin/env python3
"""
Indirect-call (jalr) target resolver — T3.

The plain callgraph only resolves direct `jal <imm>` calls. This tool
takes the next step: for each `jalr $R` site, it backward-slices within
the function to find the `lw $R, off(base)` that populated the register,
resolves the memory address being loaded, and then looks that address up
in globals.json's writer list. Each writer that stored a KNOWN VALUE to
that slot is a candidate target.

Output: `build/xref/indirect_targets.json`. Enrichment layered on top of
callgraph.json — doesn't replace it.

    {
      "stats": {
        "jalr_sites":       N,
        "resolved_address": M,   # jalr sites where we resolved the load's
                                 # memory address
        "resolved_targets": K,   # sites where at least one candidate target
                                 # came back from the writer lookup
      },
      "sites": [
        {
          "pc":           "0x00302E00",
          "function":     "entry_302df0_0x302e10",
          "register":     "t9",
          "load_pc":      "0x00302DE0",
          "load_addr":    "0x003856A0",
          "candidates":   [
            {"target": "0x002E9150", "writer_fn": "...", "writer_pc": "..."}
          ]
        }, ...
      ]
    }

Usage:
    python tools/xref/build_indirect_targets.py \\
        --elf SLUS_202.68 \\
        --functions src/generated \\
        --xref build/xref/globals.json \\
        --output build/xref/indirect_targets.json
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_xref import (  # type: ignore
    Function, discover_functions, load_elf_segments, bytes_at_va,
    LOAD_OPS, OP_LW, OP_LWU, OP_LD, OP_LUI, OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI,
    OP_ORI, OP_SPECIAL, SPECIAL_JR, SPECIAL_JALR,
    sign_ext16,
)


# MIPS GPR names for debug output.
REG_NAMES = ['zero','at','v0','v1','a0','a1','a2','a3',
             't0','t1','t2','t3','t4','t5','t6','t7',
             's0','s1','s2','s3','s4','s5','s6','s7',
             't8','t9','k0','k1','gp','sp','fp','ra']


@dataclass
class Site:
    pc: int
    function: str
    register: int
    load_pc: int | None = None
    load_addr: int | None = None
    indexed: bool = False       # True = load's base resolved only as table base
    candidates: list[dict] | None = None


def _base_of(state):
    """Register state is int | ('indexed', base, shift) | None.
    Extract the resolvable base address from either form.
    See build_xref.py for the same pattern."""
    if isinstance(state, int):
        return state
    if isinstance(state, tuple) and state and state[0] == "indexed":
        return state[1]
    return None


def backward_resolve_load_addr(
    instructions: list[tuple[int, int]],
    jalr_idx: int,
    jalr_rs: int,
) -> tuple[int | None, int | None, bool]:
    """Walk backward from the jalr site looking for the last writer of
    register `jalr_rs`. If it's a `lw $R, imm(base)` and we can resolve
    `base` (either to a concrete value OR to an indexed-table form),
    return (load_pc, load_addr, indexed_flag).

    T9 extension: supports index-scaled patterns:
        sll  $idx, $src, N
        addu $addr, $base, $idx
        lw   $R,    off($addr)
    which resolves to ("indexed", base, shift). We return load_addr as
    base+off in that case and set indexed_flag=True so the caller knows
    this is a TABLE base, not a single slot — any writer to the table
    is a candidate target.

    Bounded backward scan (~30 instructions).
    """
    # Walk backward looking for the writer of jalr_rs.
    load_idx = None
    load_base = None
    load_imm = None
    scan_start = max(0, jalr_idx - 30)
    for i in range(jalr_idx - 1, scan_start - 1, -1):
        pc, word = instructions[i]
        op = (word >> 26) & 0x3F
        rt = (word >> 16) & 0x1F
        rs = (word >> 21) & 0x1F
        rd = (word >> 11) & 0x1F
        fn = word & 0x3F
        imm = word & 0xFFFF

        # Which register does this instruction write?
        write_reg = None
        if op in LOAD_OPS or op in (OP_LUI, OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI,
                                    OP_ORI, 0x0C, 0x0E):
            # I-type writing rt.
            write_reg = rt
        elif op == OP_SPECIAL and fn not in (SPECIAL_JR, SPECIAL_JALR, 0x0C, 0x0D):
            # R-type writing rd.
            write_reg = rd
        # JAL/JALR clobber $ra (31)
        elif op == 3 or (op == 0 and fn == SPECIAL_JALR):
            write_reg = 31

        if write_reg != jalr_rs:
            continue

        # Found the writer. It must be an LW (or similar).
        if op in (OP_LW, OP_LWU):
            load_idx = i
            load_base = rs
            load_imm = sign_ext16(imm)
            break
        return None, None, False  # writer was something we can't handle

    if load_idx is None:
        return None, None, False

    # Resolve `load_base` by running constant folding forward from
    # scan_start up to load_idx. T9: tracks the ("indexed", base, shift)
    # form to catch `sll+addu` index patterns.
    reg: list = [0] + [None] * 31
    for i in range(scan_start, load_idx):
        pc, word = instructions[i]
        op = (word >> 26) & 0x3F
        rs = (word >> 21) & 0x1F
        rt = (word >> 16) & 0x1F
        rd = (word >> 11) & 0x1F
        fn = word & 0x3F
        imm = word & 0xFFFF
        sa = (word >> 6) & 0x1F

        if op == OP_LUI:
            if rt != 0: reg[rt] = (imm & 0xFFFF) << 16
        elif op in (OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI):
            base = reg[rs]
            if isinstance(base, int):
                reg[rt] = (base + sign_ext16(imm)) & 0xFFFFFFFF
            elif isinstance(base, tuple) and base and base[0] == "indexed":
                reg[rt] = ("indexed", (base[1] + sign_ext16(imm)) & 0xFFFFFFFF, base[2])
            elif rt != 0:
                reg[rt] = None
        elif op == OP_ORI:
            base = reg[rs]
            reg[rt] = ((base | imm) & 0xFFFFFFFF) if isinstance(base, int) else None
        elif op == OP_SPECIAL:
            if fn in (0x21, 0x2D):  # addu / daddu
                a, b = reg[rs], reg[rt]
                if isinstance(a, int) and isinstance(b, int):
                    reg[rd] = (a + b) & 0xFFFFFFFF
                elif isinstance(a, int) and not isinstance(b, int):
                    shift = b[2] if isinstance(b, tuple) and b[0] == "indexed" else 0
                    reg[rd] = ("indexed", a, shift)
                elif isinstance(b, int) and not isinstance(a, int):
                    shift = a[2] if isinstance(a, tuple) and a[0] == "indexed" else 0
                    reg[rd] = ("indexed", b, shift)
                elif rd != 0:
                    reg[rd] = None
            elif fn == 0x00 and rd != 0:  # sll $rd, $rt, sa
                src = reg[rt]
                if isinstance(src, int):
                    reg[rd] = (src << sa) & 0xFFFFFFFF
                else:
                    # Unknown src with constant shift → indexed sentinel.
                    reg[rd] = ("indexed", 0, sa)
            elif fn in (SPECIAL_JR, SPECIAL_JALR):
                for r in range(1, 32): reg[r] = None
            elif rd != 0:
                reg[rd] = None
        elif op == 3:  # jal: clobber caller-saved
            for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                reg[r] = None
        elif op in LOAD_OPS:
            if rt != 0: reg[rt] = None
        elif rt != 0:
            reg[rt] = None

    base_state = reg[load_base]
    base_val = _base_of(base_state)
    if base_val is None:
        return None, None, False
    load_pc = instructions[load_idx][0]
    load_addr = (base_val + load_imm) & 0xFFFFFFFF
    indexed = isinstance(base_state, tuple) and base_state and base_state[0] == "indexed"
    return load_pc, load_addr, indexed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--elf", required=True)
    ap.add_argument("--functions", required=True)
    ap.add_argument("--xref", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    print(f"[t3] Loading ELF: {args.elf}")
    overlays_toml = Path("config") / "racer_revenge.toml"
    segments = load_elf_segments(Path(args.elf), overlays_toml)
    for base, data in segments:
        print(f"[t3] Segment: 0x{base:08x} - 0x{base + len(data):08x} ({len(data):,} bytes)")
    print(f"[t3] Loading xref: {args.xref}")
    with open(args.xref) as f:
        xref = json.load(f)
    print(f"[t3] Discovering functions...")
    functions = discover_functions(Path(args.functions))
    print(f"[t3] {len(functions):,} functions")

    # Build an address→list-of-writer-values map for fast lookup.
    writers_by_addr: dict[int, list[dict]] = {}
    # For indexed lookups (T9), we also want a sorted list of known addresses
    # so we can grab all writers in a small range around the table base.
    sorted_addrs: list[int] = []
    for addr_s, entry in xref["addresses"].items():
        addr = int(addr_s, 16)
        sorted_addrs.append(addr)
        for w in entry["writers"]:
            val_s = w.get("value")
            if val_s is None: continue
            writers_by_addr.setdefault(addr, []).append({
                "value":     int(val_s, 16),
                "writer_fn": w["function"],
                "writer_pc": w["pc"],
            })
    sorted_addrs.sort()

    # Heuristic cap on how far from the table base we'll gather writers.
    # Most dispatch tables are ≤ 1 KB; bigger caps produce noisy candidates
    # and slow down T3. A function pointer table of 256 entries * 4B = 1024B.
    INDEX_TABLE_MAX_BYTES = 1024

    def writers_in_range(base_addr: int, span: int) -> list[dict]:
        """Return all stored values across [base_addr, base_addr+span)."""
        import bisect
        lo = bisect.bisect_left(sorted_addrs, base_addr)
        hi = bisect.bisect_left(sorted_addrs, base_addr + span)
        out: list[dict] = []
        seen: set[tuple[int, str]] = set()
        for idx in range(lo, hi):
            a = sorted_addrs[idx]
            for w in writers_by_addr.get(a, []):
                key = (w["value"], w["writer_fn"])
                if key in seen: continue
                seen.add(key)
                out.append({**w, "slot": f"0x{a:08x}"})
        return out

    # Walk every function looking for jalr sites.
    sites: list[Site] = []
    for i, fn in enumerate(functions):
        if i and i % 500 == 0:
            print(f"[t3]   progress: {i:,}/{len(functions):,}  "
                  f"sites={len(sites):,}")
        # Decode the function's instructions.
        pc = fn.start
        insts: list[tuple[int, int]] = []
        while pc < fn.end:
            buf = bytes_at_va(segments, pc, 4)
            if buf is None:
                break
            word = struct.unpack_from("<I", buf, 0)[0]
            insts.append((pc, word))
            pc += 4
        # Find jalr sites.
        for idx, (pc_i, word) in enumerate(insts):
            op = (word >> 26) & 0x3F
            fn_f = word & 0x3F
            if op == 0 and fn_f == SPECIAL_JALR:
                rs = (word >> 21) & 0x1F
                site = Site(pc=pc_i, function=fn.name, register=rs)
                load_pc, load_addr, indexed = backward_resolve_load_addr(
                    insts, idx, rs)
                if load_addr is not None:
                    site.load_pc = load_pc
                    site.load_addr = load_addr
                    site.indexed = indexed
                    if indexed:
                        # T9: the load uses an index we can't statically resolve,
                        # so treat load_addr as a TABLE BASE and gather writers
                        # across the window.
                        site.candidates = writers_in_range(
                            load_addr, INDEX_TABLE_MAX_BYTES)
                    else:
                        site.candidates = writers_by_addr.get(load_addr, [])
                sites.append(site)

    resolved_addr = sum(1 for s in sites if s.load_addr is not None)
    resolved_indexed = sum(1 for s in sites if s.indexed)
    resolved_targets = sum(1 for s in sites if s.candidates)

    print(f"[t3] jalr sites:          {len(sites):,}")
    print(f"[t3]   address resolved:   {resolved_addr:,}")
    print(f"[t3]     (of which indexed tables): {resolved_indexed:,}")
    print(f"[t3]   target resolved:    {resolved_targets:,}")

    # Flatten sites to JSON.
    out = {
        "stats": {
            "jalr_sites":       len(sites),
            "resolved_address": resolved_addr,
            "resolved_indexed": resolved_indexed,
            "resolved_targets": resolved_targets,
        },
        "sites": [
            {
                "pc":         f"0x{s.pc:08x}",
                "function":   s.function,
                "register":   REG_NAMES[s.register] if s.register < 32 else f"r{s.register}",
                "load_pc":    f"0x{s.load_pc:08x}" if s.load_pc else None,
                "load_addr":  f"0x{s.load_addr:08x}" if s.load_addr else None,
                "indexed":    s.indexed,
                "candidates": [
                    {
                        "target":    f"0x{c['value']:08x}",
                        "writer_fn": c["writer_fn"],
                        "writer_pc": c["writer_pc"],
                        **({"slot": c["slot"]} if "slot" in c else {}),
                    } for c in (s.candidates or [])
                ],
            } for s in sites
        ],
    }
    print(f"[t3] Writing {args.output}")
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print("[t3] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
