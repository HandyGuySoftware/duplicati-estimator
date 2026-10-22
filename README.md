# Duplicati Size Estimator

Recommends optimal **`--block-size`** and **`--dblock-size`** values for a
[Duplicati](https://duplicati.com/) backup job, based on your actual data and where
you're backing it up to.

Duplicati splits files into fixed-size **blocks** (for deduplication) and packs those
blocks into remote **volumes** (dblocks). The two sizes have very different
consequences:

- **`--block-size`** is **immutable** — it's fixed at the first backup and can never be
  changed without starting over. Too small bloats the local database; too large wastes
  deduplication.
- **`--dblock-size`** is **changeable anytime** — it trades off the number of files at the
  destination against how much data a single restore or verify has to download.

Picking these by hand means juggling your data's size distribution, your database limits,
and your connection's bandwidth. This tool does that reasoning for you and shows the
projected consequences before you commit.

---

## How it works

It's a two-step pipeline:

```
  your files ──► profiler ──► profile.json ──► estimator ──► recommended sizes
                                                             + projections
                                                             + guardrails
```

1. **Profile** your source data with a small script. It measures the file count and
   size distribution and writes a compact `profile.json`.
2. **Estimate** by feeding that profile — plus your destination details — to the
   calculator (a web page or a command-line tool). It recommends the two sizes and
   projects the backend file and block counts you'll end up with.

The two steps are split on purpose: profiling has to run where the files live (which may
be a remote NAS or server), while the recommendation math is portable. The small
`profile.json` is what moves between machines.

**Privacy:** the profile records only aggregate statistics and a short list of the
largest *file sizes* — never file names, paths, or contents. It's safe to copy between
machines and safe to share.

---

## Step 1 — Profile your source data

Use **either** profiler below. Both produce the identical `profile.json`, so pick
whichever fits your platform.

### Python (macOS, Linux, Windows — Python 3.7+)

Standard-library only, so it runs anywhere — including directly on a NAS or server over
SSH.

```bash
python3 duplicati_profiler.py /path/to/source -o profile.json

# Profile several locations at once — directories (walked) and/or individual files.
# They need not share a tree; overlapping paths are counted once.
python3 duplicati_profiler.py /data/photos /data/docs /mnt/archive.iso -o profile.json
```

Options:

| Option | Description |
|---|---|
| `path...` | One or more directories and/or files to profile (aggregated into one profile) |
| `-o`, `--output FILE` | Write the profile to a file (default: print to stdout) |
| `--follow-symlinks` | Follow symlinks (default: skip them) |
| `--top-n N` | How many of the largest file sizes to record (default: 25) |

### PowerShell (Windows and cross-platform — PowerShell 7+)

A port that needs no Python — convenient on Windows. Requires **PowerShell 7+** (a free
install from [aka.ms/powershell](https://aka.ms/powershell); it runs side-by-side with
the Windows PowerShell 5.1 that ships in Windows).

```powershell
.\duplicati_profiler.ps1 C:\path\to\source -Output profile.json

# Several locations at once — pass them positionally (directories and/or files).
# They need not share a tree; overlapping paths are counted once.
.\duplicati_profiler.ps1 C:\data\photos C:\data\docs D:\archive.iso -Output profile.json
```

Options:

| Option | Description |
|---|---|
| `<paths...>` | One or more directories and/or files to profile, passed positionally (aggregated into one profile) |
| `-Output FILE` | Write the profile to a file (default: print to stdout) |
| `-FollowSymlinks` | Follow symlinks / reparse points (default: skip them) |
| `-TopN N` | How many of the largest file sizes to record (default: 25) |

> **Running scripts on Windows:** PowerShell blocks local script execution by default, so
> `.\duplicati_profiler.ps1` may fail with a message about scripts being disabled on this
> system. To allow it for your own user (no admin rights needed), run once:
>
> ```powershell
> Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
> ```
>
> `RemoteSigned` lets local scripts you wrote run while still requiring downloaded scripts to
> be signed. Alternatively, run a single script without changing your policy:
> `pwsh -ExecutionPolicy Bypass -File .\duplicati_profiler.ps1 ...`

> **Tip:** pass multiple paths positionally (as above), not as `-Path a b c` — naming
> `-Path` explicitly only takes the first value.

> **Why 7+?** Windows PowerShell 5.1's `ConvertTo-Json` can serialize a single-element
> list as a bare value instead of an array, which would break the profile schema for a
> one-file scan. PowerShell 7+ handles this correctly. The script checks your version at
> startup and tells you if you need to upgrade.

---

## Step 2 — Run the estimator

You can use the graphical calculator or the command-line tool — they share the same
recommendation logic and projection formulas, so they agree to the number.

### Option A — Web calculator (`index.html`)

Open `index.html` in any browser (no server or install needed — just open the file), or
use the hosted copy on GitHub Pages. Then:

1. **Load** your `profile.json` (file picker or paste the JSON). The file size
   distribution appears immediately.
2. **Set** your destination type and connection details.
3. **Calculate** — the recommended sizes, projections, and guardrails appear below, and
   the reference rules are in the Guidelines section.

All computation happens locally in your browser; nothing is uploaded.

For your convenience, there is also an [on-line graphical calculator](https://handyguysoftware.github.io/duplicati-estimator/) avaialble for your use. Both share the same code, so it's just personal preference as to which you'd like to use.

### Option B — Command line (`duplicati_calc.py`)

```bash
python3 duplicati_calc.py profile.json --dest remote --up-mbps 50
```

Common examples:

```bash
# Local/NAS destination, mostly-archival data, keeping 10 versions
python3 duplicati_calc.py profile.json --dest local --change-pattern archival --versions 10

# Remote cloud with a slow, metered connection
python3 duplicati_calc.py profile.json --dest remote --up-mbps 8 --dl-cap-gb 2

# Machine-readable output
python3 duplicati_calc.py profile.json --json
```

Options:

| Option | Default | Description |
|---|---|---|
| `profile` | *(required)* | Path to the `profile.json` from Step 1 |
| `--dest {local,lan,remote}` | `remote` | Destination type |
| `--stability {stable,unstable}` | `stable` | Connection stability (remote) |
| `--change-pattern {archival,normal,churn}` | `normal` | How much data changes between backups |
| `--up-mbps N` | `50` | Upload speed in Mbps (remote) |
| `--dl-cap-gb N` | `0` | Daily download cap in GB (`0` = none, remote) |
| `--compression N` | `100` | Percent of source that lands at the destination after compression (`100` = assume none) |
| `--change-amt N` | `256` | New/changed data per backup (amount) |
| `--change-unit {KB,MB,GB,TB}` | `MB` | Unit for `--change-amt` |
| `--versions N` | `0` | Max versions retained (`0` = initial backup only) |
| `--json` | | Emit machine-readable JSON instead of a report |

`duplicati_calc.py` can also read the profile from **stdin** (omit the path argument, or
pass `-`), so you can pipe the profiler straight into it instead of saving a file:

```bash
# Python profiler piped into the calculator
python3 duplicati_profiler.py /path/to/source | python3 duplicati_calc.py --dest remote

# The PowerShell profiler works the same way — the JSON contract is language-agnostic
.\duplicati_profiler.ps1 C:\path\to\source | python3 duplicati_calc.py --dest remote
```

The profiler writes the JSON profile to stdout and its progress to stderr, so only the
profile flows through the pipe.

### Option B-PS — Command line, no Python (`duplicati_calc.ps1`)

There's a PowerShell 7+ port of the calculator too, so a Windows user can run the **entire
pipeline without installing Python**:

```powershell
# Two steps, or...
.\duplicati_profiler.ps1 C:\path\to\source -Output profile.json
.\duplicati_calc.ps1 profile.json -Dest remote -UpMbps 50

# ...pipe the profiler straight into the calculator
.\duplicati_profiler.ps1 C:\path\to\source | .\duplicati_calc.ps1 -Dest remote
```

It takes the same options as the Python CLI (PowerShell-style names: `-Dest`, `-UpMbps`,
`-DlCapGb`, `-ChangePattern`, `-Versions`, `-Compression`, `-ChangeAmt`, `-ChangeUnit`,
`-Stability`, `-AsJson`) and produces identical output. Like the Python version it reads a
profile from a file path, a positional argument, or stdin.

> **Running scripts on Windows:** like the profiler, `.\duplicati_calc.ps1` may be blocked
> by PowerShell's default execution policy. To enable it for your own user (no admin needed),
> run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` — or run a single script
> without changing your policy: `pwsh -ExecutionPolicy Bypass -File .\duplicati_calc.ps1 ...`
> (see the PowerShell profiler section under Step 1 for the full explanation).

---

## What the recommendations are based on

These are first-pass heuristics grounded in Duplicati documentation and community forum
guidance — a sensible starting point to tune against your own data, not hard limits.

**Block size** (immutable, so it errs toward future-proofing):

- Base rule: roughly **1 KB of block size per 1 GB of data** (800 GB → 800 KB,
  2 TB → 2 MB), which keeps the dedup-block count — and the local database — manageable.
- Adjusted for change pattern: archival data tolerates larger blocks; high-churn data
  favors smaller ones.
- Floored at Duplicati's **100 KB** default.
- For multi-TB sets this lands in the 5–50 MB range, matching what real large-scale users
  run (the goal is to keep the tracked block count roughly under a few million).

**Volume size** (changeable, so it's tuned to the destination):

- **Local / LAN:** targets about 10,000 backend files, clamped to 50 MB – 2 GB.
- **Remote / cloud:** starts around 200 MB, then adjusts for upload speed, any daily
  download cap (a restore pulls a whole volume), and connection stability.

**Compression:** leave at 100% for already-compressed data (photos, video, music,
zip/office files); lower it for compressible data (roughly 70–90% for documents and code,
40–60% for logs and text). The 100% default never under-estimates storage or file counts.

**Guardrails** flag the important failure modes: block larger than volume, block below
Duplicati's 10 KB minimum, and scale thresholds (millions of blocks slow the database;
~100,000+ backend files strain some destinations).

Sources: ["Choosing sizes in Duplicati"](https://www.duplicati.com/articles/Choosing-Sizes/)
and related threads from the [Duplicati Forum](https://forum.duplicati.com/) (block-size
rules of thumb, cloud-volume convergence, performance benchmarks), as well as the awesome
[Duplicati Storage Calculator](https://docs.google.com/spreadsheets/d/1woKfEqtTHxSidztNqjfmQELyIHu_yE1z0NLBZ8dxOCE/edit?pli=1&gid=1374005848#gid=1374005848)
from Duplicati Forum user **kees-z**.

---

## Files

| File | What it is |
|---|---|
| `duplicati_profiler.py` | Source-tree profiler (Python) |
| `duplicati_profiler.ps1` | Source-tree profiler (PowerShell 7+) |
| `index.html` | Web calculator (open in a browser, or hosted via GitHub Pages) |
| `duplicati_calc.py` | Command-line calculator (Python) |
| `duplicati_calc.ps1` | Command-line calculator (PowerShell 7+) — lets Windows run the whole pipeline without Python |
| `sample_profile_downloads.json` | An example profile you can load to try the calculator |
| `tests/` | pytest suite for the Python tools (`duplicati_profiler.py`, `duplicati_calc.py`) |

---

## Tests

The Python tools have a pytest suite under `tests/`. To run it:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests/
```

The suite covers the profiler (bucketing, aggregation across multiple paths, overlap
de-duplication, median/mean, symlink handling, the CLI) and the calculator (size ladders,
the recommendation heuristics for every destination/connection branch, the guardrails, and
the projection formulas — including the reference sheet's published worked example).

---

## Command-line help

**Python tools** — use `--help` (or `-h`) on either script:

```bash
python3 duplicati_profiler.py --help
python3 duplicati_calc.py --help
```

**PowerShell tools** — use `Get-Help` (PowerShell's built-in help; there is no `-h` flag):

```powershell
Get-Help ./duplicati_profiler.ps1            # synopsis, syntax, and description
Get-Help ./duplicati_profiler.ps1 -Detailed  # adds parameter descriptions and examples
Get-Help ./duplicati_profiler.ps1 -Examples  # just the usage examples
Get-Help ./duplicati_profiler.ps1 -Full      # everything

Get-Help ./duplicati_calc.ps1 -Full          # same for the calculator
```

---

## Quick start

```bash
# Everything in one line, piping the profiler into the calculator:
python3 duplicati_profiler.py ~/Documents | python3 duplicati_calc.py --dest remote

# ...or in two steps (e.g. profile on a remote NAS, recommend on your laptop):
python3 duplicati_profiler.py ~/Documents -o profile.json   # step 1
python3 duplicati_calc.py profile.json --dest remote         # step 2 (CLI)
# or open index.html in a browser and load profile.json
```

```powershell
# No Python? The all-PowerShell pipeline does the same:
./duplicati_profiler.ps1 -Path ~/Documents | ./duplicati_calc.ps1 -Dest remote
```

---

## License

MIT — see [LICENSE](LICENSE). Free to use, modify, and distribute, provided the copyright
notice and license text are retained in copies and derivative works.
