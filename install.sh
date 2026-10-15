#!/bin/sh
# power_loss installer for the Elegoo Centauri Carbon 1 / 2 (COSMOS).
# power_loss itself is based on the module by jagonmo:
#   https://gist.github.com/jagonmo/b14d6420d6d4af3de0ad6a0cdce5524c
#
# Run on the printer as root, straight from GitHub:
#
#   curl -fsSL https://raw.githubusercontent.com/peepsnet/CentauriCarbon-1-2-PowerLoss-Script/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/peepsnet/CentauriCarbon-1-2-PowerLoss-Script/main/install.sh | sh -s -- --model cc2
#
# or from a clone:   sh install.sh [options]
#
# What it does: downloads power_loss.py, makes Klipper's read-only extras
# directory writable through an overlay mount, optionally persists that
# mount across reboots, and prints (or appends) the printer.cfg section for
# your model. It never restarts Klipper (that would interrupt a print).
#
# Options:
#   --model cc1|cc2|custom   skip detection/prompt
#   --sensor N --charge N --rail N
#                            Linux GPIO numbers (required for --model custom)
#   --printer-cfg PATH       append a [power_loss] section to this file
#                            (backed up first; skipped if one already exists)
#   --yes, -y                accept the detected model without asking
#   --no-persist             don't patch the init script (mount lasts until reboot)
#   --uninstall              remove the module, the init patch and the overlay
#   --no-mount               copy/patch only, no mount or root checks (testing)
#   --help, -h               show this text
#
# Environment:
#   POWER_LOSS_REPO=owner/name   GitHub repo to download from (default: peepsnet/CentauriCarbon-1-2-PowerLoss-Script)
#   POWER_LOSS_REF=main          branch, tag or commit (pin a tag/commit!)
#   POWER_LOSS_SHA256=<hex>      require this checksum for power_loss.py

set -eu

REPO="${POWER_LOSS_REPO:-peepsnet/CentauriCarbon-1-2-PowerLoss-Script}"
REF="${POWER_LOSS_REF:-main}"
RAW_BASE="${POWER_LOSS_RAW_BASE:-https://raw.githubusercontent.com/$REPO/$REF}"
EXPECTED_SHA256="${POWER_LOSS_SHA256:-}"

LOWER="${KLIPPER_EXTRAS_DIR:-/usr/share/klipper/klippy/extras}"
UPPER="${POWER_LOSS_UPPER:-/data/klipper-extras-upper}"
WORK="${POWER_LOSS_WORK:-/data/klipper-extras-work}"
INIT="${KLIPPER_INIT:-/etc/init.d/klipper}"
MARK_BEGIN="# power_loss-overlay-begin (managed by install.sh)"
MARK_END="# power_loss-overlay-end"

MODEL=""
SENSOR=""
CHARGE=""
RAIL=""
PRINTER_CFG=""
ASSUME_YES=0
PERSIST=1
DO_UNINSTALL=0
NO_MOUNT=0

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    if [ -f "$0" ]; then
        sed -n '2,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    else
        say "Usage: install.sh [--model cc1|cc2|custom] [--sensor N --charge N --rail N]"
        say "       [--printer-cfg PATH] [--yes] [--no-persist] [--uninstall] [--help]"
    fi
}

# ---------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
    case "$1" in
        --model)         [ $# -ge 2 ] || die "--model needs a value"; MODEL=$2; shift 2 ;;
        --model=*)       MODEL=${1#--model=}; shift ;;
        --sensor)        [ $# -ge 2 ] || die "--sensor needs a value"; SENSOR=$2; shift 2 ;;
        --charge)        [ $# -ge 2 ] || die "--charge needs a value"; CHARGE=$2; shift 2 ;;
        --rail)          [ $# -ge 2 ] || die "--rail needs a value"; RAIL=$2; shift 2 ;;
        --printer-cfg)   [ $# -ge 2 ] || die "--printer-cfg needs a path"; PRINTER_CFG=$2; shift 2 ;;
        --yes|-y)        ASSUME_YES=1; shift ;;
        --no-persist)    PERSIST=0; shift ;;
        --uninstall)     DO_UNINSTALL=1; shift ;;
        --no-mount)      NO_MOUNT=1; shift ;;
        --help|-h)       usage; exit 0 ;;
        *)               die "unknown option: $1 (see --help)" ;;
    esac
done

MODEL=$(printf %s "$MODEL" | tr 'A-Z' 'a-z')
case "$MODEL" in ''|cc1|cc2|custom) ;; *) die "--model must be cc1, cc2 or custom" ;; esac

# When piped into sh, stdin is the script, so prompts must use the terminal.
TTY=""
if ( : </dev/tty ) 2>/dev/null; then TTY=/dev/tty; fi

ask() {  # ask "prompt" -> REPLY
    [ -n "$TTY" ] || die "need an answer for: $1 (no terminal available; use the command-line options)"
    printf '%s' "$1" >&2
    IFS= read -r REPLY <"$TTY" || REPLY=""
}

is_number() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

overlay_mounted() { grep -q '^klipper-extras-overlay ' /proc/mounts 2>/dev/null; }

# ---------------------------------------------------------------- uninstall
do_uninstall() {
    [ "$NO_MOUNT" -eq 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root"
    say "Removing power_loss..."
    rm -f "$UPPER/power_loss.py"
    if [ -f "$INIT" ] && grep -q 'power_loss-overlay-begin' "$INIT"; then
        cp -p "$INIT" "$INIT.bak-power_loss-uninstall"
        sed '/power_loss-overlay-begin/,/power_loss-overlay-end/d' "$INIT" >"$INIT.new"
        cat "$INIT.new" >"$INIT"
        rm -f "$INIT.new"
        say "Removed the overlay block from $INIT (backup: $INIT.bak-power_loss-uninstall)."
    fi
    if [ -d "$UPPER" ] && [ -z "$(ls -A "$UPPER" 2>/dev/null)" ]; then
        if [ "$NO_MOUNT" -eq 0 ] && overlay_mounted; then
            umount "$LOWER" || warn "could not unmount $LOWER (is Klipper using it? reboot to clear)"
        fi
        rmdir "$UPPER" 2>/dev/null || true
        rm -rf "$WORK" 2>/dev/null || true
    else
        warn "$UPPER still holds other files, so the overlay was left in place."
    fi
    say "Done. Remove the [power_loss] section from printer.cfg yourself, then restart Klipper"
    say "when no print is running. A pending state file (e.g. /data/power_loss_state.json) was left alone."
    exit 0
}

[ "$DO_UNINSTALL" -eq 1 ] && do_uninstall

# ---------------------------------------------------------------- pre-flight
if [ "$NO_MOUNT" -eq 0 ]; then
    [ "$(id -u)" -eq 0 ] || die "run as root"
    [ -d "$LOWER" ] || die "$LOWER not found: is this a COSMOS printer?"
    grep -qw overlay /proc/filesystems 2>/dev/null || die "kernel has no overlayfs support"
    [ -d /sys/class/gpio ] || warn "/sys/class/gpio missing; the module needs sysfs GPIO access"
fi

# ---------------------------------------------------------------- detection
DETECTED=""
DETECT_WHY=""

detect_model() {
    hint=""
    for f in /proc/device-tree/model /sys/firmware/devicetree/base/model \
             /etc/hostname /etc/os-release /etc/issue /etc/cosmos-release; do
        if [ -r "$f" ]; then
            hint="$hint $(tr -d '\000' <"$f" 2>/dev/null | tr 'A-Z' 'a-z' | tr '\n' ' ')"
        fi
    done
    case "$hint" in
        *"carbon 2"*|*carbon-2*|*carbon2*|*cc2*)
            DETECTED=cc2; DETECT_WHY="system identification mentions Centauri Carbon 2" ;;
        *"centauri carbon"*|*centauri-carbon*|*centauricarbon*|*cc1*)
            DETECTED=cc1; DETECT_WHY="system identification mentions Centauri Carbon" ;;
    esac
    if [ -z "$DETECTED" ]; then
        # Weak signal: Elegoo's CC2 config talks to a DSP core via rpmsg.
        for cfg in $(find /data /etc /opt -maxdepth 3 -name 'printer*.cfg' 2>/dev/null | head -n 20); do
            if grep -q 'rpmsg_ctrl-dsp_rproc' "$cfg" 2>/dev/null; then
                DETECTED=cc2; DETECT_WHY="$cfg references a DSP rpmsg MCU (CC2-style config)"
                break
            fi
        done
    fi
}

choose_model() {
    if [ -n "$MODEL" ]; then return; fi
    detect_model
    if [ -n "$DETECTED" ]; then
        say "Detected: $DETECTED ($DETECT_WHY). This is a best guess."
        if [ "$ASSUME_YES" -eq 1 ]; then MODEL=$DETECTED; return; fi
        ask "Use $DETECTED? [Y/n] "
        case "$REPLY" in ''|y|Y|yes|YES) MODEL=$DETECTED; return ;; esac
    else
        say "Could not identify the printer model automatically."
        [ "$ASSUME_YES" -eq 0 ] || die "cannot auto-select a model; pass --model cc1|cc2|custom"
    fi
    say "Which printer is this?"
    say "  1) Centauri Carbon 1"
    say "  2) Centauri Carbon 2 (pins from Elegoo's config, not yet confirmed on hardware)"
    say "  3) Other / custom (you supply the GPIO numbers)"
    while [ -z "$MODEL" ]; do
        ask "Choose 1, 2 or 3: "
        case "$REPLY" in 1) MODEL=cc1 ;; 2) MODEL=cc2 ;; 3) MODEL=custom ;; esac
    done
}

choose_model

if [ "$MODEL" = custom ]; then
    [ -n "$SENSOR" ] || { ask "Outage-detect GPIO number: "; SENSOR=$REPLY; }
    [ -n "$CHARGE" ] || { ask "Supercap charge-enable GPIO number: "; CHARGE=$REPLY; }
    [ -n "$RAIL" ]   || { ask "Supercap output-to-rail GPIO number (guarded, never driven; blank if unknown): "; RAIL=$REPLY; }
    is_number "$SENSOR" || die "sensor GPIO must be a number"
    is_number "$CHARGE" || die "charge GPIO must be a number"
    [ -z "$RAIL" ] || is_number "$RAIL" || die "rail GPIO must be a number"
    [ "$SENSOR" != "$CHARGE" ] || die "sensor and charge GPIO must differ"
    [ -z "$RAIL" ] || { [ "$RAIL" != "$SENSOR" ] && [ "$RAIL" != "$CHARGE" ]; } \
        || die "the rail GPIO must not be the sensor or charge pin"
fi

say "Installing for: $MODEL"
if [ "$MODEL" = cc2 ]; then
    warn "CC2 pin defaults are derived from Elegoo's config and NOT yet confirmed on hardware."
    warn "Read the README's CC2 section and verify sensor polarity before relying on this."
fi

# ---------------------------------------------------------------- get module
TMPD=$(mktemp -d 2>/dev/null || { mkdir -p "/tmp/power_loss.$$" && echo "/tmp/power_loss.$$"; })
trap 'rm -rf "$TMPD"' EXIT INT TERM
MODULE="$TMPD/power_loss.py"

fetch() {  # fetch URL DEST
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        die "neither curl nor wget is available"
    fi
}

if [ -f "$0" ] && [ -f "$(dirname -- "$0")/power_loss.py" ]; then
    say "Using local $(dirname -- "$0")/power_loss.py"
    cp "$(dirname -- "$0")/power_loss.py" "$MODULE"
else
    say "Downloading $RAW_BASE/power_loss.py"
    fetch "$RAW_BASE/power_loss.py" "$MODULE" || die "download failed (check network, repo and ref)"
fi

[ -s "$MODULE" ] || die "power_loss.py is empty"
grep -q 'def load_config' "$MODULE" || die "downloaded file does not look like the power_loss module"
if [ -n "$EXPECTED_SHA256" ]; then
    command -v sha256sum >/dev/null 2>&1 || die "POWER_LOSS_SHA256 set but sha256sum is missing"
    actual=$(sha256sum "$MODULE" | cut -d' ' -f1)
    [ "$actual" = "$EXPECTED_SHA256" ] || die "checksum mismatch (got $actual)"
fi
if command -v python3 >/dev/null 2>&1; then
    python3 -m py_compile "$MODULE" 2>/dev/null || die "power_loss.py failed a Python syntax check"
    rm -rf "$TMPD/__pycache__"
fi

# ---------------------------------------------------------------- install
mkdir -p "$UPPER" "$WORK"
cp "$MODULE" "$UPPER/power_loss.py"
say "Copied module to $UPPER/power_loss.py"

if [ "$NO_MOUNT" -eq 0 ]; then
    if ! overlay_mounted; then
        mount -t overlay klipper-extras-overlay \
            -o "lowerdir=$LOWER,upperdir=$UPPER,workdir=$WORK" "$LOWER" \
            || die "overlay mount failed"
        say "Mounted overlay over $LOWER"
    else
        say "Overlay already mounted"
    fi
    [ -f "$LOWER/power_loss.py" ] || die "module not visible in $LOWER after mounting"
fi

# ---------------------------------------------------------------- persistence
patch_init() {
    if [ ! -f "$INIT" ]; then
        warn "$INIT not found; add the overlay mount to your boot sequence yourself."
        return
    fi
    if grep -q 'power_loss-overlay-begin' "$INIT"; then
        say "$INIT already patched"
        return
    fi
    if grep -q 'klipper-extras-overlay' "$INIT"; then
        warn "$INIT already mounts klipper-extras-overlay (from an earlier install?). Left unchanged; check it by hand."
        return
    fi
    BLOCK="$TMPD/block"
    {
        echo "    $MARK_BEGIN"
        echo "    if ! grep -q '^klipper-extras-overlay ' /proc/mounts; then"
        echo "        mount -t overlay klipper-extras-overlay \\"
        echo "            -o lowerdir=$LOWER,upperdir=$UPPER,workdir=$WORK $LOWER"
        echo "    fi"
        echo "    $MARK_END"
    } >"$BLOCK"
    cp -p "$INIT" "$INIT.bak-power_loss"
    awk -v blk="$BLOCK" '
        { print }
        /^[ \t]*start\)/ && !done {
            while ((getline line < blk) > 0) print line
            close(blk); done = 1
        }' "$INIT.bak-power_loss" >"$TMPD/init.new"
    if grep -q 'power_loss-overlay-begin' "$TMPD/init.new"; then
        cat "$TMPD/init.new" >"$INIT"
        say "Patched $INIT (backup: $INIT.bak-power_loss). Review it before rebooting."
        say "A COSMOS update may overwrite it; re-run this installer afterwards."
    else
        rm -f "$INIT.bak-power_loss"
        warn "no 'start)' block found in $INIT, so it was not patched."
        warn "Add this inside your start sequence yourself:"
        cat "$BLOCK" >&2
    fi
}

if [ "$PERSIST" -eq 1 ]; then patch_init; else say "Skipped init patch (--no-persist): the mount will not survive a reboot."; fi

# ---------------------------------------------------------------- printer.cfg
make_snippet() {
    echo "[power_loss]"
    echo "printer_model: $MODEL"
    if [ "$MODEL" = custom ]; then
        echo "sensor_gpio: $SENSOR"
        echo "charge_gpio: $CHARGE"
        [ -z "$RAIL" ] || echo "rail_gpio: $RAIL"
    fi
    echo "save_path: /data/power_loss_state.json"
    echo "# Must match the Z lift in your [homing_override]:"
    echo "homing_override_z_lift: 5.0"
    if [ "$MODEL" = cc2 ]; then
        echo "# CC2: check polarity with POWER_LOSS_TEST_READ N=100 (mains on)."
        echo "# If every reading is 1, uncomment:"
        echo "#invert_sensor: True"
    fi
}

SNIPPET="$TMPD/snippet.cfg"
make_snippet >"$SNIPPET"

if [ -n "$PRINTER_CFG" ]; then
    [ -f "$PRINTER_CFG" ] || die "$PRINTER_CFG not found"
    if grep -q '^\[power_loss\]' "$PRINTER_CFG"; then
        warn "$PRINTER_CFG already has a [power_loss] section; not touching it."
    else
        cp -p "$PRINTER_CFG" "$PRINTER_CFG.bak-power_loss"
        { echo; cat "$SNIPPET"; } >>"$PRINTER_CFG"
        say "Appended [power_loss] to $PRINTER_CFG (backup: $PRINTER_CFG.bak-power_loss)."
    fi
else
    say ""
    say "Add this to printer.cfg:"
    say ""
    cat "$SNIPPET"
fi

say ""
say "Next steps:"
say "  1. Restart Klipper when NO print is running (the installer never does this for you)."
say "  2. Run POWER_LOSS_STATUS in the console; after ~30 s it should show charge_done=True."
if [ "$MODEL" = cc2 ]; then
    say "  3. CC2: verify the sensor with POWER_LOSS_TEST_READ N=100 before trusting a save."
fi
say "  To remove everything later: sh install.sh --uninstall"
