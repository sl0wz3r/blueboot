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
import re
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
        os.path.expanduser("~/Library/Application Support/blueboot/rtl8761b_rom_patch.dat"),
        os.path.join(here, FW_BASENAME),
        os.path.join(here, "rtl8761b_rom_patch.dat"),
        os.path.join(os.getcwd(), FW_BASENAME),
    ):
        if os.path.exists(c):
            return c
    return os.path.join(here, FW_BASENAME)


STOCK_SIG = (0x000B, 0x8761)      # hci_rev, lmp_subver of unpatched RTL8761B ROM


def _ioreg(argv, timeout=5):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout


def probe_bus(vid, pid):
    """Ask ioreg about the dongle. Unlike libusb, ioreg still reports a device that
    Parallels holds exclusively, so this gives a true FOUR-way answer:

        (None,  None,      None)   ioreg did not answer - we know NOTHING
        (False, None,      None)   not plugged in
        (True,  "pid ...", S)      plugged in, owned by another process (the VM)
        (True,  None,      S)      plugged in and free - we may claim it

    The first case has to stay distinct from the rest. A probe that observed nothing
    is not evidence that the dongle is free, and conflating the two is how an agent
    ends up asserting a cause it never established.

    S is ioreg's sessionID: a mach_absolute_time stamp taken when the device
    enumerated. Two properties make it the keystone of this tool's self-knowledge.
    It stays readable while Parallels holds the device exclusively - so it works
    during exactly the window when libusb cannot tell us anything - and it changes
    precisely when the dongle re-enumerates, which is precisely when the RAM-resident
    patch is lost. That turns "the VM has it" from a shrug into a falsifiable claim,
    and it is the only host-visible signal separating a healthy VM-owned dongle from
    one the guest is seeing as stock ROM.

    We query the IOUSB plane: ~26 KB rather than ~2 MB, and it yields only the device
    node. The IOService plane also returns the interface children, which carry
    UsbExclusiveOwner but no sessionID, so a first-match scan there is ambiguous.
    """
    try:
        out = _ioreg(["/usr/sbin/ioreg", "-p", "IOUSB", "-r", "-c",
                      "IOUSBHostDevice", "-l", "-w", "0"])
        if "+-o " not in out:          # plane unsupported here - fall back
            out = _ioreg(["/usr/sbin/ioreg", "-r", "-c",
                          "IOUSBHostDevice", "-l", "-w", "0"], timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return (None, None, None)

    for chunk in out.split("+-o ")[1:]:
        if ('"idVendor" = %d' % vid) in chunk and ('"idProduct" = %d' % pid) in chunk:
            m = re.search(r'"sessionID"\s*=\s*(\d+)', chunk)
            session = m.group(1) if m else None
            for line in chunk.splitlines():
                if "UsbExclusiveOwner" in line:
                    return (True, line.split("=", 1)[1].strip().strip('"'), session)
            return (True, None, session)
    return (False, None, None)


def _state_path(dat):
    return os.path.join(os.path.dirname(os.path.abspath(dat)), ".autopatch-state")


def _read_state(state_path):
    """Return (word, session, signature).

    Tolerates anything on disk. This file is read by a job that runs every ten
    seconds forever; a truncated or non-UTF-8 file - entirely possible after the
    unclean shutdown that a host reboot looks like - must never be able to kill it.
    """
    try:
        with open(state_path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
    except Exception:
        return ("", None, None)
    parts = raw.split(":")
    word = parts[0] if parts else ""
    session = parts[1] if len(parts) > 1 and parts[1] else None
    sig = None
    if len(parts) > 3:
        try:
            sig = (int(parts[2], 16), int(parts[3], 16))
        except ValueError:
            sig = None
    return (word, session, sig)


def _write_state(state_path, word, session=None, sig=None):
    raw = word
    if session or sig:
        raw += ":" + (session or "")
    if sig:
        raw += ":%04X:%04X" % sig
    try:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.replace(tmp, state_path)     # atomic: never leave a half-written state
    except Exception:
        pass


def auto_mode(args):
    """Unattended mode for the LaunchAgent.

    Stays silent unless something real happened, so the log is a record of events
    rather than a heartbeat. Liveness is answered by the .heartbeat file and by
    --status instead, which means an empty log genuinely reads "nothing went wrong"
    rather than "the agent died three days ago".

    Every exit routes through transition(). That is not tidiness: when a fault path
    merely printed and returned, the next poll re-read the device, found it "not
    stock", wrote 'patched' and went silent about a real failure forever.
    """
    def stamp():
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    state_path = _state_path(args.dat)

    # Liveness gets its own file, touched every run. The state file is written only
    # on CHANGE, so its mtime answers "when did the world last change" and never
    # "is this agent still alive" - and conflating those is how a dead agent goes on
    # looking healthy.
    try:
        with open(os.path.join(os.path.dirname(state_path), ".heartbeat"),
                  "w", encoding="utf-8") as fh:
            fh.write(stamp())
    except Exception:
        pass

    def transition(state, message=None, session=None, sig=None):
        prev_word, prev_session, prev_sig = _read_state(state_path)
        if state != prev_word or session != prev_session or sig != prev_sig:
            _write_state(state_path, state, session, sig)
            if message:
                print("%s  %s" % (stamp(), message))
        return state

    present, owner, session = probe_bus(args.vid, args.pid)

    if present is None:
        # ioreg told us nothing at all. That is NOT evidence the dongle is free.
        # This branch used to be absent, so (None, None) fell past both guards and
        # reached open(), letting a probe that observed nothing report a denied
        # macOS USB prompt as a confident root cause.
        transition("probe-failed",
                   "ioreg did not answer - cannot tell whether the dongle is free")
        return 0

    if present is False:
        transition("absent")
        return 0

    if owner:
        prev_word, prev_session, prev_sig = _read_state(state_path)
        if (session and prev_session and session != prev_session
                and prev_word in ("patched", "owned")):
            # The device re-enumerated, so the RAM patch is gone - and the VM already
            # holds the new enumeration, so we cannot reach it to re-patch. The guest
            # is looking at stock ROM and will find nothing, however healthy its
            # device node looks. This is the single line that was missing on the
            # morning this tool cost someone four hours.
            _write_state(state_path, "owned", session)
            print("%s  dongle re-enumerated (session %s -> %s) while held by %s - the "
                  "guest now has an UNPATCHED controller. Shut the VM down fully and "
                  "start it again; a detach/reattach will not re-enumerate it."
                  % (stamp(), prev_session, session, owner))
        else:
            # Carry the signature forward. "The VM has it" is only good news when we
            # know this enumeration is the one we patched, so the proof has to
            # survive the handoff - otherwise --status has to answer UNKNOWN.
            keep = prev_sig if (prev_session and session == prev_session) else None
            transition("owned", session=session, sig=keep)
        return 0

    try:
        dev = Dongle(args.vid, args.pid).open()
    except HciError as e:
        # Look again before blaming anything. By far the likeliest explanation is
        # that Parallels claimed the dongle between our probe and our open, which is
        # a non-event - not the denied USB prompt this used to assert on no evidence.
        _, again_owner, again_session = probe_bus(args.vid, args.pid)
        if again_owner:
            transition("owned", session=again_session)
            return 0
        detail = " ".join(str(e).split())
        if len(detail) > 220:
            detail = detail[:217] + "..."
        # A failed claim_interface and a device that will not open at all have
        # opposite fixes, so they get distinct state words and cannot mask each
        # other through transition()'s change detection.
        word = "blocked-busy" if "claim_interface" in detail else "blocked-access"
        transition(word,
                   "dongle is free on the USB bus but cannot be opened: %s" % detail,
                   session=session)
        return 1

    try:
        v = dev.read_local_version()
        sig = (v["hci_rev"], v["lmp_subver"])
        _, _, prev_sig = _read_state(state_path)

        if sig != STOCK_SIG:
            if prev_sig and sig != prev_sig:
                # A controller has three possible states, not two. "Not stock" was
                # being recorded as success, so a half-finished download looked
                # exactly like a good one.
                transition("anomalous",
                           "controller reports 0x%04X%04X - neither stock nor the "
                           "0x%04X%04X last loaded; firmware state unknown"
                           % (sig[0], sig[1], prev_sig[0], prev_sig[1]),
                           session=session, sig=sig)
                return 3
            transition("patched", session=session, sig=sig)
            return 0

        print("%s  stock ROM detected (0x%04X%04X)" % (stamp(), sig[0], sig[1]))

        try:
            dev.reset()
        except HciError:
            pass
        rom = dev.read_rom_version()

        if not os.path.exists(args.dat):
            # Routed through transition() like every other fault. This is the one
            # failure guaranteed to recur on every single poll, so it was the one
            # most in need of the dedup it used to bypass - roughly 12,000 identical
            # lines a day otherwise.
            transition("fw-missing", "ERROR firmware file missing: %s" % args.dat,
                       session=session)
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
            transition("patched", session=session, sig=got)
            print("%s  PATCHED ok -> 0x%04X%04X (%d bytes, rom_version %d)"
                  % (stamp(), got[0], got[1], len(img), rom))
            return 0
        transition("mismatch",
                   "UNEXPECTED -> 0x%04X%04X, wanted 0x%04X%04X"
                   % (got[0], got[1], exp[0], exp[1]),
                   session=session, sig=got)
        return 3
    except (Exception, SystemExit) as e:
        # SystemExit derives from BaseException, so parse_epatch's sys.exit() on a
        # truncated firmware container used to sail straight past a bare
        # `except Exception` and out of auto_mode, emitting an untimestamped orphan
        # line into a log whose one invariant is that every line starts with a date.
        transition("error", "ERROR %s" % " ".join(str(e).split()), session=session)
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


def _decode_session(session):
    """Decode ioreg's sessionID to wall-clock time.

    It is a mach_absolute_time stamp taken at enumeration, so boot time plus the
    tick count converted through mach_timebase_info gives the moment the dongle
    appeared on the bus. Returns None rather than guessing if any part of that is
    unavailable: a diagnostic that lies is worse than one that abstains.
    """
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if not m:
            return None
        boot = int(m.group(1))

        class _Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        tb = _Timebase()
        if ctypes.CDLL(None).mach_timebase_info(ctypes.byref(tb)) != 0 or not tb.denom:
            return None
        return datetime.datetime.fromtimestamp(
            boot + (int(session) * tb.numer / tb.denom) / 1e9)
    except Exception:
        return None


def status_mode(args):
    """Answer "is my Bluetooth going to work right now?" in one command.

    Deliberately never opens the device. --info cannot answer this question: it goes
    through libusb_claim_interface, which fails by design whenever the VM holds the
    dongle - that is, for the entire duration of the failure you would be debugging.
    This reads ioreg, ps and our own state files, nothing else.

    Says UNKNOWN rather than guessing. A status command that guesses is the same
    mistake as a Windows phantom device node cheerfully reporting a healthy adapter
    out of a stale cache.
    """
    state_path = _state_path(args.dat)
    hb_path = os.path.join(os.path.dirname(state_path), ".heartbeat")
    word, rec_session, rec_sig = _read_state(state_path)
    present, owner, session = probe_bus(args.vid, args.pid)

    print("blueboot status")
    print("=" * 62)

    if present is None:
        print("dongle        : UNKNOWN - ioreg did not answer")
    elif present is False:
        print("dongle        : not plugged in")
    elif owner:
        who = owner
        m = re.search(r"pid\s+(\d+)", owner)
        if m:
            try:
                comm = subprocess.run(["/bin/ps", "-p", m.group(1), "-o", "comm="],
                                      capture_output=True, text=True,
                                      timeout=5).stdout.strip()
                if comm:
                    who = "%s (pid %s)" % (os.path.basename(comm), m.group(1))
            except Exception:
                pass
        print("dongle        : present, held by %s" % who)
    else:
        print("dongle        : present and free on the host")

    if session:
        when = _decode_session(session)
        print("enumerated    : %s  (session %s)"
              % (when.strftime("%Y-%m-%d %H:%M:%S") if when else "unknown time", session))

    if rec_sig and rec_session:
        print("last patch    : session %s -> 0x%04X%04X"
              % (rec_session, rec_sig[0], rec_sig[1]))
    elif word:
        print("last state    : %s" % word)
    else:
        print("last patch    : no record")

    try:
        age = time.time() - os.path.getmtime(hb_path)
        print("agent         : %s" % ("alive, last ran %ds ago" % int(age) if age < 120
                                      else "NO HEARTBEAT - last ran %.1f hours ago"
                                           % (age / 3600.0)))
    except OSError:
        print("agent         : no heartbeat yet - has the LaunchAgent run since install?")

    print("-" * 62)

    if present is None:
        print("VERDICT: UNKNOWN - could not read the USB bus.")
        return 2
    if present is False:
        print("VERDICT: NO - the dongle is not plugged in.")
        return 1
    if not session or not rec_session:
        print("VERDICT: UNKNOWN - no patch recorded for this enumeration.")
        return 2
    if session != rec_session:
        print("VERDICT: NO - the dongle re-enumerated since it was patched")
        print("  (patched session %s, now %s), so the RAM patch is GONE."
              % (rec_session, session))
        if owner:
            print("  The VM holds it, so the host cannot re-patch it. Shut the guest")
            print("  down fully and start it again - detach/reattach will not help,")
            print("  because it does not re-enumerate the device.")
        else:
            print("  It is free on the host; the agent should re-patch within ~10s.")
        return 1
    if not rec_sig:
        print("VERDICT: UNKNOWN - this enumeration is the one on record, but no patch")
        print("  signature was recorded for it, so we cannot claim it is patched.")
        print("  Unplug and replug the dongle to let the agent patch it from scratch.")
        return 2
    if owner:
        print("VERDICT: YES - the guest holds the very enumeration we patched.")
        print("  If Bluetooth still does not work the fault is inside the guest, not")
        print("  the firmware. See \"Patched, and still not working\" in the README.")
        return 0
    print("VERDICT: YES - patched, and still on the host. Attach it to the VM.")
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
    ap.add_argument("--status", action="store_true",
                    help="report whether Bluetooth should work right now (never touches the device)")
    ap.add_argument("--test-reset", action="store_true",
                    help="issue a USB port reset and check whether the patch survives it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.auto:
        return auto_mode(args)
    if args.status:
        return status_mode(args)

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
