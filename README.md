I wrote power_loss.py (the Klipper module) and install.sh (the COSMOS overlay-mount installer) to do what the gist does. Both pass a syntax check, but I haven't run them on a printer or inside Klipper.

The module follows the same flow as the gist:

Charging: it enables supercap charging (GPIO 65) when Klipper is ready.
Detection: it watches the outage pin (GPIO 192) using GPIO edge events, and falls back to sysfs polling if the kernel rejects that. POWER_LOSS_STATUS tells you which one is active.
On an outage: it saves position, temperatures, fan, speed and file offset, turns the heaters off, then syncs and remounts the filesystems read-only without shutting down.
Next boot: it shows a Resume Precise / Resume Safe / Discard prompt. Resuming reheats the nozzle, homes X/Y, fixes up Z for the [homing_override] lift, purges and wipes at the tray, then returns to the saved position and restarts the print.
Commands: POWER_LOSS_STATUS, POWER_LOSS_RESUME, POWER_LOSS_RESUME_SAFE, POWER_LOSS_DISCARD and POWER_LOSS_TEST_READ.

I made these changes from the gist's version:

Debounce: an outage must last debounce_time (default 50 ms) before it counts, so a brief flicker doesn't trigger a save. In edge mode it re-reads the pin level after the delay. That re-read uses an ioctl I haven't confirmed works on this kernel; if it fails, the module trusts the edge and saves anyway.
Atomic save: the state file is written to a temp file, fsynced and renamed, so a half-written file can't be mistaken for valid state.
Print check: it only saves state if a print is running or paused, though it still protects the filesystems.
GPIO 203 guard: the module refuses to start if GPIO 203 (the supercap output) is configured.
Resume checks: it verifies the gcode file still exists before moving anything.
No shell strings: sync and mount run as argument lists rather than through os.system.
Wipe option: wipe: False skips the tray wipe pattern.
No gpiod tier: I left out the gpiod tier because it isn't installed on COSMOS by default.

To install, copy both files to the printer and run sh install.sh as root. Then add the [power_loss] section to printer.cfg and restart Klipper. The script backs up /etc/init.d/klipper before editing it, but check the result before rebooting, and re-check it after any COSMOS update.

Test with a print you don't care about before relying on it. homing_override_z_lift and the hardcoded wipe coordinates are specific to this printer's macros. Whether motors and heaters keep any power during an outage is also unconfirmed, so the module only moves anything on resume, not at the moment of the outage.
