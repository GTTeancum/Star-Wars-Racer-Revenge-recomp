# Handoff to Codex

**As of commit `b443c81` (2026-05-25 22:00)** — cycle 28 closed with sub_0031D200 (the 748KB game-logic dispatcher) executing per frame for the first time in project history.

Read AGENTS.md, then this file, then `WORKLOG.md` (newest entries on top — read the cycle-28 phase 8 onward block).

---

## The one-sentence summary

The architectural blocker that has dominated this project since cycle 1 (sub_31D200 couldn't be compiled, so no game logic could run) was **resolved end-to-end in cycle 28**. The function is now compiled (chunked, stripped, linked) and runs every frame alongside the rendering pipeline. The plateau debugging has shifted from *"how do we get sub_31D200 to exist"* to *"why does sub_31D200's internal dispatch self-loop without advancing further."*

---

## Current runtime state (reproducible)

Build is current. To verify:

```bash
cmake --build build --config Release --parallel
timeout 15 build/Release/racer_revenge.exe > /tmp/check.txt 2>&1
grep -E 'FrameDiag.*frame=(241|361)' /tmp/check.txt
```

Expected output (key fields):
```
[FrameDiag] frame=241 state=0x31d200 ... vif1Idx=0x68 ... state6=0xffffffff modT0=0x16
[FrameDiag] frame=361 state=0x31d200 ... vif1Idx=0xa3 ... state6=0xffffffff modT0=0x16
```

Plus, earlier in the log:
```
[BridgeExp] frame=90 calling sub_31D200(pc=0x31d200)...
[BridgeExp] sub_31D200 returned cleanly; new 0x384670=0x251b10
[BridgeExp] forcing 0x384670=0x31D200 (game-logic state-fn install)
```

Zero `recover-pc` events in stderr. exe size: **125 MB**.

---

## What "running per frame" means in code

`src/main.cpp` `gameFrameLoop`:

1. Every frame, read game state fn ptr from `rdram[0x384670]`.
2. From frame 90 onward, force-install `0x384670 = 0x31d200` (one-shot via `s_d200Tried` atomic — see "PHASE 34/35 BRIDGE" block in main.cpp).
3. Each subsequent frame:
   - Call `gs_initState` (0x251B10) in its own R5900Context to drive VIF1 packet build.
   - Call `sub_31D200_0x31d200(pc=0x31d200)` as the state fn.
   - Call `vif1_frameSubmit` (0x251DF0).

This dual-call pattern is the live state. Don't undo it without a replacement strategy.

---

## The chunking pipeline (sub_31D200)

The 2.2 GB `src/generated/sub_0031D200_0x31d200.cpp` is too big for any compiler. It's split into 482 chunks under `src/generated_chunks/` (gitignored). Pipeline:

```bash
# Regenerate chunks from scratch (~5 min):
python tools/split_giant_function.py --max-mb 2.0

# Patch 4 known recompiler bugs (3 eret + 1 source truncation):
python tools/fix_chunk_recompiler_bugs.py

# Strip redundant inline JR/JALR dispatch tables (5.7 MB → 2.7 KB per mega-chunk):
python tools/strip_inline_dispatch.py

# Compile all 482 chunks with clang-cl into src/clang_objs/ (~5 min):
src/generated_chunks/build_chunks.bat

# Measure chunk sizes:
python tools/measure_chunks.py
```

Resulting state on disk:
- `src/generated_chunks/`: 482 chunk .cpp files + master `sub_0031D200_0x31d200.cpp` + `sub_0031D200_chunks.h` + `build_chunks.bat`
- `src/clang_objs/`: 482 chunk .obj files + master .obj (total ~16 MB)

CMakeLists.txt globs `src/clang_objs/sub_0031D200_chunk_*.obj`. When present, `D200_CHUNKS_AVAILABLE` is defined, which makes `src/large_function_stubs.cpp` skip its interpreter-fallback definition of `sub_0031D200_0x31d200` (otherwise duplicate symbol).

**If chunks are missing**, the build still works — falls back to MIPS interpreter stub at /Od. Slow but correct.

---

## Tools added this cycle

| Tool | Purpose |
|---|---|
| `tools/split_giant_function.py` | Splits 2.2 GB generated .cpp into chunks. **Modified**: added `--max-mb` byte-aware bin-pack mode (default 0 = legacy). Use `--max-mb 2.0`. |
| `tools/measure_chunks.py` | Reports chunk size distribution (verifies splitter changes). |
| `tools/fix_chunk_recompiler_bugs.py` | Post-processor: patches the eret pattern (missing `} else { ... }`) and source-truncation pattern. Idempotent. |
| `tools/strip_inline_dispatch.py` | Removes redundant `switch (jumpTarget)` blocks from chunks (the runtime's `lookupFunction()` handles dispatch already). 1.4 GB → 16 MB saved. Idempotent. |
| `tools/test_chunk_compile.bat` | Quick sanity: compile master + chunk_0000 + chunk_0019 with clang-cl. |
| `tools/test_chunk_compile_failures.bat` | Quick sanity: compile the 4 historically-failing chunks (0264, 0452, 0464, 0481). |

---

## Critical files modified this cycle

- `src/main.cpp` — boot callback overrides (cb[0], cb[3], cb[7], cb[10], cb[11], cb[13], cb[18], cb[31], cb[34], cb[42], cb[45], cb[57], cb[62], cb[65] = 14 specific + 51 type-A bulk), chain tracers, bridge experiment, dual-call frame loop, FrameDiag with `modT0` readout.
- `CMakeLists.txt` — `D200_CHUNKS_AVAILABLE` conditional define; `/Od` retained on generated sources with OOM warning comment.
- `src/large_function_stubs.cpp` — `sub_0031D200_0x31d200` guarded by `#ifndef D200_CHUNKS_AVAILABLE`.
- `tools/split_giant_function.py` — byte-aware split + `/arch:AVX` in generated batch + INC6 Kernel include path.
- `CLAUDE.md` — VIF1_MARK clarification (hardware-set, not software); sub_2F57C0 correction (it's `ExitThread` syscall, not interior label of sub_002F5538); sub_31D200 black-hole architectural finding (now partially obsolete after this cycle).

---

## Three project memories (Codex should know these, reproduced inline)

### 1. VIF1_MARK is hardware-set, not software

Across all generated .cpp files, **zero** write to `0x10003C30` (VIF1_MARK). On real PS2, it's set by the VIF1 hardware unit when it processes a MARK opcode in a DMA chain. The recompilation has no VIF1 interpreter, so MARK never naturally fires. Forcing `VIF1_MARK=1` (cycle 28 phase 8, reverted) makes `vif1_frameSubmit` engage but it JALRs into garbage because the chain build is incomplete. The current GIF DMA "2D" path (`sub_2596A0`) works and is what renders the test triangle.

**Do not** retry the VIF1_MARK force-engage without first implementing a VIF1 microcode interpreter.

### 2. Helper-call instability via lookupFunction

Calling recompiled helpers from C++ overrides via `runtime->lookupFunction(VA)(rdram, ctx, runtime)` is fragile. Phase 7 found that `cb[11]` worked (helper `func_299130`) but `cb[12]` (`func_24CF80`) and `cb[31]` (`func_299130` with different args) caused cascading recover-pc cascades. Hypothesis: stack-frame contract mismatch (the helper's `lw $ra, 0($sp); jr $ra` reads garbage if the caller's $sp invariants differ from the recompiled helper's expectations).

**Pattern to use**: prefer pure data-only translations (just `sw`/`sd`/`sq` to fixed rdram addresses). If you must invoke a helper, set `$sp = 0x44BC80` (EE stack region) explicitly and verify the helper's prologue/epilogue matches your context.

### 3. sub_31D200 is the architectural game-logic black hole (now partially obsolete)

`sub_0031D200` (748 KB, 55627 labels) is the only consumer of `modTable[0]` (rdram[0x385160]) — module 6 writes 22 there when state[6]=`-1`, and only sub_31D200 reads it. Before cycle 28 phase 29, this function was excluded from the build (2.2 GB output otherwise) so no downstream consumer existed for any module-state work.

**Now**: sub_31D200 is compiled (chunked) and linked, and runs per frame from frame=90 onward (bridge installed in `gameFrameLoop`). The memory entry remains correct about the *architectural* dependency, but the *blocker is now crossed*.

---

## What's STILL not working (deliberately)

| Subsystem | Why it doesn't work | What it blocks |
|---|---|---|
| **VIF1 3D rendering** | Needs VIF1 microcode interpreter. The chain build relies on VU1 microcode also missing. | Any 3D content. The visible test triangle uses the simpler GIF DMA 2D path. |
| **CDVD** | Stub returns "no disc". No IOP module loader, no texture streaming, no save/load. | All disc-loaded content; IOP GFX module (which would write capA/capB naturally — currently stubbed to 0x100 in main.cpp Phase 5b). |
| **SIF/IOP/RPC** | Skeletal stubs only. `sif_dmaSend` confirmed sending ASCII log messages to "IOP" but no real module exists. | State[6]'s natural transition to 0xFFFFFFFF (we synthesize via the FFF200 sentinel — see CLAUDE.md state machine section). |
| **VU0 microcode mode** | Partial (handled in recompiled code). | A few specific games — mostly fine for this game. |
| **VU1 microcode** | Stub. | 3D rendering chain. |
| **SPU2 audio** | Stub. | All audio. |
| **Pad input** | raylib hooked up but pad subsystem not wired. | Controller input. |

---

## What's frozen / don't touch without reason

- **`/Od` on generated sources** — `cl.exe` OOM at 3.6 GB RAM on `/O1` (cycle 28 phase 31). See `SIZE_REDUCTION.md` for the full plan; do NOT try `/O1+` again without addressing per-file profiling first.
- **CDVD-stub capA/capB = 0x100** — written in main.cpp Phase 5b. Unlocks `vif1_buildPacket` capacity gate. Removing it stalls `vif1Idx` at 0.
- **cb[13] partial translation** — skips the capA/capB clears specifically. If you re-translate cb[13] faithfully, you undo the CDVD stub.
- **65 of 66 boot callbacks translated** — only cb[30] remains, and it needs `inject_extra_entry_points.py` for fn target 0x29F190 (an interior label) + helper-call instability solution. Don't translate cb[30] without addressing both.
- **`build/Release/racer_revenge.exe` is 125 MB** — known, accepted, plan in `SIZE_REDUCTION.md`. Target at ship time is 10 MB. **Not** a current-cycle concern.

---

## Where to pick up (the actual next investigation)

**The question**: sub_31D200 runs every frame but `0x384670` stays at `0x31d200` (sub_31D200 self-loops). Why doesn't its internal dispatch advance to a different state?

**Hypothesis space** (ranked by likelihood):

1. sub_31D200's `modTable[0]==22` branch goes deep enough to hit a TODO_NAMED stub that returns early, never reaching the `setGameState` call. Investigate by instrumenting which interior label sub_31D200 reaches before returning.

2. sub_31D200 expects an additional module slot (beyond 6) to be in a specific state. StateDump every frame shows only slot [6] populated. Some path may be waiting for slot [N] = some value that nothing is writing.

3. sub_31D200 polls an IOP response (rdram[some address]) that never arrives without SIF.

4. sub_31D200 expects a callback registered via one of the interior labels (one of the 558 entry points). Some upstream code should populate the function pointer; that upstream code may itself be unreached.

**Suggested first probe** (cheap, informative): instrument sub_31D200's master dispatcher to log which interior labels are hit. Look at `src/generated_chunks/sub_0031D200_0x31d200.cpp` — it's a `switch (ctx->pc)` over the original entry points. Add a per-PC counter and dump after 100 frames. Whichever entries are hit most tell you the loop sub_31D200 is stuck in.

---

## Build / verify commands (full reference)

```bash
# Full clean build:
rm -rf build && cmake -S . -B build -G "Visual Studio 17 2022" && cmake --build build --config Release --parallel

# Incremental (after main.cpp edit):
cmake --build build --config Release --target racer_revenge

# Headless smoke (15 sec):
timeout 15 build/Release/racer_revenge.exe > logs/check.txt 2> logs/check_err.txt

# Key state checks:
grep BridgeExp logs/check.txt           # sub_31D200 bridge fired
grep ChainTrace logs/check.txt           # which chain functions ran
grep 'FrameDiag.*frame=(241|361)' logs/check.txt   # late-frame state
grep -c 'recover-pc' logs/check_err.txt  # MUST be 0 for stable baseline
```

If `chunks` need rebuilding (you changed sub_31D200's source or the splitter):

```bash
rm -rf src/generated_chunks/sub_0031D200_chunk_*.cpp src/generated_chunks/sub_0031D200_0x31d200.cpp src/generated_chunks/sub_0031D200_chunks.h src/generated_chunks/build_chunks.bat
rm -f src/clang_objs/sub_0031D200_*.obj
python tools/split_giant_function.py --max-mb 2.0
python tools/fix_chunk_recompiler_bugs.py
python tools/strip_inline_dispatch.py
src/generated_chunks/build_chunks.bat   # ~5 min
python tools/measure_chunks.py           # verify
```

---

## Git state

- Branch: `master`, **89 commits ahead of origin/master**.
- Latest commit: `b443c81` (tools/test_chunk_compile.bat dir fix; cosmetic).
- Last substantive commit: `8391d0c` (WORKLOG closing cycle 28).
- Submodule `tools/PS2Recomp` has uncommitted local changes (unrelated to this cycle's work — check `git -C tools/PS2Recomp status`).
- Nothing else uncommitted.

Push when ready: `git push origin master`.

---

## Personality / process reminder

- Steve values **honesty over encouragement**. Push back on bad assumptions.
- **Supply full files**, never partial snippets. Steve copy/pastes; he doesn't edit.
- **Ground all findings in the ELF** — don't assume; verify with `tools/decode_mips.py 0xADDR N`.
- `WORKLOG.md` is the substitute for asking Steve questions. Log decisions there as you go.
- Boot callback / chain investigation is "find evidence in the binary, then act" — no speculation phases.
