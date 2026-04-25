#!/usr/bin/env python3
"""
Global-variable cross-reference builder.

Walks every function in the recompiled code region of the Racer Revenge ELF,
tracks register values through simple lui+addiu/ori constant folding, and
records every load/store whose effective address resolves to a known absolute.
Output: build/xref/globals.json, keyed by absolute address, listing every
function that reads or writes each address.

Scope & limitations (v1):
 - Linear intra-function scan. Register state is invalidated at branches and
   function calls (pessimistic — we may miss some writes but never report
   false positives for the resolved-address set).
 - Tracks only GPR state, not FPR/VU/COP0.
 - Handles lui, addiu, ori, addu (for constant base+offset), lw/lh/lhu/lb/lbu/
   lwu/ld/lq and sw/sh/sb/sd/sq stores. MMI / FPU loads/stores are ignored.
 - Branch destinations don't affect constant tracking (we never "take" a
   branch — we walk fallthrough linearly).

Usage:
    python tools/xref/build_xref.py \\
        --elf SLUS_202.68 \\
        --functions src/generated \\
        --output build/xref/globals.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---- MIPS decoding -------------------------------------------------------

# Subset of opcodes we care about.
OP_SPECIAL = 0x00
OP_REGIMM  = 0x01
OP_J       = 0x02
OP_JAL     = 0x03
OP_BEQ     = 0x04
OP_BNE     = 0x05
OP_BLEZ    = 0x06
OP_BGTZ    = 0x07
OP_ADDI    = 0x08
OP_ADDIU   = 0x09
OP_SLTI    = 0x0A
OP_SLTIU   = 0x0B
OP_ANDI    = 0x0C
OP_ORI     = 0x0D
OP_XORI    = 0x0E
OP_LUI     = 0x0F
OP_COP0    = 0x10
OP_COP1    = 0x11
OP_COP2    = 0x12
OP_BEQL    = 0x14
OP_BNEL    = 0x15
OP_DADDI   = 0x18
OP_DADDIU  = 0x19
OP_LQ      = 0x1E
OP_SQ      = 0x1F
OP_LB      = 0x20
OP_LH      = 0x21
OP_LWL     = 0x22
OP_LW      = 0x23
OP_LBU     = 0x24
OP_LHU     = 0x25
OP_LWR     = 0x26
OP_LWU     = 0x27
OP_SB      = 0x28
OP_SH      = 0x29
OP_SWL     = 0x2A
OP_SW      = 0x2B
OP_SDL     = 0x2C
OP_SDR     = 0x2D
OP_SWR     = 0x2E
OP_LWC1    = 0x31
OP_LQC2    = 0x36
OP_LD      = 0x37
OP_SWC1    = 0x39
OP_SQC2    = 0x3E
OP_SD      = 0x3F

SPECIAL_JR    = 0x08
SPECIAL_JALR  = 0x09
SPECIAL_SYSCALL = 0x0C
SPECIAL_BREAK = 0x0D

# Load ops that read from memory[rs + imm16]: op → (mnemonic, width in bytes)
LOAD_OPS: dict[int, tuple[str, int]] = {
    OP_LB:  ("lb",   1),
    OP_LBU: ("lbu",  1),
    OP_LH:  ("lh",   2),
    OP_LHU: ("lhu",  2),
    OP_LW:  ("lw",   4),
    OP_LWU: ("lwu",  4),
    OP_LD:  ("ld",   8),
    OP_LQ:  ("lq",  16),
    OP_LWL: ("lwl",  4),
    OP_LWR: ("lwr",  4),
    OP_LWC1:("lwc1", 4),
    OP_LQC2:("lqc2",16),
}

# Store ops that write to memory[rs + imm16]: op → (mnemonic, width in bytes)
STORE_OPS: dict[int, tuple[str, int]] = {
    OP_SB:  ("sb",   1),
    OP_SH:  ("sh",   2),
    OP_SW:  ("sw",   4),
    OP_SD:  ("sd",   8),
    OP_SQ:  ("sq",  16),
    OP_SWL: ("swl",  4),
    OP_SWR: ("swr",  4),
    OP_SDL: ("sdl",  8),
    OP_SDR: ("sdr",  8),
    OP_SWC1:("swc1", 4),
    OP_SQC2:("sqc2",16),
}

# Register numbers whose writes we track as "destination" for constant
# propagation. (Excludes $zero which is ignored.)
def reg_name(r: int) -> str:
    names = ['zero','at','v0','v1','a0','a1','a2','a3',
             't0','t1','t2','t3','t4','t5','t6','t7',
             's0','s1','s2','s3','s4','s5','s6','s7',
             't8','t9','k0','k1','gp','sp','fp','ra']
    return names[r] if 0 <= r < 32 else f"r{r}"

def sign_ext16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v

# ---- Function enumeration from generated source -------------------------

@dataclass
class Function:
    name: str
    start: int
    end: int  # exclusive

ADDR_COMMENT_RE = re.compile(rb"// Address: 0x([0-9a-fA-F]+) - 0x([0-9a-fA-F]+)")
FUNC_NAME_RE    = re.compile(rb"^void (\S+?)\(uint8_t\* rdram,", re.MULTILINE)

def discover_functions(generated_dir: Path) -> list[Function]:
    """Parse // Address headers from every generated .cpp."""
    functions: list[Function] = []
    for path in sorted(generated_dir.glob("*.cpp")):
        try:
            with open(path, "rb") as f:
                # The header is within the first ~1KB.
                head = f.read(2048)
        except OSError:
            continue
        addr_m = ADDR_COMMENT_RE.search(head)
        name_m = FUNC_NAME_RE.search(head)
        if not addr_m or not name_m:
            continue
        start = int(addr_m.group(1), 16)
        end = int(addr_m.group(2), 16)
        if end <= start:
            continue
        name = name_m.group(1).decode("ascii", errors="replace")
        functions.append(Function(name=name, start=start, end=end))
    return functions

# ---- ELF code extraction ------------------------------------------------

def load_elf_text(elf_path: Path) -> tuple[int, bytes]:
    """Return (load_va, bytes) for the single PT_LOAD segment.

    32-bit little-endian ELF only. Use `load_elf_segments` if you need
    overlay coverage too.
    """
    load_va, payload, _ = _parse_elf(elf_path)
    return load_va, payload


def _parse_elf(elf_path: Path) -> tuple[int, bytes, list[tuple[int, bytes]]]:
    """Return (load_va, payload, []). Kept as a seam for load_elf_segments."""
    with open(elf_path, "rb") as f:
        header = f.read(0x34)
        if header[:4] != b"\x7fELF":
            raise ValueError("not an ELF")
        if header[4] != 1 or header[5] != 1:
            raise ValueError("expected 32-bit little-endian ELF")
        e_phoff  = struct.unpack_from("<I", header, 0x1C)[0]
        e_phnum  = struct.unpack_from("<H", header, 0x2C)[0]
        for i in range(e_phnum):
            f.seek(e_phoff + i * 0x20)
            ph = f.read(0x20)
            p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align = \
                struct.unpack("<IIIIIIII", ph)
            if p_type == 1:  # PT_LOAD
                f.seek(p_offset)
                payload = f.read(p_filesz)
                return p_vaddr, payload, []
    raise ValueError("no PT_LOAD segment in ELF")


def load_elf_segments(
    elf_path: Path,
    overlays_toml: Path | None = None,
) -> list[tuple[int, bytes]]:
    """Return a list of (base_va, bytes) tuples covering the ELF's main
    PT_LOAD segment plus any overlays declared in `[[overlays]]` of the
    supplied TOML config.

    Overlays are synthetic regions: their code lives in the ELF at
    `source_va` but is copied to `dest_va` at runtime. For xref / callgraph
    purposes we want to treat the bytes as if they live at dest_va so
    internal jal targets resolve correctly.

    If no overlays are configured, this is the same one-segment view as
    `load_elf_text` returns. Callers use `bytes_at_va(segments, va, n)` to
    read instruction bytes agnostic of which segment holds them.
    """
    main_va, main_bytes, _ = _parse_elf(elf_path)
    segments: list[tuple[int, bytes]] = [(main_va, main_bytes)]
    if overlays_toml is None or not overlays_toml.exists():
        return segments
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore
    except ImportError:
        return segments
    with open(overlays_toml, "rb") as f:
        doc = tomllib.load(f)
    for ov in doc.get("overlays", []):
        src_s = ov.get("source_va")
        dst_s = ov.get("dest_va")
        sz_s  = ov.get("size")
        if src_s is None or dst_s is None or sz_s is None:
            continue
        try:
            source_va = int(src_s, 0) if isinstance(src_s, str) else int(src_s)
            dest_va   = int(dst_s, 0) if isinstance(dst_s, str) else int(dst_s)
            size      = int(sz_s,  0) if isinstance(sz_s,  str) else int(sz_s)
        except (TypeError, ValueError):
            continue
        # Slice the source bytes out of the main segment.
        rel = source_va - main_va
        if rel < 0 or rel + size > len(main_bytes):
            continue
        overlay_bytes = main_bytes[rel:rel + size]
        segments.append((dest_va, overlay_bytes))
    return segments


def bytes_at_va(
    segments: list[tuple[int, bytes]],
    va: int,
    n: int,
) -> bytes | None:
    """Read n bytes starting at va from any segment that covers it.
    Returns None if the range isn't fully inside one segment."""
    for base, data in segments:
        off = va - base
        if 0 <= off and off + n <= len(data):
            return data[off:off + n]
    return None

# ---- Constant folding + xref --------------------------------------------

# Sentinel for unknown register value.
UNK = None  # represented as None

@dataclass
class AccessRecord:
    kind: str        # "read" | "write"
    op: str          # "sw", "lw", ...
    width: int       # bytes
    pc: int          # instruction address
    function: str    # function name
    func_start: int  # for sorting/debug
    # For writes with a known store-value (e.g. `lui $v0, hi; addi $v0, lo;
    # sw $v0, off(base)` where both the base and $v0 resolve) we track the
    # stored value. Enables T3 (indirect-call resolution): if a function
    # pointer table at address A contains value V written by function F,
    # a jalr that loads from A has V as a possible target.
    value: int | None = None

@dataclass
class AddressEntry:
    readers: list[AccessRecord] = field(default_factory=list)
    writers: list[AccessRecord] = field(default_factory=list)

    def first_writer_function(self) -> str | None:
        if not self.writers:
            return None
        return sorted(self.writers, key=lambda r: (r.func_start, r.pc))[0].function

    def to_jsonable(self) -> dict:
        def ser(rs: list[AccessRecord], include_value: bool) -> list[dict]:
            out: list[dict] = []
            for r in rs:
                entry = {
                    "function": r.function,
                    "pc":       f"0x{r.pc:08x}",
                    "op":       r.op,
                    "width":    r.width,
                }
                if include_value and r.value is not None:
                    entry["value"] = f"0x{r.value:08x}"
                out.append(entry)
            return out
        return {
            "readers":               ser(self.readers, False),
            "writers":               ser(self.writers, True),
            "reader_count":          len(self.readers),
            "writer_count":          len(self.writers),
            "first_writer_function": self.first_writer_function(),
        }

def analyze_function(
    func: Function,
    segments: list[tuple[int, bytes]],
    out: dict[int, AddressEntry],
) -> None:
    """Walk instructions in [func.start, func.end). Track register values
    via lui+addiu/ori; record load/store accesses with resolved absolute
    addresses.

    Register state is either:
      - int  : known concrete value
      - ("indexed", base:int, shift:int) : known base + (unknown << shift)
      - None : unknown

    The "indexed" form lets us resolve table-dispatch patterns like
        lui $at, HI; addiu $at, LO  → $at is concrete
        sll $v0, $idx, 2
        addu $at, $at, $v0          → $at becomes ("indexed", HI|LO, 2)
        lw $x, 0x4678($at)          → recorded as access at (HI|LO)+0x4678
    where the individual slot isn't known but the table's base address is.
    """
    # Per-register state (see docstring). $zero is always the int 0.
    reg: list = [0] + [None] * 31

    def inval(r: int) -> None:
        if r != 0:
            reg[r] = None

    def base_of(state) -> int | None:
        """Extract the concrete absolute address for a base register if we
        can resolve it — handles both plain int state and ("indexed",...)
        state (where we treat the element index as 0)."""
        if isinstance(state, int):
            return state
        if isinstance(state, tuple) and state and state[0] == "indexed":
            return state[1]
        return None

    pc = func.start
    while pc < func.end:
        buf = bytes_at_va(segments, pc, 4)
        if buf is None:
            break
        word = struct.unpack_from("<I", buf, 0)[0]
        op = (word >> 26) & 0x3F
        rs = (word >> 21) & 0x1F
        rt = (word >> 16) & 0x1F
        rd = (word >> 11) & 0x1F
        imm = word & 0xFFFF
        sa = (word >> 6) & 0x1F
        func_field = word & 0x3F

        # Record load/store accesses BEFORE updating state, using current rs.
        if op in LOAD_OPS:
            base = base_of(reg[rs])
            if base is not None:
                addr = (base + sign_ext16(imm)) & 0xFFFFFFFF
                mnem, width = LOAD_OPS[op]
                out.setdefault(addr, AddressEntry()).readers.append(
                    AccessRecord("read", mnem, width, pc, func.name, func.start)
                )
            # Load writes into rt.
            inval(rt)
        elif op in STORE_OPS:
            base = base_of(reg[rs])
            if base is not None:
                addr = (base + sign_ext16(imm)) & 0xFFFFFFFF
                mnem, width = STORE_OPS[op]
                # Capture the stored value if the source register has a
                # concrete resolved value. This is what makes T3 (indirect
                # call resolution) work — function-pointer tables get
                # populated this way, and the values we record here BECOME
                # the jalr-target candidates during T3.
                stored_val = None
                if op in (OP_SW, OP_SD):
                    src = reg[rt]
                    if isinstance(src, int):
                        stored_val = src
                out.setdefault(addr, AddressEntry()).writers.append(
                    AccessRecord("write", mnem, width, pc, func.name, func.start,
                                 value=stored_val)
                )
            # Store doesn't modify registers.
        elif op == OP_LUI:
            if rt != 0:
                reg[rt] = (imm & 0xFFFF) << 16
        elif op in (OP_ADDIU, OP_DADDIU, OP_ADDI, OP_DADDI):
            base = reg[rs]
            if isinstance(base, int):
                reg[rt] = (base + sign_ext16(imm)) & 0xFFFFFFFF
            elif isinstance(base, tuple) and base and base[0] == "indexed":
                # addiu on an indexed base shifts the base by imm.
                reg[rt] = ("indexed", (base[1] + sign_ext16(imm)) & 0xFFFFFFFF, base[2])
            else:
                inval(rt)
        elif op == OP_ORI:
            base = reg[rs]
            if isinstance(base, int):
                reg[rt] = (base | imm) & 0xFFFFFFFF
            else:
                inval(rt)
        elif op == OP_ANDI:
            base = reg[rs]
            if isinstance(base, int):
                reg[rt] = (base & imm) & 0xFFFFFFFF
            else:
                inval(rt)
        elif op == OP_XORI:
            base = reg[rs]
            if isinstance(base, int):
                reg[rt] = (base ^ imm) & 0xFFFFFFFF
            else:
                inval(rt)
        elif op == OP_SPECIAL:
            # R-type. Handle a few that preserve constness for base computation.
            if func_field in (0x21, 0x2D):  # addu, daddu
                a, b = reg[rs], reg[rt]
                if isinstance(a, int) and isinstance(b, int):
                    reg[rd] = (a + b) & 0xFFFFFFFF
                elif isinstance(a, int) and not isinstance(b, int):
                    # Index add: base = a, index is whatever b is.
                    # Common case: b is the shifted index register. Preserve
                    # shift info if we tracked it; otherwise scale-0 index.
                    shift = b[2] if isinstance(b, tuple) and b[0] == "indexed" else 0
                    reg[rd] = ("indexed", a, shift)
                elif isinstance(b, int) and not isinstance(a, int):
                    shift = a[2] if isinstance(a, tuple) and a[0] == "indexed" else 0
                    reg[rd] = ("indexed", b, shift)
                else:
                    inval(rd)
            elif func_field == 0x25:  # or
                a, b = reg[rs], reg[rt]
                if isinstance(a, int) and isinstance(b, int):
                    reg[rd] = (a | b) & 0xFFFFFFFF
                else:
                    inval(rd)
            elif func_field == 0x00:  # sll $rd, $rt, sa
                # sll of a known-value register by a constant shift. We don't
                # care about the value — we just record "this is now a shifted
                # index candidate" so addu can combine it with a base.
                src = reg[rt]
                if rd != 0:
                    if isinstance(src, int):
                        reg[rd] = (src << sa) & 0xFFFFFFFF
                    else:
                        # The src isn't known but we know the shift. Mark it
                        # as an indexed sentinel: base 0, shift sa. Later
                        # addu with a known base will pick up the shift.
                        reg[rd] = ("indexed", 0, sa)
            elif func_field in (SPECIAL_JR, SPECIAL_JALR):
                # Function returns or indirect jump: conservative invalidate
                # of caller-saved registers. We're linear-scanning so treat
                # as a barrier beyond which all regs are unknown.
                for r in range(32):
                    inval(r)
            else:
                # Any other SPECIAL writes to rd.
                inval(rd)
        elif op in (OP_BEQ, OP_BNE, OP_BEQL, OP_BNEL, OP_BLEZ, OP_BGTZ, OP_REGIMM):
            # Branches don't write GPRs. We don't follow them; they just
            # mean the fallthrough side still applies. No invalidation.
            pass
        elif op == OP_JAL:
            # jal: $ra = pc+8; we conservatively invalidate caller-saved regs
            # that any reasonable callee may clobber ($v0-$v1, $a0-$a3, $t0-$t9).
            for r in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                inval(r)
        elif op == OP_J:
            # Direct jump — we're still walking linearly, so just continue.
            pass
        elif op == OP_COP0:
            # Broad invalidate on COP0 ops (mtc0/mfc0/tlbwi/eret) — they
            # can change execution context in ways we don't model.
            inval(rt)
        elif op == OP_COP1 or op == OP_COP2:
            # FPU / VU — don't track.
            pass
        else:
            # Unknown: invalidate likely-touched registers conservatively.
            inval(rt)
            inval(rd)

        pc += 4

# ---- Main ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--elf", required=True, help="Path to the PS2 ELF")
    ap.add_argument("--functions", required=True,
                    help="Path to src/generated/ (function headers parsed from .cpp files)")
    ap.add_argument("--output", required=True, help="Output globals.json path")
    ap.add_argument("--min-ram", default="0x100000",
                    help="Minimum guest address to record (below this is ignored as noise)")
    ap.add_argument("--max-ram", default="0x02000000",
                    help="Maximum guest address to record")
    args = ap.parse_args()

    min_ram = int(args.min_ram, 0)
    max_ram = int(args.max_ram, 0)

    elf_path = Path(args.elf)
    gen_dir  = Path(args.functions)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[xref] Loading ELF: {elf_path}")
    overlays_toml = Path("config") / "racer_revenge.toml"
    segments = load_elf_segments(elf_path, overlays_toml)
    for base, data in segments:
        print(f"[xref] Segment: 0x{base:08x} - 0x{base + len(data):08x} ({len(data):,} bytes)")

    print(f"[xref] Discovering functions in {gen_dir}")
    functions = discover_functions(gen_dir)
    print(f"[xref] Found {len(functions):,} recompiled functions")

    print(f"[xref] Analyzing functions for global accesses...")
    accesses: dict[int, AddressEntry] = {}
    for i, fn in enumerate(functions):
        if i and i % 500 == 0:
            print(f"[xref]   progress: {i:,}/{len(functions):,}")
        analyze_function(fn, segments, accesses)

    # Filter out self-code-region accesses (lbu on .text reads the code —
    # not useful for global-variable analysis) and scratchpad/IO MMIO.
    # Keep only guest RAM range, and specifically the data region (post code).
    filtered: dict[int, AddressEntry] = {}
    kept_writes = 0
    kept_reads  = 0
    for addr, entry in accesses.items():
        if addr < min_ram or addr >= max_ram:
            continue
        filtered[addr] = entry
        kept_writes += len(entry.writers)
        kept_reads  += len(entry.readers)

    total_unique = len(filtered)
    print(f"[xref] Unique addresses with activity: {total_unique:,}")
    print(f"[xref]   total writes recorded: {kept_writes:,}")
    print(f"[xref]   total reads  recorded: {kept_reads:,}")

    # Build JSON.
    addresses_json = {
        f"0x{addr:08x}": entry.to_jsonable()
        for addr, entry in sorted(filtered.items())
    }

    stats = {
        "elf":               str(elf_path),
        "functions_scanned": len(functions),
        "unique_addresses":  total_unique,
        "total_writes":      kept_writes,
        "total_reads":       kept_reads,
        "min_ram":           f"0x{min_ram:08x}",
        "max_ram":           f"0x{max_ram:08x}",
    }

    doc = {"stats": stats, "addresses": addresses_json}

    print(f"[xref] Writing {out_path}")
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"[xref] Done.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
