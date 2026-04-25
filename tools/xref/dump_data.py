#!/usr/bin/env python3
"""
Dump guest memory from the ELF at a given VA, in a chosen format.

Useful for ad-hoc reverse-engineering questions like "what's in the
dispatch table at 0x385120?" or "show me the first 64 bytes of the
overlay at 0x3849A0 as MIPS disassembly."

Usage:
    python tools/xref/dump_data.py --va 0x385120 --size 0x60
    python tools/xref/dump_data.py --va 0x385120 --size 0x60 --format words
    python tools/xref/dump_data.py --va 0x385120 --size 0x60 --format pairs
    python tools/xref/dump_data.py --va 0x3849A0 --size 0x40 --format mips
    python tools/xref/dump_data.py --va 0x384670 --size 16 --format dwords
    python tools/xref/dump_data.py --va 0x3850A0 --size 0x80 --format strings

Formats:
    bytes       hex bytes + ASCII preview (the default)
    words       32-bit LE words, 4 per row with addresses
    dwords      64-bit LE words, 2 per row
    pairs       word pairs as (lo, hi) — handy for tables of
                (id, handler) entries
    mips        decoded MIPS instructions (minimal, for quick checks)
    strings     attempt to render as null-terminated ASCII
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def va_to_offset(va: int, load_va: int) -> int:
    # Racer Revenge's single PT_LOAD maps file offset 0x80 → VA 0x100000.
    # We parse the ELF header instead of hard-coding.
    return va - load_va + 0x80


def load_elf_segment(elf: Path) -> tuple[int, int, int]:
    """Return (load_va, file_offset, file_size) for the single PT_LOAD."""
    with open(elf, "rb") as f:
        header = f.read(0x34)
        if header[:4] != b"\x7fELF":
            sys.exit("not an ELF")
        e_phoff = struct.unpack_from("<I", header, 0x1C)[0]
        e_phnum = struct.unpack_from("<H", header, 0x2C)[0]
        for i in range(e_phnum):
            f.seek(e_phoff + i * 0x20)
            ph = f.read(0x20)
            p_type, p_offset, p_vaddr, _, p_filesz, *_ = struct.unpack("<IIIIIIII", ph)
            if p_type == 1:  # PT_LOAD
                return p_vaddr, p_offset, p_filesz
    sys.exit("no PT_LOAD in ELF")


def read_elf_bytes(elf: Path, va: int, size: int) -> bytes:
    load_va, load_off, load_size = load_elf_segment(elf)
    if va < load_va or va + size > load_va + load_size:
        sys.exit(f"VA range 0x{va:08x}..0x{va+size:08x} is outside the "
                 f"loaded segment [0x{load_va:08x}..0x{load_va+load_size:08x})")
    with open(elf, "rb") as f:
        f.seek(load_off + (va - load_va))
        return f.read(size)


# ---- Formatters ----------------------------------------------------------

def fmt_bytes(va: int, data: bytes, width: int = 16) -> str:
    lines: list[str] = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        hex_part = hex_part.ljust(width * 3 - 1)
        lines.append(f"  0x{va + off:08x}:  {hex_part}  {ascii_part}")
    return "\n".join(lines)


def fmt_words(va: int, data: bytes) -> str:
    lines: list[str] = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        words = [struct.unpack_from("<I", chunk, i)[0]
                 for i in range(0, len(chunk), 4)]
        parts = "  ".join(f"{w:08x}" for w in words)
        lines.append(f"  0x{va + off:08x}:  {parts}")
    return "\n".join(lines)


def fmt_dwords(va: int, data: bytes) -> str:
    lines: list[str] = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        if len(chunk) >= 8:
            vals = [struct.unpack_from("<Q", chunk, i)[0]
                    for i in range(0, len(chunk) - 7, 8)]
            parts = "  ".join(f"{v:016x}" for v in vals)
            lines.append(f"  0x{va + off:08x}:  {parts}")
    return "\n".join(lines)


def fmt_pairs(va: int, data: bytes) -> str:
    """Pair of 32-bit words per row: common for (id, handler) tables."""
    lines: list[str] = []
    for off in range(0, len(data), 8):
        chunk = data[off:off + 8]
        if len(chunk) < 8:
            break
        a = struct.unpack_from("<I", chunk, 0)[0]
        b = struct.unpack_from("<I", chunk, 4)[0]
        lines.append(f"  0x{va + off:08x}:  ({a:#010x}, {b:#010x})")
    return "\n".join(lines)


def fmt_strings(va: int, data: bytes) -> str:
    """Walk looking for printable null-terminated strings."""
    lines: list[str] = []
    i = 0
    while i < len(data):
        if not (0x20 <= data[i] < 0x7F):
            i += 1
            continue
        start = i
        while i < len(data) and 0x20 <= data[i] < 0x7F:
            i += 1
        # Only print runs of >= 4 chars.
        if i - start >= 4:
            s = data[start:i].decode("ascii")
            lines.append(f"  0x{va + start:08x}: {s!r}")
        if i < len(data) and data[i] == 0:
            i += 1
    if not lines:
        lines.append("  (no printable runs >= 4 chars)")
    return "\n".join(lines)


# Minimal MIPS decoder — just enough for "is this code or not" sanity checks.
def fmt_mips(va: int, data: bytes) -> str:
    regs = ['zero','at','v0','v1','a0','a1','a2','a3',
            't0','t1','t2','t3','t4','t5','t6','t7',
            's0','s1','s2','s3','s4','s5','s6','s7',
            't8','t9','k0','k1','gp','sp','fp','ra']
    def disas(word: int, pc: int) -> str:
        op = (word >> 26) & 0x3F
        rs = (word >> 21) & 0x1F
        rt = (word >> 16) & 0x1F
        rd = (word >> 11) & 0x1F
        sa = (word >> 6) & 0x1F
        fn = word & 0x3F
        imm = word & 0xFFFF
        simm = imm - 0x10000 if imm & 0x8000 else imm
        tgt = ((pc + 4) & 0xF0000000) | ((word & 0x03FFFFFF) << 2)
        rn = lambda i: regs[i]
        if word == 0:
            return "nop"
        if op == 0:
            if fn == 0x00 and sa == 0: return f"sll ${rn(rd)}, ${rn(rt)}, 0"
            if fn == 0x08:             return f"jr ${rn(rs)}"
            if fn == 0x09:             return f"jalr ${rn(rs)}"
            if fn == 0x21:             return f"addu ${rn(rd)}, ${rn(rs)}, ${rn(rt)}"
            if fn == 0x25:             return f"or ${rn(rd)}, ${rn(rs)}, ${rn(rt)}"
            if fn == 0x2D:             return f"daddu ${rn(rd)}, ${rn(rs)}, ${rn(rt)}"
        if op == 0x02:  return f"j 0x{tgt:x}"
        if op == 0x03:  return f"jal 0x{tgt:x}"
        if op == 0x04:  return f"beq ${rn(rs)}, ${rn(rt)}, 0x{pc + 4 + simm * 4:x}"
        if op == 0x05:  return f"bne ${rn(rs)}, ${rn(rt)}, 0x{pc + 4 + simm * 4:x}"
        if op == 0x09:  return f"addiu ${rn(rt)}, ${rn(rs)}, {simm}"
        if op == 0x0D:  return f"ori ${rn(rt)}, ${rn(rs)}, 0x{imm:x}"
        if op == 0x0F:  return f"lui ${rn(rt)}, 0x{imm:x}"
        if op == 0x23:  return f"lw ${rn(rt)}, {simm}(${rn(rs)})"
        if op == 0x2B:  return f"sw ${rn(rt)}, {simm}(${rn(rs)})"
        if op == 0x3F:  return f"sd ${rn(rt)}, {simm}(${rn(rs)})"
        if word == 0xC: return "syscall 0"
        return f"?? 0x{word:08x}"
    lines: list[str] = []
    for off in range(0, len(data) - 3, 4):
        word = struct.unpack_from("<I", data, off)[0]
        pc = va + off
        lines.append(f"  0x{pc:08x}: {word:08x}  {disas(word, pc)}")
    return "\n".join(lines)


FORMATTERS = {
    "bytes":    fmt_bytes,
    "words":    fmt_words,
    "dwords":   fmt_dwords,
    "pairs":    fmt_pairs,
    "strings":  fmt_strings,
    "mips":     fmt_mips,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--elf", default="SLUS_202.68",
                    help="Path to the PS2 ELF (default: SLUS_202.68)")
    ap.add_argument("--va", required=True, help="Starting virtual address (hex)")
    ap.add_argument("--size", required=True, help="Bytes to dump (hex or decimal)")
    ap.add_argument("--format", default="bytes", choices=sorted(FORMATTERS),
                    help="Display format (default: bytes)")
    args = ap.parse_args()

    va = int(args.va, 0)
    size = int(args.size, 0)
    data = read_elf_bytes(Path(args.elf), va, size)

    print(f"ELF data dump: {args.elf}  VA 0x{va:08x}..0x{va+size:08x} "
          f"({size:d} bytes, format={args.format})")
    print(FORMATTERS[args.format](va, data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
