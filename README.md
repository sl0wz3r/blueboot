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
discoverable device sitting next to it, is the signature.

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

The tool validates it: `Realtech` container signature, a `project_id` in the trailing
extension block matching your chip, and a patch whose `chip_id` matches the controller's ROM
version. Mismatches are refused rather than uploaded.

Linux users: `linux-firmware` ships `rtl_bt/rtl8761bu_fw.bin`, a different (older) build of
the same thing. Either works; they are not byte-identical.

## Usage

Requires `brew install libusb`.

```bash
# 1. Release the dongle from the VM  (Parallels: Devices > USB & Bluetooth > your adapter)

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
again. The agent re-applies it automatically:

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
while libusb does not. That gives a real three-way answer instead of one ambiguous failure:

| situation | action |
|---|---|
| not plugged in | silent, exit 0 |
| plugged in, owned by a VM | silent, exit 0 — never competes with the hypervisor |
| free, already patched | silent, exit 0 |
| free, running stock ROM | **patch it**, log one line |
| free but libusb cannot open it | **log a warning** — macOS likely denied USB access |

That last row is the point. Without it a denied USB-access prompt looks exactly like
"everything is fine", and the agent fails silently forever. It logs only on state *changes*,
so a 10-second timer does not produce a heartbeat log.

```bash
tail -f ~/Library/Logs/blueboot.log
launchctl print gui/$UID/com.blueboot.autopatch | grep -E "state =|runs =|last exit"
```

### Timing

If your hypervisor is set to auto-connect the dongle, it will grab it before the agent can
patch it. Leave it on "ask", and **let the dongle sit on macOS for a few seconds before
connecting it to the VM**. The log line is your confirmation.

## Tests

```bash
python3 tests/test_auto_mode.py
```

18 assertions covering every branch of the unattended logic against a fake controller — no
USB hardware touched. Beyond return codes it asserts that the bytes the patch branch would
put on the wire reassemble to exactly the correct patch image: 116 fragments, end marker set,
none over 252 bytes, `HCI_Reset` first.

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
