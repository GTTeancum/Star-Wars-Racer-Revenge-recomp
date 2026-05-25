#!/usr/bin/env python3
"""
measure_chunks.py — Report size distribution of sub_0031D200 chunk files.

The chunking experiment from cycle 27 (split_giant_function.py) divides
sub_0031D200's 2.1GB by LABEL COUNT, not byte size — so chunks vary wildly
in size (some <500KB, others >100MB).  MSVC fails on anything past ~3MB
per file, so 50%+ of chunks currently won't compile.

This script measures the actual distribution so the next chunking iteration
can be done informed (target: all chunks <= ~2MB for MSVC, ~5MB for clang-cl).

Usage:
    python tools/measure_chunks.py [--dir src/generated_chunks]
"""

from __future__ import annotations

import argparse
import glob
import os
import sys


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="src/generated_chunks",
                   help="Directory containing sub_0031D200_chunk_*.cpp files")
    p.add_argument("--max-mb", type=float, default=2.0,
                   help="Threshold for 'oversized' classification (default 2.0 MB)")
    args = p.parse_args()

    pattern = os.path.join(args.dir, "sub_0031D200_chunk_*.cpp")
    chunks = sorted(glob.glob(pattern))
    if not chunks:
        print(f"No chunks found at {pattern}", file=sys.stderr)
        return 1

    sizes = [(os.path.basename(c), os.path.getsize(c)) for c in chunks]
    total_bytes = sum(s for _, s in sizes)

    print(f"Chunks measured: {len(sizes)}")
    print(f"Total size:      {total_bytes / 1024 / 1024:.1f} MB")
    print(f"Average size:    {total_bytes / len(sizes) / 1024:.0f} KB")
    print()

    # Bucket distribution
    bucket_defs = [
        ("<=100KB", 100 * 1024),
        ("<=500KB", 500 * 1024),
        ("<=1MB",   1024 * 1024),
        ("<=2MB",   2 * 1024 * 1024),
        ("<=5MB",   5 * 1024 * 1024),
        ("<=10MB",  10 * 1024 * 1024),
        ("<=50MB",  50 * 1024 * 1024),
        (">50MB",   float("inf")),
    ]
    buckets = {label: 0 for label, _ in bucket_defs}
    for _, s in sizes:
        for label, ceiling in bucket_defs:
            if s <= ceiling:
                buckets[label] += 1
                break
    for label, count in buckets.items():
        print(f"  {label:>10}: {count:>4} chunks")
    print()

    # Oversized count
    threshold = int(args.max_mb * 1024 * 1024)
    oversized = [(n, s) for n, s in sizes if s > threshold]
    print(f"Oversized (> {args.max_mb} MB): {len(oversized)} chunks "
          f"({100*len(oversized)/len(sizes):.0f}% of total)")
    print()

    print("Top 10 by size:")
    for name, s in sorted(sizes, key=lambda x: -x[1])[:10]:
        print(f"  {name}: {s / 1024 / 1024:>6.1f} MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
