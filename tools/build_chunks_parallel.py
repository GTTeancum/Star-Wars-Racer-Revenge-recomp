#!/usr/bin/env python3
"""
build_chunks_parallel.py — Compiles sub_0031D200 chunks with clang-cl in parallel.

Uses subprocess.Popen to run N clang-cl processes simultaneously.
Run from the project root or pass --root.

Usage:
    python tools/build_chunks_parallel.py [--jobs N] [--root DIR]
"""

import os
import sys
import subprocess
import argparse
import time
import shutil
from pathlib import Path

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=4, help="Parallel compile jobs (default 4)")
    p.add_argument("--root", default=r"C:\Programming\GitHub\Star-Wars-Racer-Revenge-recomp")
    return p.parse_args()

def find_clang_cl():
    # Try explicit path first
    path = r"C:\Program Files\LLVM\bin\clang-cl.exe"
    if os.path.exists(path):
        return path
    # Try PATH
    found = shutil.which("clang-cl")
    if found:
        return found
    print("ERROR: clang-cl not found. Install LLVM or set PATH.", file=sys.stderr)
    sys.exit(1)

def setup_vcvars_env():
    """Run vcvarsall.bat and capture the environment."""
    vcvars = r"C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat"
    if not os.path.exists(vcvars):
        print("WARNING: vcvarsall.bat not found, using current environment.", file=sys.stderr)
        return os.environ.copy()

    cmd = f'"{vcvars}" x64 > nul 2>&1 && set'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    env = os.environ.copy()
    for line in result.stdout.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            env[k] = v
    return env

def main():
    args = parse_args()
    root = Path(args.root)

    clang_cl = find_clang_cl()
    env = setup_vcvars_env()

    src_dir = root / "src" / "generated_chunks"
    out_dir = root / "src" / "clang_objs"
    out_dir.mkdir(exist_ok=True)

    includes = [
        root / "tools" / "PS2Recomp" / "ps2xRuntime" / "include",
        root / "tools" / "PS2Recomp" / "ps2xRuntime" / "src" / "lib" / "Kernel",
        root / "src" / "generated",
        root / "src" / "generated_chunks",
        root / "include",
        root / "build" / "_deps" / "raylib-src" / "src",
        root / "build" / "_deps" / "raylib-src" / "src" / "external" / "glfw" / "include",
    ]
    include_flags = [f"-I{inc}" for inc in includes if inc.exists()]

    base_flags = [
        "/c", "/Od", "/bigobj", "/std:c++20", "/EHsc", "/MD",
    ] + include_flags

    # Find all chunk files plus the master dispatcher
    chunk_files = sorted(src_dir.glob("sub_0031D200_chunk_*.cpp"))
    master_file = src_dir / "sub_0031D200_0x31d200.cpp"
    all_files = [master_file] + list(chunk_files) if master_file.exists() else list(chunk_files)

    print(f"[build] {len(all_files)} files to compile ({args.jobs} parallel jobs)")
    print(f"[build] Using clang-cl: {clang_cl}")
    print(f"[build] Output dir: {out_dir}")

    total = len(all_files)
    done = 0
    failed = 0
    skipped = 0
    start_time = time.time()

    # Build work queue
    work = []
    for cpp in all_files:
        obj = out_dir / (cpp.stem + ".obj")
        # Skip if .obj is newer than .cpp
        if obj.exists() and obj.stat().st_mtime > cpp.stat().st_mtime:
            skipped += 1
            done += 1
            continue
        work.append((cpp, obj))

    print(f"[build] {skipped} already up-to-date, {len(work)} to compile")

    active = {}  # proc -> (cpp, obj)
    work_idx = 0

    while work_idx < len(work) or active:
        # Fill slots
        while len(active) < args.jobs and work_idx < len(work):
            cpp, obj = work[work_idx]
            cmd = [clang_cl] + base_flags + [f"/Fo{obj}", str(cpp)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env)
            active[proc] = (cpp, obj)
            work_idx += 1

        if not active:
            break

        # Poll active processes
        finished = []
        for proc in list(active):
            ret = proc.poll()
            if ret is not None:
                cpp, obj = active[proc]
                output, _ = proc.communicate()
                done += 1
                if ret != 0:
                    failed += 1
                    print(f"\n[FAIL] {cpp.name} (exit {ret})", flush=True)
                    # Show first few error lines
                    errors = [l for l in output.splitlines() if 'error:' in l]
                    for err in errors[:3]:
                        print(f"       {err}", flush=True)
                else:
                    elapsed = time.time() - start_time
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"\r[{done:3d}/{total}] OK {cpp.stem[:40]:<40}  ETA {eta:.0f}s   ",
                          end="", flush=True)
                finished.append(proc)
        for proc in finished:
            del active[proc]

        time.sleep(0.1)

    elapsed = time.time() - start_time
    print(f"\n[build] Done in {elapsed:.1f}s. OK={done-failed-skipped} skip={skipped} FAIL={failed}")
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
