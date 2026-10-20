#!/usr/bin/env python3
"""
duplicati_calc.py — Recommend Duplicati --block-size and --dblock-size.

Reads a profile JSON produced by duplicati_profiler.py and, combined with
destination/connection characteristics passed as flags, recommends the two
size parameters and projects the backend consequences (file and block counts,
storage) using the same model as the companion HTML calculator.

This is the "decide" half of the profiler → calculator pipeline. It shares the
exact recommendation heuristics and the reference-sheet projection formulas
used by index.html, so both front-ends agree to the number.

Usage:
    python3 duplicati_calc.py profile.json
    python3 duplicati_calc.py profile.json --dest remote --up-mbps 50 --dl-cap-gb 1
    python3 duplicati_calc.py profile.json --dest local --change-pattern archival --versions 10
    python3 duplicati_calc.py profile.json --json          # machine-readable output

Heuristic provenance (Duplicati forum "Choosing Sizes" + threads):
  * block-size in KB ≈ total data in GB  (800 GB → 800 KB, 2 TB → 2 MB)
  * floor at the 100 KB default (enforced AFTER change multipliers)
  * local/LAN volume targets ~10k files (index+block pairs): total/10000*2
  * remote volume converges on ~200 MB, tuned by bandwidth / download cap / stability

Projection formulas are from the reference Google-Sheets calculator
("This one actually works right" tab), validated to reproduce its outputs.

Standard library only; Python 3.7+.
"""

import argparse
import json
import math
import sys

KB = 1024
MB = 1024 * KB
GB = 1024 * MB
TB = 1024 * GB

BLOCK_STEPS_KB = [10, 25, 50, 100, 150, 200, 250, 300, 400, 500, 750,
                  1000, 1500, 2000, 3000, 4000, 5000,
                  6000, 8000, 10000, 15000, 20000, 25000, 30000, 40000, 50000]
VOL_STEPS_MB = [10, 25, 50, 100, 150, 200, 250, 300, 500, 750,
                1000, 1500, 2000, 3000, 5000]


def fmt_bytes(b):
    if b >= GB:
        return f"{b/GB:.2f} GB" if b < 10 * GB else f"{b/GB:.0f} GB"
    if b >= MB:
        return f"{b/MB:.1f} MB" if b < 10 * MB else f"{b/MB:.0f} MB"
    if b >= KB:
        return f"{b/KB:.0f} KB"
    return f"{round(b)} B"


def nice_block_kb(b):
    kb = b / KB
    pick = BLOCK_STEPS_KB[0]
    for s in BLOCK_STEPS_KB:
        if kb >= s:
            pick = s
    return pick  # in KB


def nice_vol(b):
    mb = b / MB
    pick = VOL_STEPS_MB[0]
    for s in VOL_STEPS_MB:
        if mb >= s:
            pick = s
    return (pick // 1000, "GB") if pick >= 1000 else (pick, "MB")


def size_str_block(kb):
    return f"{kb}KB"


def size_str_vol(val, unit):
    return f"{val}{unit}"


def parse_size_to_bytes(val, unit):
    return int(val * (KB if unit == "KB" else MB if unit == "MB" else GB))


# Duplicati hard/soft limits for guardrail validation.
BLOCK_HARD_MIN = 10 * KB        # Duplicati's absolute minimum --block-size
BLOCK_SOFT_MAX = MB             # above this, forum advises evaluating memory/dedup impact
VOL_SOFT_MAX = GB               # above this, volumes get slow to process
BLOCKS_WARN = 5_000_000         # local DB bloat beyond a few million blocks
FILES_WARN = 100_000            # destination listing limits (FTP, pagination)


def validate(block_final_bytes, vol_final_bytes, projection):
    """Return a list of {level, message} guardrail findings. level: 'error'|'warn'|'info'."""
    findings = []
    # Hard errors first.
    if block_final_bytes > vol_final_bytes:
        findings.append({"level": "error",
            "message": f"Block size ({fmt_bytes(block_final_bytes)}) exceeds volume size "
                       f"({fmt_bytes(vol_final_bytes)}). A dedup block must fit inside a volume — "
                       f"increase --dblock-size or decrease --block-size."})
    if block_final_bytes < BLOCK_HARD_MIN:
        findings.append({"level": "error",
            "message": f"Block size ({fmt_bytes(block_final_bytes)}) is below Duplicati's 10 KB "
                       f"hard minimum."})
    # Advisories.
    if block_final_bytes > BLOCK_SOFT_MAX:
        findings.append({"level": "info",
            "message": f"Block size ({fmt_bytes(block_final_bytes)}) exceeds 1 MB — fine for large/"
                       f"archival sets, but evaluate memory use and reduced deduplication first. "
                       f"Remember it is immutable after the first backup."})
    if vol_final_bytes > VOL_SOFT_MAX:
        findings.append({"level": "info",
            "message": f"Volume size ({fmt_bytes(vol_final_bytes)}) exceeds 1 GB — volumes this large "
                       f"are slower to process and make a single-file restore download more data."})
    # Scale warnings from the projection.
    blocks = projection.get("blocks_all") or projection.get("blocks_initial", 0)
    if blocks > BLOCKS_WARN:
        findings.append({"level": "warn",
            "message": f"~{blocks:,} dedup blocks will be tracked in the local database. Beyond a few "
                       f"million, database operations (backup, recreate) slow noticeably — consider a "
                       f"larger block size (but note it is immutable once set)."})
    files = projection.get("total_backend_files_all") or projection.get("total_backend_files_initial", 0)
    if files > FILES_WARN:
        findings.append({"level": "warn",
            "message": f"~{files:,} files will be created at the destination. Some backends (FTP, and "
                       f"paginated listings on S3/OneDrive) slow down or cap large file lists — "
                       f"consider a larger volume size to reduce the count."})
    return findings


def recommend(profile, dest, stability, change_pattern, up_mbps, dl_cap_gb,
              compression_pct, change_amt, change_unit, versions):
    total = profile.get("total_bytes", 0)
    median = profile.get("median_file_bytes", 0)
    reasons = []

    # ---- BLOCK SIZE ----
    # Base: block size in KB ≈ total data in GB.
    block_bytes = (total / GB) * KB
    reasons.append(
        f"Base block ≈ 1 KB per 1 GB of data (800 GB→800 KB, 2 TB→2 MB) "
        f"→ {fmt_bytes(block_bytes)} for {fmt_bytes(total)}.")
    # Change-pattern multipliers FIRST.
    if change_pattern == "archival":
        block_bytes *= 1.5
        reasons.append("Archival pattern → block ×1.5 (coarse dedup fine; fewer blocks speed the DB).")
    elif change_pattern == "churn":
        block_bytes *= 0.6
        reasons.append("High-churn pattern → block ×0.6 (small edits re-upload whole blocks).")
    # Floor LAST, so churn can never push below the 100 KB default.
    if block_bytes < 100 * KB:
        block_bytes = 100 * KB
        reasons.append("Held at the 100 KB default floor (block size is immutable after first backup).")
    block_kb = nice_block_kb(block_bytes)
    block_final_bytes = block_kb * KB

    median_note = None
    if median and median < block_final_bytes:
        median_note = (f"Most files ({fmt_bytes(median)} median) are smaller than one block; "
                       f"within-file dedup won't help much — block size mainly affects DB size here.")

    # ---- VOLUME (dblock) SIZE ----
    if dest in ("local", "lan"):
        vol_bytes = (total / 10000) * 2
        reasons.append("Local/LAN → volume targets ~10,000 files (index+block pairs): total/10000×2 "
                       f"= {fmt_bytes(vol_bytes)}.")
        if vol_bytes < 50 * MB:
            vol_bytes = 50 * MB
            reasons.append("Clamped up to the 50 MB default (tiny volumes create needless churn).")
        if vol_bytes > 2 * GB:
            vol_bytes = 2 * GB
            reasons.append("Clamped to 2 GB (larger volumes slow per-file restore processing).")
    else:
        vol_bytes = 200 * MB
        reasons.append("Remote → starting at ~200 MB (range cloud users converged on).")
        if 0 < up_mbps < 10:
            vol_bytes = 100 * MB
            reasons.append(f"Slow upload ({up_mbps} Mbps) → 100 MB so a failed transfer re-sends less.")
        elif up_mbps >= 200:
            vol_bytes = 500 * MB
            reasons.append(f"Fast upload ({up_mbps} Mbps) → ~500 MB (fewer files, quick transfers).")
        if dl_cap_gb > 0:
            cap = dl_cap_gb * GB
            if vol_bytes > cap * 0.5:
                vol_bytes = max(50 * MB, cap * 0.25)
                reasons.append(f"Daily download cap {dl_cap_gb} GB → volume held to ~¼ of cap "
                               f"({fmt_bytes(vol_bytes)}) so a verify/restore won't blow it in one file.")
        if stability == "unstable":
            vol_bytes = min(vol_bytes, 100 * MB)
            reasons.append("Unstable connection → capped at 100 MB to limit re-transmit cost.")
    vol_val, vol_unit = nice_vol(vol_bytes)
    vol_final_bytes = parse_size_to_bytes(vol_val, vol_unit)

    # ---- PROJECTIONS (reference-sheet formulas) ----
    comp = compression_pct / 100.0
    cu = {"KB": KB, "MB": MB, "GB": GB, "TB": TB}[change_unit]
    change_bytes = change_amt * cu

    data_init = round((total * comp) / vol_final_bytes) + 1
    if change_bytes * comp <= vol_final_bytes:
        data_all = versions + data_init
    else:
        data_all = math.ceil((change_bytes * comp) / vol_final_bytes) * versions + data_init
    index_init, index_all = data_init, data_all
    list_init, list_all = 1, versions
    files_init = data_init + index_init + list_init
    files_all = data_all + index_all + list_all
    blocks_init = math.ceil(total / block_final_bytes)
    blocks_all = blocks_init + versions * math.ceil(change_bytes / block_final_bytes)
    avg_blocks = math.ceil(blocks_init / data_init) if data_init else 0
    store_init = total * comp
    store_all = total * comp + versions * change_bytes * comp

    projection = {
        "storage_initial_bytes": int(store_init),
        "storage_all_bytes": int(store_all),
        "data_files_initial": data_init,
        "data_files_all": data_all,
        "index_files_initial": index_init,
        "index_files_all": index_all,
        "list_files_initial": list_init,
        "list_files_all": list_all,
        "total_backend_files_initial": files_init,
        "total_backend_files_all": files_all,
        "blocks_initial": blocks_init,
        "blocks_all": blocks_all,
        "avg_blocks_per_data_file": avg_blocks,
    }
    return {
        "block_size": size_str_block(block_kb),
        "dblock_size": size_str_vol(vol_val, vol_unit),
        "reasons": reasons,
        "median_note": median_note,
        "projection": projection,
        "guardrails": validate(block_final_bytes, vol_final_bytes, projection),
    }


def render_text(profile, rec):
    p = profile
    out = []
    out.append("=" * 60)
    out.append("Duplicati Size Estimator — Recommendation")
    out.append("=" * 60)
    if p.get("scanned_path"):
        out.append(f"Source: {p['scanned_path']}")
    out.append(f"  {p.get('file_count', 0):,} files · {fmt_bytes(p.get('total_bytes', 0))} total · "
               f"median {fmt_bytes(p.get('median_file_bytes', 0))}")
    out.append("")
    out.append(f"  --block-size={rec['block_size']}   (dedup chunk — IMMUTABLE after first backup)")
    out.append(f"  --dblock-size={rec['dblock_size']}   (remote volume — changeable anytime)")
    out.append("")
    out.append("Why these values:")
    for r in rec["reasons"]:
        out.append(f"  • {r}")
    if rec["median_note"]:
        out.append(f"  ⚠ {rec['median_note']}")
    out.append("")
    pr = rec["projection"]
    out.append("Projections:                     initial        all versions")
    out.append(f"  Backend storage          {fmt_bytes(pr['storage_initial_bytes']):>12}   {fmt_bytes(pr['storage_all_bytes']):>12}")
    out.append(f"  Data (dblock) files      {pr['data_files_initial']:>12,}   {pr['data_files_all']:>12,}")
    out.append(f"  Index files              {pr['index_files_initial']:>12,}   {pr['index_files_all']:>12,}")
    out.append(f"  List files               {pr['list_files_initial']:>12,}   {pr['list_files_all']:>12,}")
    out.append(f"  Total backend files      {pr['total_backend_files_initial']:>12,}   {pr['total_backend_files_all']:>12,}")
    out.append(f"  Dedup blocks tracked     {pr['blocks_initial']:>12,}   {pr['blocks_all']:>12,}")
    out.append(f"  Avg blocks / data file   {pr['avg_blocks_per_data_file']:>12,}")
    guards = rec.get("guardrails", [])
    if guards:
        out.append("")
        out.append("Guardrails:")
        marks = {"error": "✗ ERROR", "warn": "⚠ WARNING", "info": "ℹ note"}
        for g in guards:
            out.append(f"  {marks.get(g['level'], g['level'])}: {g['message']}")
    out.append("=" * 60)
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Recommend Duplicati block/dblock sizes from a source profile.")
    ap.add_argument("profile", nargs="?", default="-",
                    help="Path to profile JSON (from duplicati_profiler.py). "
                         "Omit or use '-' to read the profile from stdin, so the profiler "
                         "can be piped in: duplicati_profiler.py SRC | duplicati_calc.py")
    ap.add_argument("--dest", choices=["local", "lan", "remote"], default="remote",
                    help="Destination type (default: remote)")
    ap.add_argument("--stability", choices=["stable", "unstable"], default="stable")
    ap.add_argument("--change-pattern", choices=["archival", "normal", "churn"], default="normal")
    ap.add_argument("--up-mbps", type=float, default=50, help="Upload speed in Mbps (remote)")
    ap.add_argument("--dl-cap-gb", type=float, default=0, help="Daily download cap in GB, 0=none (remote)")
    ap.add_argument("--compression", type=float, default=100, help="Compression %% (100=none)")
    ap.add_argument("--change-amt", type=float, default=256, help="New/changed data per backup (amount)")
    ap.add_argument("--change-unit", choices=["KB", "MB", "GB", "TB"], default="MB")
    ap.add_argument("--versions", type=int, default=0, help="Max versions retained (0=initial only)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = ap.parse_args(argv)

    try:
        if args.profile == "-":
            # Read the profile from stdin so the profiler can be piped in.
            raw = sys.stdin.read()
            if not raw.strip():
                print("error: no profile on stdin. Pass a file path, or pipe a profile in: "
                      "duplicati_profiler.py SRC | duplicati_calc.py", file=sys.stderr)
                return 2
            profile = json.loads(raw)
        else:
            with open(args.profile) as f:
                profile = json.load(f)
    except (OSError, ValueError) as e:
        print(f"error: could not read profile: {e}", file=sys.stderr)
        return 2

    rec = recommend(profile, args.dest, args.stability, args.change_pattern,
                    args.up_mbps, args.dl_cap_gb, args.compression,
                    args.change_amt, args.change_unit, args.versions)

    if args.json:
        print(json.dumps({"recommendation": rec, "inputs": vars(args)}, indent=2))
    else:
        print(render_text(profile, rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
