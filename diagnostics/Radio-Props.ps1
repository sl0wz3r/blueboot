# Decoded HCI/LMP version, subversion and feature mask for the local radio.
$ErrorActionPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '_Find-Adapter.ps1')
$dev = Find-BtAdapter; if (-not $dev) { exit 1 }
$map=@{1='BD_ADDR (u64)';2='Manufacturer';3='LMP supported features';4='LMP version';
       5='LMP subversion';6='HCI version';7='HCI revision'}
$ver=@{9='5.0';10='5.1';11='5.2';12='5.3';13='5.4'}
"Bluetooth radio properties {A92F26CA-EDA7-4B1D-9DB2-27B68AA5A2EB}:"
Get-PnpDeviceProperty -InstanceId $dev.InstanceId | Where-Object { $_.KeyName -like '*A92F26CA*' } | ForEach-Object {
  $id = ($_.KeyName -split '\s+')[-1]
  $name = $map[[int]$id]
  if ($name) {
    $v=$_.Data
    $hex = try { '0x{0:X}' -f [uint64]$v } catch { '' }
    $note = ''
    if (($id -eq '4' -or $id -eq '6') -and $ver[[int]$v]) { $note = '  Bluetooth ' + $ver[[int]$v] }
    if ($id -eq '5' -and $hex -eq '0x8761') { $note = '  <-- STOCK ROM, not patched' }
    "  {0,-2} {1,-24} = {2,-22} {3}{4}" -f $id, $name, $v, $hex, $note
  }
}
"`nBluetooth Support Service: " + (Get-Service bthserv).Status
