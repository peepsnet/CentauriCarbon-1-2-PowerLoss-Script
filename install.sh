#!/bin/sh
# Installs power_loss.py on COSMOS (Centauri Carbon) via an overlay mount
# over Klipper's read-only extras directory. Run on the printer as root.
set -e

LOWER=/usr/share/klipper/klippy/extras
UPPER=/data/klipper-extras-upper
WORK=/data/klipper-extras-work
INIT=/etc/init.d/klipper
SRC="$(dirname "$0")/power_loss.py"

mkdir -p "$UPPER" "$WORK"
cp "$SRC" "$UPPER/power_loss.py"

if ! mount | grep -q klipper-extras-overlay; then
    mount -t overlay klipper-extras-overlay \
        -o lowerdir=$LOWER,upperdir=$UPPER,workdir=$WORK $LOWER
fi
ls "$LOWER/power_loss.py"

# Persist across reboots (a COSMOS update may overwrite this file).
if ! grep -q klipper-extras-overlay "$INIT"; then
    cp "$INIT" "$INIT.bak"
    # Insert the mount block right after the first "start)" line.
    awk -v lower="$LOWER" -v upper="$UPPER" -v work="$WORK" '
        { print }
        /^[[:space:]]*start\)/ && !done {
            print "    if ! mount | grep -q klipper-extras-overlay; then"
            print "        mount -t overlay klipper-extras-overlay \\"
            print "            -o lowerdir=" lower ",upperdir=" upper ",workdir=" work " " lower
            print "    fi"
            done = 1
        }' "$INIT.bak" > "$INIT"
    chmod --reference="$INIT.bak" "$INIT"
    echo "Patched $INIT (backup: $INIT.bak) -- review it before rebooting."
fi

cat <<'EOF'

Now add to printer.cfg:

[power_loss]
sensor_gpio: 192
charge_gpio: 65
save_path: /data/power_loss_state.json
homing_override_z_lift: 5.0

Then: service klipper restart, and run POWER_LOSS_STATUS in the console.
EOF
