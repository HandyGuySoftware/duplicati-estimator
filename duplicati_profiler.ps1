<#
.SYNOPSIS
    duplicati_profiler.ps1 - Source-tree profiler for the Duplicati size estimator (PowerShell port).

.DESCRIPTION
    Walks a directory tree, measures file count and a size-distribution histogram,
    and emits a compact JSON "profile" to stdout (or a file with -Output). The profile
    is the input contract for the companion HTML calculator (index.html),
    which recommends optimal --block-size and --dblock-size values.

    This is a straight port of duplicati_profiler.py and produces JSON matching the
    SAME schema (schema_version 1), so the two profilers are interchangeable. The
    profile contains only aggregate statistics plus a bounded list of the largest
    file SIZES (sizes only, never names/paths), so it is safe to move between machines.

    Requires PowerShell 7+ (cross-platform: Windows, macOS, Linux). PowerShell 7
    is a free install from https://aka.ms/powershell and runs side-by-side with the
    Windows PowerShell 5.1 that ships in Windows. (5.1 is not supported because its
    ConvertTo-Json can unwrap a single-element array, which would break the schema.)

.PARAMETER Path
    Directory to scan.

.PARAMETER Output
    Write the JSON profile to this file (default: stdout).

.PARAMETER FollowSymlinks
    Follow symlinks / reparse points (default: skip them).

.PARAMETER TopN
    How many largest file sizes to record (default 25).

.EXAMPLE
    ./duplicati_profiler.ps1 -Path C:\Users\me\Documents

.EXAMPLE
    ./duplicati_profiler.ps1 -Path C:\data -Output profile.json

.EXAMPLE
    ./duplicati_profiler.ps1 -Path C:\data -FollowSymlinks
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0, ValueFromRemainingArguments = $true)]
    [string[]] $Path,

    [Alias('o')]
    [string] $Output,

    [switch] $FollowSymlinks,

    [int] $TopN = 25
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Error ("This profiler requires PowerShell 7+ (found $($PSVersionTable.PSVersion)). " +
        "Install it from https://aka.ms/powershell; it runs alongside Windows PowerShell 5.1.")
    exit 2
}

# Byte-size unit constants (binary, matching the Python profiler).
$KB = 1024L
$MB = 1024L * $KB
$GB = 1024L * $MB

# Histogram bucket edges in bytes. Each bucket is [MinBytes, MaxBytes).
# The final bucket is open-ended (MaxBytes = $null).
$Buckets = @(
    [pscustomobject]@{ Label = '<10 KB';      Min = 0L;         Max = (10L * $KB) }
    [pscustomobject]@{ Label = '10-100 KB';   Min = (10L * $KB); Max = (100L * $KB) }
    [pscustomobject]@{ Label = '100 KB-1 MB'; Min = (100L * $KB); Max = $MB }
    [pscustomobject]@{ Label = '1-10 MB';     Min = $MB;        Max = (10L * $MB) }
    [pscustomobject]@{ Label = '10-100 MB';   Min = (10L * $MB); Max = (100L * $MB) }
    [pscustomobject]@{ Label = '100 MB-1 GB'; Min = (100L * $MB); Max = $GB }
    [pscustomobject]@{ Label = '1-10 GB';     Min = $GB;        Max = (10L * $GB) }
    [pscustomobject]@{ Label = '>10 GB';      Min = (10L * $GB); Max = $null }
)

function Get-BucketIndex {
    param([long] $Size)
    for ($i = 0; $i -lt $Buckets.Count; $i++) {
        $b = $Buckets[$i]
        if ($null -eq $b.Max) {
            if ($Size -ge $b.Min) { return $i }
        }
        elseif ($Size -ge $b.Min -and $Size -lt $b.Max) {
            return $i
        }
    }
    return ($Buckets.Count - 1)  # safety net
}

# --- Validate input ---------------------------------------------------------
$missing = @($Path | Where-Object { -not (Test-Path -LiteralPath $_) })
if ($missing.Count -gt 0) {
    [Console]::Error.WriteLine("error: path(s) not found: $($missing -join ', ')")
    exit 2
}

# --- Walk the tree ----------------------------------------------------------
$SAMPLE_CAP = 2000000   # store up to ~2M sizes for an exact median
$bucketCount = $Buckets.Count

[long[]]  $histCounts = New-Object 'long[]'   $bucketCount
[long[]]  $histBytes  = New-Object 'long[]'   $bucketCount
[long]    $totalBytes = 0L
[long]    $fileCount  = 0L
[long]    $unreadable = 0L

# Collect sizes for an exact median (capped), and track the top-N largest.
$sizesSample = [System.Collections.Generic.List[long]]::new()
$largest     = [System.Collections.Generic.List[long]]::new()

# Enumerate across every input path. Directories are walked recursively; individual
# files are counted directly. Get-ChildItem works on both Windows PowerShell 5.1 and 7+.
# Inaccessible subtrees are tolerated (-ErrorAction Ignore). Reparse points (symlinks/
# junctions) are handled below so we neither double-count nor escape the tree. Overlapping
# inputs are de-duplicated by resolved real path ($seen).
$reparse = [System.IO.FileAttributes]::ReparsePoint
$seen = [System.Collections.Generic.HashSet[string]]::new()

# Count one FileInfo into the running tallies. Script-scoped vars are updated in place.
function Add-FileInfo {
    param([System.IO.FileInfo] $Info)
    try {
        # Skip reparse points (symlinks/junctions) unless explicitly following them.
        if (-not $FollowSymlinks -and (($Info.Attributes -band $reparse) -eq $reparse)) {
            return
        }
        # De-dupe across overlapping inputs by resolved real path.
        $real = $Info.FullName
        if (-not $seen.Add($real)) { return }
        $size = [long] $Info.Length
    }
    catch {
        $script:unreadable++
        return
    }

    $script:fileCount++
    $script:totalBytes += $size
    $idx = Get-BucketIndex -Size $size
    $histCounts[$idx]++
    $histBytes[$idx] += $size

    if ($sizesSample.Count -lt $SAMPLE_CAP) {
        $sizesSample.Add($size)
    }

    # Track top-N largest sizes (keep the list sorted ascending; [0] is smallest).
    if ($largest.Count -lt $TopN) {
        $largest.Add($size)
        $largest.Sort()
    }
    elseif ($size -gt $largest[0]) {
        $largest[0] = $size
        $largest.Sort()
    }
}

foreach ($p in $Path) {
    if (Test-Path -LiteralPath $p -PathType Container) {
        $files = Get-ChildItem -LiteralPath $p -File -Recurse -Force -ErrorAction Ignore
        foreach ($info in $files) { Add-FileInfo -Info $info }
    }
    else {
        Add-FileInfo -Info ([System.IO.FileInfo]::new((Resolve-Path -LiteralPath $p).Path))
    }
}

# --- Median -----------------------------------------------------------------
[long] $median = 0L
if ($sizesSample.Count -gt 0) {
    $sorted = [long[]] $sizesSample.ToArray()
    [array]::Sort($sorted)
    $n = $sorted.Length
    if ($n % 2 -eq 1) {
        $median = $sorted[[int]([math]::Floor($n / 2))]
    }
    else {
        $hi = [int]($n / 2)
        # Floor division to match the Python profiler exactly. A plain [long]
        # cast would round-to-nearest and could differ by 1 byte on even counts.
        $median = [long][math]::Floor((($sorted[$hi - 1] + $sorted[$hi]) / 2.0))
    }
}

[long] $mean = 0L
if ($fileCount -gt 0) {
    $mean = [long]([math]::Floor($totalBytes / $fileCount))
}

# --- Assemble histogram (ordered to match the schema) -----------------------
$histogram = foreach ($i in 0..($bucketCount - 1)) {
    $b = $Buckets[$i]
    [ordered]@{
        label     = $b.Label
        min_bytes = [long] $b.Min
        max_bytes = $b.Max        # $null for the open-ended final bucket -> JSON null
        count     = [long] $histCounts[$i]
        bytes     = [long] $histBytes[$i]
    }
}

# Largest sizes, descending (sizes only - never names or paths).
$largestDesc = @($largest.ToArray() | Sort-Object -Descending)

# scanned_path stays a string (the calculators display it): join multiple inputs,
# each resolved to an absolute path to match the Python profiler.
$scannedPath = (@($Path | ForEach-Object {
    (Resolve-Path -LiteralPath $_).Path
}) -join '; ')

$result = [ordered]@{
    schema_version     = 1
    scanned_path       = $scannedPath
    generated_at       = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    total_bytes        = [long] $totalBytes
    file_count         = [long] $fileCount
    mean_file_bytes    = [long] $mean
    median_file_bytes  = [long] $median
    median_is_exact    = ($sizesSample.Count -eq $fileCount)
    histogram          = @($histogram)
    largest_file_bytes = $largestDesc
    errors             = [ordered]@{ unreadable_paths = [long] $unreadable }
}

# Depth must cover: profile -> histogram (array) -> bucket object -> scalar = 4+.
$json = $result | ConvertTo-Json -Depth 6

if ($Output) {
    Set-Content -LiteralPath $Output -Value $json -Encoding UTF8
    # Status to stderr (so stdout stays clean for piping), mirroring the Python tool.
    [Console]::Error.WriteLine("Wrote profile to $Output")
}
else {
    Write-Output $json
}
