#!/usr/bin/env bash
# Full regen pipeline with automated gates.
#
# Runs the recompiler → rebuilds xref + callgraph → rebuilds indirect
# targets → reclassifies functions → regenerates memory_map watchpoints
# → runs xref regression tests. Non-zero exit if anything along the way
# fails, so it's safe to chain into CI or a pre-commit hook.
#
# Usage:
#     ./tools/regen.sh                        # full pipeline
#     ./tools/regen.sh --skip-recomp          # skip ps2_recomp (fast rebuild of xref only)
#     ./tools/regen.sh --skip-tests           # don't run tests.py at the end
#
# Environment:
#     ELF                 Path to the PS2 ELF (default: SLUS_202.68)
#     CONFIG              TOML config (default: config/racer_revenge.toml)
#     GENERATED           Output dir for .cpp (default: src/generated)
#     PS2RECOMP           ps2_recomp binary (default: auto-detect under tools/PS2Recomp/build)

set -euo pipefail

ELF="${ELF:-SLUS_202.68}"
CONFIG="${CONFIG:-config/racer_revenge.toml}"
GENERATED="${GENERATED:-src/generated}"
PS2RECOMP="${PS2RECOMP:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ -z "${PS2RECOMP}" ]]; then
    # Auto-detect the most recent ps2_recomp.exe build.
    candidates=(
        "tools/PS2Recomp/build/ps2xRecomp/Release/ps2_recomp.exe"
        "tools/PS2Recomp/build/ps2xRecomp/Release/ps2_recomp"
    )
    for c in "${candidates[@]}"; do
        if [[ -x "$c" ]]; then
            PS2RECOMP="$c"
            break
        fi
    done
fi

SKIP_RECOMP=0
SKIP_TESTS=0
for arg in "$@"; do
    case "$arg" in
        --skip-recomp) SKIP_RECOMP=1 ;;
        --skip-tests)  SKIP_TESTS=1  ;;
        *) echo "unknown arg: $arg" >&2; exit 64 ;;
    esac
done

banner() { printf '\n==========  %s  ==========\n' "$1"; }

if [[ "${SKIP_RECOMP}" -eq 0 ]]; then
    if [[ -z "${PS2RECOMP}" || ! -x "${PS2RECOMP}" ]]; then
        echo "ps2_recomp binary not found; set PS2RECOMP or pass --skip-recomp" >&2
        exit 2
    fi
    banner "1/5  ps2_recomp"
    "${PS2RECOMP}" "${CONFIG}"

    # Drop stale .cpp files left over from previous runs whose function
    # boundaries changed. Without this, an old file that the new
    # ps2_recompiled_functions.h no longer declares will fail to compile
    # ("identifier not found") while building a caller that still references
    # the old symbol. Idempotent; expected to be a no-op on a clean tree.
    python3 tools/prune_stale_generated.py
fi

banner "2/5  build_xref"
python3 tools/xref/build_xref.py \
    --elf "${ELF}" --functions "${GENERATED}" \
    --output build/xref/globals.json

banner "3/5  build_callgraph"
python3 tools/xref/build_callgraph.py \
    --elf "${ELF}" --functions "${GENERATED}" \
    --output build/xref/callgraph.json

banner "4/5  build_indirect_targets + classify + gen_watchpoints"
python3 tools/xref/build_indirect_targets.py \
    --elf "${ELF}" --functions "${GENERATED}" \
    --xref build/xref/globals.json \
    --output build/xref/indirect_targets.json
python3 tools/xref/classify.py > /dev/null
python3 tools/gen_watchpoints.py \
    --memory-map config/memory_map.toml \
    --output src/generated/memory_map_watchpoints.h
python3 tools/xref/dead_functions.py \
    --output build/xref/dead_functions.json > /dev/null

if [[ "${SKIP_TESTS}" -eq 0 ]]; then
    banner "5/6  xref regression tests"
    if ! python3 tools/xref/tests.py; then
        echo
        echo "FAIL: xref regression tests failed — xref has regressed." >&2
        exit 1
    fi

    banner "6/6  runtime smoke test (T14)"
    # Only run the smoke test if the exe exists. Regenerating source doesn't
    # itself rebuild the exe, so this gate mostly catches runtime/main.cpp
    # regressions when invoked after a build. The test is tolerant: it only
    # checks post-boot state, not binary reproducibility.
    if [[ -x "build/Release/racer_revenge.exe" ]]; then
        if ! python3 tools/smoke_test.py --runtime 10; then
            echo
            echo "FAIL: runtime smoke test failed — boot state has regressed." >&2
            exit 1
        fi
    else
        echo "(skipping: build/Release/racer_revenge.exe not found; build first)"
    fi

    echo
    echo "PASS: full regen completed with all gates green."
else
    echo
    echo "Skipped tests.py + smoke test (gate disabled)."
fi
