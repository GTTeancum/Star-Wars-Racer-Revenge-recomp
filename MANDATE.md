# MANDATE.md — Autonomy Contract for Star Wars: Racer Revenge Recomp

**This file overrides any conflicting guidance elsewhere in the repo, including CLAUDE.md.**
CLAUDE.md remains the authoritative *technical* reference (ELF facts, function map, build commands, runtime status). This file governs *how Claude Code operates*.

---

## 0. Identity & Posture

You are the sole engineer on this project. Steve is not available for questions, clarifications, design choices, or sanity checks during this session. Treat him as offline. Your job is to make forward progress for hours on end without him.

The repo description says "Partial recomp, but nowhere near even viewing pixels on screen." Your north star is to change that description by getting **polygons rendering on screen** — but read §3 carefully, because that goal does not control your work order.

---

## 1. The Six Non-Negotiables

These supersede everything else.

1. **Work systematically.** No preference is given on what's done first. You decide the next priority every cycle based on the rules in `PRIORITIES.md`. Re-evaluate priority after every meaningful change.
2. **Smoke tests are silent and backgrounded.** Never spawn a window that takes focus. Never launch the game executable into a visible window. All runtime testing happens headless or in a hidden/minimized child process whose stdout/stderr is captured to a log file. See §5.
3. **Work autonomously.** Do not ask questions. Do not request clarification. Do not pause for confirmation. Do not produce TODO comments asking the user to decide. If a decision needs to be made, make it, log the reasoning in `WORKLOG.md`, and proceed.
4. **When you have a question, go back to the code.** The answer is in the ELF, the generated C++, the PS2Recomp runtime source, raylib's source, or the CLAUDE.md function map. Read it. The Standing RE Rules from CLAUDE.md still apply: ground every finding in the ELF, never assume code is dead, never assume BSS variables have specific effects without tracing.
5. **No deliverables until polygons are on screen.** Do not produce status reports, summaries, "here's what I did" messages, or progress recaps to the user during the session. Commit your work, log to `WORKLOG.md`, keep moving. The *first* moment a deliverable is permitted is when you have visual confirmation of geometry rendering — and even then, the deliverable is one terse message. After that initial milestone, return to the same silent-work posture.
6. **Work long-form.** Expect to run for hours uninterrupted. Pace yourself accordingly: don't burn context on verbose reasoning in chat — push the reasoning into `WORKLOG.md` entries and git commit messages where it belongs.

---

## 2. First Actions (in order, no deviations)

The user has already cloned the repo to `C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp` with submodules initialized. You start from inside that directory.

1. Read, in this order: `MANDATE.md` (this file), `PRIORITIES.md`, `CLAUDE.md`, the tail of `WORKLOG.md` (last 200 lines is enough; if it doesn't exist yet, create it from the template in §6).
2. Verify submodule state: `git submodule status`. If anything is out-of-sync, run `git submodule update --init --recursive`. Log the result.
3. Inventory the repo:
   - `dir` (or equivalent) the root, `config\`, `docs\`, `include\`, `src\`, `tools\`
   - Check whether `SLUS_202.68` exists in the repo root. If not, search the working tree once (`where /R . SLUS_202.68`) and note the result. **Do not** ask Steve to provide it. If genuinely missing, continue with work that doesn't require runtime execution (code generation, runtime stubs, build system, tooling, RE) and note the missing-ELF status in `WORKLOG.md`.
   - Check whether `src\generated\` exists and is populated with .cpp files. If not, the recompiler needs to run (see CLAUDE.md "Regenerate recompiled code"). This is itself a valid Tier-0 task if needed.
4. Create the `logs\` directory if it doesn't exist. All build/run output goes there.
5. Attempt a clean build per CLAUDE.md instructions. Capture full output to `logs\build_<timestamp>.log`. Do not paste it to chat.
6. Based on the build result and the priority rules in `PRIORITIES.md`, pick the next task. Start work.

From this point on, you are in the work loop described in §4.

---

## 3. The Polygons-On-Screen Goal — What It Is and Isn't

It is **the milestone that unlocks the first deliverable**. It is **not** the prioritization rule.

Do not bias your task selection toward graphics work just because that's where the milestone lies. The graphics pipeline depends on memory, DMA, VIF1, GS, and the game-state machine all functioning. Fixing a syscall bug or a DMA stub may be the highest-leverage thing you can do on a given cycle even if it has no direct visual output. Trust the priority rules.

When polygons do appear on screen, the chain of evidence you need to capture in `WORKLOG.md` is:
- A headless or hidden-window run that emits a screenshot to disk (raylib's `TakeScreenshot` or direct framebuffer readback).
- The screenshot file path, dimensions, and a brief description ("4 triangles visible, vertex colors only, no texture yet" is fine).
- The commit hash of the build that produced it.

At that point, and only at that point, surface a single short message to the user. After the message, return to silent work — do not wait for a response.

---

## 4. The Work Loop

Every cycle:

1. **Pick.** Re-read the tail of `WORKLOG.md` and `PRIORITIES.md`. Pick the next task using the priority rules. If a task is in progress from a previous cycle, finish it before picking a new one.
2. **Plan.** Write a short plan entry into `WORKLOG.md` (3–8 lines). What you're doing, what evidence you'll use to verify it, what the rollback condition is.
3. **Execute.** Make the change. Since you have direct file access, write complete files when you modify them — don't leave a file half-edited.
4. **Verify.** Build. Run the silent smoke test (§5). Check the log. If it failed, you debug it — you do not ask.
5. **Log.** Append result to `WORKLOG.md`: what changed, what the smoke test showed, what's next. Trim log snippets — link to the full log file rather than pasting kilobytes.
6. **Commit.** `git commit` with a clear message. Don't push to origin unless you've been told the remote accepts pushes. Local commits are fine; the log is the handoff artifact.
7. **Loop.**

Never end a cycle in a broken-build state without an entry in `WORKLOG.md` explaining why. If you have to break the build mid-refactor, log it explicitly so the next cycle picks it up first.

---

## 5. Silent Smoke Tests — Hard Requirements

The user's PC must not lose focus. Period.

**Allowed:**
- Headless runs that exit after N frames without opening a window. Add a `--headless --frames N --screenshot <path>` mode to the executable if it doesn't exist; this is high-priority infrastructure work.
- Hidden-window runs on Windows. raylib provides `SetConfigFlags(FLAG_WINDOW_HIDDEN)` — use it. Launch the process via `start /B` or with a `CREATE_NO_WINDOW` flag if shelling from a Python harness.
- Any test that produces evidence on disk (screenshot, log, framebuffer dump, performance counter dump).

**Forbidden:**
- Launching `racer_revenge.exe` directly into a visible window.
- Opening any GUI tool (Ghidra, debugger UIs, etc.) that grabs focus. For disassembly, use command-line tools (`objdump`, `readelf`, `nm`, custom Python scripts against the ELF).
- Anything that plays audio out the speakers. Mute or stub SPU2 output during tests.
- Long-running processes you don't kill. Every test has a hard timeout — Windows: use a watchdog thread or `taskkill /F /IM racer_revenge.exe /T` after the timeout expires. Wrap test invocations in a Python or batch harness that enforces this.

If you need to verify rendering before headless screenshotting is wired up, **add the screenshot infrastructure first**. That is itself a valid priority-1 task.

---

## 6. WORKLOG.md — The Substitute for Asking Questions

Every time you would have asked Steve something, you instead:
1. Decide.
2. Log the decision and reasoning in `WORKLOG.md`.
3. Move on.

If `WORKLOG.md` doesn't exist, create it with this header:

```
# WORKLOG — Racer Revenge Recomp

Append-only journal. Newest entries at the top. Each session starts with a "SESSION START" marker.

Entry format:
  ## [YYYY-MM-DD HH:MM] <short title>
  **Type:** plan | result | decision | blocker-resolved | milestone
  **Cycle:** <n>
  <body>
```

Entry types:
- **plan** — what you're about to do and how you'll verify it
- **result** — what happened, with log file references
- **decision** — a judgment call made in lieu of asking Steve, with the reasoning
- **blocker-resolved** — you hit something that looked like it needed Steve, and you resolved it from the code instead. *Always* log these — they're the proof that the autonomy mandate is working.
- **milestone** — significant progress (build green for first time, first instruction executes, first DMA packet, first triangle, etc.)

Keep entries terse. Traceability, not prose.

---

## 7. Decision Defaults (use these when you would otherwise hesitate)

- **Library choice ambiguous?** Use what PS2Recomp already pulls in (raylib 5.5). Don't add new dependencies unless an existing one genuinely can't do the job — and then log the decision.
- **Stub vs. real implementation?** Stub it with the minimum behavior that doesn't crash, log a TODO with the VA it relates to, and move on. Real implementation happens when something downstream demands it.
- **Refactor temptation?** No. Make the smallest change that moves the priority needle. Refactors are deferred until they unblock something concrete.
- **Unknown PS2 hardware behavior?** Check in order: PS2Recomp's runtime source, the EE Core / GS User's Manual references PS2Recomp links, public PCSX2 source on GitHub. Pick the simplest interpretation that matches the recompiled code's expectations. Log the source used.
- **Generated code looks wrong?** It usually isn't — the recompiler is more trustworthy than your reading of MIPS. Verify by disassembling the original instruction range from the ELF before patching generated output. If the generated code IS wrong, prefer fixing it via TOML config and regenerating over hand-editing the .cpp.
- **Build takes too long to iterate?** Narrow the build to the runtime target only (`cmake --build build --target racer_revenge --config Release`) and skip regenerating the 3400 .cpp files unless you've touched the TOML.
- **Test hangs?** Kill it after the configured timeout, log the hang, treat it as a failed run, move on to a different angle of attack. Don't sit watching it.
- **Patch theory feels uncertain?** Per the Standing RE Rules — trace every overwritten byte through the ELF before writing. Data-only patches to read-only tables are safe; code patches require complete execution path verification.

---

## 8. Communication With User — Strict Rules

- **During work:** Silent. No status messages. No "I'm now doing X." No "should I do Y or Z?" Use `WORKLOG.md` and git commits.
- **Polygons milestone:** One terse message. Format: `Polygons on screen. Build <commit-hash>. Screenshot: <path>. Detail: <one sentence>.` Then go back to silent work.
- **Unrecoverable blocker:** Defined narrowly. The only true unrecoverable blockers are: (a) the toolchain (MSVC/CMake) is actually missing from the machine, (b) the ELF is missing AND no work can be done without it (rare — see §2.3), (c) the disk is full or the git history is corrupted in a way you can't repair. For any of these, write a single message describing the blocker and what you tried. For *anything else*, you debug it yourself.
- **End of session:** If you've been working for many hours and want to checkpoint, do not message the user. Just commit, update `WORKLOG.md` with a "SESSION PAUSE" marker, and stop. Steve will pick up the log when he checks back.

---

## 9. Honest Failure Modes To Watch For In Yourself

- **Drifting into report-writing.** You start summarizing in chat instead of in `WORKLOG.md`. Stop. Move it to the log.
- **Asking implicit questions.** Phrases like "let me know if..." or "I could either... or..." are violations. Pick one.
- **Premature graphics chase.** You see the polygons goal and start hacking the GS stub when the syscall layer is broken. Re-read `PRIORITIES.md`.
- **Refactor spiral.** You "clean up" instead of "advance." If you haven't built and smoke-tested in the last hour, you're probably refactoring.
- **Skipping verification.** Every change builds and smoke-tests before commit. No exceptions.

---

## 10. When This Session Ends

`WORKLOG.md` is the handoff. The next Claude Code session (or the next time Steve checks in) reads the tail of that file and knows exactly where you stopped and why. That is the only handoff artifact. There is no "summary email." There is no "status report." There is the log.
