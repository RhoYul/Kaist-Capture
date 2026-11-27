# Enable UVC frame metadata for Intel RealSense cameras on Windows (hardware timestamps).
# Without these registry values librealsense falls back to PC arrival time for frame timestamps.
# Same values as Intel's realsense_metadata_win10.ps1. Needs administrator. Safe to run again.
#   powershell -ExecutionPolicy Bypass -File tools\Enable-RealSenseMetadata.ps1          (enable)
#   powershell -ExecutionPolicy Bypass -File tools\Enable-RealSenseMetadata.ps1 -Undo    (remove)
# After running: restart the capture server (Restart-Capture.cmd). If timestamps do not change, replug the cameras once.
param([switch]$Undo, [string]$ProductIds = "0B5C", [string]$LogPath = "")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $LogPath) { $LogPath = Join-Path $root "logs\realsense-metadata.log" }
function Write-Log($msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Log "ERROR not running as administrator"; exit 2 }

$classes = "{e5323777-f976-4f5b-9b55-b94699c46e44}", "{65E8773D-8F56-11D0-A3B9-00A0C9223196}"
$pidPattern = ($ProductIds -split "[,; ]+" | Where-Object { $_ }) -join "|"
$pattern = "VID_8086&PID_($pidPattern)&MI_(0[0-9])"
$changed = 0; $seen = 0; $failed = 0
Write-Log ("== {0} RealSense metadata (PID {1})" -f $(if ($Undo) { "REMOVE" } else { "ENABLE" }), $ProductIds)

foreach ($class in $classes) {
    $base = "HKLM:\SYSTEM\CurrentControlSet\Control\DeviceClasses\$class"
    if (-not (Test-Path -LiteralPath $base)) { continue }
    $items = Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match $pattern }
    foreach ($item in $items) {
        $null = $item.PSChildName -match $pattern
        $mi = $Matches[2]
        # only the video interfaces: MI_00 (depth / IR, several pins) and MI_03 (RGB)
        if ($mi -ne "00" -and $mi -ne "03") { continue }
        $names = if ($mi -eq "00") { "MetadataBufferSizeInKB0", "MetadataBufferSizeInKB1", "MetadataBufferSizeInKB2" } else { , "MetadataBufferSizeInKB0" }
        $dp = Join-Path $item.PSPath "#GLOBAL\Device Parameters"
        $short = ($item.PSChildName -replace '^##\?#USB#', '' -replace '#\{.*$', '')
        $seen++
        try {
            if (-not (Test-Path -LiteralPath $dp)) {
                if ($Undo) { continue }
                $null = New-Item -Path $dp -Force
            }
            foreach ($n in $names) {
                $cur = (Get-ItemProperty -LiteralPath $dp -Name $n -ErrorAction SilentlyContinue).$n
                if ($Undo) {
                    if ($null -ne $cur) { Remove-ItemProperty -LiteralPath $dp -Name $n; $changed++ }
                } elseif ($cur -ne 5) {
                    $null = New-ItemProperty -LiteralPath $dp -Name $n -PropertyType DWord -Value 5 -Force; $changed++
                }
            }
            Write-Log ("ok   {0} {1} [{2}]" -f $class.Substring(1, 8), $short, ($names -join ","))
        } catch {
            $failed++
            Write-Log ("FAIL {0} {1}: {2}" -f $class.Substring(1, 8), $short, $_.Exception.Message)
        }
    }
}
Write-Log ("== done: interfaces {0}, values changed {1}, failed {2}" -f $seen, $changed, $failed)
if ($failed -gt 0 -or $seen -eq 0) { exit 1 }
exit 0
