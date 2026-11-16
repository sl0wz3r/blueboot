# Identity, driver binding and health of the USB Bluetooth adapter.
$ErrorActionPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '_Find-Adapter.ps1')
$dev = Find-BtAdapter; if (-not $dev) { exit 1 }
$ID = $dev.InstanceId

"=== 1. RADIO INFO VIA BluetoothGetRadioInfo (HCI truth) ==="
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential)]
public struct BLUETOOTH_FIND_RADIO_PARAMS { public uint dwSize; }
[StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
public struct BLUETOOTH_RADIO_INFO {
  public uint dwSize;
  public ulong address;
  [MarshalAs(UnmanagedType.ByValTStr, SizeConst=248)] public string szName;
  public uint ulClassofDevice;
  public ushort lmpSubversion;
  public ushort manufacturer;
}
public static class BT {
  [DllImport("bthprops.cpl", SetLastError=true)]
  public static extern IntPtr BluetoothFindFirstRadio(ref BLUETOOTH_FIND_RADIO_PARAMS p, out IntPtr hRadio);
  [DllImport("bthprops.cpl", SetLastError=true)]
  public static extern uint BluetoothGetRadioInfo(IntPtr hRadio, ref BLUETOOTH_RADIO_INFO info);
  [DllImport("bthprops.cpl")] public static extern bool BluetoothFindRadioClose(IntPtr h);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
}
'@
$p = New-Object BLUETOOTH_FIND_RADIO_PARAMS
$p.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][BLUETOOTH_FIND_RADIO_PARAMS])
$hRadio = [IntPtr]::Zero
$hFind = [BT]::BluetoothFindFirstRadio([ref]$p, [ref]$hRadio)
if ($hFind -ne [IntPtr]::Zero) {
  $info = New-Object BLUETOOTH_RADIO_INFO
  $info.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][BLUETOOTH_RADIO_INFO])
  if ([BT]::BluetoothGetRadioInfo($hRadio, [ref]$info) -eq 0) {
    "Class of Device : 0x" + ('{0:X6}' -f $info.ulClassofDevice)
    "LMP subversion  : 0x" + ('{0:X4}' -f $info.lmpSubversion) + "   <-- firmware build id"
    "Manufacturer    : " + $info.manufacturer + " (0x" + ('{0:X4}' -f $info.manufacturer) + ")  [0x005D=Realtek]"
    "  0x8761 = RTL8761B stock ROM (NOT patched - radio will not find anything)"
  }
  [BT]::CloseHandle($hRadio) | Out-Null
  [BT]::BluetoothFindRadioClose($hFind) | Out-Null
} else { "No Bluetooth radio found" }

"`n=== 2. DRIVER BINDING ==="
foreach ($k in 'DEVPKEY_Device_Service','DEVPKEY_Device_DriverInfPath','DEVPKEY_Device_DriverVersion',
               'DEVPKEY_Device_MatchingDeviceId','DEVPKEY_Device_DriverProvider','DEVPKEY_Device_ProblemCode',
               'DEVPKEY_Device_CompatibleIds','DEVPKEY_Device_Stack') {
  $v = (Get-PnpDeviceProperty -InstanceId $ID -KeyName $k).Data
  if ($v) { "  {0,-34}: {1}" -f ($k -replace '^DEVPKEY_Device_',''), ($v -join ' | ') }
}
"  {0,-34}: {1}" -f 'Status', $dev.Status

"`n=== 3. DEVICE PARAMETERS ==="
Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Enum\$ID\Device Parameters" |
  Select-Object * -Exclude PS* | Format-List | Out-String -Width 160

"`n=== 4. BLUETOOTH EVENT LOG (last 25) ==="
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName=@('BTHUSB','Microsoft-Windows-Bluetooth-BthLEEnum','bthserv')} -MaxEvents 25 |
  Select-Object TimeCreated, Id, LevelDisplayName, @{n='Msg';e={($_.Message -split "`n")[0]}} |
  Format-Table -AutoSize -Wrap | Out-String -Width 200
