# blueboot

*Realtek RTL8761B Bluetooth firmware loader for macOS.*

Make a Realtek **RTL8761B** USB Bluetooth adapter (TP-Link UB500 and friends) actually work
inside a **Windows 11 ARM64** VM — by loading the controller's firmware from the macOS host,
with no Windows kernel driver and no boot-security changes.

## The problem

An RTL8761B dongle powers up running stock ROM firmware. In that state it enumerates as a
standard USB Bluetooth device (class `E0/01/01`), answers HCI version queries, and looks
completely healthy to the OS — Device Manager reports **"Generic Bluetooth Adapter, this
device is working properly"**, the Microsoft BR/EDR and LE enumerators appear, and the
Bluetooth services all run.

It just never finds anything. Scans return nothing, forever, and nothing pairs.

Linux's own driver says why, in `drivers/bluetooth/btrtl.c`:

> Match a set of subver values that correspond to stock firmware, **which is not compatible
> with standard btusb**. If matched, upload an alternative firmware that does conform to
> standard btusb. **Once that firmware is uploaded, the subver changes to a different value.**

Every OS that supports these dongles uploads that patch at init — Linux via `btrtl`, Windows
x64 via Realtek's `RtkBtfilter.sys` lower filter. **On Windows ARM64 nothing does**, because:

* Realtek/TP-Link ship x86 and x64 driver binaries only. There is no ARM64 build from
  Realtek, TP-Link, any OEM, or the Microsoft Update Catalog.
* Windows on ARM emulates **user-mode** x86/x64 only. Kernel drivers are never emulated, so
  the x64 `.sys` can never load regardless of signing.
* The vendor INF is decorated `NTamd64.10.0` anyway, so it does not even match on ARM64 —
  the inbox generic driver wins by default.

So a passed-through dongle in an ARM64 VM is stuck on ROM firmware permanently.

## The fix

Patch the controller from the **host** side before the VM gets it. The upload is the same
sequence Linux performs, over libusb, and the patch persists across the USB port reset the
guest issues when it attaches.

No Windows driver. No Secure Boot change, no test signing, no WDAC exemption — which matters
if the guest is a managed corporate machine where none of those are available to you.

## Where this applies

The upload happens on the **macOS side**, over USB, before the dongle is handed to anything — so
the hypervisor is not part of the fix. Parallels Desktop, VMware Fusion, UTM and VirtualBox all
behave the same once the controller is already patched; only the macOS host matters.

That is also the limit: **this needs a Mac.** On bare-metal Windows on ARM — a Snapdragon X Elite
laptop, a Surface Pro 11, a ThinkPad X13s — there is no host to run it from, and the patch lives
in volatile controller RAM, so patching the dongle on a Mac and carrying it across does not
survive the unplug. Everything under *The problem* above still explains those machines; this tool
cannot fix them. [Swap the adapter](#if-the-patch-does-not-survive-the-handoff) instead.

## Is this your problem?

Check the radio's firmware id. `lmp_subver = 0x8761` with `hci_rev = 0x000B` is the RTL8761B
stock-ROM fingerprint:

```powershell
.\diagnostics\Radio-Props.ps1     # in the Windows guest
```

```
LMP subversion  = 0x8761   <-- STOCK ROM, not patched
HCI revision    = 0x000B
Manufacturer    = 93 (Realtek)
```

Rule out the boring causes first — stopped services, airplane mode, a soft-blocked radio:

```powershell
.\diagnostics\Probe-SoftBlock.ps1
.\diagnostics\Probe-Posture.ps1   # also runs a real BR/EDR inquiry
```

An inquiry that completes cleanly (`ERROR_NO_MORE_ITEMS`) yet finds nothing, with a
discoverable device sitting next to it, is the signature — *provided the adapter is actually
present*. `Radio-Props.ps1` reads the PnP property store, which keeps serving values after a
device stops being present, so a stale node can report a patched radio that is not there. If
your controller already reads `0xC761` and Bluetooth still does not work, skip to
[Patched, and still not working](#patched-and-still-not-working).

### The same fingerprint on Linux

If you arrived from a kernel log rather than Device Manager, this is the same bug. `btrtl` prints
the identifiers lowercase and without the `0x`, through
`examining hci_ver=%02x hci_rev=%04x lmp_ver=%02x lmp_subver=%04x` — a stock RTL8761BU on USB
matches `hci_ver=0a`, `hci_rev=000b`, `lmp_subver=8761`. Linux then uploads
`rtl_bt/rtl8761bu_fw.bin`. On Windows ARM64 nothing does.

One naming trap: there is no `RTL_ROM_LMP_8761B` constant in `btrtl.c`. The B and BU parts reuse
the 8761**A**'s subver — `#define RTL_ROM_LMP_8761A 0x8761` — and are separated by revision and
bus, with `IC_INFO(RTL_ROM_LMP_8761A, 0xb, 0xa, HCI_USB)` being the 8761BU row
(`lmps, hcir, hciv, bus`). Inside the patch container the part is identified by `project_id 14`.

## Getting the firmware

**The firmware is not included in this repository.** It is Realtek/TP-Link proprietary and
redistributing it is not mine to do. You already have it if you own the adapter:

1. Download the driver package for your adapter from the vendor (for the UB500, tp-link.com).
2. Extract it and find `Driver/Windows_11_64bit/rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat`.
3. Pass it with `--dat`, or set `UB500_FW=/path/to/that.dat`.

The tool checks the `Realtech` container signature, rejects the unrelated `RTBTCore`
format, and selects the patch whose `chip_id` matches the controller's ROM version — a
container with no matching patch is refused rather than uploaded. It also reads the
`project_id` from the trailing extension block and prints it (`14` = 8761B) for you to eyeball;
it does not currently refuse on that alone.

Linux users: `linux-firmware` ships `rtl_bt/rtl8761bu_fw.bin`, a different (older) build of
the same thing. Either works; they are not byte-identical.

## Usage

Requires `brew install libusb`.

```bash
# 1. Release the dongle from the VM  (Parallels: Devices > USB & Bluetooth > your adapter)

python3 blueboot.py --status        # will Bluetooth work right now? (never opens the device)
python3 blueboot.py --info          # read-only: what firmware is it running?
python3 blueboot.py                 # upload the patch
python3 blueboot.py --test-reset    # does the patch survive a USB port reset?

# 2. Reattach to the VM WITHOUT unplugging
```

Success looks like:

```
State         : STOCK ROM - patch NOT loaded
  selected    : chip_id=2  ->  29124 bytes
  expect after: hci_rev=0xDDD5  lmp_subver=0xC761
  uploading… fragment 116/116  (100.0%)
Controller    : hci_ver=0x09  hci_rev=0xDDD5  manufacturer=93 (Realtek)  lmp_subver=0xC761

SUCCESS: controller now reports firmware 0xDDD5C761, exactly matching the
container's fw_version. The patch is live.
```

`btrtl` logs running firmware as `"0x%04x%04x" % (hci_rev, lmp_subver)`, so a correct patch
makes the controller report the container's own `fw_version` split across those two fields.
That is an exact target, not a vague "something changed".

### Read the Bluetooth version only after a reset

Three different readings appear, and only the last is real:

| when | `hci_ver`/`lmp_ver` | means |
|---|---|---|
| unpatched stock ROM | `0x0A` | claims 5.1, delivers nothing |
| on the host, right after upload | `0x09` | transient — loaded, not yet reset |
| in the guest, after `HCI_Reset` | **`0x0D`** | **Bluetooth 5.4** |

### Safety

The patch is written to controller **RAM**, never to flash. Nothing is written to any
non-volatile memory, and unplugging the dongle restores the stock ROM state exactly. This is
the same operation Linux performs on every boot.

## Keeping it patched

RAM patch + power cycle = gone, and when it is gone Bluetooth silently stops finding things
again. Unplugging the dongle does it, and so does **rebooting or shutting down the Mac** — the
dongle loses bus power with the host. That second case is the one that bites, because your VM
may come back from suspend believing nothing happened; see
[Patched, and still not working](#patched-and-still-not-working). The agent re-applies the patch
automatically:

```bash
./install-autopatch.sh /path/to/rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat
./install-autopatch.sh --uninstall
```

A LaunchAgent runs `--auto` every 10 seconds. It installs a **copy** of the tool and firmware
into `~/Library/Application Support/blueboot/` rather than running them from
`~/Downloads`: that folder, along with `~/Desktop` and `~/Documents`, is TCC-protected, and a
background LaunchAgent reading from it can be denied with no visible error.

### How it decides what to do

`--auto` asks `ioreg` first, because `ioreg` still reports a device a VM holds exclusively
while libusb does not. That gives a real four-way answer instead of one ambiguous failure:

| situation | action |
|---|---|
| `ioreg` did not answer | **log once** — a probe that saw nothing is *not* evidence the dongle is free |
| not plugged in | silent, exit 0 |
| owned by a VM, same enumeration we patched | silent, exit 0 — never competes with the hypervisor |
| owned by a VM, **a new enumeration** | **log a warning** — the guest has an unpatched controller |
| free, already patched | silent, exit 0 |
| free, running stock ROM | **patch it**, log one line |
| free but libusb cannot open it | **log a warning**, quoting the real libusb error |

Those warning rows are the point. A fault that looks like "everything is fine" is worse than a
loud failure, because nobody goes looking. It logs only on state *changes*, so a 10-second timer
does not produce a heartbeat log; `--status` and a `.heartbeat` file answer "is the agent alive"
instead, so an empty log means nothing went wrong rather than nothing ran.

### How it knows the VM's dongle is still patched

`ioreg` exposes a **`sessionID`** on the USB device node: a `mach_absolute_time` stamp taken when
the device enumerated. Two properties make it the keystone of the whole design. It stays readable
while the hypervisor holds the device exclusively — exactly the window in which libusb can tell
you nothing — and it changes precisely when the dongle re-enumerates, which is precisely when the
RAM patch is lost.

So the agent records the session it patched, and compares. "The VM has it" stops being a shrug
and becomes a claim you can falsify:

```
2026-11-18 08:36:05  PATCHED ok -> 0xDDD5C761 (29124 bytes, rom_version 1)
2026-11-18 08:51:46  dongle re-enumerated (session 4986975767 -> 59093045483) while held by
                     pid 5968, prl_vm_app - the guest now has an UNPATCHED controller.
```

Without that line the agent does the right thing, says nothing, and leaves you to discover by
hand that the adapter your guest is holding is not the one that got patched.

```bash
tail -f ~/Library/Logs/blueboot.log
launchctl print gui/$UID/com.blueboot.autopatch | grep -E "state =|runs =|last exit"
```

### Timing

If your hypervisor is set to auto-connect the dongle, it will grab it before the agent can
patch it. Leave it on "ask", and **let the dongle sit on macOS for a few seconds before
connecting it to the VM**. The log line is your confirmation.

## Patched, and still not working

Everything above assumes the dongle reaching the guest is the dongle you patched. Sometimes it
is not — and the result looks *identical* to the stock-ROM problem this tool exists to solve,
which is exactly why it earns its own section. If you re-patch a controller that already reads
`0xC761`, you will do it forever and nothing will change.

The recipe: reboot the Mac while the VM is running. Your hypervisor **suspends** the guest
rather than shutting it down, then resumes it. The dongle meanwhile lost bus power, so its RAM
patch is gone and the agent correctly re-applies it within seconds. But the resumed guest is
driving a virtual USB controller restored from an image written *before* the host rebooted. The
dongle gets handed to a virtual port that cannot complete a USB port reset, and you are left
with two device nodes, neither usable:

- a **phantom** `USB\VID_2357&PID_0604\…` node serving device properties out of cache. It will
  cheerfully report `Status OK`, the radio `On`, and a patched `LMP subversion` — all of it days
  stale, none of it in contact with hardware.
- an **`USB\RESET_FAILURE`** node (`ProblemCode 43`, `CM_PROB_FAILED_POST_START`), shown as
  *Unknown USB Device (Port Reset Failed)*, squatting on the port the real device arrived on.
  It carries no VID/PID, because enumeration died before Windows could read the descriptor.

### Telling it apart

Ask about **presence**, not properties. The property store keeps answering long after a device
stops being present, so anything reading it will confirm a healthy adapter that is not there.

```powershell
# 1. Actually present, or only remembered?
Get-PnpDevice -InstanceId 'USB\VID_2357&PID_0604\<serial>' -PresentOnly

# 2. When did Windows last enumerate it? A stale date means it never re-arrived.
(Get-PnpDeviceProperty -InstanceId 'USB\VID_2357&PID_0604\<serial>' |
  Where-Object KeyName -eq 'DEVPKEY_Device_LastArrivalDate').Data

# 3. The blunt one. This contradicts a "healthy" device node outright.
pnputil /restart-device "USB\VID_2357&PID_0604\<serial>"
#   -> "The device is not connected."   (error 1167)

# 4. Is a reset-failure node squatting on the port?
Get-PnpDevice -PresentOnly | Where-Object Status -ne 'OK'
```

A live radio answers this, and it needs no message pump:

```powershell
[Windows.Devices.Bluetooth.BluetoothAdapter,Windows.Devices.Bluetooth,ContentType=WindowsRuntime] | Out-Null
# GetDefaultAsync() returns the real BD_ADDR, read off the controller.
```

**Do not** use `DeviceInformation.FindAllAsync` with an `AssociationEndpoint` selector as a health
check. It hangs in a console host whether the radio is healthy or dead, so it manufactures exactly
the false negative this section exists to kill.

From the Mac, one command answers the whole question:

```bash
python3 blueboot.py --status
```

### What does not fix it

| attempt | why it fails |
|---|---|
| Detach/reattach in the hypervisor menu | Moves the host-side claim only. The guest never sees a disconnect, so nothing re-enumerates and `LastArrivalDate` does not move. |
| `pnputil /restart-device` | The device is not connected. There is nothing to restart. |
| `pnputil /remove-device` + `/scan-devices` | The nodes do rebuild — onto the same wedged virtual port. Scope it to one instance id: a class-wide or wildcard removal takes out every other device on that controller, security keys included. |
| Re-running the patch | The firmware was never the problem. |

### What does fix it

**Shut the guest fully down and start it again.** Not a suspend, and not closing the window if
your hypervisor is set to suspend on close. That rebuilds the virtual USB controller, which is
the only thing that was ever broken.

Patch the dongle while the VM is off, then attach it once the guest has reached the desktop.
Attaching mid-boot re-opens the race between hypervisor passthrough and Windows enumeration,
which is how the wedged port happens in the first place.

## Tests

```bash
python3 tests/test_auto_mode.py
```

43 assertions covering every branch of the unattended logic against a fake controller — no
USB hardware touched. Beyond return codes it asserts that the bytes the patch branch would
put on the wire reassemble to exactly the correct patch image: fragment count derived from the
image size, end marker set, none over 252 bytes, `HCI_Reset` first. It also asserts the state
file contents, including the session-change edge that warns a VM is holding an unpatched
controller.

Without the firmware blob the suite exits **77**, not 0 — a skip that reports success is how a
test suite comes to advertise coverage it is not providing.

**What is not covered:** the physical event — unplug, replug, agent patches it unattended.
That needs a real power cycle to produce a stock-ROM device. The USB layer itself is proven
by real uploads to real hardware; `--auto` shares the same `parse_epatch()` and `download()`
code paths.

## Known limitation: headset microphones

Under Parallels the guest fails to set up the adapter's SCO isochronous endpoints:

```
[XHC:2357:0604:08.03s] ep create: dscr 07:05, attr 01, mps 0000, interval 01
[XHC:2357:0604] Failed to init endpoint 03
[XHC:2357:0604] Failed to init endpoint 83
```

Endpoints `0x03`/`0x83` are the SCO voice pipes on interface 1. A2DP stereo audio runs over
ACL/L2CAP and is unaffected, but hands-free/headset mode (HFP/HSP) needs SCO. **No firmware
or driver change fixes this** — it is the virtual USB controller. `mps 0000` is interface 1
alt setting 0, the Bluetooth-spec zero-bandwidth default, which the hypervisor cannot open as
a host pipe.

It does not fault the device: control, the HCI-event interrupt pipe and both ACL bulk pipes
come up normally, and the attach succeeds.

## Protocol notes

Verified against `drivers/bluetooth/btrtl.c` and `btrtl.h` (Linux master), and against a real
device.

The pattern is not new. [BrcmPatchRAM](https://github.com/acidanthera/BrcmPatchRAM) does the same
job for Broadcom BCM20702/BCM20703 controllers — upload the vendor firmware from the host, because
whatever normally does it is missing. This is that idea applied to Realtek's RTL87xx ROM-patch
protocol, with macOS as the host rather than the target.

```
HCI transport : USB control transfer, bmRequestType 0x20 (class, device),
                bRequest 0, wValue 0, wIndex 0, payload = HCI command packet.
                HCI events arrive on interrupt IN endpoint 0x81.
Read ROM ver  : vendor opcode 0xFC6D          -> { u8 status, u8 version }
Download      : vendor opcode 0xFC20, payload = { u8 index, u8 data[<=252] }
                RTL_FRAG_LEN = 252;  frag_num = fw_len / 252 + 1
                index increments, wraps to 1 after 0x7F,
                final fragment ORs in 0x80 and carries fw_len % 252 bytes
                                              -> { u8 status, u8 index }
Patch select  : "Realtech" v1 container; choose the patch whose
                chip_id == rom_version + 1; copy (patch_length - 4) bytes and
                append the container's 4-byte fw_version.
No re-enumeration: the same USB handle stays valid throughout.
```

The config blob is optional for 8761BU — `btrtl` marks it `config_needed = false`, and
Realtek's Windows driver references ROM-patch files only. `--append-config` adds the 6-byte
no-op config that Linux appends, if you want to match that path exactly.

## Corroboration

Independent reports of this exact fingerprint, none of which arrive at the firmware explanation:

* [BlueCaster issue #66](https://github.com/SeiyaFunaokaJP/BlueCaster/issues/66) — the project
  was called **A2DP-Windows-Bridge** when this was filed. "LMP subversion 0x8761 is the ROM
  code… finds nothing in a scan", which started working the moment valid firmware was uploaded.
  The closest published statement of the root cause.
* [Parallels forum — "Bluetooth USB adapter recognised in Windows 11 ARM but cannot discover any
  devices"](https://forum.parallels.com/threads/bluetooth-usb-adapter-recognised-in-windows-11-arm-but-cannot-discover-any-devices-parallels-desktop-27-m4-mac.370824/)
  — the whole fingerprint, reported independently: "Generic Bluetooth Adapter", "This device is
  working properly", `bth.inf_arm64` / `BTHUSB`, PowerShell `ProblemCode = 0`, and a scan that
  finds nothing. Unanswered.
* [Parallels forum — "USB Bluetooth Adapter: macOS and Parallels 26, Windows 11
  ARM"](https://forum.parallels.com/threads/usb-bluetooth-adapter-macos-and-parallels-26-windows-11-arm.369311/)
  — three users, no resolution.
* [VMware Fusion on an M4 Pro MacBook
  Pro](https://community.broadcom.com/vmware-cloud-foundation/discussion/bluetooth-on-m4pro-macbook-pro-with-fusion-and-windows11-arm64)
  — the same failure under a different hypervisor, and the most useful data point of the lot: the
  same dongle **works in a Linux ARM64 guest on that host**. That is `btrtl` uploading the patch
  in the Linux guest, and nothing uploading it in the Windows guest.
* [UTM issue #4995](https://github.com/utmapp/UTM/issues/4995) — UTM has no Bluetooth passthrough
  at all, so a USB dongle is the only route there.

## If the patch does not survive the handoff

Attaching the device to a guest triggers a USB port reset. On the hardware this was developed
against the patch survives it (`--test-reset` verifies this directly). If yours reverts to
`lmp_subver 0x8761`:

1. Try `--append-config`.
2. **Swap the adapter** for a chipset that needs no host firmware upload and therefore works
   with Microsoft's inbox driver as-is — a CSR8510-based adapter is the usual choice.
   Firmware-upload chipsets (the Realtek RTL87xx family, most Broadcom) all hit this wall,
   and no ARM64 Realtek Bluetooth driver exists to wait for.

## Files

```
blueboot.py               the loader (--info / --auto / --test-reset / --append-config)
install-autopatch.sh      LaunchAgent install / uninstall
tests/test_auto_mode.py   branch coverage for --auto, no hardware needed
diagnostics/
  _Find-Adapter.ps1       shared: locate the adapter by VID/PID
  Probe-Adapter.ps1       identity, driver binding, device params, event log
  Radio-Props.ps1         decoded HCI/LMP version, subversion, feature mask
  Probe-SoftBlock.ps1     services, radio state, filter drivers in the stack
  Probe-Posture.ps1       can this machine load a self-built driver? + inquiry test
  Scan-Only.ps1           just the BR/EDR inquiry test
```

The PowerShell scripts locate the adapter by VID/PID; override with `$env:UB500_VIDPID`.

## Licence

MIT — see [LICENSE](LICENSE).

The Realtek firmware blob is **not** covered by this licence, is **not** distributed here,
and remains the property of its owners. This project only transports a file you already have
to a device you already own.
