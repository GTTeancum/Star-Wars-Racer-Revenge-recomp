#!/usr/bin/env python3
"""
Runtime smoke test — T14.

Runs `racer_revenge.exe` headlessly for a few seconds, captures its
post-boot [Diag] output, and compares to a committed golden JSON. Catches
regressions from ps2_recomp / ps2_runtime / main.cpp changes that pass
their own unit tests but break actual game boot.

The Diag dump is produced by the existing thread in main.cpp that runs
~2.5s after bootstrap (see "Phase 6" in main.cpp). If that timing changes,
adjust the --runtime arg accordingly.

Expected state checks include:
  - `game_state_fn` (0x384670) equals the expected post-boot value
  - Syscall dispatch table slots 1-3 contain the expected state handlers
  - Key overlay regions contain expected first 4 words
  - No "ResetEE" spam (i.e. runtime didn't go into a retry-loop)
  - No unhandled exceptions logged

Usage:
    python tools/smoke_test.py                      # run + diff
    python tools/smoke_test.py --update             # refresh golden from current run
    python tools/smoke_test.py --runtime 10         # longer run (default 10s)
    python tools/smoke_test.py --exe path/to/exe

Exit code: 0 if all checks pass, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


GOLDEN_DEFAULT = Path("config") / "smoke_test_golden.json"
EXE_DEFAULT = Path("build") / "Release" / "racer_revenge.exe"


# Regex to pick out the two common [Diag] patterns we care about:
#   "[Diag]   0x384670 (game state fn) = 0x251b10"
#   "[Diag]     [16] (syscall 0x40) = 0x5a"
#   "[Diag]   overlay @ kseg0 0x80074000 (phys 0x74000) = 27bdffe0 24050026 ffb00000 80802d"
STATE_FN_RE = re.compile(r"\[Diag\]\s+0x([0-9a-fA-F]+)\s+\(game state fn\)\s*=\s*0x([0-9a-fA-F]+)")
DISPATCH_SLOT_RE = re.compile(r"\[Diag\]\s+\[(\d+)\]\s+\(syscall\s+0x[0-9a-fA-F]+\)\s*=\s*0x([0-9a-fA-F]+)")
OVERLAY_RE = re.compile(
    r"\[Diag\]\s+overlay @ (?:kseg0\s+)?(?:phys\s+)?0x([0-9a-fA-F]+)[^=]*=\s+([0-9a-fA-F]+\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+\s+[0-9a-fA-F]+)"
)


def parse_diag(output: str) -> dict:
    """Extract a compact dict of the interesting Diag fields from raw stdout."""
    parsed: dict = {
        "game_state_fn":         None,
        "dispatch_table":        {},
        "overlays":              {},
        "reset_ee_count":        output.count("[ResetEE]"),
        "exception_count":       output.lower().count("exception"),
        "fallback_count":        output.count("SyscallOverride:fallback"),
    }
    for m in STATE_FN_RE.finditer(output):
        parsed["game_state_fn"] = f"0x{int(m.group(2), 16):08x}"
    for m in DISPATCH_SLOT_RE.finditer(output):
        slot = int(m.group(1))
        parsed["dispatch_table"][str(slot)] = f"0x{int(m.group(2), 16):08x}"
    for m in OVERLAY_RE.finditer(output):
        addr = f"0x{int(m.group(1), 16):08x}"
        parsed["overlays"][addr] = m.group(2).strip()
    return parsed


def run_exe(exe: Path, runtime_s: int, elf: Path,
            screenshot: Path | None = None,
            frames: int | None = None) -> tuple[str, int]:
    """Run the exe headlessly for `runtime_s` seconds (wall clock), kill,
    return (output, rc).

    MANDATE §5: the runtime is always launched with --headless so the host
    window is FLAG_WINDOW_HIDDEN and cannot steal focus. The runtime is also
    bounded by --runtime-seconds; the subprocess timeout is a backstop.

    On Windows we additionally pass CREATE_NO_WINDOW so the spawned process
    does not get a console window even when raylib's FLAG_WINDOW_HIDDEN is
    ineffective (e.g. console subsystem launches)."""
    if not exe.exists():
        sys.exit(f"exe not found: {exe}")
    if not elf.exists():
        sys.exit(f"elf not found: {elf}")

    cmd = [
        str(exe.resolve()),
        str(elf.resolve()),
        "--headless",
        "--runtime-seconds", str(runtime_s),
    ]
    if screenshot is not None:
        cmd += ["--screenshot", str(Path(screenshot).resolve())]
    if frames is not None:
        cmd += ["--frames", str(frames)]

    creationflags = 0
    if os.name == "nt":
        # CREATE_NO_WINDOW = 0x08000000
        creationflags = 0x08000000

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(exe.parent),
            capture_output=True,
            text=True,
            # +5s grace beyond the runtime's own --runtime-seconds bound so
            # the runtime has time to do its shutdown handshake. The runtime
            # is responsible for stopping itself; this is just a backstop.
            timeout=runtime_s + 5,
            creationflags=creationflags,
        )
        return proc.stdout + proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as e:
        # The runtime didn't honour --runtime-seconds. Treat as a hang.
        return (e.stdout or "") + (e.stderr or ""), 124


def diff_snapshot(golden: dict, actual: dict, strict: bool) -> list[str]:
    failures: list[str] = []
    if actual["game_state_fn"] != golden.get("game_state_fn"):
        failures.append(
            f"game_state_fn: expected {golden.get('game_state_fn')!r}, got {actual['game_state_fn']!r}"
        )
    for slot, expected in golden.get("dispatch_table", {}).items():
        actual_val = actual["dispatch_table"].get(slot)
        if actual_val != expected:
            failures.append(
                f"dispatch_table[{slot}]: expected {expected!r}, got {actual_val!r}"
            )
    for addr, expected in golden.get("overlays", {}).items():
        actual_val = actual["overlays"].get(addr)
        if actual_val != expected:
            failures.append(
                f"overlay @ {addr}: expected {expected!r}, got {actual_val!r}"
            )
    # Health-metric thresholds. Treat as hard limits in strict mode.
    max_resets = golden.get("max_reset_ee_count", 10)
    if actual["reset_ee_count"] > max_resets:
        failures.append(
            f"ResetEE spam: {actual['reset_ee_count']} hits (max allowed {max_resets})"
        )
    max_exceptions = golden.get("max_exception_count", 2)
    if actual["exception_count"] > max_exceptions:
        failures.append(
            f"exception count too high: {actual['exception_count']} (max {max_exceptions})"
        )
    max_fallbacks = golden.get("max_fallback_count", 100)
    if actual["fallback_count"] > max_fallbacks:
        failures.append(
            f"SyscallOverride:fallback spam: {actual['fallback_count']} hits (max {max_fallbacks})"
        )
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exe", default=str(EXE_DEFAULT),
                    help=f"Path to racer_revenge.exe (default: {EXE_DEFAULT})")
    ap.add_argument("--elf", default="SLUS_202.68",
                    help="Path to the PS2 ELF (default: ./SLUS_202.68)")
    ap.add_argument("--golden", default=str(GOLDEN_DEFAULT),
                    help=f"Path to golden JSON (default: {GOLDEN_DEFAULT})")
    ap.add_argument("--runtime", type=int, default=10,
                    help="How many seconds to run the exe (default 10)")
    ap.add_argument("--update", action="store_true",
                    help="Write the current observed state to the golden file instead of checking")
    ap.add_argument("--log", default=None,
                    help="Optional file path to dump raw exe output for debugging")
    ap.add_argument("--screenshot", default=None,
                    help="Optional path to write a host-window screenshot")
    ap.add_argument("--frames", type=int, default=None,
                    help="Optional host-frame limit (passed to the exe)")
    args = ap.parse_args()

    print(f"[smoke] Running {args.exe} headlessly for {args.runtime}s...")
    output, rc = run_exe(Path(args.exe), args.runtime, Path(args.elf),
                         screenshot=Path(args.screenshot) if args.screenshot else None,
                         frames=args.frames)
    print(f"[smoke] exit code={rc} output_bytes={len(output)}")
    if args.log:
        Path(args.log).write_text(output, encoding="utf-8", errors="replace")
        print(f"[smoke] wrote raw output to {args.log}")

    actual = parse_diag(output)
    print("[smoke] Observed state:")
    print(f"  game_state_fn:      {actual['game_state_fn']}")
    print(f"  dispatch slots:     {len(actual['dispatch_table'])} populated")
    print(f"  overlay peeks:      {len(actual['overlays'])}")
    print(f"  ResetEE hits:       {actual['reset_ee_count']}")
    print(f"  exception mentions: {actual['exception_count']}")
    print(f"  fallback mentions:  {actual['fallback_count']}")

    golden_path = Path(args.golden)
    if args.update:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        to_write = {
            "game_state_fn":    actual["game_state_fn"],
            "dispatch_table":   actual["dispatch_table"],
            "overlays":         actual["overlays"],
            "max_reset_ee_count":  10,
            "max_exception_count": 2,
            "max_fallback_count":  100,
        }
        golden_path.write_text(json.dumps(to_write, indent=2), encoding="utf-8")
        print(f"[smoke] wrote golden snapshot to {golden_path}")
        return 0

    if not golden_path.exists():
        print(f"[smoke] No golden at {golden_path}. Run with --update to create one.")
        return 1

    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    failures = diff_snapshot(golden, actual, strict=True)

    if not failures:
        print(f"[smoke] PASS")
        return 0

    print(f"[smoke] FAIL ({len(failures)} issue(s)):")
    for f in failures:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
