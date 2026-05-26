# Binary Size Reduction Plan

**Goal:** racer_revenge.exe ≤ 10 MB (per project requirement).

**Current state (cycle 28 phase 30):** 125 MB at /Od, no LTO, debug symbols on.

For reference:
- Original PS2 ELF `SLUS_202.68`: 2.8 MB
- PCSX2 full-PS2 emulator (comparison): ~50 MB
- 10 MB target gives ~7 MB headroom over the original ELF for runtime + recompiled code expansion

## Current Size Breakdown (post-chunking, /Od)

| Component | Size | Notes |
|---|---|---|
| 3406 recompiled function .obj files | ~90 MB | The biggest contributor |
| PS2 runtime lib (`ps2_runtime`) | ~15 MB | Memory, GS, DMA, threading, syscalls |
| raylib | ~10 MB | Windowing, OpenGL, audio, input |
| sub_31D200 chunks (482 files) | ~10 MB | Post-strip (was 975 MB pre-strip) |

`125 MB exe` after linker dedup.

## Why /Od Is Currently Forced

`CMakeLists.txt` sets `/Od` on every `${GENERATED_SOURCES}` file. Historical reason: MSVC's optimizer OOMs on giant generated functions. Cycle 28 phase 31 confirmed this is still true after chunking — attempting `/O1` on a clean build, `cl.exe` allocated **3.6 GB RAM** on a single source file and stalled. Killed and reverted.

The OOM is caused by the same recompiler bloat pattern that the cycle-28 chunking + `tools/strip_inline_dispatch.py` resolved for sub_31D200: giant inline `switch (jumpTarget)` tables that the optimizer can't analyze in tractable memory.

## Five-Step Reduction Path (in ROI order)

### Step 1: Per-file optimization profile  →  ~60-70 MB
- Build a script that tries `/O1` on each generated `.cpp` with a RAM/time threshold
- Files that OOM or exceed N seconds get marked `/Od`-required
- Rest get `/O1`
- CMakeLists applies per-file COMPILE_FLAGS based on the resulting allowlist
- **Bounded, no recompiler changes.** Most files probably compile fine at /O1; only the ones with mega-dispatch tables OOM.

### Step 2: Apply dispatch-table strip to non-sub_31D200 functions  →  ~45-55 MB
- The 5-20 files identified in Step 1 as OOM-prone almost certainly have the same `switch (jumpTarget) + lookupFunction` redundancy that `tools/strip_inline_dispatch.py` fixed for sub_31D200
- Generalize that stripper to operate on any generated file
- After stripping, those files should compile at /O1 too
- **Mechanical, low-risk.**

### Step 3: LTO + strip debug symbols  →  ~30-40 MB
- Add `/GL` (whole-program optimization) to compile, `/LTCG` to link
- Drop debug info (or move to separate .pdb that isn't shipped)
- **Build-flag work.** Watch for /LTCG memory issues; same OOM class.

### Step 4: Trim raylib + runtime to actual surface area  →  ~25-35 MB
- raylib pulls in 3D mesh loaders, GLTF, OBJ, etc. we don't use
- The PS2 runtime has CDVD/audio/MemoryCard subsystems we don't yet exercise
- Configure both with module-disable flags (raylib has `-DSUPPORT_*=OFF` cmake options)
- **Link-time selectivity.**

### Step 5: Recompiler emission changes  →  ~5-10 MB
This is the only path to actually hit the 10 MB target. The recompiler currently emits verbose inline macros for every MIPS instruction. Example:
```cpp
// Current emission for a single `lb` instruction:
SET_GPR_S32(ctx, 0, (int8_t)READ8(ADD32(GPR_U32(ctx, 0), 828)));
// → compiles to ~30 bytes of x86 inlined
```
Replace with helper function calls:
```cpp
// Proposed:
op_lb(ctx, 0, 0, 828);
// → compiles to ~5 bytes of x86 (call + register load)
```
Multi-cycle work in the `ps2_recomp` submodule (not this repo). Would shrink the recompiled code 3-5x.

## Risks to Mitigate

- **Optimization may surface latent correctness bugs**: the recompiler emits code with implicit ordering dependencies (delay slots, hi/lo register pairs, vu0_vf SIMD lanes, `in_delay_slot` flag toggles). Each step that turns on optimization should be followed by a full smoke test comparison against the /Od baseline. The cycle-28 baseline (`logs/d200_stripped_long.txt`) is the reference: FrameDiag at frames 1/61/121/181/241 must match exactly, vif1Idx must increment normally, state6 must advance from 0xfff200 to 0xffffffff at frame=61, zero recover-pc events.

- **MSVC + LTO OOM**: even after Step 2 strips known mega-dispatchers, MSVC's LTO step may hit memory limits when handling the full 3400-file program. Mitigation: try /Os instead of /O2; or switch to clang-cl for the whole project (it tolerated the chunks better).

- **clang-cl vs MSVC ABI mismatch**: if Steps 1-4 require switching some files to clang-cl for optimization, watch for ABI / exception-handling incompatibilities. The current build already mixes both (chunks via clang-cl, rest via MSVC).

## When To Do This Work

NOT now. Size reduction is engineering hygiene and doesn't block progress toward visible gameplay. The next downstream-of-cycle-28 task is bridging the call chain into the newly-linked sub_31D200. Do Step 1 once gameplay is observable and the binary stabilizes.

## Verification Pattern (for each step)

```bash
# Before:
ls -la build/Release/racer_revenge.exe         # record size
timeout 15 build/Release/racer_revenge.exe > before.txt 2>&1

# Apply step

# Rebuild from clean:
rm -rf build/racer_revenge.dir
cmake --build build --config Release --target racer_revenge

# After:
ls -la build/Release/racer_revenge.exe         # measure delta
timeout 15 build/Release/racer_revenge.exe > after.txt 2>&1
diff <(grep FrameDiag before.txt) <(grep FrameDiag after.txt)  # must be empty
```

If the diff is non-empty, the optimization broke something. Revert and investigate.
