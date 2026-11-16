# Rule out the mundane reasons a radio finds nothing: stopped services, airplane
# mode, a soft-blocked radio. Run this before blaming firmware.
$ErrorActionPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '_Find-Adapter.ps1')
$dev = Find-BtAdapter; if (-not $dev) { exit 1 }

"=== BLUETOOTH SERVICES ==="
Get-Service bthserv,BTAGService,bthusb,BTHPORT,BthEnum | Select-Object Status,StartType,Name,DisplayName |
  Format-Table -AutoSize | Out-String -Width 160

"=== RADIO STATE (authoritative, via WinRT) ==="
try {
  [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
  $task = $asTask.MakeGenericMethod([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]).Invoke($null,@($op))
  $task.Wait(5000) | Out-Null
  foreach ($r in $task.Result) { "Radio: {0,-28} Kind={1} State={2}" -f $r.Name, $r.Kind, $r.State }
} catch { "WinRT radio query failed: " + $_.Exception.Message }

"RadioState (device key, 2 = on): " +
  (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Enum\$($dev.InstanceId)\Device Parameters").RadioState

"`n=== FILTER DRIVERS IN THIS DEVICE'S STACK ==="
"Anything here other than the USB stack and BTHUSB is a third-party filter -"
"endpoint-security or device-control products commonly insert one."
(Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_Stack').Data |
  ForEach-Object { "  $_" }

"`n=== PAIRED / PRESENT PERIPHERALS ==="
Get-PnpDevice | Where-Object { $_.InstanceId -like 'BTHENUM*' -or $_.InstanceId -like 'BTHLE*' } |
  Select-Object Status, Class, FriendlyName | Format-Table -AutoSize | Out-String -Width 160
