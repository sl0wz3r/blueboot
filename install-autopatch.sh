#!/bin/bash
# Install (or remove) a LaunchAgent that keeps the UB500's Realtek firmware patch applied.
#
# The patch lives in controller RAM, so it is lost on every unplug, power cycle or Mac
# reboot - and when it is lost, Bluetooth silently stops finding devices again. This agent
# re-applies it automatically whenever the dongle is attached to macOS and running stock ROM.
#
# It deliberately installs a COPY of the tool and firmware into
# ~/Library/Application Support/, rather than running them from ~/Downloads: files under
# ~/Downloads, ~/Desktop and ~/Documents are TCC-protected, and a background LaunchAgent
# reading them can be denied without any visible error.

set -euo pipefail

LABEL="com.ub500.autopatch"
APPSUP="$HOME/Library/Application Support/ub500-autopatch"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/ub500-autopatch.log"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL="${UB500_INTERVAL:-10}"

uninstall() {
  echo "Removing $LABEL…"
  launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  rm -rf "$APPSUP"
  echo "Removed. (Log kept at $LOG)"
  echo "Note: the dongle keeps whatever firmware state it currently has until it is unplugged."
  exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

# --- locate dependencies -----------------------------------------------------
PYBIN="$(command -v python3 || true)"
[[ -x "/opt/homebrew/bin/python3" ]] && PYBIN="/opt/homebrew/bin/python3"
if [[ -z "$PYBIN" ]]; then echo "error: python3 not found" >&2; exit 1; fi

# The Realtek ROM patch is proprietary and is NOT distributed with this tool.
# Point at your own copy, extracted from TP-Link's UB500 driver package.
FW_BASENAME="rtl8761b_mp_chip_bt40_fw_asic_rom_patch_new.dat"
DAT="${1:-${UB500_FW:-}}"
if [[ -z "$DAT" ]]; then
  for c in "$SRC_DIR/$FW_BASENAME" "$PWD/$FW_BASENAME"; do
    [[ -f "$c" ]] && DAT="$c" && break
  done
fi
if [[ -z "$DAT" || ! -f "$DAT" ]]; then
  cat >&2 <<MSG
error: Realtek ROM patch not found.

  This tool does not ship the firmware - it is Realtek/TP-Link proprietary.
  Download TP-Link's UB500 driver package, extract it, and find:
      Driver/Windows_11_64bit/$FW_BASENAME

  Then run:  $0 /path/to/$FW_BASENAME
  or set:    UB500_FW=/path/to/$FW_BASENAME $0
MSG
  exit 1
fi
if [[ ! -f "$SRC_DIR/rtl8761b-fwload.py" ]]; then echo "error: rtl8761b-fwload.py missing" >&2; exit 1; fi

# sanity: the interpreter must actually be able to reach libusb
if ! "$PYBIN" - <<'PY' >/dev/null 2>&1
import ctypes, ctypes.util
for c in (ctypes.util.find_library("usb-1.0"), "/opt/homebrew/lib/libusb-1.0.0.dylib"):
    if c:
        try: ctypes.CDLL(c); raise SystemExit(0)
        except OSError: pass
raise SystemExit(1)
PY
then
  echo "error: $PYBIN cannot load libusb-1.0. Try: brew install libusb" >&2; exit 1
fi

# --- install -----------------------------------------------------------------
echo "Installing $LABEL"
mkdir -p "$APPSUP" "$(dirname "$PLIST")" "$(dirname "$LOG")"
install -m 0755 "$SRC_DIR/rtl8761b-fwload.py" "$APPSUP/rtl8761b-fwload.py"
install -m 0644 "$DAT"                        "$APPSUP/rtl8761b_rom_patch.dat"
echo "  tool     -> $APPSUP/rtl8761b-fwload.py"
echo "  firmware -> $APPSUP/rtl8761b_rom_patch.dat"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYBIN</string>
        <string>$APPSUP/rtl8761b-fwload.py</string>
        <string>--auto</string>
        <string>--dat</string>
        <string>$APPSUP/rtl8761b_rom_patch.dat</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>
</dict>
</plist>
PLISTEOF
plutil -lint "$PLIST" >/dev/null
echo "  agent    -> $PLIST  (every ${INTERVAL}s)"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl enable "gui/$UID/$LABEL"

echo
echo "Installed. Status:"
launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "state|program|runs|last exit" | sed 's/^/  /' || true
echo
echo "Log:       tail -f $LOG"
echo "Remove:    $SRC_DIR/install-autopatch.sh --uninstall"
echo
echo "How it behaves: silent when the dongle is unplugged, already patched, or held by the"
echo "VM. When you plug the dongle in, let it sit on macOS for a few seconds before"
echo "connecting it to Windows - the agent patches it within ${INTERVAL}s and logs a line."
