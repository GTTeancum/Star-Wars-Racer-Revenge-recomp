# WORKLOG — Racer Revenge Recomp

Append-only journal. **Newest entries at the top.** Each session starts with a "SESSION START" marker and ends with a "SESSION PAUSE" or "SESSION END" marker.

This file is the substitute for asking Steve questions. Every decision that would have been a question to him is instead a `decision` entry here.

---

## Entry Format

```
## [YYYY-MM-DD HH:MM] <short title>
**Type:** plan | result | decision | blocker-resolved | milestone | session-marker
**Cycle:** <n>
<body — terse, evidence-linked>
```

## Entry Types

- **plan** — what you're about to do, what evidence you'll use to verify it, what the rollback condition is. 3–8 lines.
- **result** — what happened. Include build log path, smoke test log path, screenshot path if applicable. Trim raw output — link, don't paste.
- **decision** — a judgment call made in lieu of asking Steve. Format: *Question I would have asked / Answer I chose / Reasoning / What I'd change if this turns out wrong.*
- **blocker-resolved** — something that looked like it needed Steve, resolved from the code instead. Note what made it look like a blocker and what made it not one. These prove the autonomy mandate is working.
- **milestone** — significant progress. First green build. First instruction executes. First syscall handler completes. First DMA packet built. First triangle. First textured triangle. Etc.
- **session-marker** — `SESSION START`, `SESSION PAUSE`, `SESSION END`. Pause means resumable; End means a logical chapter is complete.

## House Rules

- Keep entries terse. The point is traceability, not prose. If you need more than ~15 lines, you're probably narrating instead of logging.
- Always include the commit hash if the entry corresponds to one.
- Always include log file paths when a build or smoke test ran. Format: `logs/build_20260521_1430.log`, `logs/smoketest_20260521_1432.log`.
- Don't paste large hex dumps or stack traces. Save them to `logs/` and reference the path.
- If you make a `decision`-type entry, the reasoning section is mandatory. No silent decisions.

---

<!-- ENTRIES BELOW THIS LINE — NEWEST FIRST -->

## [2026-05-21 20:50] SESSION START

**Type:** session-marker
**Cycle:** 1

First real work session. Steve is offline. Working from MANDATE.md autonomy
contract. No prior cycle context to resume.

## [2026-05-21 21:04] Cycle 1 plan: bring up the toolchain + headless harness

**Type:** plan
**Cycle:** 1

Repo state at session start:
- ELF only present under `PS2_game/SLUS_202.68`; root path the toolchain expects is empty.
- `src/generated/` does not exist — ps2_recomp has never been run here.
- `tools/PS2Recomp/build/` does not exist — toolchain not built.
- No silent smoke-test mode in `main.cpp`; `tools/smoke_test.py` launches the exe in a visible window.

Cycle plan (Tier-0 only — nothing else is valid until these pass):
1. Stage ELF at `./SLUS_202.68` (copy from PS2_game/).
2. Build PS2Recomp toolchain (analyzer + recomp). Likely first-time build on this machine.
3. Run `ps2_recomp` to regenerate `src/generated/*.cpp` (~3400 files).
4. Add `--headless --frames N --screenshot PATH --runtime-seconds N` modes to `main.cpp`; patch `ps2_runtime` with a post-frame hook so the runtime can be bounded + screenshotted without forking it.
5. Update `tools/smoke_test.py` to always pass `--headless` and `CREATE_NO_WINDOW`.
6. Clean build the runtime; capture log to `logs/`.
7. Run the silent smoke test; identify Tier-1 crash point from the trace.

Rollback: if recompiler regen produces broken code, revert to the prior `src/generated/` (none on disk — so the only safe rollback is git-reset of the TOML, which we have not touched). No risk to ELF or git history.

## [2026-05-21 21:05] ELFIO upstream pin no longer exists

**Type:** blocker-resolved
**Cycle:** 1

`tools/PS2Recomp/ps2xRecomp/CMakeLists.txt` pinned ELFIO at commit
`7d30a22fc5aac06adfe7887ae57f3701b6b5f913`, which is no longer reachable
upstream. CMake/git fail with `unable to read tree`. Bumped the pin to
`8ae6cec5d60495822ecd57d736f66149da9b1830` (ELFIO Release_3.12 tag, verified
via the GitHub tags API). The ELFIO API surface PS2Recomp uses (elfio,
section, segment, symbol_section_accessor, etc.) is stable across these
releases. CMake reconfigure + build succeeded after the bump.

This is a submodule edit, not a recompiler logic change — recorded here in
case the submodule is ever re-pinned upstream and the diff needs to be
re-applied.

## [2026-05-21 21:07] Toolchain built; src/generated regenerated

**Type:** result
**Cycle:** 1

- `ps2_analyzer.exe`, `ps2_recomp.exe`: built clean under `tools/PS2Recomp/build/`.
- Ran `ps2_recomp config/racer_revenge.toml`. Log: `logs/ps2recomp_gen_20260521_2057.log`.
- Output: 3403 files in `src/generated/`, ending with "Recompilation completed successfully".
- One expected entry: `Skipping function (runtime TODO wrapper): sub_0031D200` — the 748KB giant flagged in CLAUDE.md.

## [2026-05-21 21:08] Headless smoke-test infrastructure added

**Type:** result
**Cycle:** 1

MANDATE §5 requires silent smoke tests. Implementation:

- `ps2_runtime.h`: added `PostFrameHook = std::function<bool(uint64_t)>` and `setPostFrameHook()`. Stored as `m_postFrameHook`. Returning `true` from the hook calls `requestStop()`.
- `ps2_runtime.cpp::run()`: invokes the hook once per host frame after `EndDrawing()`. Static frame counter in the call site (run() is single-instance).
- `src/main.cpp`: new `parseArgs()` for `--headless`, `--frames N`, `--screenshot PATH`, `--runtime-seconds N`. `--headless` calls `SetConfigFlags(FLAG_WINDOW_HIDDEN | FLAG_WINDOW_UNFOCUSED)` before `runtime.initialize()`. `--screenshot` is wired through the post-frame hook so `TakeScreenshot` runs on the GL-owning thread.
- `tools/smoke_test.py`: now always passes `--headless --runtime-seconds N` and uses `CREATE_NO_WINDOW` on Windows.

Decision points logged below.

## [2026-05-21 21:09] Decision: patch ps2xRuntime rather than wrap it

**Type:** decision
**Cycle:** 1

*Question:* should the headless / screenshot hook live in main.cpp wrapping
the runtime, or as a thin extension point in ps2xRuntime?

*Answer:* extension point in ps2xRuntime (10-line patch: header + cpp).

*Reasoning:* `TakeScreenshot` must run on the GL-owning thread, which is
the thread inside `PS2Runtime::run()`. Without a hook, the only options
were (a) spawn a separate raylib instance (impossible — single GL ctx) or
(b) busy-wait on a stop flag and screenshot from inside main, but main is
blocked on `runtime.run()`. The hook is the minimum invasive addition.

*What I'd change if this is wrong:* the hook is opt-in (nullptr by default),
so the runtime behaviour is unchanged for non-test runs. Reverting is a
2-file diff if PS2Recomp upstream picks up a similar feature.

## [2026-05-21 22:00] First silent smoke test — boot dies on 0x2f7468

**Type:** result
**Cycle:** 1

`logs/smoketest_20260521_2200.log` (157 lines headless capture).
- Runtime boots, raylib initialises with FLAG_WINDOW_HIDDEN, OpenGL ctx ok.
- Game thread enters _start → boot chain → reaches a function-pointer table
  iteration that bottoms out at `Function at address 0x2f7468 not found`.
  Then a stub-throw on `sub_002F7E20` kills the game thread →
  `g_activeThreads → 0` → runtime exits within ~3s. The 5.5s Diag dump
  never fires, so the golden parse sees `game_state_fn=None` and fails 20/20.
- Smoke test: 0/20 checks pass.

Root cause: PS2Recomp v0.4 does not parse `[[extra_entry_points]]` from the
TOML. CLAUDE.md and main.cpp comments imply the feature exists, but a
`grep -r extra_entry_points tools/PS2Recomp/` returns *zero* hits in the
recompiler source. The TOML key is silently ignored. 0x2F7468 (interior
label inside sub_002F7430) is never registered as an alias, and the
containing function's `switch (ctx->pc)` lacks a case for it — so the
dispatcher recovers a few times then gives up.

## [2026-05-21 22:09] Decision: post-process generated .cpp instead of patching the recompiler

**Type:** decision
**Cycle:** 1

*Question:* should I add `[[extra_entry_points]]` support to PS2Recomp
proper (config_manager.cpp + code_generator.cpp), or post-process the
generated .cpp files from a Python script?

*Answer:* Python post-processor (`tools/inject_extra_entry_points.py`).

*Reasoning:*
- The recompiler patch would be invasive (config struct field, TOML key
  parsing, analysis-step injection, plus emission for both the switch case
  and the alias `registerFunction`). High blast radius, multi-file
  recompiler build dependency.
- The post-processor is a single self-contained script. Idempotent. Runs
  after `ps2_recomp`. Inserts `case 0xAAu: goto label_AA;` into the
  switch, prepends `label_AA:` before the instruction comment, and emits
  `src/generated/extra_entry_points_register.cpp` with trampoline
  registrations.
- If PS2Recomp upstream ever adds the feature, removing the script is a
  one-line CI change.

*What I'd change if this is wrong:* if the regex-based patcher breaks on
some unusual function shape (e.g. one without a switch and without the
`ctx->pc = 0xstart` anchor), the recompiler patch becomes the better
option. Easy to detect — the script emits WARN lines for skips.

## [2026-05-21 22:13] extra_entry_points injected; boot reaches per-frame loop

**Type:** result
**Cycle:** 1

`logs/smoketest_20260521_2220.log` (1,178,280 lines — heavy syscall
warning + recover-pc spam, runtime fault path).
- Patched 9 generated .cpp files; emitted 12-trampoline register file.
- Boot output now shows the full bootstrap chain: `[Bootstrap] 3-second
  delay elapsed`, `[Bootstrap] 0x384670 (state fn) = 0x251b10`, GS
  bootstrap, `[FrameDiag] frame=1 state=0x251b10`.
- Game thread enters the per-frame loop. ResetEE syscall fires (real PS2
  EE-reset syscall #1 — already commented out, harmless). The dispatcher
  hits `Error: Called unimplemented function at address 0x2f57c0`, which
  in v0.4's runtime calls `runtime->requestStop()` → kills the runtime
  before the Diag dump can run.

0x2F57C0 is one of dozens of tiny `$v1=N; syscall 0; jr $ra` wrappers that
the analyzer rolls into `sub_002F5538` (stubbed in TOML as "heavy loops" —
likely mislabelled, but irrelevant: the stub throws on first call too).

## [2026-05-21 22:33] Decision: unimplemented-function → silent noop instead of hard stop

**Type:** decision
**Cycle:** 1

*Question:* should an unregistered address kill the runtime, or be
treated as a no-op return?

*Answer:* no-op return (set `ctx->pc = ra`), with a one-shot warning per
address.

*Reasoning:*
- MANDATE §7: "Stub it with the minimum behavior that doesn't crash, log a
  TODO with the VA it relates to, and move on. Real implementation happens
  when something downstream demands it."
- The current behaviour killed every single smoke test on the first
  uncovered interior address. With no way for the game to make progress,
  every Tier-1 bug looks identical from the outside (`exit code 0,
  game_state_fn=None`). Converting it to noop+log lets the next failure
  surface, which is what the priority rules want.
- The warning is deduplicated per-address via `std::unordered_set`, so the
  log stays usable.

*What I'd change if this is wrong:* if a no-op'd function was supposed to
set $v0 in a way the caller relies on, the caller may misbehave further
downstream. That's a strictly better diagnostic position than "process is
dead", so we accept the trade. The hard-stop can be re-enabled by reverting
~30 lines in `tools/PS2Recomp/ps2xRuntime/src/lib/ps2_runtime.cpp`.

## [2026-05-21 22:47] Smoke test: 16/20 passing — first end-to-end pipeline frame

**Type:** milestone
**Cycle:** 1

Headless smoke test results vs `config/smoke_test_golden.json`:

| Check | Status |
|---|---|
| `game_state_fn = 0x00251b10` | PASS |
| dispatch_table[1..3] = 0x002fe300 | **FAIL** (None) |
| dispatch_table[16..30] (11 slots) | PASS |
| overlay 0x80074000 | **FAIL** (got `0 0 0 0`) |
| overlay 0x80075000, 0x80076000, 0x82000, 0x1F20000 | PASS |
| ResetEE spam < 10 | PASS (0) |
| exception count < 2 | PASS (1) |
| fallback count < 100 | PASS (11) |

Net: 16/20 vs 0/20 at start of cycle. The runtime now runs to the 10s
watchdog without dying.

Screenshot captured: `build/Release/cycle1_visible_test.png` (raylib's
TakeScreenshot strips the directory component from the path arg —
files-up next to the exe). Content: uniform MAGENTA 640x448.

That magenta is the `UploadFrame` fallback in `ps2_runtime.cpp:455` — the
GS host-presentation buffer never gets latched, so `copyLatchedHost
PresentationFrame` returns false on every host frame. The PS2 framebuffer
exists in GS VRAM (or doesn't, depending) but the runtime's GS subsystem
isn't presenting it. This is the Tier-2 polygons blocker: not boot, not
dispatch, not syscalls — the GS pipeline itself.

Smoke test log: `logs/smoketest_20260521_2247.log` (post runtime-tweak).
Headless screenshot policy: every 60 frames + on last frame.

## [2026-05-21 22:55] Cycle 1 pause — next is GS host-presentation

**Type:** plan
**Cycle:** 2

Next-cycle pick per PRIORITIES.md tiebreakers:
- "Highest information value": GS latch path. Either we find that the GS
  is genuinely not receiving GIF DMA submissions (boot-side bug), or it
  is but `latchHostPresentationFrame` doesn't pick them up
  (runtime-side bug). Either answer constrains the entire Tier-2 surface.
- The dispatch_table[1..3] gap is informationally smaller (cosmetic delta
  vs golden) and the 0x80074000 overlay gap is likely just a memcpy timing
  issue — both deferred behind the GS work.

Next plan:
1. Trace whether `func_257080` (VIF1 packet builder) is being called.
2. Trace whether GIF DMA channel 2 packets are reaching the GS.
3. Check `GS::latchHostPresentationFrame` for what input it expects.
4. Confirm whether the test_state_fn one-shot at bootstrap reaches the
   GS framebuffer (it submits an explicit GIF DMA).

If GIF traffic IS reaching the GS but the host latch isn't picking it up,
the fix is in `ps2_runtime/src/lib/gs/`. If GIF traffic ISN'T reaching the
GS, the fix is in the boot/state-machine path.

## [2026-05-21 23:15] GS latch diagnostic added; cause identified

**Type:** result
**Cycle:** 2

Added one-shot `[gs:latch-fail]` trace at the failure point in
`latchHostPresentationFrame()`. First smoke run after rebuild:

```
[gs:latch-fail] pmode=0x0 smode2=0x0 dispfb1=0x1400 dispfb2=0x1400
display1=0x1bf27f00000000 display2=0x1bf27f00000000 bgcolor=0x0
enableCrt1=0 enableCrt2=0 hasDisplaySetup1=1 hasDisplaySetup2=1
```

DISPFB1 + DISPLAY1 are programmed — `hasDisplaySetup1=1`. PMODE is zero,
both CRTs nominally disabled. Latch refuses to present, returns magenta.
The game writes the display-frame registers via memory stores but never
touches PMODE. `GsSetCrt` syscall would set PMODE bit 0, but the game
doesn't issue it before the per-frame loop begins reading the GS.

## [2026-05-21 23:22] Bootstrap PMODE force is insufficient — game re-clears it

**Type:** result
**Cycle:** 2

Added bootstrap-side `runtime->memory().gs().pmode |= 1` in main.cpp
alongside the other Initialized writes. Log confirms it fires
("[Bootstrap] Forced GS PMODE.EN1=1 (was 0)"), but the next
`[gs:latch-fail]` trace immediately shows `pmode=0x0` again — something
in the boot path writes pmode back to 0 (likely the GS init function
zeroing the register block as part of reset).

## [2026-05-21 23:25] Decision: relax CRT-validity check to hasDisplaySetup

**Type:** decision
**Cycle:** 2

*Question:* track down what's clearing PMODE and patch it, or relax the
latch's validity rule?

*Answer:* relax the latch (treat `hasDisplaySetup1` as sufficient,
ignore the PMODE enable bit).

*Reasoning:*
- The PMODE-clearing writer is somewhere deep in the GS init path; finding
  it requires deep MIPS/Ghidra work. Hours, not minutes.
- The latch relaxation is one line, perfectly opt-in to this game's
  behavior, and the trade is minor (a game that genuinely wants to blank
  the CRT during mode-switch will appear unblanked — acceptable for now).
- Per PRIORITIES.md tiebreaker #1: highest information value. The relax
  immediately tells us whether the GS has rasterized output. If yes,
  polygons. If no, the gap is elsewhere.

*What I'd change if this is wrong:* if the game depends on PMODE-blank
semantics (e.g. for fades), this will show wrong frames in those
windows. Revert is one line in `latchHostPresentationFrame()`.

## [2026-05-21 23:26] MILESTONE — POLYGONS ON SCREEN

**Type:** milestone
**Cycle:** 2

`docs/polygons_milestone_cycle2.png` (committed; mirror of
`logs/screenshots/polygons_milestone_cycle2.png` and
`build/Release/cycle2_crt_relax.png`).

Content: a solid mint-green rectangle on black background, dimensions
~440×248 pixels, positioned top-left. This is the synthetic
`test_state_fn` sprite — submitted as a GIF DMA chain at bootstrap with
PRIM=sprite, XYZ2 TL=(100,100), XYZ2 BR=(540,348) in screen pixels
(matches the geometry observed in the screenshot). RGB cycles per
frame; this capture caught it on a green phase.

This is the first time the PS2 GS pipeline has end-to-end produced
visible host-framebuffer content in this repo. The chain is:

  test_state_fn → GIF DMA chan 2 → m_pendingGifTransfers →
  submitGifPacket → GS processGIFPacket → rasterize into VRAM →
  latchHostPresentationFrame → copyLatchedHostPresentationFrame →
  UploadFrame → raylib DrawTexturePro → TakeScreenshot.

Build commit: will record after this commit lands. Headless run,
FLAG_WINDOW_HIDDEN, --runtime-seconds 10, --screenshot every 60 frames.

Smoke test status: still 16/20 (the 4 outstanding failures —
dispatch_table[1..3] and overlay@0x80074000 — are not in the polygons
path and remain for the next session).

**Type:** session-marker
**Cycle:** 0

Initial WORKLOG.md committed alongside MANDATE.md, PRIORITIES.md, and SESSION_BOOTSTRAP.md. No work performed yet. Next Claude Code session begins from here.
