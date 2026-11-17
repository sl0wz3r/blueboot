#!/usr/bin/env python3
"""
blueboot.py - host-side Realtek RTL8761B/BU Bluetooth ROM-patch loader for macOS.

WHY THIS EXISTS
---------------
A Realtek RTL8761B dongle powers up running *stock ROM firmware*. In that state it
enumerates as a standard USB Bluetooth device (class E0/01/01), answers HCI version
queries, and looks perfectly healthy to the OS -- but it is not a usable HCI
controller. Linux's own driver says so in drivers/bluetooth/btrtl.c:

    "Match a set of subver values that correspond to stock firmware, which is not
     compatible with standard btusb. If matched, upload an alternative firmware
     that does conform to standard btusb. Once that firmware is uploaded, the
     subver changes to a different value."

Every OS that supports these dongles uploads that patch at init: Linux via btrtl,
Windows x64 via Realtek's RtkBtfilter.sys lower filter. On Windows 11 **ARM64**
there is no such driver -- Realtek/TP-Link ship x86 and x64 binaries only, and
Windows-on-ARM cannot emulate kernel-mode code. So a passed-through dongle in an
ARM64 VM is stuck on ROM firmware forever.

This tool closes that gap from the macOS side: it performs the exact same patch
upload Linux does, over libusb, before the dongle is handed to the VM.

SAFETY
------
The patch is loaded into controller RAM, not flashed. Nothing is written to any
non-volatile memory. Unplugging the dongle restores the stock ROM state exactly.
This is the same operation Linux performs on every single boot.

PROTOCOL (verified against drivers/bluetooth/btrtl.c + btrtl.h, Linux master)
-----------------------------------------------------------------------------
  HCI transport  : USB control transfer, bmRequestType=0x20 (class, device),
                   bRequest=0, wValue=0, wIndex=0, payload = HCI command packet.
                   HCI events arrive on interrupt IN endpoint 0x81.
  Read ROM ver   : vendor opcode 0xFC6D -> { u8 status, u8 version }
  Download       : vendor opcode 0xFC20, payload = { u8 index, u8 data[<=252] }
                   RTL_FRAG_LEN = 252; frag_num = fw_len/252 + 1
                   index increments; wraps to 1 after reaching 0x7F;
                   final fragment ORs in 0x80 and carries fw_len % 252 bytes.
                   Response: { u8 status, u8 index }
  Patch select   : "Realtech" container -> pick the patch whose
                   chip_id == rom_version + 1, copy (patch_length - 4) bytes and
                   append the container's 4-byte fw_version.
"""

import argparse
import ctypes
import ctypes.util
import datetime
import os
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------- libusb glue

def _load_libusb():
    candidates = []
    found = ctypes.util.find_library("usb-1.0")
    if found:
        candidates.append(found)
    candidates += [
        "/opt/homebrew/lib/libusb-1.0.0.dylib",
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/usr/local/lib/libusb-1.0.0.dylib",
        "libusb-1.0.so.0",
    ]
    for c in candidates:
        try:
            return ctypes.CDLL(c)
        except OSError:
            continue
    sys.exit("error: libusb-1.0 not found. Install it with:  brew install libusb")


lu = _load_libusb()


class DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_uint8),
        ("bDescriptorType", ctypes.c_uint8),
        ("bcdUSB", ctypes.c_uint16),
        ("bDeviceClass", ctypes.c_uint8),
        ("bDeviceSubClass", ctypes.c_uint8),
        ("bDeviceProtocol", ctypes.c_uint8),
        ("bMaxPacketSize0", ctypes.c_uint8),
        ("idVendor", ctypes.c_uint16),
        ("idProduct", ctypes.c_uint16),
        ("bcdDevice", ctypes.c_uint16),
        ("iManufacturer", ctypes.c_uint8),
        ("iProduct", ctypes.c_uint8),
        ("iSerialNumber", ctypes.c_uint8),
        ("bNumConfigurations", ctypes.c_uint8),
    ]


lu.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
lu.libusb_exit.argtypes = [ctypes.c_void_p]
lu.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
lu.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
lu.libusb_close.argtypes = [ctypes.c_void_p]
lu.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lu.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
lu.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
lu.libusb_get_device.argtypes = [ctypes.c_void_p]
lu.libusb_get_device.restype = ctypes.c_void_p
lu.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.POINTER(DeviceDescriptor)]
lu.libusb_strerror.argtypes = [ctypes.c_int]
lu.libusb_strerror.restype = ctypes.c_char_p
lu.libusb_control_transfer.argtypes = [
    ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
    ctypes.c_uint16, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint16, ctypes.c_uint,
]
lu.libusb_reset_device.argtypes = [ctypes.c_void_p]
lu.libusb_get_device_list.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
lu.libusb_get_device_list.restype = ctypes.c_ssize_t
lu.libusb_free_device_list.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
lu.libusb_interrupt_transfer.argtypes = [
    ctypes.c_void_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
]


def lerr(code):
    s = lu.libusb_strerror(ctypes.c_int(code))
    return s.decode() if s else "error %d" % code


# ------------------------------------------------------------------ HCI layer

HCI_EVENT_EP = 0x81
CMD_REQ_TYPE = 0x20          # host->device | class | recipient=device  (btusb cmdreq_type)
EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F

OP_RESET = 0x0C03
OP_READ_LOCAL_VERSION = 0x1001
OP_RTL_READ_ROM_VERSION = 0xFC6D
OP_RTL_DOWNLOAD = 0xFC20

RTL_FRAG_LEN = 252

# linux-firmware ships rtl8761bu_config.bin as a 6-byte no-op: signature 0x8723AB55 +
# total_len 0 (zero entries). btrtl appends it when present; Realtek's own Windows driver
# does not (RtkBtfilter.sys references rom_patch files only). Off by default, matching the
# vendor driver for this exact VID/PID; --append-config reproduces the Linux path instead.
RTL_NOOP_CONFIG = bytes.fromhex("55ab23870000")


class HciError(Exception):
    pass


class Dongle:
    def __init__(self, vid, pid, timeout=3000, verbose=False):
        self.vid, self.pid = vid, pid
        self.timeout = timeout
        self.verbose = verbose
        self.ctx = ctypes.c_void_p()
        self.h = None
        self.claimed = None

    def open(self):
        rc = lu.libusb_init(ctypes.byref(self.ctx))
        if rc != 0:
            raise HciError("libusb_init failed: %s" % lerr(rc))
        self.h = lu.libusb_open_device_with_vid_pid(self.ctx, self.vid, self.pid)
        if not self.h:
            raise HciError(
                "could not open %04x:%04x.\n"
                "  - Is the dongle plugged in?\n"
                "  - Is it still attached to the Parallels VM? It must be released to macOS\n"
                "    first (Parallels menu bar: Devices > USB & Bluetooth > TP-Link UB500 Adapter).\n"
                "  - macOS may also need to grant USB access to your terminal."
                % (self.vid, self.pid)
            )
        lu.libusb_set_auto_detach_kernel_driver(self.h, 1)   # no-op on macOS, harmless
        rc = lu.libusb_claim_interface(self.h, 0)
        if rc != 0:
            raise HciError(
                "claim_interface(0) failed: %s\n"
                "  Something else owns the dongle - almost certainly the Parallels VM.\n"
                "  Release it to macOS and try again." % lerr(rc)
            )
        self.claimed = 0
        return self

    def close(self):
        if self.h:
            if self.claimed is not None:
                lu.libusb_release_interface(self.h, self.claimed)
            lu.libusb_close(self.h)
            self.h = None
        if self.ctx:
            lu.libusb_exit(self.ctx)
            self.ctx = ctypes.c_void_p()

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def descriptor(self):
        d = DeviceDescriptor()
        dev = lu.libusb_get_device(self.h)
        lu.libusb_get_device_descriptor(dev, ctypes.byref(d))
        return d

    # -- raw transport -----------------------------------------------------

    def _send_cmd(self, opcode, params=b""):
        pkt = struct.pack("<HB", opcode, len(params)) + params
        buf = (ctypes.c_ubyte * len(pkt)).from_buffer_copy(pkt)
        rc = lu.libusb_control_transfer(
            self.h, CMD_REQ_TYPE, 0x00, 0x0000, 0x0000, buf, len(pkt), self.timeout
        )
        if rc < 0:
            raise HciError("control transfer for opcode 0x%04X failed: %s" % (opcode, lerr(rc)))
        if rc != len(pkt):
            raise HciError("short control transfer: %d of %d bytes" % (rc, len(pkt)))

    def _read_event(self, timeout=None):
        buf = (ctypes.c_ubyte * 260)()
        n = ctypes.c_int(0)
        rc = lu.libusb_interrupt_transfer(
            self.h, HCI_EVENT_EP, buf, 260, ctypes.byref(n),
            self.timeout if timeout is None else timeout,
        )
        if rc != 0:
            raise HciError("interrupt read failed: %s" % lerr(rc))
        return bytes(bytearray(buf[: n.value]))

    def cmd(self, opcode, params=b"", expect_status=True):
        """Send an HCI command, return the Command Complete return-parameters."""
        self._send_cmd(opcode, params)
        for _ in range(32):                     # tolerate unrelated events in between
            evt = self._read_event()
            if len(evt) < 2:
                continue
            code, plen = evt[0], evt[1]
            body = evt[2 : 2 + plen]
            if code == EVT_CMD_COMPLETE and len(body) >= 3:
                op = struct.unpack_from("<H", body, 1)[0]
                if op != opcode:
                    continue
                ret = body[3:]
                if expect_status and (not ret or ret[0] != 0x00):
                    raise HciError(
                        "opcode 0x%04X returned HCI status 0x%02X"
                        % (opcode, ret[0] if ret else 0xFF)
                    )
                return ret
            if code == EVT_CMD_STATUS and len(body) >= 4:
                op = struct.unpack_from("<H", body, 2)[0]
                if op == opcode:
                    if body[0] != 0x00:
                        raise HciError("opcode 0x%04X status 0x%02X" % (opcode, body[0]))
                    return b""
            if self.verbose:
                print("    (ignoring event 0x%02X)" % code)
        raise HciError("no Command Complete for opcode 0x%04X" % opcode)

    # -- typed helpers -----------------------------------------------------

    def reset(self):
        self.cmd(OP_RESET)

    def read_local_version(self):
        r = self.cmd(OP_READ_LOCAL_VERSION)
        if len(r) < 9:
            raise HciError("short Read_Local_Version response (%d bytes)" % len(r))
        hci_ver = r[1]
        hci_rev, = struct.unpack_from("<H", r, 2)
        lmp_ver = r[4]
        manufacturer, = struct.unpack_from("<H", r, 5)
        lmp_subver, = struct.unpack_from("<H", r, 7)
        return dict(hci_ver=hci_ver, hci_rev=hci_rev, lmp_ver=lmp_ver,
                    manufacturer=manufacturer, lmp_subver=lmp_subver)

    def read_rom_version(self):
        r = self.cmd(OP_RTL_READ_ROM_VERSION)
        if len(r) < 2:
            raise HciError("short ROM version response")
        return r[1]


# ------------------------------------------------------- firmware container

def parse_epatch(blob, rom_version):
    """Select and build the patch image, exactly as btrtl.c rtl_parse_firmware()."""
    if blob[:8] != b"Realtech":
        if blob[:8] == b"RTBTCore":
            raise SystemExit("error: this is a v2 (RTBTCore) container; not supported here.")
        raise SystemExit("error: bad EPATCH signature (expected 'Realtech', got %r)" % blob[:8])

    fw_version, = struct.unpack_from("<I", blob, 8)
    num_patches, = struct.unpack_from("<H", blob, 12)

    chip_id_base = 14
    length_base = chip_id_base + 2 * num_patches
    offset_base = length_base + 2 * num_patches
    if len(blob) < offset_base + 4 * num_patches:
        raise SystemExit("error: firmware file truncated")

    patch_offset = patch_length = None
    table = []
    for i in range(num_patches):
        cid, = struct.unpack_from("<H", blob, chip_id_base + 2 * i)
        ln, = struct.unpack_from("<H", blob, length_base + 2 * i)
        off, = struct.unpack_from("<I", blob, offset_base + 4 * i)
        table.append((cid, ln, off))
        if cid == rom_version + 1:
            patch_offset, patch_length = off, ln

    if patch_offset is None:
        raise SystemExit(
            "error: no patch in this file for ROM version %d (need chip_id %d; file has %s)"
            % (rom_version, rom_version + 1, ", ".join(str(t[0]) for t in table))
        )
    if patch_length < 4 or patch_offset + patch_length > len(blob):
        raise SystemExit("error: patch extent out of range")

    # btrtl: copy (patch_length - 4) bytes, then append the container fw_version.
    img = bytearray(blob[patch_offset : patch_offset + patch_length - 4])
    img += struct.pack("<I", fw_version)
    return bytes(img), fw_version, table


def project_id(blob):
    """Read project_id from the trailing extension section (btrtl rtl_parse_firmware)."""
    if len(blob) < 8 or blob[-4:] != b"\x51\x04\xfd\x77":
        return None
    p = len(blob) - 4
    while p >= 16:
        opcode = blob[p - 1]
        length = blob[p - 2]
        p -= 2
        if opcode == 0xFF:
            return None
        if length == 0:
            return None
        if opcode == 0x00 and length == 1:
            return blob[p - 1]
        p -= length
    return None


# -------------------------------------------------------------- download

def download(dev, img, verbose=False, progress=True):
    fw_len = len(img)
    frag_num = fw_len // RTL_FRAG_LEN + 1
    j = 0
    sent = 0
    for i in range(frag_num):
        index = j
        j += 1
        if index == 0x7F:
            j = 1
        frag_len = RTL_FRAG_LEN
        if i == frag_num - 1:
            index |= 0x80                      # data-end marker
            frag_len = fw_len % RTL_FRAG_LEN
        chunk = img[sent : sent + frag_len]
        payload = bytes([index]) + chunk
        r = dev.cmd(OP_RTL_DOWNLOAD, payload)
        if len(r) < 2:
            raise HciError("download fragment %d: short response" % i)
        sent += RTL_FRAG_LEN
        if progress and (verbose or (i % 20 == 0) or i == frag_num - 1):
            pct = 100.0 * min(sent, fw_len) / fw_len
            print("\r  uploading… fragment %3d/%d  (%5.1f%%)" % (i + 1, frag_num, pct),
                  end="", flush=True)
    if progress:
        print()


# ------------------------------------------------------------------- report

def describe(v):
    return ("hci_ver=0x%02X (%d)  hci_rev=0x%04X  lmp_ver=0x%02X  "
            "manufacturer=%d%s  lmp_subver=0x%04X"
            % (v["hci_ver"], v["hci_ver"], v["hci_rev"], v["lmp_ver"],
               v["manufacturer"], " (Realtek)" if v["manufacturer"] == 93 else "",
               v["lmp_subver"]))


FW_BASENAME = "rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat"


def default_dat():
    """Locate the Realtek ROM patch.

    The firmware is Realtek/TP-Link proprietary and is deliberately NOT distributed
    with this tool - extract it from the vendor driver package yourself (see README).
    Search order: $UB500_FW, the agent's installed copy, this directory, the cwd.
    """
    env = os.environ.get("UB500_FW")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (
        os.path.expanduser("~/Library/Application Support/ub500-autopatch/rtl8761b_rom_patch.dat"),
        os.path.join(here, FW_BASENAME),
        os.path.join(here, "rtl8761b_rom_patch.dat"),
        os.path.join(os.getcwd(), FW_BASENAME),
    ):
        if os.path.exists(c):
            return c
    return os.path.join(here, FW_BASENAME)


def probe_bus(vid, pid):
    """Ask ioreg about the dongle. Unlike libusb, ioreg still reports a device that
    Parallels holds exclusively, so this gives a true three-way answer:

        (False, None)      not plugged in
        (True,  "pid ...") plugged in, owned by another process (normally the VM)
        (True,  None)      plugged in and free - we may claim it

    That third case matters: if libusb then fails to open a free device, the cause
    is almost certainly a denied macOS USB-access prompt, which would otherwise be
    an entirely silent failure in an unattended agent.
    """
    try:
        out = subprocess.run(
            ["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0"],
            capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return (None, None)

    for chunk in out.split("+-o ")[1:]:
        if ('"idVendor" = %d' % vid) in chunk and ('"idProduct" = %d' % pid) in chunk:
            for line in chunk.splitlines():
                if "UsbExclusiveOwner" in line:
                    owner = line.split("=", 1)[1].strip().strip('"')
                    return (True, owner)
            return (True, None)
    return (False, None)


def auto_mode(args):
    """Unattended mode for the LaunchAgent.

    Stays completely silent unless it actually does something, so the log is a
    record of real events rather than a heartbeat. Every 'nothing to do' case -
    dongle unplugged, dongle currently owned by the VM, already patched - exits 0
    without output. It never competes with Parallels: if the VM holds the device,
    libusb simply cannot open it and we leave it alone.
    """
    def stamp():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Log only on state CHANGES, so a 10-second timer does not fill the log with
    # heartbeat lines while still surfacing anything that actually needs attention.
    state_path = os.path.join(os.path.dirname(os.path.abspath(args.dat)), ".autopatch-state")

    def transition(state, message=None):
        try:
            previous = open(state_path).read().strip()
        except OSError:
            previous = ""
        if state != previous:
            try:
                open(state_path, "w").write(state)
            except OSError:
                pass
            if message:
                print("%s  %s" % (stamp(), message))
        return state

    present, owner = probe_bus(args.vid, args.pid)
    if present is False:
        transition("absent")
        return 0
    if owner:
        transition("owned")          # the VM has it - expected, nothing to do
        return 0

    try:
        dev = Dongle(args.vid, args.pid).open()
    except HciError as e:
        # Free on the bus yet unopenable: this is the silent-failure case worth
        # shouting about, because it usually means macOS denied USB access.
        transition("blocked",
                   "dongle is free on the USB bus but cannot be opened - macOS has "
                   "probably not granted USB access to this agent (%s)" % e.__class__.__name__)
        return 1

    try:
        v = dev.read_local_version()
        if not (v["lmp_subver"] == 0x8761 and v["hci_rev"] == 0x000B):
            transition("patched")
            return 0
        print("%s  stock ROM detected (0x%04X%04X)" % (stamp(), v["hci_rev"], v["lmp_subver"]))

        try:
            dev.reset()
        except HciError:
            pass
        rom = dev.read_rom_version()

        if not os.path.exists(args.dat):
            print("%s  ERROR firmware file missing: %s" % (stamp(), args.dat))
            return 1
        blob = open(args.dat, "rb").read()
        img, fw_version, _ = parse_epatch(blob, rom)
        if args.append_config:
            img += RTL_NOOP_CONFIG

        download(dev, img, progress=False)
        after = dev.read_local_version()
        exp = ((fw_version >> 16) & 0xFFFF, fw_version & 0xFFFF)
        got = (after["hci_rev"], after["lmp_subver"])
        if got == exp:
            transition("patched")
            print("%s  PATCHED ok -> 0x%04X%04X (%d bytes, rom_version %d)"
                  % (stamp(), got[0], got[1], len(img), rom))
            return 0
        print("%s  UNEXPECTED -> 0x%04X%04X, wanted 0x%04X%04X"
              % (stamp(), got[0], got[1], exp[0], exp[1]))
        return 3
    except Exception as e:
        print("%s  ERROR %s" % (stamp(), e))
        return 1
    finally:
        dev.close()


def test_reset(args):
    """Issue a real USB port reset - what a VM's virtual xHCI does on attach - and
    report whether the RAM-resident patch survives it."""
    def read_version():
        d = Dongle(args.vid, args.pid).open()
        try:
            return d.read_local_version()
        finally:
            d.close()

    before = read_version()
    print("before reset  : %s" % describe(before))

    d = Dongle(args.vid, args.pid).open()
    rc = lu.libusb_reset_device(d.h)
    print("port reset    : %s" % ("ok" if rc == 0 else lerr(rc)))
    d.close()
    time.sleep(2.0)

    after = read_version()
    print("after reset   : %s" % describe(after))

    if after["lmp_subver"] == 0x8761 and after["hci_rev"] == 0x000B:
        print("\nWIPED: the reset reverted the controller to stock ROM.")
        print("A VM attach will do the same, so host-side patching cannot persist.")
        return 1
    print("\nSURVIVED: still 0x%04X%04X after a port reset."
          % (after["hci_rev"], after["lmp_subver"]))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Upload the Realtek RTL8761B ROM patch to a USB Bluetooth dongle from macOS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The patch lives in controller RAM. Unplugging the dongle fully reverts it.",
    )
    ap.add_argument("--vid", type=lambda s: int(s, 0), default=0x2357, help="USB vendor id (default 0x2357)")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=0x0604, help="USB product id (default 0x0604)")
    ap.add_argument("--dat", default=default_dat(),
                    help="Realtek ROM-patch .dat (see README; or set UB500_FW)")
    ap.add_argument("--info", action="store_true", help="report controller state and exit (no writes)")
    ap.add_argument("--no-reset", action="store_true", help="skip the initial HCI_Reset")
    ap.add_argument("--force", action="store_true", help="upload even if the controller looks already patched")
    ap.add_argument("--append-config", action="store_true",
                    help="append the 6-byte no-op config blob, as Linux btrtl does (fallback)")
    ap.add_argument("--auto", action="store_true",
                    help="unattended: patch only if present, free and stock; silent otherwise")
    ap.add_argument("--test-reset", action="store_true",
                    help="issue a USB port reset and check whether the patch survives it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.auto:
        return auto_mode(args)

    print("Realtek RTL8761B ROM-patch loader")
    print("=" * 62)

    if args.test_reset:
        return test_reset(args)

    with Dongle(args.vid, args.pid, verbose=args.verbose) as dev:
        d = dev.descriptor()
        print("USB device    : %04x:%04x  bcdDevice=0x%04X  class=%02X/%02X/%02X"
              % (d.idVendor, d.idProduct, d.bcdDevice,
                 d.bDeviceClass, d.bDeviceSubClass, d.bDeviceProtocol))

        if not args.no_reset and not args.info:
            try:
                dev.reset()
                print("HCI_Reset     : ok")
            except HciError as e:
                print("HCI_Reset     : %s (continuing)" % e)

        before = dev.read_local_version()
        print("Controller    : %s" % describe(before))

        rom = dev.read_rom_version()
        print("ROM version   : %d  -> needs patch chip_id %d" % (rom, rom + 1))

        stock = (before["lmp_subver"] == 0x8761 and before["hci_rev"] == 0x000B)
        print("State         : %s" % ("STOCK ROM - patch NOT loaded" if stock
                                      else "lmp_subver/hci_rev differ from stock; may already be patched"))

        if args.info:
            return 0

        if not stock and not args.force:
            print("\nController does not look like unpatched stock firmware.")
            print("Re-run with --force to upload anyway, or unplug/replug and retry.")
            return 2

        if not os.path.exists(args.dat):
            sys.exit(
                "error: firmware file not found: %s\n"
                "  The Realtek ROM patch is proprietary and is not shipped with this tool.\n"
                "  Extract %s\n"
                "  from TP-Link's UB500 driver package (Driver/Windows_11_64bit/) and pass\n"
                "  it with --dat, or set UB500_FW." % (args.dat, FW_BASENAME))
        blob = open(args.dat, "rb").read()
        pid_ = project_id(blob)
        img, fw_version, table = parse_epatch(blob, rom)
        print("\nFirmware file : %s" % args.dat)
        print("  container   : 'Realtech'  fw_version=0x%08X  patches=%s"
              % (fw_version, ", ".join("chip_id=%d(len=%d)" % (c, l) for c, l, _ in table)))
        print("  project_id  : %s%s" % (pid_, "  (14 = RTL8761B)" if pid_ == 14 else ""))
        print("  selected    : chip_id=%d  ->  %d bytes" % (rom + 1, len(img)))
        if args.append_config:
            img += RTL_NOOP_CONFIG
            print("  config      : appended %d-byte no-op config (Linux btrtl behaviour)"
                  % len(RTL_NOOP_CONFIG))

        # btrtl logs the running firmware as "0x%04x%04x" % (hci_rev, lmp_subver), so a
        # successful patch makes the controller report the container's fw_version split
        # across those two fields. That is an exact, predictable target.
        exp_hci_rev = (fw_version >> 16) & 0xFFFF
        exp_lmp_subver = fw_version & 0xFFFF
        print("  expect after: hci_rev=0x%04X  lmp_subver=0x%04X" % (exp_hci_rev, exp_lmp_subver))
        print()

        download(dev, img, verbose=args.verbose)

        after = dev.read_local_version()
        print("\nController    : %s" % describe(after))

        if after["hci_rev"] == exp_hci_rev and after["lmp_subver"] == exp_lmp_subver:
            print("\nSUCCESS: controller now reports firmware 0x%04X%04X, exactly matching the"
                  % (after["hci_rev"], after["lmp_subver"]))
            print("container's fw_version. The patch is live.")
            print("The patch is live. Now reattach the dongle to the Windows VM")
            print("WITHOUT unplugging it (Parallels: Devices > USB & Bluetooth).")
            return 0

        if after["lmp_subver"] != before["lmp_subver"] or after["hci_rev"] != before["hci_rev"]:
            print("\nPARTIAL: the firmware version changed (0x%04X%04X -> 0x%04X%04X) but does not"
                  % (before["hci_rev"], before["lmp_subver"], after["hci_rev"], after["lmp_subver"]))
            print("match the expected 0x%04X%04X. The radio may still work; test it."
                  % (exp_hci_rev, exp_lmp_subver))
            return 0

        print("\nWARNING: version unchanged - still stock ROM (0x%04X%04X). The upload reported"
              % (after["hci_rev"], after["lmp_subver"]))
        print("success but did not take. Try: unplug/replug, then re-run; or re-run with")
        print("--append-config.")
        return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HciError as e:
        sys.exit("HCI error: %s" % e)
    except KeyboardInterrupt:
        sys.exit(130)
