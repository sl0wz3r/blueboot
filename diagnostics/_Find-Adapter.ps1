# Shared helper: locate the Bluetooth adapter by VID/PID rather than by serial, so
# these scripts work on any machine. Override with  $env:UB500_VIDPID.
function Find-BtAdapter {
    $vidpid = $env:UB500_VIDPID
    if (-not $vidpid) { $vidpid = 'VID_2357&PID_0604' }   # TP-Link UB500 / Realtek RTL8761B
    $d = Get-PnpDevice | Where-Object { $_.InstanceId -like "*$vidpid*" } | Select-Object -First 1
    if (-not $d) {
        Write-Host "No USB Bluetooth adapter matching $vidpid is present."
        Write-Host "Set `$env:UB500_VIDPID to target a different adapter."
        return $null
    }
    return $d
}
