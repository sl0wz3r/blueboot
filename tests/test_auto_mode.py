#!/usr/bin/env python3
"""Tests for blueboot.py --auto decision logic.

The USB layer underneath is already proven: the real patch upload succeeded against
the actual dongle. What was untested is the *unattended* decision logic added for the
LaunchAgent - the branch that decides whether to patch, stay silent, or warn - because
reaching its patch branch for real needs a device in stock ROM state, which needs a
physical power cycle.

So we substitute a fake controller and exercise every branch, asserting not just the
return codes but that the bytes actually put on the wire are the correct patch image,
and that the state file records what the next poll will need to read. No USB hardware
is touched.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "blueboot.py"))
spec = importlib.util.spec_from_file_location("fwload", TOOL)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# The firmware is proprietary and not in this repo; find the user's own copy the
# same way the tool does. Without it these tests cannot run.
#
# Exit 77, not 0. A skip that reports success is how a suite comes to advertise
# coverage it is not providing - the same class of lie as a cached device property.
DAT = m.default_dat()
if not os.path.exists(DAT):
    print("SKIP: Realtek ROM patch not found (looked for %s)." % os.path.basename(DAT))
    print("      Set UB500_FW=/path/to/rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat")
    sys.exit(77)

STOCK = dict(hci_ver=0x0A, hci_rev=0x000B, lmp_ver=0x0A, manufacturer=93, lmp_subver=0x8761)
PATCHED = dict(hci_ver=0x09, hci_rev=0xDDD5, lmp_ver=0x09, manufacturer=93, lmp_subver=0xC761)
ODD = dict(hci_ver=0x09, hci_rev=0xAAAA, lmp_ver=0x09, manufacturer=93, lmp_subver=0xBBBB)


class FakeDongle:
    """Stands in for a real RTL8761B over USB."""

    def __init__(self, stock=True, version=None):
        self.stock = stock
        self.version = version          # force an arbitrary controller signature
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
        if self.version is not None:
            return dict(self.version)
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
def scenario(probe, fake, seed_state=None, remove_dat=False):
    """Install fakes and an isolated .dat dir (auto_mode keeps state beside it).

    seed_state pre-writes .autopatch-state so a test can express what the PREVIOUS
    poll saw - which is the only way to exercise the session-change edge detection.
    """
    tmp = tempfile.mkdtemp()
    dat = os.path.join(tmp, "patch.dat")
    shutil.copyfile(DAT, dat)
    if remove_dat:
        os.remove(dat)
    state_file = os.path.join(tmp, ".autopatch-state")
    if seed_state is not None:
        with open(state_file, "w") as fh:
            fh.write(seed_state)
    real_probe, real_dongle = m.probe_bus, m.Dongle
    m.probe_bus = lambda vid, pid: probe
    m.Dongle = lambda vid, pid, **kw: fake
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rc = m.auto_mode(Args(dat))
        state = ""
        try:
            with open(state_file) as fh:
                state = fh.read().strip()
        except OSError:
            pass
        yield rc, out.getvalue(), fake, state
    finally:
        m.probe_bus, m.Dongle = real_probe, real_dongle
        shutil.rmtree(tmp, ignore_errors=True)


results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print("  %-62s %s%s" % (name, "PASS" if condition else "FAIL",
                            "" if condition else "  <- " + detail))


print("auto_mode branch coverage")
print("=" * 78)

# 1. free + stock -> must patch, with the correct bytes
fake = FakeDongle(stock=True)
with scenario((True, None, "S1"), fake) as (rc, log, f, state):
    blob = open(DAT, "rb").read()
    expected, fw_version, _ = m.parse_epatch(blob, 1)
    sent = b"".join(frag[1:] for frag in f.fragments)
    check("free+stock: returns 0", rc == 0, "rc=%r" % rc)
    check("free+stock: logs PATCHED with real version",
          "PATCHED ok -> 0xDDD5C761" in log, log.strip() or "<no output>")
    check("free+stock: fragment count matches image size",
          len(f.fragments) == len(expected) // m.RTL_FRAG_LEN + 1, len(f.fragments))
    check("free+stock: bytes on the wire == selected patch image",
          sent == expected, "%d vs %d bytes" % (len(sent), len(expected)))
    check("free+stock: final fragment carries end marker 0x80",
          f.fragments[-1][0] & 0x80 == 0x80, hex(f.fragments[-1][0]))
    check("free+stock: no fragment payload exceeds 252 bytes",
          all(len(x) - 1 <= 252 for x in f.fragments))
    check("free+stock: HCI_Reset issued before download", f.reset_called)
    check("free+stock: device closed", f.closed)
    check("free+stock: state records session AND signature",
          state == "patched:S1:DDD5:C761", repr(state))

# 2. free + already patched -> silent no-op
fake = FakeDongle(stock=False)
with scenario((True, None, "S1"), fake) as (rc, log, f, state):
    check("free+patched: returns 0", rc == 0, "rc=%r" % rc)
    check("free+patched: writes nothing", not f.fragments, len(f.fragments))
    check("free+patched: silent", log.strip() == "", repr(log))
    check("free+patched: state records session", state.startswith("patched:S1"), repr(state))

# 3. owned by the VM -> must not compete with Parallels
fake = FakeDongle(stock=True)
with scenario((True, "pid 8669, prl_vm_app", "S1"), fake) as (rc, log, f, state):
    check("vm-owned: returns 0", rc == 0, "rc=%r" % rc)
    check("vm-owned: never opens the device", not f.fragments)
    check("vm-owned: silent", log.strip() == "", repr(log))
    check("vm-owned: state records the held session", state == "owned:S1", repr(state))

# 4. unplugged -> silent
fake = FakeDongle(stock=True)
with scenario((False, None, None), fake) as (rc, log, f, state):
    check("absent: returns 0", rc == 0, "rc=%r" % rc)
    check("absent: silent", log.strip() == "", repr(log))

# 5. free but unopenable -> the silent-failure case must be LOUD, and must report
#    the REAL libusb reason rather than guessing at a denied macOS prompt.
class Unopenable:
    def open(self):
        raise m.HciError("claim_interface(0) failed: LIBUSB_ERROR_ACCESS")

with scenario((True, None, "S1"), Unopenable()) as (rc, log, f, state):
    check("blocked: returns non-zero", rc != 0, "rc=%r" % rc)
    check("blocked: reports the real libusb reason, not a guess",
          "LIBUSB_ERROR_ACCESS" in log, log.strip() or "<no output>")
    check("blocked: does NOT assert a denied USB prompt it never observed",
          "probably not granted" not in log, log.strip())
    check("blocked: busy and access get distinct state words",
          state.startswith("blocked-busy"), repr(state))

# ---------------------------------------------------------------------------
# Regressions for the failure of 2026-11-18. Every one of these passed silently
# before: the agent did the right thing and said nothing a human could act on.
# ---------------------------------------------------------------------------

# 6. ioreg said nothing -> must NOT be treated as "free", must NOT open the device
class MustNotOpen:
    opened = False

    def open(self):
        MustNotOpen.opened = True
        raise m.HciError("should never be reached")

with scenario((None, None, None), MustNotOpen()) as (rc, log, f, state):
    check("probe-failed: returns 0", rc == 0, "rc=%r" % rc)
    check("probe-failed: never attempts to open the device", not MustNotOpen.opened)
    check("probe-failed: says ioreg did not answer", "ioreg did not answer" in log,
          log.strip() or "<no output>")
    check("probe-failed: distinct state word", state == "probe-failed", repr(state))

# 7. THE ONE THAT COST A MORNING: the dongle re-enumerated while the VM held it.
#    The patch is gone, the host cannot reach the device to redo it, and the guest
#    is looking at stock ROM while every surface in Windows reports a healthy
#    adapter from cache.
fake = FakeDongle(stock=True)
with scenario((True, "pid 5968, prl_vm_app", "S2"), fake,
              seed_state="patched:S1:DDD5:C761") as (rc, log, f, state):
    check("re-enumerated: returns 0", rc == 0, "rc=%r" % rc)
    check("re-enumerated: never opens the device (the VM has it)", not f.fragments)
    check("re-enumerated: WARNS that the guest is unpatched",
          "UNPATCHED controller" in log, log.strip() or "<no output>")
    check("re-enumerated: names both sessions", "S1" in log and "S2" in log, log.strip())
    check("re-enumerated: says restart the VM, not detach/reattach",
          "Shut the VM down fully" in log, log.strip())
    check("re-enumerated: state advances to the new session", state == "owned:S2", repr(state))

# 8. Same session still held -> healthy handoff, must stay silent
fake = FakeDongle(stock=True)
with scenario((True, "pid 5968, prl_vm_app", "S1"), fake,
              seed_state="patched:S1:DDD5:C761") as (rc, log, f, state):
    check("same-session: returns 0", rc == 0, "rc=%r" % rc)
    check("same-session: silent (this is the normal, healthy case)",
          log.strip() == "", repr(log))
    check("same-session: carries the patch signature across the handoff",
          state == "owned:S1:DDD5:C761", repr(state))

# 9. Controller in a third state -> must not be recorded as success
fake = FakeDongle(version=ODD)
with scenario((True, None, "S1"), fake,
              seed_state="patched:S1:DDD5:C761") as (rc, log, f, state):
    check("anomalous: returns non-zero", rc != 0, "rc=%r" % rc)
    check("anomalous: says firmware state is unknown",
          "firmware state unknown" in log, log.strip() or "<no output>")
    check("anomalous: distinct state word", state.startswith("anomalous"), repr(state))

# 10. Missing firmware must route through transition(), not print on every poll
fake = FakeDongle(stock=True)
with scenario((True, None, "S1"), fake, remove_dat=True) as (rc, log, f, state):
    check("fw-missing: returns non-zero", rc != 0, "rc=%r" % rc)
    check("fw-missing: logs once with a timestamp",
          "firmware file missing" in log, log.strip() or "<no output>")
    check("fw-missing: distinct state word so the next poll stays quiet",
          state.startswith("fw-missing"), repr(state))

# 11. A corrupt state file must never be able to kill a job that runs every 10s
fake = FakeDongle(stock=False)
with scenario((True, None, "S1"), fake, seed_state="\x00\xff not valid utf8 \xfe") as (
        rc, log, f, state):
    check("corrupt-state: survives and returns 0", rc == 0, "rc=%r" % rc)
    check("corrupt-state: recovers by rewriting clean state",
          state.startswith("patched:S1"), repr(state))

print("=" * 78)
failed = [n for n, ok, _ in results if not ok]
print("%d/%d passed" % (len(results) - len(failed), len(results)))
sys.exit(1 if failed else 0)
