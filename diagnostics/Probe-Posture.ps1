# Two questions this answers:
#   1. Could this machine load a self-built kernel driver at all? (Secure Boot,
#      code-integrity policy, HVCI, admin.) If not, the host-side patch route in
#      this repo is the only option available to you.
#   2. Does the radio actually transmit and receive? A BR/EDR inquiry that returns
#      cleanly with zero results, repeatedly, is the classic unpatched-ROM symptom.
$ErrorActionPreference='SilentlyContinue'

"=== A. CAN THIS MACHINE LOAD A SELF-BUILT DRIVER? ==="
"IsAdmin      : " + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
try { "SecureBoot   : " + (Confirm-SecureBootUEFI) } catch { "SecureBoot   : <needs elevation>" }
$dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard
"CodeIntegrityPolicyEnforcementStatus : " + $dg.CodeIntegrityPolicyEnforcementStatus + "  (0=off 1=audit 2=ENFORCED)"
"SecurityServicesRunning              : " + ($dg.SecurityServicesRunning -join ',') + "  (2 = HVCI/Memory Integrity)"
"Edition      : " + (Get-CimInstance Win32_OperatingSystem).Caption
"Managed      : PartOfDomain=" + (Get-CimInstance Win32_ComputerSystem).PartOfDomain
""
"If SecureBoot is True or code integrity is ENFORCED, a test-signed kernel driver"
"will not load. Patch the controller from the USB host side instead."

"`n=== B. FUNCTIONAL TEST: DOES BR/EDR INQUIRY ACTUALLY WORK? ==="
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential)]
public struct SYSTEMTIME { public ushort y,m,dow,d,h,mi,s,ms; }
[StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
public struct BLUETOOTH_DEVICE_INFO {
  public uint dwSize;
  public ulong Address;
  public uint ulClassofDevice;
  [MarshalAs(UnmanagedType.Bool)] public bool fConnected;
  [MarshalAs(UnmanagedType.Bool)] public bool fRemembered;
  [MarshalAs(UnmanagedType.Bool)] public bool fAuthenticated;
  public SYSTEMTIME stLastSeen;
  public SYSTEMTIME stLastUsed;
  [MarshalAs(UnmanagedType.ByValTStr, SizeConst=248)] public string szName;
}
[StructLayout(LayoutKind.Sequential)]
public struct BLUETOOTH_DEVICE_SEARCH_PARAMS {
  public uint dwSize;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnAuthenticated;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnRemembered;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnUnknown;
  [MarshalAs(UnmanagedType.Bool)] public bool fReturnConnected;
  [MarshalAs(UnmanagedType.Bool)] public bool fIssueInquiry;
  public byte cTimeoutMultiplier;
  public IntPtr hRadio;
}
public static class BT2 {
  [DllImport("bthprops.cpl", SetLastError=true)]
  public static extern IntPtr BluetoothFindFirstDevice(ref BLUETOOTH_DEVICE_SEARCH_PARAMS p, ref BLUETOOTH_DEVICE_INFO i);
  [DllImport("bthprops.cpl", SetLastError=true)]
  public static extern bool BluetoothFindNextDevice(IntPtr h, ref BLUETOOTH_DEVICE_INFO i);
  [DllImport("bthprops.cpl")] public static extern bool BluetoothFindDeviceClose(IntPtr h);
}
'@
$sp = New-Object BLUETOOTH_DEVICE_SEARCH_PARAMS
$sp.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][BLUETOOTH_DEVICE_SEARCH_PARAMS])
$sp.fReturnAuthenticated=$true; $sp.fReturnRemembered=$true; $sp.fReturnUnknown=$true; $sp.fReturnConnected=$true
$sp.fIssueInquiry=$true; $sp.cTimeoutMultiplier=6; $sp.hRadio=[IntPtr]::Zero
$di = New-Object BLUETOOTH_DEVICE_INFO
$di.dwSize = [Runtime.InteropServices.Marshal]::SizeOf([type][BLUETOOTH_DEVICE_INFO])
"Put something into pairing mode first, then run this."
$sw = [Diagnostics.Stopwatch]::StartNew()
$h = [BT2]::BluetoothFindFirstDevice([ref]$sp, [ref]$di)
if ($h -eq [IntPtr]::Zero) {
  "INQUIRY RESULT: nothing found. Win32 error = " + [Runtime.InteropServices.Marshal]::GetLastWin32Error()
  "  259 = ERROR_NO_MORE_ITEMS -> the inquiry ran, but heard no replies."
  "  With a discoverable device nearby, that points at unpatched ROM firmware."
} else {
  $n=0
  do {
    $n++
    "  [{0}] {1,-30} CoD=0x{2:X6} auth={3} connected={4}" -f $n,$di.szName,$di.ulClassofDevice,$di.fAuthenticated,$di.fConnected
  } while ([BT2]::BluetoothFindNextDevice($h, [ref]$di))
  [BT2]::BluetoothFindDeviceClose($h) | Out-Null
  "INQUIRY RESULT: $n device(s) in $([int]$sw.Elapsed.TotalSeconds)s -> BR/EDR TX/RX WORKING"
}
$sw.Stop()
