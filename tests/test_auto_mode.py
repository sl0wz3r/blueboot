#!/usr/bin/env python3
"""Tests for rtl8761b-fwload.py --auto decision logic.

The USB layer underneath is already proven: the real patch upload succeeded against
the actual dongle. What was untested is the *unattended* decision logic added for the
LaunchAgent - the branch that decides whether to patch, stay silent, or warn - because
reaching its patch branch for real needs a device in stock ROM state, which needs a
physical power cycle.

So we substitute a fake controller and exercise every branch, asserting not just the
return codes but that the bytes actually put on the wire are the correct patch image.
No USB hardware is touched.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "rtl8761b-fwload.py"))
spec = importlib.util.spec_from_file_location("fwload", TOOL)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# The firmware is proprietary and not in this repo; find the user's own copy the
# same way the tool does. Without it these tests cannot run, so say so and skip.
DAT = m.default_dat()
if not os.path.exists(DAT):
    print("SKIP: Realtek ROM patch not found (looked for %s)." % os.path.basename(DAT))
    print("      Set UB500_FW=/path/to/rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat")
    sys.exit(0)

STOCK = dict(hci_ver=0x0A, hci_rev=0x000B, lmp_ver=0x0A, manufacturer=93, lmp_subver=0x8761)
PATCHED = dict(hci_ver=0x09, hci_rev=0xDDD5, lmp_ver=0x09, manufacturer=93, lmp_subver=0xC761)


class FakeDongle:
    """Stands in for a real RTL8761B over USB."""

    def __init__(self, stock=True):
        self.stock = stock
        self.fragments = []
        self.reset_called = False
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True

    def reset(self):
        self.reset_called = True

    def read_rom_version(self):
        return 1                                   # selects the 29124-byte patch

    def read_local_version(self):
        return dict(STOCK if self.stock else PATCHED)

    def cmd(self, opcode, params=b"", expect_status=True):
        assert opcode == m.OP_RTL_DOWNLOAD, "unexpected opcode 0x%04X" % opcode
        self.fragments.append(params)
        if params[0] & 0x80:                       # end-of-transfer marker
            self.stock = False
        return b"\x00" + bytes([params[0]])        # {status, index}


class Args:
    vid, pid = 0x2357, 0x0604
    append_config = False

    def __init__(self, dat):
        self.dat = dat


@contextlib.contextmanager
def scenario(probe, fake):
    """Install fakes and an isolated .dat dir (auto_mode keeps state beside it)."""
    tmp = tempfile.mkdtemp()
    dat = os.path.join(tmp, "patch.dat")
    shutil.copyfile(DAT, dat)
    real_probe, real_dongle = m.probe_bus, m.Dongle
    m.probe_bus = lambda vid, pid: probe
    m.Dongle = lambda vid, pid, **kw: fake
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = m.auto_mode(Args(dat))
        yield rc, out.getvalue(), fake
    finally:
        m.probe_bus, m.Dongle = real_probe, real_dongle
        shutil.rmtree(tmp, ignore_errors=True)


results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print("  %-56s %s%s" % (name, "PASS" if condition else "FAIL",
                            "" if condition else "  <- " + detail))


print("auto_mode branch coverage")
print("=" * 72)

# 1. free + stock -> must patch, with the correct bytes
fake = FakeDongle(stock=True)
with scenario((True, None), fake) as (rc, log, f):
    blob = open(DAT, "rb").read()
    expected, fw_version, _ = m.parse_epatch(blob, 1)
    sent = b"".join(frag[1:] for frag in f.fragments)
    check("free+stock: returns 0", rc == 0, "rc=%r" % rc)
    check("free+stock: logs PATCHED with real version",
          "PATCHED ok -> 0xDDD5C761" in log, log.strip() or "<no output>")
    check("free+stock: 116 fragments sent", len(f.fragments) == 116, len(f.fragments))
    check("free+stock: bytes on the wire == selected patch image",
          sent == expected, "%d vs %d bytes" % (len(sent), len(expected)))
    check("free+stock: final fragment carries end marker 0x80",
          f.fragments[-1][0] & 0x80 == 0x80, hex(f.fragments[-1][0]))
    check("free+stock: no fragment payload exceeds 252 bytes",
          all(len(x) - 1 <= 252 for x in f.fragments))
    check("free+stock: HCI_Reset issued before download", f.reset_called)
    check("free+stock: device closed", f.closed)

# 2. free + already patched -> silent no-op
fake = FakeDongle(stock=False)
with scenario((True, None), fake) as (rc, log, f):
    check("free+patched: returns 0", rc == 0, "rc=%r" % rc)
    check("free+patched: writes nothing", not f.fragments, len(f.fragments))
    check("free+patched: silent", log.strip() == "", repr(log))

# 3. owned by the VM -> must not compete with Parallels
fake = FakeDongle(stock=True)
with scenario((True, "pid 8669, prl_vm_app"), fake) as (rc, log, f):
    check("vm-owned: returns 0", rc == 0, "rc=%r" % rc)
    check("vm-owned: never opens the device", not f.fragments)
    check("vm-owned: silent", log.strip() == "", repr(log))

# 4. unplugged -> silent
fake = FakeDongle(stock=True)
with scenario((False, None), fake) as (rc, log, f):
    check("absent: returns 0", rc == 0, "rc=%r" % rc)
    check("absent: silent", log.strip() == "", repr(log))

# 5. free but unopenable -> the silent-failure case must be LOUD
class Unopenable:
    def open(self):
        raise m.HciError("claim failed")

real_dongle = m.Dongle
real_probe = m.probe_bus
tmp = tempfile.mkdtemp()
dat = os.path.join(tmp, "patch.dat")
shutil.copyfile(DAT, dat)
m.probe_bus = lambda vid, pid: (True, None)
m.Dongle = lambda vid, pid, **kw: Unopenable()
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = m.auto_mode(Args(dat))
m.Dongle, m.probe_bus = real_dongle, real_probe
shutil.rmtree(tmp, ignore_errors=True)
check("blocked: returns non-zero", rc != 0, "rc=%r" % rc)
check("blocked: warns about USB access", "USB access" in out.getvalue(),
      out.getvalue().strip() or "<no output>")

print("=" * 72)
failed = [n for n, ok, _ in results if not ok]
print("%d/%d passed" % (len(results) - len(failed), len(results)))
sys.exit(1 if failed else 0)
