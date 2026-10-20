"""Tests for duplicati_profiler.py — the source-tree profiler."""
import json
import os

import pytest

import duplicati_profiler as prof
from duplicati_profiler import (
    KB, MB, GB,
    BUCKETS,
    bucket_index,
    profile_paths,
    profile_tree,
    main,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def write_file(path, size):
    """Create a file of exactly `size` zero bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


@pytest.fixture
def sample_tree(tmp_path):
    """A small tree with files spanning several histogram buckets.

    Layout:
      a/f1        = 5,000 bytes       (10-100 KB bucket... actually <10 KB? 5000 < 10240 -> <10 KB)
      a/sub/f2    = 50,000 bytes      (10-100 KB)
      b/f3        = 2,000,000 bytes   (1-10 MB)
      standalone  = 500 bytes         (<10 KB)
    """
    root = tmp_path
    write_file(str(root / "a" / "f1"), 5_000)
    write_file(str(root / "a" / "sub" / "f2"), 50_000)
    write_file(str(root / "b" / "f3"), 2_000_000)
    write_file(str(root / "standalone.bin"), 500)
    return root


# --------------------------------------------------------------------------- #
# bucket_index
# --------------------------------------------------------------------------- #

class TestBucketIndex:
    def test_zero_bytes_is_first_bucket(self):
        assert bucket_index(0) == 0

    def test_boundaries_are_left_inclusive(self):
        # Each bucket is [lo, hi); the lower edge belongs to the bucket.
        assert bucket_index(10 * KB) == 1          # exactly 10 KB -> "10-100 KB"
        assert bucket_index(10 * KB - 1) == 0      # one below -> "<10 KB"
        assert bucket_index(MB) == 3               # exactly 1 MB -> "1-10 MB"

    def test_open_ended_last_bucket(self):
        last = len(BUCKETS) - 1
        assert bucket_index(10 * GB) == last
        assert bucket_index(10 * GB + 1) == last
        assert bucket_index(500 * GB) == last

    def test_every_bucket_reachable(self):
        # A representative size in each bucket maps back to that bucket index.
        samples = [0, 20 * KB, 500 * KB, 5 * MB, 50 * MB, 500 * MB, 5 * GB, 50 * GB]
        for i, size in enumerate(samples):
            assert bucket_index(size) == i


# --------------------------------------------------------------------------- #
# profile_paths — core aggregation
# --------------------------------------------------------------------------- #

class TestProfilePaths:
    def test_single_directory_counts_all_files(self, sample_tree):
        p = profile_paths([str(sample_tree / "a")])
        assert p["file_count"] == 2
        assert p["total_bytes"] == 55_000

    def test_multiple_dirs_and_file_aggregate(self, sample_tree):
        p = profile_paths([
            str(sample_tree / "a"),
            str(sample_tree / "b"),
            str(sample_tree / "standalone.bin"),
        ])
        assert p["file_count"] == 4
        assert p["total_bytes"] == 55_000 + 2_000_000 + 500

    def test_single_file_path(self, sample_tree):
        p = profile_paths([str(sample_tree / "standalone.bin")])
        assert p["file_count"] == 1
        assert p["total_bytes"] == 500

    def test_overlapping_paths_deduplicated(self, sample_tree):
        # Passing the root AND a subdir of it must not double-count.
        combined = profile_paths([str(sample_tree), str(sample_tree / "a")])
        root_only = profile_paths([str(sample_tree)])
        assert combined["file_count"] == root_only["file_count"]
        assert combined["total_bytes"] == root_only["total_bytes"]

    def test_empty_directory(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        p = profile_paths([str(empty)])
        assert p["file_count"] == 0
        assert p["total_bytes"] == 0
        assert p["median_file_bytes"] == 0
        assert p["mean_file_bytes"] == 0
        assert p["median_is_exact"] is True  # 0 sampled == 0 files

    def test_scanned_path_joins_multiple_with_semicolons(self, sample_tree):
        p = profile_paths([str(sample_tree / "a"), str(sample_tree / "b")])
        assert "; " in p["scanned_path"]
        assert p["scanned_path"].count(";") == 1

    def test_scanned_path_is_absolute(self, sample_tree, monkeypatch):
        # Even a relative input is recorded as an absolute path.
        monkeypatch.chdir(sample_tree)
        p = profile_paths(["a"])
        assert os.path.isabs(p["scanned_path"])


# --------------------------------------------------------------------------- #
# Statistics: mean, median, histogram
# --------------------------------------------------------------------------- #

class TestStatistics:
    def test_mean_is_floor_division(self, tmp_path):
        write_file(str(tmp_path / "f1"), 10)
        write_file(str(tmp_path / "f2"), 15)   # mean = 25//2 = 12 (floor)
        p = profile_paths([str(tmp_path)])
        assert p["mean_file_bytes"] == 12

    def test_median_odd_count(self, tmp_path):
        for i, sz in enumerate((100, 200, 300)):
            write_file(str(tmp_path / f"f{i}"), sz)
        p = profile_paths([str(tmp_path)])
        assert p["median_file_bytes"] == 200

    def test_median_even_count_is_floor_of_average(self, tmp_path):
        # sizes 100, 201 -> median = (100+201)//2 = 150 (floor, not 150.5)
        write_file(str(tmp_path / "f1"), 100)
        write_file(str(tmp_path / "f2"), 201)
        p = profile_paths([str(tmp_path)])
        assert p["median_file_bytes"] == 150

    def test_histogram_counts_and_bytes(self, sample_tree):
        p = profile_paths([str(sample_tree)])
        by_label = {b["label"]: b for b in p["histogram"]}
        # 500 and 5,000 are both < 10 KB
        assert by_label["<10 KB"]["count"] == 2
        assert by_label["<10 KB"]["bytes"] == 500 + 5_000
        # 50,000 is in 10-100 KB
        assert by_label["10-100 KB"]["count"] == 1
        # 2,000,000 is in 1-10 MB
        assert by_label["1-10 MB"]["count"] == 1

    def test_histogram_always_has_all_buckets(self, tmp_path):
        write_file(str(tmp_path / "f"), 100)
        p = profile_paths([str(tmp_path)])
        assert len(p["histogram"]) == len(BUCKETS)
        # Open-ended last bucket serialises max_bytes as None (JSON null).
        assert p["histogram"][-1]["max_bytes"] is None

    def test_histogram_sum_equals_totals(self, sample_tree):
        p = profile_paths([str(sample_tree)])
        assert sum(b["count"] for b in p["histogram"]) == p["file_count"]
        assert sum(b["bytes"] for b in p["histogram"]) == p["total_bytes"]


# --------------------------------------------------------------------------- #
# largest_file_bytes / top_n
# --------------------------------------------------------------------------- #

class TestLargestFiles:
    def test_largest_sorted_descending(self, sample_tree):
        p = profile_paths([str(sample_tree)])
        largest = p["largest_file_bytes"]
        assert largest == sorted(largest, reverse=True)
        assert largest[0] == 2_000_000  # biggest file in the tree

    def test_top_n_caps_the_list(self, tmp_path):
        for i in range(10):
            write_file(str(tmp_path / f"f{i}"), (i + 1) * 100)
        p = profile_paths([str(tmp_path)], top_n=3)
        assert len(p["largest_file_bytes"]) == 3
        assert p["largest_file_bytes"] == [1000, 900, 800]


# --------------------------------------------------------------------------- #
# Symlink handling
# --------------------------------------------------------------------------- #

class TestSymlinks:
    def test_symlinked_file_skipped_by_default(self, tmp_path):
        real = tmp_path / "real.bin"
        write_file(str(real), 1234)
        link = tmp_path / "link.bin"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        p = profile_paths([str(tmp_path)])
        # Only the real file counts; the symlink is skipped.
        assert p["file_count"] == 1
        assert p["total_bytes"] == 1234

    def test_follow_symlinks_includes_link_target(self, tmp_path):
        real = tmp_path / "real.bin"
        write_file(str(real), 1234)
        link = tmp_path / "link.bin"
        try:
            os.symlink(str(real), str(link))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        p = profile_paths([str(tmp_path)], follow_symlinks=True)
        # With follow + realpath de-dup, the link resolves to the same real file,
        # so it is still counted once (not twice).
        assert p["file_count"] == 1
        assert p["total_bytes"] == 1234


# --------------------------------------------------------------------------- #
# Error accounting
# --------------------------------------------------------------------------- #

class TestErrors:
    def test_unreadable_count_starts_zero(self, sample_tree):
        p = profile_paths([str(sample_tree)])
        assert p["errors"]["unreadable_paths"] == 0


# --------------------------------------------------------------------------- #
# profile_tree back-compat wrapper
# --------------------------------------------------------------------------- #

def test_profile_tree_matches_profile_paths(sample_tree):
    a = profile_tree(str(sample_tree))
    b = profile_paths([str(sample_tree)])
    # Compare everything except the timestamp, which is wall-clock.
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b


# --------------------------------------------------------------------------- #
# Schema shape
# --------------------------------------------------------------------------- #

def test_profile_schema_keys(sample_tree):
    p = profile_paths([str(sample_tree)])
    expected = {
        "schema_version", "scanned_path", "generated_at", "total_bytes",
        "file_count", "mean_file_bytes", "median_file_bytes", "median_is_exact",
        "histogram", "largest_file_bytes", "errors",
    }
    assert set(p.keys()) == expected
    assert p["schema_version"] == 1
    bucket_keys = {"label", "min_bytes", "max_bytes", "count", "bytes"}
    for b in p["histogram"]:
        assert set(b.keys()) == bucket_keys


def test_profile_is_json_serialisable(sample_tree):
    p = profile_paths([str(sample_tree)])
    # Round-trips through JSON without error and preserves file_count.
    reloaded = json.loads(json.dumps(p))
    assert reloaded["file_count"] == p["file_count"]


# --------------------------------------------------------------------------- #
# main() / CLI
# --------------------------------------------------------------------------- #

class TestMain:
    def test_main_writes_to_output_file(self, sample_tree, tmp_path, capsys):
        out = tmp_path / "profile.json"
        rc = main([str(sample_tree), "-o", str(out)])
        assert rc == 0
        data = json.loads(out.read_text())
        assert data["file_count"] == 4
        # Status line goes to stderr, not stdout.
        err = capsys.readouterr().err
        assert "Wrote profile to" in err

    def test_main_prints_json_to_stdout(self, sample_tree, capsys):
        rc = main([str(sample_tree)])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["file_count"] == 4

    def test_main_multiple_paths(self, sample_tree, capsys):
        rc = main([str(sample_tree / "a"), str(sample_tree / "b")])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["file_count"] == 3  # 2 in a/ + 1 in b/

    def test_main_missing_path_errors(self, sample_tree, tmp_path, capsys):
        missing = str(tmp_path / "does_not_exist")
        rc = main([str(sample_tree / "a"), missing])
        assert rc == 2
        err = capsys.readouterr().err
        assert "not found" in err
        assert missing in err

    def test_main_top_n_flag(self, tmp_path, capsys):
        for i in range(5):
            write_file(str(tmp_path / f"f{i}"), (i + 1) * 1000)
        rc = main([str(tmp_path), "--top-n", "2"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["largest_file_bytes"]) == 2
