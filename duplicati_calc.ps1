<#
.SYNOPSIS
    duplicati_calc.ps1 - Recommend Duplicati --block-size and --dblock-size (PowerShell port).

.DESCRIPTION
    Reads a profile JSON produced by duplicati_profiler.ps1 (or the Python profiler) and,
    combined with destination/connection characteristics passed as parameters, recommends
    the two size parameters and projects the backend consequences (file and block counts,
    storage).

    This is a straight port of duplicati_calc.py and reproduces its recommendation
    heuristics and the reference-sheet projection formulas EXACTLY, so every front-end
    (this script, duplicati_calc.py, and the HTML calculator) agrees to the number.

    Together with duplicati_profiler.ps1 it lets a Windows user run the whole pipeline with
    no Python at all:

        .\duplicati_profiler.ps1 C:\data | .\duplicati_calc.ps1 -Dest remote

    The profile may be supplied as a file path (-InputFile), piped in on stdin, or passed
    as the first positional argument. Requires PowerShell 7+.

.EXAMPLE
    ./duplicati_calc.ps1 profile.json

.EXAMPLE
    ./duplicati_calc.ps1 -InputFile profile.json -Dest remote -UpMbps 50 -DlCapGb 1

.EXAMPLE
    .\duplicati_profiler.ps1 C:\data | .\duplicati_calc.ps1 -Dest local -ChangePattern archival -Versions 10

.EXAMPLE
    ./duplicati_calc.ps1 profile.json -AsJson
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $InputFile,

    [ValidateSet('local', 'lan', 'remote')]
    [string] $Dest = 'remote',

    [ValidateSet('stable', 'unstable')]
    [string] $Stability = 'stable',

    [ValidateSet('archival', 'normal', 'churn')]
    [string] $ChangePattern = 'normal',

    [double] $UpMbps = 50,

    [double] $DlCapGb = 0,

    [double] $Compression = 100,

    [double] $ChangeAmt = 256,

    [ValidateSet('KB', 'MB', 'GB', 'TB')]
    [string] $ChangeUnit = 'MB',

    [int] $Versions = 0,

    [switch] $AsJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error ("This calculator requires PowerShell 7+ (found $($PSVersionTable.PSVersion)). " +
        "Install it from https://aka.ms/powershell.")
    exit 2
}

# Binary size units (match the Python tool exactly).
$KB = 1024.0
$MB = 1024.0 * $KB
$GB = 1024.0 * $MB
$TB = 1024.0 * $GB

# "Nice" size ladders (identical to duplicati_calc.py).
$BLOCK_STEPS_KB = @(10, 25, 50, 100, 150, 200, 250, 300, 400, 500, 750,
    1000, 1500, 2000, 3000, 4000, 5000,
    6000, 8000, 10000, 15000, 20000, 25000, 30000, 40000, 50000)
$VOL_STEPS_MB = @(10, 25, 50, 100, 150, 200, 250, 300, 500, 750,
    1000, 1500, 2000, 3000, 5000)

# Duplicati hard/soft limits for guardrail validation.
$BLOCK_HARD_MIN = 10 * $KB
$BLOCK_SOFT_MAX = $MB
$VOL_SOFT_MAX = $GB
$BLOCKS_WARN = 5000000
$FILES_WARN = 100000


function Format-PyFixed {
    # Mirror Python's f-string "{:.Nf}" formatting, which rounds half-to-even
    # (banker's rounding). .NET composite format strings ({0:0}) round
    # half-away-from-zero instead, which diverges on exact .5 boundaries — so
    # round explicitly with ToEven, then format the already-rounded value.
    param([double] $v, [int] $decimals)
    $r = [math]::Round($v, $decimals, [System.MidpointRounding]::ToEven)
    $fmt = if ($decimals -le 0) { '0' } else { '0.' + ('0' * $decimals) }
    return ([string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "{0:$fmt}", $r))
}

function Format-Bytes {
    param([double] $b)
    if ($b -ge $GB) {
        if ($b -lt 10 * $GB) { return ((Format-PyFixed ($b / $GB) 2) + ' GB') }
        return ((Format-PyFixed ($b / $GB) 0) + ' GB')
    }
    if ($b -ge $MB) {
        if ($b -lt 10 * $MB) { return ((Format-PyFixed ($b / $MB) 1) + ' MB') }
        return ((Format-PyFixed ($b / $MB) 0) + ' MB')
    }
    if ($b -ge $KB) { return ((Format-PyFixed ($b / $KB) 0) + ' KB') }
    return ('{0} B' -f [long][math]::Round($b, [System.MidpointRounding]::ToEven))
}

function Format-PyFloat {
    # Mirror Python's str(float): whole values render with a trailing ".0"
    # (e.g. 300.0, 8.0, 2.0); fractional values use their minimal decimal form.
    param([double] $v)
    if ($v -eq [math]::Floor($v)) {
        return ('{0:0.0}' -f $v)
    }
    return ([string] $v)
}

function Get-NiceBlockKb {
    param([double] $b)
    $kb = $b / $KB
    $pick = $BLOCK_STEPS_KB[0]
    foreach ($s in $BLOCK_STEPS_KB) { if ($kb -ge $s) { $pick = $s } }
    return $pick   # in KB
}

function Get-NiceVol {
    param([double] $b)
    $mb = $b / $MB
    $pick = $VOL_STEPS_MB[0]
    foreach ($s in $VOL_STEPS_MB) { if ($mb -ge $s) { $pick = $s } }
    if ($pick -ge 1000) {
        return @{ val = [int][math]::Floor($pick / 1000); unit = 'GB' }
    }
    return @{ val = [int] $pick; unit = 'MB' }
}

function ConvertTo-SizeBytes {
    param([double] $val, [string] $unit)
    $mult = switch ($unit) { 'KB' { $KB } 'MB' { $MB } 'GB' { $GB } default { $GB } }
    # Python int() truncates toward zero.
    return [long][math]::Truncate($val * $mult)
}

function Get-Guardrails {
    param([double] $blockFinalBytes, [double] $volFinalBytes, [hashtable] $projection)
    $findings = New-Object System.Collections.Generic.List[object]

    if ($blockFinalBytes -gt $volFinalBytes) {
        $findings.Add(@{ level = 'error'; message =
            "Block size ($(Format-Bytes $blockFinalBytes)) exceeds volume size " +
            "($(Format-Bytes $volFinalBytes)). A dedup block must fit inside a volume — " +
            "increase --dblock-size or decrease --block-size." })
    }
    if ($blockFinalBytes -lt $BLOCK_HARD_MIN) {
        $findings.Add(@{ level = 'error'; message =
            "Block size ($(Format-Bytes $blockFinalBytes)) is below Duplicati's 10 KB hard minimum." })
    }
    if ($blockFinalBytes -gt $BLOCK_SOFT_MAX) {
        $findings.Add(@{ level = 'info'; message =
            "Block size ($(Format-Bytes $blockFinalBytes)) exceeds 1 MB — fine for large/archival " +
            "sets, but evaluate memory use and reduced deduplication first. Remember it is " +
            "immutable after the first backup." })
    }
    if ($volFinalBytes -gt $VOL_SOFT_MAX) {
        $findings.Add(@{ level = 'info'; message =
            "Volume size ($(Format-Bytes $volFinalBytes)) exceeds 1 GB — volumes this large are " +
            "slower to process and make a single-file restore download more data." })
    }
    $blocks = if ($projection.blocks_all) { $projection.blocks_all } else { $projection.blocks_initial }
    if ($blocks -gt $BLOCKS_WARN) {
        $findings.Add(@{ level = 'warn'; message =
            "~$('{0:N0}' -f $blocks) dedup blocks will be tracked in the local database. Beyond a few " +
            "million, database operations (backup, recreate) slow noticeably — consider a larger " +
            "block size (but note it is immutable once set)." })
    }
    $files = if ($projection.total_backend_files_all) { $projection.total_backend_files_all } else { $projection.total_backend_files_initial }
    if ($files -gt $FILES_WARN) {
        $findings.Add(@{ level = 'warn'; message =
            "~$('{0:N0}' -f $files) files will be created at the destination. Some backends (FTP, and " +
            "paginated listings on S3/OneDrive) slow down or cap large file lists — consider a larger " +
            "volume size to reduce the count." })
    }
    return $findings
}

function Get-Recommendation {
    param([object] $Profile)

    $total = [double]($Profile.total_bytes)
    $median = [double]($Profile.median_file_bytes)
    $reasons = New-Object System.Collections.Generic.List[string]

    # ---- BLOCK SIZE ----
    $blockBytes = ($total / $GB) * $KB
    $reasons.Add("Base block ≈ 1 KB per 1 GB of data (800 GB→800 KB, 2 TB→2 MB) → " +
        "$(Format-Bytes $blockBytes) for $(Format-Bytes $total).")
    if ($ChangePattern -eq 'archival') {
        $blockBytes *= 1.5
        $reasons.Add("Archival pattern → block ×1.5 (coarse dedup fine; fewer blocks speed the DB).")
    }
    elseif ($ChangePattern -eq 'churn') {
        $blockBytes *= 0.6
        $reasons.Add("High-churn pattern → block ×0.6 (small edits re-upload whole blocks).")
    }
    if ($blockBytes -lt 100 * $KB) {
        $blockBytes = 100 * $KB
        $reasons.Add("Held at the 100 KB default floor (block size is immutable after first backup).")
    }
    $blockKb = Get-NiceBlockKb $blockBytes
    $blockFinalBytes = $blockKb * $KB

    $medianNote = $null
    if ($median -and $median -lt $blockFinalBytes) {
        $medianNote = "Most files ($(Format-Bytes $median) median) are smaller than one block; " +
            "within-file dedup won't help much — block size mainly affects DB size here."
    }

    # ---- VOLUME (dblock) SIZE ----
    if ($Dest -eq 'local' -or $Dest -eq 'lan') {
        $volBytes = ($total / 10000) * 2
        $reasons.Add("Local/LAN → volume targets ~10,000 files (index+block pairs): total/10000×2 " +
            "= $(Format-Bytes $volBytes).")
        if ($volBytes -lt 50 * $MB) {
            $volBytes = 50 * $MB
            $reasons.Add("Clamped up to the 50 MB default (tiny volumes create needless churn).")
        }
        if ($volBytes -gt 2 * $GB) {
            $volBytes = 2 * $GB
            $reasons.Add("Clamped to 2 GB (larger volumes slow per-file restore processing).")
        }
        if ($Stability -eq 'unstable') {
            $volBytes = [math]::Min($volBytes, 100 * $MB)
            $reasons.Add("Unstable link (e.g. Wi-Fi) → capped at 100 MB so a dropped transfer " +
                "re-sends less.")
        }
    }
    else {
        $volBytes = 200 * $MB
        $reasons.Add("Remote → starting at ~200 MB (range cloud users converged on).")
        if ($UpMbps -gt 0 -and $UpMbps -lt 10) {
            $volBytes = 100 * $MB
            $reasons.Add("Slow upload ($(Format-PyFloat $UpMbps) Mbps) → 100 MB so a failed transfer re-sends less.")
        }
        elseif ($UpMbps -ge 200) {
            $volBytes = 500 * $MB
            $reasons.Add("Fast upload ($(Format-PyFloat $UpMbps) Mbps) → ~500 MB (fewer files, quick transfers).")
        }
        if ($DlCapGb -gt 0) {
            $cap = $DlCapGb * $GB
            if ($volBytes -gt $cap * 0.5) {
                $volBytes = [math]::Max(50 * $MB, $cap * 0.25)
                $reasons.Add("Daily download cap $(Format-PyFloat $DlCapGb) GB → volume held to ~¼ of cap " +
                    "($(Format-Bytes $volBytes)) so a verify/restore won't blow it in one file.")
            }
        }
        if ($Stability -eq 'unstable') {
            $volBytes = [math]::Min($volBytes, 100 * $MB)
            $reasons.Add("Unstable connection → capped at 100 MB to limit re-transmit cost.")
        }
    }
    $nv = Get-NiceVol $volBytes
    $volVal = $nv.val
    $volUnit = $nv.unit
    $volFinalBytes = ConvertTo-SizeBytes $volVal $volUnit

    # ---- PROJECTIONS (reference-sheet formulas) ----
    $comp = $Compression / 100.0
    $cu = switch ($ChangeUnit) { 'KB' { $KB } 'MB' { $MB } 'GB' { $GB } 'TB' { $TB } }
    $changeBytes = $ChangeAmt * $cu

    # Python round() is banker's rounding; [math]::Round defaults to the same (ToEven).
    $dataInit = [long][math]::Round(($total * $comp) / $volFinalBytes) + 1
    if (($changeBytes * $comp) -le $volFinalBytes) {
        $dataAll = $Versions + $dataInit
    }
    else {
        $dataAll = [long][math]::Ceiling(($changeBytes * $comp) / $volFinalBytes) * $Versions + $dataInit
    }
    $indexInit = $dataInit; $indexAll = $dataAll
    $listInit = 1; $listAll = $Versions
    $filesInit = $dataInit + $indexInit + $listInit
    $filesAll = $dataAll + $indexAll + $listAll
    $blocksInit = [long][math]::Ceiling($total / $blockFinalBytes)
    $blocksAll = $blocksInit + $Versions * [long][math]::Ceiling($changeBytes / $blockFinalBytes)
    $avgBlocks = if ($dataInit) { [long][math]::Ceiling($blocksInit / $dataInit) } else { 0 }
    $storeInit = $total * $comp
    $storeAll = $total * $comp + $Versions * $changeBytes * $comp

    $projection = @{
        storage_initial_bytes       = [long][math]::Truncate($storeInit)
        storage_all_bytes           = [long][math]::Truncate($storeAll)
        data_files_initial          = $dataInit
        data_files_all              = $dataAll
        index_files_initial         = $indexInit
        index_files_all             = $indexAll
        list_files_initial          = $listInit
        list_files_all              = $listAll
        total_backend_files_initial = $filesInit
        total_backend_files_all     = $filesAll
        blocks_initial              = $blocksInit
        blocks_all                  = $blocksAll
        avg_blocks_per_data_file    = $avgBlocks
    }

    return @{
        block_size  = "${blockKb}KB"
        dblock_size = "$volVal$volUnit"
        reasons     = $reasons
        median_note = $medianNote
        projection  = $projection
        guardrails  = (Get-Guardrails $blockFinalBytes $volFinalBytes $projection)
    }
}

function Format-Report {
    param([object] $Profile, [hashtable] $Rec)
    $pr = $Rec.projection
    $sep = ('=' * 60)
    $out = New-Object System.Collections.Generic.List[string]
    $out.Add($sep)
    $out.Add('Duplicati Size Estimator — Recommendation')
    $out.Add($sep)
    if ($Profile.PSObject.Properties.Name -contains 'scanned_path' -and $Profile.scanned_path) {
        $out.Add("Source: $($Profile.scanned_path)")
    }
    $fc = [long]($Profile.file_count)
    $out.Add("  $('{0:N0}' -f $fc) files · $(Format-Bytes ([double]$Profile.total_bytes)) total · " +
        "median $(Format-Bytes ([double]$Profile.median_file_bytes))")
    $out.Add('')
    $out.Add("  --block-size=$($Rec.block_size)   (dedup chunk — IMMUTABLE after first backup)")
    $out.Add("  --dblock-size=$($Rec.dblock_size)   (remote volume — changeable anytime)")
    $out.Add('')
    $out.Add('Why these values:')
    foreach ($r in $Rec.reasons) { $out.Add("  • $r") }
    if ($Rec.median_note) { $out.Add("  ⚠ $($Rec.median_note)") }
    $out.Add('')

    # Right-align numeric columns to width 12, with thousands separators, mirroring Python's {:>12,}.
    function Col([object] $v) {
        if ($v -is [string]) { return $v.PadLeft(12) }
        return ('{0:N0}' -f [long]$v).PadLeft(12)
    }
    $out.Add('Projections:                     initial        all versions')
    $out.Add("  Backend storage          $(Col (Format-Bytes ([double]$pr.storage_initial_bytes)))   $(Col (Format-Bytes ([double]$pr.storage_all_bytes)))")
    $out.Add("  Data (dblock) files      $(Col $pr.data_files_initial)   $(Col $pr.data_files_all)")
    $out.Add("  Index files              $(Col $pr.index_files_initial)   $(Col $pr.index_files_all)")
    $out.Add("  List files               $(Col $pr.list_files_initial)   $(Col $pr.list_files_all)")
    $out.Add("  Total backend files      $(Col $pr.total_backend_files_initial)   $(Col $pr.total_backend_files_all)")
    $out.Add("  Dedup blocks tracked     $(Col $pr.blocks_initial)   $(Col $pr.blocks_all)")
    $out.Add("  Avg blocks / data file   $(Col $pr.avg_blocks_per_data_file)")

    $guards = @($Rec.guardrails)   # force array: PS unwraps 0/1-element collections
    if ($guards.Count -gt 0) {
        $out.Add('')
        $out.Add('Guardrails:')
        $marks = @{ error = '✗ ERROR'; warn = '⚠ WARNING'; info = 'ℹ note' }
        foreach ($g in $guards) {
            $mark = if ($marks.ContainsKey($g.level)) { $marks[$g.level] } else { $g.level }
            $out.Add("  ${mark}: $($g.message)")
        }
    }
    $out.Add($sep)
    return ($out -join "`n")
}

# --- Load the profile: -InputFile / positional path / stdin ----------------
$pathArg = $InputFile
if ([string]::IsNullOrEmpty($pathArg)) { $pathArg = '-' }

# Read the raw profile text first (stdin or file), so the empty-stdin case can be
# reported cleanly without being swallowed by the JSON-parse catch below.
if ($pathArg -eq '-') {
    $raw = [Console]::In.ReadToEnd()
    if ([string]::IsNullOrWhiteSpace($raw)) {
        [Console]::Error.WriteLine("error: no profile on stdin. Pass a file path, or pipe a profile in: " +
            "duplicati_profiler.ps1 -Path SRC | duplicati_calc.ps1")
        exit 2
    }
}
else {
    try {
        $raw = Get-Content -LiteralPath $pathArg -Raw
    }
    catch {
        [Console]::Error.WriteLine("error: could not read profile: $($_.Exception.Message)")
        exit 2
    }
}

try {
    $profile = $raw | ConvertFrom-Json
}
catch {
    [Console]::Error.WriteLine("error: could not read profile: $($_.Exception.Message)")
    exit 2
}

$rec = Get-Recommendation -Profile $profile

if ($AsJson) {
    $inputs = @{
        dest = $Dest; stability = $Stability; change_pattern = $ChangePattern
        up_mbps = $UpMbps; dl_cap_gb = $DlCapGb; compression = $Compression
        change_amt = $ChangeAmt; change_unit = $ChangeUnit; versions = $Versions
    }
    @{ recommendation = $rec; inputs = $inputs } | ConvertTo-Json -Depth 8
}
else {
    Format-Report -Profile $profile -Rec $rec
}
