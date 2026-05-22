# PRIORITIES.md — How To Pick The Next Task

Read this at the start of every cycle. Pick exactly one task. Do not parallelize.

The priority tiers below are evaluated top-down. Find the lowest-numbered tier that contains an applicable task. Within a tier, use the tiebreakers listed at the bottom.

---

## Tier 0 — Foundation Stability

A broken foundation makes all other work invalid. Fix these first, always.

- **Build is red.** Anywhere in the chain: recompiler regeneration, runtime compile, generated code compile, link, or post-link. The build must be green at the end of every cycle. If it's red, that's your task.
- **Smoke test harness can't run silently** per MANDATE §5. Building or fixing the harness is Tier 0. Without it, you have no verification loop.
- **Headless / hidden-window mode is missing or broken.** Same reasoning — without it, you can't verify without stealing the user's focus.
- **`WORKLOG.md` shows the previous session ended with a "PAUSE — IN PROGRESS" entry.** Resume that work first.
- **Git is in a dirty/conflicted/detached state from a previous session.** Clean it up before any new work.
- **Submodule (`tools/PS2Recomp`) is missing or doesn't match the pinned commit.** Sync it.

---

## Tier 1 — Crash Path Advancement

The game's boot sequence has a known order (see CLAUDE.md "Key ELF Functions"). Whatever crashes, hangs, or returns early *first* in that sequence is the next priority.

Boot sequence in execution order:

1. `_start` (0x100008) — entry, sets $gp/stack, calls init chain
2. `boot_subinit` (0x239C40)
3. `boot_init2` (0x2F5E20)
4. `boot_overlay_init` (0x2FE7C0) → `kernel_overlay_loader` (0x2FE8D0) — copies 3 kseg0 overlays via syscall 0x5A
5. `setGameState` (0x2FDDF8) → installs frame dispatch + exception handlers
6. `frameDispatch` (0x2FE400) — VBlank-driven entry
7. `moduleManager_mainLoop` (0x302DF0) — infinite main loop
8. Per-frame chain:
   - `gs_dispatchHelper` (0x133660) → `module_renderPrep` (0x3075A8) → `renderList_manager` (0x30B4F0)
   - `gs_initState` (0x251B10) — first call inits GS, subsequent calls dispatch
   - `vif1_buildPacket` (0x257080) — builds VIF1 DMA packet, sets VIF1_MARK=1
   - `vif1_frameSubmit` (0x251DF0) — submits DMA, polls VIF1_MARK
   - `gs_waitCSRFinish` (0x258E70) — spins on GS_CSR FINISH bit
9. `vblank_waitGated` (0x2EB0C8) → `vblank_wait` (0x2F6030) or `vblank_waitCancellable` (0x2F60C0)

**Rule: whatever step the smoke test currently dies at, fix that.** Don't work ahead. If the kernel overlay loader can't install syscall handlers, there's no point implementing the GS yet — the game will never get to the per-frame chain.

How to find the current crash point:
- Run the silent smoke test with maximum trace logging enabled (add it if it doesn't exist — see Tier 0 by extension).
- Tail `logs\smoketest_*.log` for the last successful trace event or the exception/abort line.
- Cross-reference the last PC value (if logged) against CLAUDE.md's function map. That tells you which step you're stuck at.

If the smoke test does not produce a useful crash trace, **adding the trace infrastructure is Tier 0**, not Tier 1.

---

## Tier 2 — Pipeline Completeness Toward Polygons

Once the boot sequence reaches the per-frame loop without dying, polygons require these subsystems to be more than stubs:

- **VIF1 DMA path** — the recompiled code builds a VIF1 packet and submits it; the runtime needs to actually parse the packet (UNPACK commands, MPG microprogram loads, MSCAL execution triggers) and feed vertices to whatever stands in for the GS.
- **GS primitive submission** — the runtime needs to translate GS register writes (PRIM, RGBAQ, XYZ2, etc.) into draw calls. The minimum viable path: collect vertices into a buffer, on FINISH emit a draw call to raylib with positions and colors only. Textures, depth, blending all come later.
- **VU1 microcode execution** — VIF1 packets contain VU1 microprograms (the T&L code). Two options: (a) implement a minimal VU1 interpreter, (b) intercept known-good microprograms and short-circuit them with a CPU-side transform. CLAUDE.md notes VU1 is currently a stub. Pick option (b) for first polygons — log the microprogram contents, identify a single common one (likely a simple position+color passthrough or a basic perspective transform), and emulate just that one on the CPU. Real VU1 emulation is a later tier.
- **Module / render-list machinery** — `module_renderPrep` and `renderList_manager` need to actually populate the render list. If they're early-returning because some flag (like `gs_subsystem_ready_flag` at the address `gs_dispatchHelper` reads) is never set, find what sets it on real hardware and ensure the runtime sets it too.

Pick the one that's currently the blocker per the trace log. If the trace shows VIF1_buildPacket is never called, the problem is upstream in the module/dispatch layer — work on that.

---

## Tier 3 — Quality Of Life For The Work Loop Itself

These are valid tasks when no Tier 0–2 work is available *and* they will meaningfully accelerate future cycles. They are NOT valid procrastination from harder problems.

- Faster incremental builds (precompiled headers, smarter CMake targets, splitting the giant generated code into a static lib with cached objects).
- Better trace logging — structured logs, PC-to-function-name translation, ring buffers for crash diagnostics.
- Automated regression checks — re-run the last N smoke tests after each build to catch regressions.
- Tooling to diff generated code between regenerations (so a TOML change doesn't silently break something far away).

---

## Tier 4 — Beyond First Polygons

Do not touch any of this until the polygons milestone is hit:

- Audio (SPU2)
- Input (Pad — the runtime has raylib input ready, but it's not wired)
- CDVD / file I/O (the game currently has no disc to read from; asset extraction via `rr_asset_tool.py` is the bridge)
- Texture support
- IPU / MPEG decode for FMVs
- Full VU1 microcode interpreter / recompiler
- The 7 large excluded functions (see CLAUDE.md "Large functions excluded from build")
- The 4 missing function addresses (vtable fallback at 0x3c7b80, interior labels)
- The giant `sub_0031D200` (748KB single function)

---

## Tiebreakers Within A Tier

When multiple tasks share a tier:

1. **Highest information value.** A task whose outcome will tell you more about the system wins. A trace-adding task beats a refactor.
2. **Smallest blast radius.** A change touching one file beats one touching ten.
3. **Reversible over irreversible.** A change you can revert in one commit beats a multi-commit migration.
4. **Build-time impact.** A change that doesn't trigger full regeneration beats one that does.
5. **Documented over guessed.** A task where CLAUDE.md or the function map gives you a clear target beats one where you'd be inferring intent from instruction patterns.

---

## Anti-Priorities (Things That Look Important But Aren't, Yet)

- Making the code "clean." It doesn't need to be clean. It needs to run.
- Naming things. Generated functions have generated names. Live with it. Renaming is Tier 4 minimum.
- Performance. The PS2 ran at 294 MHz with 32MB RAM. A modern PC has headroom of multiple orders of magnitude. Profile only if something is actually too slow to iterate on.
- Cross-platform support. Windows + MSVC only. Don't add Linux/Mac branches unless explicitly told.
- Documentation outside `WORKLOG.md` and inline `// VA 0xXXXXXX:` comments where you've made non-obvious changes. No README updates. No design docs.
- Replacing the recompiler. PS2Recomp is the toolchain. If it has a bug, work around it in TOML config; only patch the recompiler itself as an absolute last resort, and log it heavily.
