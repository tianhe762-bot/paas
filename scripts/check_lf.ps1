$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$targets = Get-ChildItem -Path $root -Recurse -File |
    Where-Object {
        $_.FullName -notmatch '\\.git\\|\\data\\|\\logs\\|\\.venv\\|__pycache__|\\.pytest_cache\\' -and
        $_.Name -ne 'bulk_report.txt'
    }
$bad = @()
foreach ($t in $targets) {
    $bytes = [System.IO.File]::ReadAllBytes($t.FullName)
    for ($i = 0; $i -lt $bytes.Length - 1; $i++) {
        if ($bytes[$i] -eq 13 -and $bytes[$i + 1] -eq 10) {
            $bad += $t.FullName
            break
        }
    }
}
if ($bad.Count -gt 0) {
    Write-Error "CRLF line endings found:`n$($bad -join "`n")"
    exit 1
}
Write-Host "OK: all text files use LF line endings."
