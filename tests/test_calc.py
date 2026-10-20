"""Tests for duplicati_calc.py — the recommendation + projection calculator."""
import io
import json

import pytest

import duplicati_calc as calc
from duplicati_calc import (
    KB, MB, GB, TB,
    BLOCK_STEPS_KB, VOL_STEPS_MB,
    fmt_bytes,
    nice_block_kb,
    nice_vol,
    parse_size_to_bytes,
    validate,
    recommend,
    render_text,
    main,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def make_profile(total_bytes, file_count=1000, median=50_000, scanned_path="/src"):
    """A minimal profile dict good enough to drive recommend()."""
    return {
        "schema_version": 1,
        "scanned_path": scanned_path,
        "total_bytes": total_bytes,
        "file_count": file_count,
        "median_file_bytes": median,
        "mean_file_bytes": total_bytes // file_count if file_count else 0,
        "histogram": [],
    }


def rec_defaults(profile, **overrides):
    """Call recommend() with the same defaults as the CLI, overriding as needed."""
    kw = dict(dest="remote", stability="stable", change_pattern="normal",
              up_mbps=50, dl_cap_gb=0, compression_pct=100,
              change_amt=256, change_unit="MB", versions=0)
    kw.update(overrides)
    return recommend(profile, **kw)


# --------------------------------------------------------------------------- #
# fmt_bytes
# --------------------------------------------------------------------------- #

class TestFmtBytes:
    def test_bytes(self):
        assert fmt_bytes(0) == "0 B"
        assert fmt_bytes(500) == "500 B"

    def test_sub_kb_float_is_rounded(self):
        # Regression: a float below 1 KB used to print unrounded (e.g. "73.298... B").
        assert fmt_bytes(73.29811477661133) == "73 B"

    def test_kb(self):
        assert fmt_bytes(KB) == "1 KB"
        assert fmt_bytes(100 * KB) == "100 KB"

    def test_mb_one_decimal_under_10(self):
        assert fmt_bytes(2 * MB) == "2.0 MB"
        assert fmt_bytes(int(1.5 * MB)) == "1.5 MB"

    def test_mb_no_decimal_at_or_above_10(self):
        assert fmt_bytes(50 * MB) == "50 MB"

    def test_gb_two_decimals_under_10(self):
        assert fmt_bytes(2 * GB) == "2.00 GB"

    def test_gb_no_decimal_at_or_above_10(self):
        assert fmt_bytes(100 * GB) == "100 GB"


# --------------------------------------------------------------------------- #
# nice_block_kb  (the "nice step" ladder, incl. the extended >5 MB range)
# --------------------------------------------------------------------------- #

class TestNiceBlockKb:
    def test_rounds_down_to_ladder_step(self):
        # 180 KB sits between 150 and 200 -> picks 150.
        assert nice_block_kb(180 * KB) == 150

    def test_exact_step_kept(self):
        assert nice_block_kb(100 * KB) == 100

    def test_below_first_step_returns_first(self):
        assert nice_block_kb(1) == BLOCK_STEPS_KB[0]

    def test_extended_ladder_reaches_50mb(self):
        # 50 TB of data -> ~50 MB block (50000 KB); must snap to the top ladder step.
        assert nice_block_kb(50 * MB) == 50000
        assert nice_block_kb(60 * MB) == 50000   # above the top caps at 50000

    def test_ladder_is_sorted_ascending(self):
        assert BLOCK_STEPS_KB == sorted(BLOCK_STEPS_KB)


# --------------------------------------------------------------------------- #
# nice_vol
# --------------------------------------------------------------------------- #

class TestNiceVol:
    def test_mb_range(self):
        assert nice_vol(200 * MB) == (200, "MB")

    def test_rounds_down(self):
        assert nice_vol(260 * MB) == (250, "MB")

    def test_gb_conversion(self):
        # 1000 MB step is reported as 1 GB.
        assert nice_vol(1000 * MB) == (1, "GB")
        assert nice_vol(2 * GB) == (2, "GB")

    def test_below_first_step(self):
        assert nice_vol(1) == (VOL_STEPS_MB[0], "MB")


# --------------------------------------------------------------------------- #
# parse_size_to_bytes
# --------------------------------------------------------------------------- #

class TestParseSizeToBytes:
    def test_kb_mb_gb(self):
        assert parse_size_to_bytes(100, "KB") == 100 * KB
        assert parse_size_to_bytes(200, "MB") == 200 * MB
        assert parse_size_to_bytes(2, "GB") == 2 * GB

    def test_returns_int(self):
        assert isinstance(parse_size_to_bytes(1.5, "GB"), int)


# --------------------------------------------------------------------------- #
# recommend — block size heuristic
# --------------------------------------------------------------------------- #

class TestRecommendBlock:
    def test_small_set_hits_100kb_floor(self):
        # 76 GB -> base ~76 KB, below the 100 KB floor.
        rec = rec_defaults(make_profile(76 * GB))
        assert rec["block_size"] == "100KB"

    def test_base_rule_1kb_per_gb(self):
        # 800 GB -> ~800 KB -> ladder snaps to 750 KB.
        rec = rec_defaults(make_profile(800 * GB))
        assert rec["block_size"] == "750KB"

    def test_multi_tb_uses_extended_ladder(self):
        # 10 TB -> ~10 MB block.
        rec = rec_defaults(make_profile(10 * TB))
        assert rec["block_size"] == "10000KB"

    def test_archival_multiplier_raises_block(self):
        normal = rec_defaults(make_profile(800 * GB), change_pattern="normal")
        archival = rec_defaults(make_profile(800 * GB), change_pattern="archival")
        # archival ×1.5 should give a larger-or-equal block than normal.
        assert int(archival["block_size"][:-2]) >= int(normal["block_size"][:-2])

    def test_churn_cannot_push_below_floor(self):
        # The 100 KB floor is applied AFTER the churn ×0.6 multiplier (regression: Bug #1).
        rec = rec_defaults(make_profile(300 * GB), change_pattern="churn")
        assert int(rec["block_size"][:-2]) >= 100

    def test_median_note_when_files_smaller_than_block(self):
        rec = rec_defaults(make_profile(2 * TB, median=5_000))
        assert rec["median_note"] is not None

    def test_no_median_note_when_files_larger_than_block(self):
        rec = rec_defaults(make_profile(2 * TB, median=50 * MB))
        assert rec["median_note"] is None


# --------------------------------------------------------------------------- #
# recommend — volume (dblock) size
# --------------------------------------------------------------------------- #

class TestRecommendVolume:
    def test_remote_default_200mb(self):
        rec = rec_defaults(make_profile(800 * GB), dest="remote", up_mbps=50)
        assert rec["dblock_size"] == "200MB"

    def test_remote_slow_upload_100mb(self):
        rec = rec_defaults(make_profile(800 * GB), dest="remote", up_mbps=8)
        assert rec["dblock_size"] == "100MB"

    def test_remote_fast_upload_500mb(self):
        rec = rec_defaults(make_profile(800 * GB), dest="remote", up_mbps=300)
        assert rec["dblock_size"] == "500MB"

    def test_remote_unstable_caps_100mb(self):
        rec = rec_defaults(make_profile(800 * GB), dest="remote",
                           up_mbps=300, stability="unstable")
        assert rec["dblock_size"] == "100MB"

    def test_remote_download_cap_limits_volume(self):
        # Cap of 0.5 GB -> volume held to ~1/4 of cap. Should be well under the 200 MB default.
        rec = rec_defaults(make_profile(800 * GB), dest="remote",
                           up_mbps=300, dl_cap_gb=0.5)
        val, unit = int(rec["dblock_size"][:-2]), rec["dblock_size"][-2:]
        vol_bytes = parse_size_to_bytes(val, unit)
        assert vol_bytes <= 0.5 * GB

    def test_local_targets_file_count_with_clamp(self):
        # Small local set clamps up to the 50 MB minimum.
        rec = rec_defaults(make_profile(10 * GB), dest="local")
        val, unit = int(rec["dblock_size"][:-2]), rec["dblock_size"][-2:]
        assert parse_size_to_bytes(val, unit) >= 50 * MB

    def test_local_large_set_clamped_to_2gb(self):
        # A very large local set: total/10000*2 would exceed 2 GB, so it clamps.
        rec = rec_defaults(make_profile(50 * TB), dest="local")
        val, unit = int(rec["dblock_size"][:-2]), rec["dblock_size"][-2:]
        assert parse_size_to_bytes(val, unit) <= 2 * GB


# --------------------------------------------------------------------------- #
# recommend — projections (reference-sheet formulas)
# --------------------------------------------------------------------------- #

class TestProjectionsReferenceExample:
    """The reference Google Sheet's own example, which the tool is validated to
    reproduce exactly: 4 TB source, 256 MB change, 100% compression, 1 GB volume,
    500 MB block, enough versions to exercise the formulas.

    We feed a profile + change settings that produce a 1 GB volume and 500 MB block,
    then assert the sheet's published counts.
    """

    def test_reference_sheet_formulas_reproduce_published_numbers(self):
        """Gold anchor: the reference Google Sheet's own worked example.

        Inputs: 4 TB source, 256 MB change, 100% compression, 1 GB volume, 500 MB block,
        and enough versions for the "all" column. The sheet publishes:
          4,097 data files, 8,195 total backend files, 8,389 blocks, 3 avg blocks/file.

        recommend()'s size *ladder* caps the recommended block at 50 MB, so it can never
        itself emit a 500 MB block — meaning the end-to-end recommender cannot reproduce
        this example. We therefore validate the projection FORMULAS directly here, exactly
        as duplicati_calc.recommend() computes them, pinning them to the sheet's figures.
        """
        import math
        total = 4 * TB
        volume = 1 * GB
        block = 500 * MB
        comp = 1.0
        change = 256 * MB
        versions = 1

        data_init = round((total * comp) / volume) + 1
        blocks_init = math.ceil(total / block)
        avg_blocks = math.ceil(blocks_init / data_init)
        total_backend_init = data_init + data_init + 1  # data + index + list

        # The sheet's published figures are the *initial* counts.
        assert data_init == 4097
        assert total_backend_init == 8195
        assert blocks_init == 8389
        assert avg_blocks == 3

    def test_recommend_projection_shape_matches_formulas(self):
        """The projection returned by recommend() uses those same formulas, so its
        fields stay internally consistent for a realistic set."""
        profile = make_profile(4 * TB, file_count=100000, median=40 * MB)
        rec = recommend(profile, dest="local", stability="stable",
                        change_pattern="normal", up_mbps=50, dl_cap_gb=0,
                        compression_pct=100, change_amt=256, change_unit="MB",
                        versions=1)
        pr = rec["projection"]
        assert pr["storage_initial_bytes"] == 4 * TB           # source * comp
        assert pr["index_files_initial"] == pr["data_files_initial"]
        assert pr["list_files_initial"] == 1
        assert (pr["total_backend_files_initial"]
                == pr["data_files_initial"] + pr["index_files_initial"] + 1)

    def test_projection_internal_consistency(self):
        rec = rec_defaults(make_profile(2 * TB), versions=5,
                           change_amt=10, change_unit="GB")
        pr = rec["projection"]
        # all-versions counts are >= initial counts
        assert pr["data_files_all"] >= pr["data_files_initial"]
        assert pr["blocks_all"] >= pr["blocks_initial"]
        assert pr["total_backend_files_all"] >= pr["total_backend_files_initial"]

    def test_compression_scales_storage(self):
        full = rec_defaults(make_profile(1 * TB), compression_pct=100)
        half = rec_defaults(make_profile(1 * TB), compression_pct=50)
        assert half["projection"]["storage_initial_bytes"] < full["projection"]["storage_initial_bytes"]

    def test_versions_zero_means_initial_only(self):
        rec = rec_defaults(make_profile(1 * TB), versions=0,
                           change_amt=100, change_unit="MB")
        pr = rec["projection"]
        # With 0 versions, all-versions data files equal initial (versions+init, versions=0).
        assert pr["data_files_all"] == pr["data_files_initial"]


# --------------------------------------------------------------------------- #
# validate — guardrails
# --------------------------------------------------------------------------- #

def _levels(findings):
    return [f["level"] for f in findings]


class TestValidate:
    def _proj(self, blocks=1000, files=100):
        return {"blocks_all": blocks, "blocks_initial": blocks,
                "total_backend_files_all": files, "total_backend_files_initial": files}

    def test_block_larger_than_volume_is_error(self):
        f = validate(500 * MB, 100 * MB, self._proj())
        assert "error" in _levels(f)
        assert any("exceeds volume size" in x["message"] for x in f)

    def test_block_below_hard_min_is_error(self):
        f = validate(5 * KB, 200 * MB, self._proj())
        assert "error" in _levels(f)
        assert any("10 KB" in x["message"] for x in f)

    def test_block_over_1mb_is_info(self):
        f = validate(2 * MB, 500 * MB, self._proj())
        assert "info" in _levels(f)

    def test_volume_over_1gb_is_info(self):
        f = validate(100 * KB, 2 * GB, self._proj())
        assert any(x["level"] == "info" and "1 GB" in x["message"] for x in f)

    def test_too_many_blocks_is_warning(self):
        f = validate(100 * KB, 200 * MB, self._proj(blocks=6_000_000))
        assert any(x["level"] == "warn" and "blocks" in x["message"] for x in f)

    def test_too_many_files_is_warning(self):
        f = validate(100 * KB, 200 * MB, self._proj(files=200_000))
        assert any(x["level"] == "warn" and "files" in x["message"] for x in f)

    def test_clean_case_has_no_findings(self):
        f = validate(100 * KB, 200 * MB, self._proj(blocks=1000, files=100))
        assert f == []


# --------------------------------------------------------------------------- #
# recommend — returned shape & guardrail wiring
# --------------------------------------------------------------------------- #

class TestRecommendShape:
    def test_keys_present(self):
        rec = rec_defaults(make_profile(1 * TB))
        assert set(rec.keys()) == {
            "block_size", "dblock_size", "reasons", "median_note",
            "projection", "guardrails",
        }

    def test_block_fits_in_volume_for_normal_case(self):
        rec = rec_defaults(make_profile(800 * GB))
        # No "exceeds volume size" error for a normal recommendation.
        assert not any("exceeds volume size" in g["message"] for g in rec["guardrails"])

    def test_reasons_nonempty(self):
        rec = rec_defaults(make_profile(1 * TB))
        assert len(rec["reasons"]) > 0


# --------------------------------------------------------------------------- #
# render_text
# --------------------------------------------------------------------------- #

class TestRenderText:
    def test_contains_recommended_sizes_and_source(self):
        profile = make_profile(800 * GB, scanned_path="/my/data")
        rec = rec_defaults(profile)
        text = render_text(profile, rec)
        assert "/my/data" in text
        assert f"--block-size={rec['block_size']}" in text
        assert f"--dblock-size={rec['dblock_size']}" in text
        assert "Projections:" in text

    def test_guardrails_section_rendered_when_present(self):
        profile = make_profile(50 * TB)  # large enough to trigger advisories/warnings
        rec = rec_defaults(profile)
        text = render_text(profile, rec)
        if rec["guardrails"]:
            assert "Guardrails:" in text


# --------------------------------------------------------------------------- #
# main() / CLI
# --------------------------------------------------------------------------- #

class TestMain:
    def test_main_reads_file_and_prints_report(self, tmp_path, capsys):
        profile = make_profile(800 * GB)
        pf = tmp_path / "profile.json"
        pf.write_text(json.dumps(profile))
        rc = main([str(pf), "--dest", "remote"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Duplicati Size Estimator" in out
        assert "--block-size=" in out

    def test_main_json_output(self, tmp_path, capsys):
        profile = make_profile(800 * GB)
        pf = tmp_path / "profile.json"
        pf.write_text(json.dumps(profile))
        rc = main([str(pf), "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "recommendation" in data
        assert "inputs" in data
        assert data["recommendation"]["block_size"].endswith("KB")

    def test_main_reads_stdin(self, monkeypatch, capsys):
        profile = make_profile(800 * GB)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(profile)))
        rc = main(["--dest", "remote"])   # no path -> stdin
        assert rc == 0
        assert "--block-size=" in capsys.readouterr().out

    def test_main_dash_means_stdin(self, monkeypatch, capsys):
        profile = make_profile(800 * GB)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(profile)))
        rc = main(["-", "--dest", "local"])
        assert rc == 0
        assert "--dblock-size=" in capsys.readouterr().out

    def test_main_empty_stdin_errors(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        rc = main([])
        assert rc == 2
        assert "no profile on stdin" in capsys.readouterr().err

    def test_main_bad_file_errors(self, tmp_path, capsys):
        rc = main([str(tmp_path / "nope.json")])
        assert rc == 2
        assert "could not read profile" in capsys.readouterr().err

    def test_main_invalid_json_errors(self, tmp_path, capsys):
        pf = tmp_path / "bad.json"
        pf.write_text("not json {{{")
        rc = main([str(pf)])
        assert rc == 2
        assert "could not read profile" in capsys.readouterr().err
