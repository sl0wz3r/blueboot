# Shared helper: locate the Bluetooth adapter by VID/PID rather than by serial, so
# these scripts work on any machine. Override with  $env:UB500_VIDPID.
#
# -PresentOnly matters far more than it looks. Windows keeps serving device
# properties out of its store long after a device stops being present, so without it
# this helper will happily hand every other script in here a PHANTOM node - one that
# reports a healthy, patched radio while being attached to no hardware at all. That
# is the failure described under "Patched, and still not working" in the README, and
# it is indistinguishable from success unless you ask about presence explicitly.
function Find-BtAdapter {
    $vidpid = $env:UB500_VIDPID
    if (-not $vidpid) { $vidpid = 'VID_2357&PID_0604' }   # TP-Link UB500 / Realtek RTL8761B

    $present = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like "*$vidpid*" })
    if ($present.Count -gt 1) {
        Write-Host "WARNING: $($present.Count) present devices match $vidpid; using the first."
        $present | ForEach-Object { Write-Host "         $($_.InstanceId)" }
    }
    if ($present.Count -ge 1) { return $present[0] }

    # Nothing is present. If a node nevertheless EXISTS, say so loudly: that is the
    # phantom case, and every property you read from it will mislead you.
    $stale = @(Get-PnpDevice | Where-Object { $_.InstanceId -like "*$vidpid*" })
    if ($stale.Count -ge 1) {
        Write-Host "WARNING: a device node for $vidpid exists but is NOT PRESENT."
        Write-Host "         Its properties are cached and do not reflect live hardware."
        Write-Host "         Check for an 'Unknown USB Device (Port Reset Failed)' node, and"
        Write-Host "         see 'Patched, and still not working' in the README."
        return $null
    }

    Write-Host "No USB Bluetooth adapter matching $vidpid is present."
    Write-Host "Set `$env:UB500_VIDPID to target a different adapter."
    return $null
}
