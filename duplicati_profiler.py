#!/usr/bin/env python3
"""
duplicati_profiler.py — Source-tree profiler for the Duplicati size estimator.

Walks a directory tree, measures file count and a size-distribution histogram,
and emits a compact JSON "profile" to stdout (or a file with -o). The profile
is the input contract for the companion HTML calculator, which recommends
optimal --block-size and --dblock-size values.

The profile contains only aggregate statistics plus a bounded list of the
largest file sizes (sizes only, never names/paths), so it is safe to move
between machines.

Usage:
    python3 duplicati_profiler.py /path/to/source
    python3 duplicati_profiler.py /path/to/source -o profile.json
    python3 duplicati_profiler.py /path/to/source --follow-symlinks
    python3 duplicati_profiler.py /dir/one /dir/two /some/file.iso -o profile.json

One or more paths may be given; directories are walked recursively and individual files
are counted directly, aggregating into a single combined profile. Paths need not share a
tree. Overlapping inputs are de-duplicated by resolved real path.

Standard library only; runs on Python 3.7+.
"""

import argparse
import datetime
import json
import os
import sys

# Histogram bucket edges in bytes. Each bucket is [min_bytes, max_bytes).
# The final bucket is open-ended (max_bytes = None).
KB = 1024
MB = 1024 * KB
GB = 1024 * MB

BUCKETS = [
    ("<10 KB",        0,          10 * KB),
    ("10-100 KB",     10 * KB,    100 * KB),
    ("100 KB-1 MB",   100 * KB,   MB),
    ("1-10 MB",       MB,         10 * MB),
    ("10-100 MB",     10 * MB,    100 * MB),
    ("100 MB-1 GB",   100 * MB,   GB),
    ("1-10 GB",       GB,         10 * GB),
    (">10 GB",        10 * GB,    None),
]


def bucket_index(size):
    """Return the histogram bucket index for a given file size in bytes."""
    for i, (_label, lo, hi) in enumerate(BUCKETS):
        if hi is None:
            if size >= lo:
                return i
        elif lo <= size < hi:
            return i
    return len(BUCKETS) - 1  # safety net


SAMPLE_CAP = 2_000_000  # store up to ~2M sizes (~16 MB RAM) for exact median


class _Accumulator:
    """Running tallies shared across all scanned paths, so multiple directories
    and/or individual files aggregate into one combined profile."""

    def __init__(self, top_n=25):
        self.top_n = top_n
        self.hist_counts = [0] * len(BUCKETS)
        self.hist_bytes = [0] * len(BUCKETS)
        self.total_bytes = 0
        self.file_count = 0
        self.unreadable = 0
        self.largest = []        # kept small; list of ints (sizes only)
        self.sizes_sample = []   # up to SAMPLE_CAP sizes, for an exact median
        self.seen = set()        # resolved real paths, to avoid double-counting overlap

    def add_file(self, fpath, follow_symlinks):
        """Count a single file into the tallies. Returns False if unreadable/skipped."""
        try:
            if not follow_symlinks and os.path.islink(fpath):
                # Skip symlinks by default (don't double-count / escape the tree).
                return False
            # De-dupe across overlapping inputs by resolved real path.
            real = os.path.realpath(fpath)
            if real in self.seen:
                return False
            self.seen.add(real)
            size = os.path.getsize(fpath)
        except (OSError, ValueError):
            self.unreadable += 1
            return False

        self.file_count += 1
        self.total_bytes += size
        idx = bucket_index(size)
        self.hist_counts[idx] += 1
        self.hist_bytes[idx] += size

        if len(self.sizes_sample) < SAMPLE_CAP:
            self.sizes_sample.append(size)

        # Track top-N largest sizes.
        if len(self.largest) < self.top_n:
            self.largest.append(size)
            self.largest.sort()
        elif size > self.largest[0]:
            self.largest[0] = size
            self.largest.sort()
        return True

    def add_path(self, path, follow_symlinks):
        """Add one input path: walk it if a directory, count it if a file."""
        if os.path.isdir(path):
            for dirpath, _dirnames, filenames in os.walk(path, followlinks=follow_symlinks):
                for name in filenames:
                    self.add_file(os.path.join(dirpath, name), follow_symlinks)
        else:
            self.add_file(path, follow_symlinks)


def profile_paths(paths, follow_symlinks=False, top_n=25):
    """Profile one or more paths (directories walked recursively, files counted
    directly) into a single combined profile dict matching the schema contract."""
    acc = _Accumulator(top_n=top_n)
    for path in paths:
        acc.add_path(path, follow_symlinks)

    # Median.
    median = 0
    if acc.sizes_sample:
        s = sorted(acc.sizes_sample)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) // 2

    mean = (acc.total_bytes // acc.file_count) if acc.file_count else 0

    histogram = []
    for i, (label, lo, hi) in enumerate(BUCKETS):
        histogram.append({
            "label": label,
            "min_bytes": lo,
            "max_bytes": hi,
            "count": acc.hist_counts[i],
            "bytes": acc.hist_bytes[i],
        })

    # scanned_path stays a string (the calculators display it): join multiple inputs.
    scanned = "; ".join(os.path.abspath(p) for p in paths)

    return {
        "schema_version": 1,
        "scanned_path": scanned,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "total_bytes": acc.total_bytes,
        "file_count": acc.file_count,
        "mean_file_bytes": mean,
        "median_file_bytes": median,
        "median_is_exact": len(acc.sizes_sample) == acc.file_count,
        "histogram": histogram,
        "largest_file_bytes": sorted(acc.largest, reverse=True),
        "errors": {"unreadable_paths": acc.unreadable},
    }


def profile_tree(root, follow_symlinks=False, top_n=25):
    """Backward-compatible single-path entry point (kept for any existing callers)."""
    return profile_paths([root], follow_symlinks=follow_symlinks, top_n=top_n)


def main(argv=None):
    p = argparse.ArgumentParser(description="Profile one or more source trees/files for the Duplicati size estimator.")
    p.add_argument("path", nargs="+",
                   help="One or more directories and/or files to scan. Directories are walked "
                        "recursively; files are counted individually. Paths need not share a tree.")
    p.add_argument("-o", "--output", help="Write JSON profile to this file (default: stdout)")
    p.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks (default: skip them)")
    p.add_argument("--top-n", type=int, default=25, help="How many largest file sizes to record (default 25)")
    args = p.parse_args(argv)

    missing = [p for p in args.path if not os.path.exists(p)]
    if missing:
        print(f"error: path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 2

    profile = profile_paths(args.path, follow_symlinks=args.follow_symlinks, top_n=args.top_n)
    text = json.dumps(profile, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"Wrote profile to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
