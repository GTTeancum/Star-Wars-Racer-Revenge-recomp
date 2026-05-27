#!/usr/bin/env python3
"""
split_giant_function.py — Splits the 2.1GB sub_0031D200 C++ file into N chunks.

Strategy:
  * Labels are split into N groups by file-order position.
  * Each chunk gets a switch table covering ALL its labels (not just resumable
    entry points) so that cross-chunk goto calls can dispatch correctly.
  * Cross-chunk gotos are rewritten as:
      ctx->pc = 0xADDR; sub_0031D200_chunk_XXXX(rdram, ctx, runtime); return;
  * The master dispatcher uses the EXACT original switch-table PCs to call the
    right chunk (exact PC -> chunk, no overlapping address ranges).

Usage:
    python split_giant_function.py [--chunks N] [--input FILE] [--outdir DIR]
"""

import re
import os
import sys
import argparse
from collections import defaultdict

# ---- Config ---------------------------------------------------------------
DEFAULT_INPUT  = r"C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp\src\generated\sub_0031D200_0x31d200.cpp"
DEFAULT_OUTDIR = r"C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp\src\generated_chunks"
DEFAULT_CHUNKS = 120
DEFAULT_MAX_MB = 0.0  # 0 = use --chunks fallback (legacy mode); >0 = byte-aware bin-pack

HEADER = """\
#include "ps2_runtime_macros.h"
#include "ps2_runtime.h"
#include "ps2_recompiled_functions.h"
#include "ps2_recompiled_stubs.h"
#include "ps2_syscalls.h"
#include "ps2_stubs.h"
#include "ps2_mips_interp.h"
#include "sub_0031D200_chunks.h"
#ifdef PS2_FUNCTION_LOG_TRACKER
#include "ps2_log.h"
#endif
"""

LABEL_DEF_RE  = re.compile(r'^label_([0-9a-fA-F]+):')
LABEL_GOTO_RE = re.compile(r'\bgoto label_([0-9a-fA-F]+);')
SWITCH_CASE_RE = re.compile(r'^\s+case 0x([0-9a-fA-F]+)u: goto label_([0-9a-fA-F]+);')

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--chunks",  type=int, default=DEFAULT_CHUNKS,
                   help="Target chunk count (used when --max-mb is 0)")
    p.add_argument("--max-mb",  type=float, default=DEFAULT_MAX_MB,
                   help="Max chunk size in MB (byte-aware bin-pack mode). "
                        "0 = use --chunks fallback (legacy label-count split).")
    p.add_argument("--input",   default=DEFAULT_INPUT)
    p.add_argument("--outdir",  default=DEFAULT_OUTDIR)
    return p.parse_args()

def main():
    args = parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print(f"[split] Input : {args.input}")
    print(f"[split] Output: {args.outdir}")
    print(f"[split] Chunks: {args.chunks}")

    # ------------------------------------------------------------------
    # Pass 1: collect all label definitions and original switch-table cases
    # ------------------------------------------------------------------
    print("[split] Pass 1: scanning labels, switch table, and per-label byte size ...", flush=True)

    label_order  = []          # list of hex strings in file order
    switch_cases = {}          # original switch: pc_hex -> lbl_hex
    switch_end_line = None
    label_bytes  = {}          # lbl_hex -> bytes of the label body (incl. header line)

    with open(args.input, "r", encoding="utf-8", errors="replace") as f:
        in_switch = False
        current_label = None
        current_bytes = 0
        for lineno, line in enumerate(f, 1):
            stripped = line.strip()
            line_size = len(line.encode("utf-8", errors="replace"))

            if not in_switch and "switch (ctx->pc)" in line:
                in_switch = True
                continue

            if in_switch:
                m = SWITCH_CASE_RE.match(line)
                if m:
                    switch_cases[m.group(1).lower()] = m.group(2).lower()
                elif "default: break;" in line and switch_end_line is None:
                    switch_end_line = lineno
                    in_switch = False
                continue

            # AFTER the switch: track labels + per-label byte size
            if switch_end_line is not None:
                if line and line[0] not in (' ', '\t', '\r', '\n', '}'):
                    m = LABEL_DEF_RE.match(stripped)
                    if m:
                        # Close previous label tally
                        if current_label is not None:
                            label_bytes[current_label] = current_bytes
                        current_label = m.group(1).lower()
                        current_bytes = line_size
                        label_order.append(current_label)
                        continue
                # Any line within a label body (indented code, blank lines, etc.)
                if current_label is not None:
                    current_bytes += line_size
        # Close the final label
        if current_label is not None:
            label_bytes[current_label] = current_bytes

    n_labels = len(label_order)
    total_bytes = sum(label_bytes.values())
    print(f"[split] Found {n_labels} labels, {len(switch_cases)} switch cases, "
          f"switch ends at line {switch_end_line}")
    print(f"[split] Total label-body bytes: {total_bytes/1024/1024:.1f} MB")

    # ------------------------------------------------------------------
    # Assign labels to chunks
    #
    # Two modes:
    #   --max-mb > 0 (byte-aware bin-pack): walk labels in file order,
    #     accumulate byte size, start a new chunk when adding the next
    #     label would exceed max bytes.  Produces evenly-sized chunks
    #     regardless of how bytes-per-label varies (1000x in practice).
    #   --max-mb == 0 (legacy): split by label count into args.chunks
    #     bins of equal label count.  Bimodal output — some chunks
    #     <500KB, some >100MB.
    # ------------------------------------------------------------------
    label_to_chunk = {}
    label_to_addr  = {}

    if args.max_mb > 0.0:
        max_bytes = int(args.max_mb * 1024 * 1024)
        print(f"[split] Mode: byte-aware bin-pack (max chunk size {args.max_mb:.1f} MB)")
        chunk_id = 0
        current_chunk_bytes = 0
        for lbl in label_order:
            lbl_size = label_bytes.get(lbl, 0)
            # If adding this label would exceed max AND the current chunk
            # is non-empty, start a new chunk.  (Always allow the first
            # label into a fresh chunk, even if it alone exceeds max — we
            # can't split a single label.)
            if current_chunk_bytes > 0 and current_chunk_bytes + lbl_size > max_bytes:
                chunk_id += 1
                current_chunk_bytes = 0
            label_to_chunk[lbl] = chunk_id
            label_to_addr[lbl]  = int(lbl, 16)
            current_chunk_bytes += lbl_size
        actual_chunks = chunk_id + 1
        print(f"[split] Bin-packed {n_labels} labels -> {actual_chunks} chunks")
    else:
        n_chunks = args.chunks
        chunk_size = max(1, (n_labels + n_chunks - 1) // n_chunks)
        print(f"[split] Mode: legacy label-count split (target {n_chunks} chunks)")
        for idx, lbl in enumerate(label_order):
            chunk_id = idx // chunk_size
            label_to_chunk[lbl] = chunk_id
            label_to_addr[lbl]  = int(lbl, 16)
        actual_chunks = max(label_to_chunk.values()) + 1 if label_to_chunk else 0
        print(f"[split] Assigning {n_labels} labels -> {actual_chunks} chunks (target {n_chunks})")

    # Group ALL labels by chunk (for the chunk's switch table)
    chunk_labels = defaultdict(list)   # chunk_id -> [lbl_hex, ...]
    for lbl, chunk_id in label_to_chunk.items():
        chunk_labels[chunk_id].append(lbl)

    # ------------------------------------------------------------------
    # Write chunk files (header + full switch table, code written in pass 2)
    # ------------------------------------------------------------------
    chunk_files = []
    for c in range(actual_chunks):
        path = os.path.join(args.outdir, f"sub_0031D200_chunk_{c:04d}.cpp")
        fh = open(path, "w", encoding="utf-8")
        chunk_files.append(fh)
        fh.write(f"// Auto-split chunk {c} of sub_0031D200\n")
        fh.write(HEADER + "\n")
        fh.write(f"void sub_0031D200_chunk_{c:04d}(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {{\n")
        # Write switch table for ALL labels in this chunk
        cases = sorted(chunk_labels[c], key=lambda x: int(x, 16))
        if cases:
            fh.write("    switch (ctx->pc) {\n")
            for lbl_hex in cases:
                fh.write(f"        case 0x{lbl_hex}u: goto label_{lbl_hex};\n")
            fh.write("        default: break;\n")
            fh.write("    }\n\n")
        fh.write("    return; // unreachable fall-through\n\n")

    print("[split] Pass 2: streaming and rewriting gotos ...", flush=True)

    # ------------------------------------------------------------------
    # Pass 2: stream code blocks to correct chunk files, rewrite cross-chunk gotos
    # ------------------------------------------------------------------
    current_label_chunk = 0
    lines_written = 0
    PROGRESS_INTERVAL = 2_000_000

    def rewrite_goto(line, current_label_chunk):
        m = LABEL_GOTO_RE.search(line)
        if not m:
            return line
        target = m.group(1).lower()
        if target not in label_to_chunk:
            # Target label is not defined anywhere in the source.  This happens
            # because sub_31D200 runs into the data segment — the recompiler
            # emits case entries for every plausible PC value but only defines
            # labels for actual instructions.  PCs in data regions get case
            # entries without corresponding labels.  Drop the line entirely;
            # the runtime's exception/recover-pc handler catches PCs that fall
            # through to default.
            return ""
        target_chunk = label_to_chunk[target]
        if target_chunk == current_label_chunk:
            return line

        # Cross-chunk: replace `goto label_X;` with cross-chunk dispatch.
        # Two shapes to handle:
        #  (a) `        goto label_X;`           — standalone unconditional goto
        #  (b) `        case 0xXu: goto label_X;` — inside an inner switch table
        # For (a) we replace the whole line.  For (b) we MUST preserve the
        # `case 0xXu:` prefix so the inner switch dispatches correctly; we
        # replace just the `goto label_X;` portion.
        target_addr = label_to_addr[target]
        replacement = (
            f"{{ ctx->pc = 0x{target_addr:x}u; "
            f"sub_0031D200_chunk_{target_chunk:04d}(rdram, ctx, runtime); return; }}"
        )
        # In-place substitute the goto only — preserves any `case XXu:` prefix
        # on the same line.  The trailing semicolon is replaced by the block
        # we emit (which has its own `; }`).
        return LABEL_GOTO_RE.sub(replacement, line, count=1)

    with open(args.input, "r", encoding="utf-8", errors="replace") as f:
        labels_seen = 0

        for lineno, line in enumerate(f, 1):
            if lineno <= switch_end_line + 4:
                continue

            # Non-indented line: either a label definition or closing brace
            if line and line[0] not in (' ', '\t', '\r', '\n'):
                stripped = line.strip()
                m = LABEL_DEF_RE.match(stripped)
                if m:
                    lbl = m.group(1).lower()
                    if lbl in label_to_chunk:
                        current_label_chunk = label_to_chunk[lbl]
                    chunk_files[current_label_chunk].write(line)
                    lines_written += 1
                    labels_seen += 1
                # else: closing brace or other non-indented line — skip
                continue

            # Indented code line
            rewritten = rewrite_goto(line, current_label_chunk)
            chunk_files[current_label_chunk].write(rewritten)
            lines_written += 1

            if lines_written % PROGRESS_INTERVAL == 0:
                print(f"[split]   ... {lines_written:,} lines, {labels_seen}/{n_labels} labels", flush=True)

    # Close chunks with closing brace
    for fh in chunk_files:
        fh.write("}\n")
        fh.close()

    print(f"[split] Wrote {lines_written:,} code lines to {actual_chunks} chunk files")

    # ------------------------------------------------------------------
    # Generate header with all chunk forward declarations
    # ------------------------------------------------------------------
    header_path = os.path.join(args.outdir, "sub_0031D200_chunks.h")
    with open(header_path, "w", encoding="utf-8") as hf:
        hf.write("#pragma once\n")
        hf.write('#include "ps2_runtime.h"\n\n')
        hf.write("// Forward declarations for all sub_0031D200 chunks\n")
        for c in range(actual_chunks):
            hf.write(f"void sub_0031D200_chunk_{c:04d}(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime);\n")
        hf.write("\n")
    print(f"[split] Wrote header: {header_path}")

    # ------------------------------------------------------------------
    # Build exact-PC dispatch table for the master entry function.
    #
    # The original recompiler switch only contains analyzer-discovered entry
    # points.  Racer Revenge also stores callback pointers into the later
    # sub_31D200 range in data tables (for example 0x3C9EF0..0x3CC2D0). Those
    # callbacks are registered to this master at runtime, so the master must be
    # able to dispatch every real label, not only the original switch cases.
    # ------------------------------------------------------------------
    # Initial entry: the function is called with ctx->pc=0x31d200 (its address)
    # label_31d200 should be in label_to_chunk
    initial_label = label_order[0] if label_order else None  # first label in file
    initial_chunk = label_to_chunk.get(initial_label, 0)

    # Build master switch: pc -> chunk
    master_dispatch = {}  # pc_hex -> chunk_id
    for pc_hex, lbl_hex in switch_cases.items():
        if lbl_hex in label_to_chunk:
            master_dispatch[pc_hex] = label_to_chunk[lbl_hex]
    for lbl_hex, chunk_id in label_to_chunk.items():
        master_dispatch.setdefault(lbl_hex, chunk_id)
    # Also add the function's own start address as an entry
    if initial_label:
        master_dispatch["31d200"] = initial_chunk

    master_path = os.path.join(args.outdir, "sub_0031D200_0x31d200.cpp")
    with open(master_path, "w", encoding="utf-8") as mf:
        mf.write("// Master entry for sub_0031D200 -- dispatches to split chunks\n")
        mf.write(HEADER + "\n")
        mf.write("void sub_0031D200_0x31d200(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {\n")
        mf.write("#ifdef PS2_FUNCTION_LOG_TRACKER\n")
        mf.write('    PS_LOG_ENTRY("sub_0031D200_0x31d200");\n')
        mf.write("#endif\n")
        mf.write("    if (ctx->pc == 0u) ctx->pc = 0x31d200u;\n")
        mf.write("    switch (ctx->pc) {\n")
        for pc_hex in sorted(master_dispatch, key=lambda x: int(x, 16)):
            c = master_dispatch[pc_hex]
            mf.write(f"        case 0x{pc_hex}u: sub_0031D200_chunk_{c:04d}(rdram, ctx, runtime); return;\n")
        mf.write("        default: break;\n")
        mf.write("    }\n")
        mf.write("    // Analyzer-discovered entries past the compiled chunk range are still real ELF code.\n")
        mf.write("    // Interpret them instead of silently returning via $ra.\n")
        mf.write("    if (ctx->pc >= 0x31d200u && ctx->pc < 0x3d5a00u) {\n")
        mf.write("        interpretMipsKseg0(rdram, ctx, runtime, ctx->pc);\n")
        mf.write("        return;\n")
        mf.write("    }\n")
        mf.write("    // Unknown pc outside the giant-function range -- return via $ra.\n")
        mf.write("    ctx->pc = GPR_U32(ctx, 31);\n")
        mf.write("}\n")
    print(f"[split] Wrote master: {master_path}")

    # ------------------------------------------------------------------
    # Generate build_chunks.bat
    # ------------------------------------------------------------------
    bat_path = os.path.join(args.outdir, "build_chunks.bat")
    project_root = r"C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp"
    incdir1 = rf"{project_root}\tools\PS2Recomp\ps2xRuntime\include"
    incdir2 = rf"{project_root}\src\generated"
    incdir3 = rf"{project_root}\include"
    incdir4 = rf"{project_root}\build\_deps\raylib-src\src"
    incdir5 = rf"{project_root}\build\_deps\raylib-src\src\external\glfw\include"
    # ps2_stubs.h includes "Stubs/Unimplemented.h" via a path relative to
    # the Kernel lib dir.  Without this include path, clang-cl fails on
    # every chunk.
    incdir6 = rf"{project_root}\tools\PS2Recomp\ps2xRuntime\src\lib\Kernel"
    outdir_obj = rf"{project_root}\src\clang_objs"

    with open(bat_path, "w", encoding="utf-8") as bf:
        bf.write("@echo off\n")
        bf.write("setlocal enabledelayedexpansion\n\n")
        bf.write(r'call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul 2>&1' + "\n")
        bf.write(r'set PATH=C:\Program Files\LLVM\bin;%PATH%' + "\n\n")
        bf.write(f'set SRCDIR={args.outdir}\n')
        bf.write(f'set OUTDIR={outdir_obj}\n')
        bf.write(f'set INC1={incdir1}\n')
        bf.write(f'set INC2={incdir2}\n')
        bf.write(f'set INC3={incdir3}\n')
        bf.write(f'set INC4={incdir4}\n')
        bf.write(f'set INC5={incdir5}\n')
        bf.write(f'set INC6={incdir6}\n')
        bf.write(f'set INCCHUNKS={args.outdir}\n\n')
        bf.write('if not exist "%OUTDIR%" mkdir "%OUTDIR%"\n\n')
        bf.write("echo Compiling sub_0031D200 chunks with clang-cl...\n")
        bf.write("set FAILED=0\n\n")
        # /arch:AVX enables SSE4.1 (required for _mm_blendv_ps used in
        # VU0 vector-blend emulation, e.g. PS2_VBLEND macro).  Without
        # this flag clang-cl errors on every chunk that touches a VBLEND.
        bf.write('set CLANGFLAGS=/c /Od /bigobj /std:c++20 /EHsc /MD /arch:AVX\n')
        bf.write('set INCLUDES=-I"%INC1%" -I"%INC2%" -I"%INC3%" -I"%INC4%" -I"%INC5%" -I"%INC6%" -I"%INCCHUNKS%"\n\n')
        bf.write("echo   Compiling master dispatcher...\n")
        bf.write(f'clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUTDIR%\\sub_0031D200_0x31d200.obj" "%SRCDIR%\\sub_0031D200_0x31d200.cpp"\n')
        bf.write("if errorlevel 1 (echo   FAILED: master & set FAILED=1)\n\n")
        bf.write("echo   Compiling chunk files...\n")
        for c in range(actual_chunks):
            fname = f"sub_0031D200_chunk_{c:04d}"
            bf.write(f'clang-cl %CLANGFLAGS% %INCLUDES% -Fo"%OUTDIR%\\{fname}.obj" "%SRCDIR%\\{fname}.cpp"\n')
            bf.write(f"if errorlevel 1 (echo   FAILED: {fname} & set FAILED=1)\n")
        bf.write("\nif %FAILED%==1 (echo. & echo === SOME CHUNKS FAILED ===) else (echo. & echo All chunks compiled OK.)\n")
    print(f"[split] Wrote batch: {bat_path}")
    print("[split] Done.")


if __name__ == "__main__":
    main()
