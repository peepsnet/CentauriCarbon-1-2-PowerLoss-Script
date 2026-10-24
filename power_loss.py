# power_loss.py -- Power Loss Recovery for Elegoo Centauri Carbon (COSMOS)
#
# ORIGINAL AUTHOR: jagonmo
#   https://gist.github.com/jagonmo/b14d6420d6d4af3de0ad6a0cdce5524c
# This module is a derivative of jagonmo's "power_loss" Klipper module.
# The idea, the reverse engineering of Elegoo's firmware, the pin research,
# the module design (instant + layer-checkpoint state, POWER_LOSS_RESUME /
# POWER_LOSS_RESUME_SAFE), the resume sequence (nozzle reheat, real X/Y
# homing, Z correction for [homing_override], tray purge/wipe), the real
# power-cut testing on a Centauri Carbon 1, and the command and option
# names are all jagonmo's work. Thank you.
#
# Changes in this version: printer_model profiles (CC1 / CC2 / custom),
# configurable rail-pin guard, outage debounce, atomic state save, save only
# while printing, configurable tray macro, and an installer.
#
# Upstream credits carried over from the original: Elegoo's official firmware
# and CC2 config (GPL-3.0), KlipperPLR by BigTreeTech (forked from
# Yumi-Lab/Yumi_PLR) for the G-code replay technique, and
# docs.opencentauri.cc.
#
# License: GPL-3.0, as recommended by the original, because the design derives
# from Elegoo's GPL-3.0 sources. Keep these notices if you redistribute.
#
# Klipper "extras" module. Detects a real mains outage through the
# mainboard's outage-detect GPIO, saves print state to disk using the
# supercapacitor hold-up time, protects the filesystems, and offers a
# guided resume on the next boot.
#
# Supported models (printer_model):
#   cc1    Centauri Carbon 1 -- known pins, used as defaults:
#            PG0  -> Linux GPIO 192  outage detect (input)
#            PC1  -> Linux GPIO 65   supercap charge enable (output)
#            PG11 -> Linux GPIO 203  supercap output to rail -- NEVER USED
#   cc2    Centauri Carbon 2 -- pins from Elegoo's printer_dsp.cfg,
#          converted to Linux numbers. The outage sensor has been confirmed
#          by a real unplug test on one CC2; the charge pin has not been
#          independently verified:
#            PG9  -> Linux GPIO 201  outage detect (input)
#            PD20 -> Linux GPIO 116  supercap charge enable (output)
#            PG11 -> Linux GPIO 203  supercap output to rail -- NEVER USED
#   custom Any other board: sensor_gpio and charge_gpio must be set
#          explicitly, and rail_gpio should be set so it is guarded.
#
# printer.cfg (CC1; the pins shown are the CC1 defaults):
#   [power_loss]
#   printer_model: cc1
#   sensor_gpio: 192
#   charge_gpio: 65
#   #rail_gpio: 203             # line that must never be touched
#   poll_interval: 0.01
#   debounce_time: 0.05        # outage must persist this long to count
#   charge_time: 30
#   save_path: /data/power_loss_state.json
#   z_hop: 10.0
#   travel_speed: 3000
#   homing_override_z_lift: 5.0   # must match "G0 Z<N>" in [homing_override]
#   purge_mm: 15.0
#   move_temp: 200.0
#   wipe: True                 # run the tray wipe pattern (CC1 coordinates)
#   tray_macro: MOVE_TO_TRAY   # macro that parks at the purge tray; blank
#                              # skips it
#   #invert_sensor: False
#
# Commands:
#   POWER_LOSS_STATUS [RECHECK=1] | POWER_LOSS_RESUME | POWER_LOSS_RESUME_SAFE
#   POWER_LOSS_DISCARD | POWER_LOSS_TEST_READ N=<n>
#
# Detection tiers: raw GPIO v1 ioctl edge events -> sysfs polling fallback.

import os
import re
import json
import time
import fcntl
import struct
import logging
import subprocess

GPIO_SYSFS = "/sys/class/gpio"
GPIO_CHIP_PATH = "/dev/gpiochip0"

# ---- GPIO character device v1 uAPI (linux/gpio.h), stdlib only ----
_HANDLE_INPUT = 1 << 0
_EVENT_RISING = 1 << 0
_EVENT_FALLING = 1 << 1
_EVENT_BOTH = _EVENT_RISING | _EVENT_FALLING
_EV_ID_RISING = 0x01

_EVREQ_FMT = "<3I32si"                 # lineoffset, handleflags, eventflags, label, fd
_EVREQ_SIZE = struct.calcsize(_EVREQ_FMT)
_EVDATA_SIZE = 16                      # u64 timestamp + u32 id (+ pad)
_HANDLE_DATA_SIZE = 64                 # struct gpiohandle_data { u8 values[64]; }


def _ioc(direction, ioc_type, nr, size):
    return (direction << 30) | (size << 16) | (ioc_type << 8) | nr


_IOC_WRITE, _IOC_READ = 1, 2
_GET_LINEEVENT_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0xB4, 0x04, _EVREQ_SIZE)
_GET_LINE_VALUES_IOCTL = _ioc(_IOC_READ | _IOC_WRITE, 0xB4, 0x08,
                              _HANDLE_DATA_SIZE)


# Per-model defaults. None means "unknown: the user must set it".
MODEL_PROFILES = {
    "cc1": {
        "sensor_gpio": 192,
        "charge_gpio": 65,
        "rail_gpio": 203,
        "tray_macro": "MOVE_TO_TRAY",
        "wipe": True,
    },
    # CC2: taken from Elegoo's printer_dsp.cfg [por] section
    # (sensor_pin=host:PG9, charge_pin=host:PD20, ctrl_pin=host:PG11) and
    # converted with the Allwinner rule 32*(port letter index)+pin, which
    # reproduces the CC1 numbers. Outage sensor confirmed by an unplug test
    # on one CC2; charge pin not independently verified.
    "cc2": {
        "sensor_gpio": 201,    # PG9
        "charge_gpio": 116,    # PD20
        "rail_gpio": 203,      # PG11
        "tray_macro": "",
        "wipe": False,
    },
    "custom": {
        "sensor_gpio": None,
        "charge_gpio": None,
        "rail_gpio": None,
        "tray_macro": "",
        "wipe": False,
    },
}


class SysfsGpio:
    """Minimal sysfs GPIO. Klipper's pin system only covers MCU pins;
    these live on the host SoC."""

    def __init__(self, line):
        self.line = line
        self.path = "%s/gpio%d" % (GPIO_SYSFS, line)
        if not os.path.exists(self.path):
            try:
                with open("%s/export" % GPIO_SYSFS, "w") as f:
                    f.write(str(line))
            except OSError as e:
                if e.errno != 16:  # EBUSY: already exported
                    raise

    def set_direction(self, direction):
        with open("%s/direction" % self.path, "w") as f:
            f.write(direction)

    def read(self):
        with open("%s/value" % self.path, "r") as f:
            return int(f.read().strip())

    def write(self, value):
        with open("%s/value" % self.path, "w") as f:
            f.write("1" if value else "0")


class PowerLoss:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

        self.model = config.getchoice(
            "printer_model", {m: m for m in MODEL_PROFILES}, "cc1")
        profile = MODEL_PROFILES[self.model]

        sensor = config.getint("sensor_gpio", profile["sensor_gpio"],
                               minval=0)
        charge = config.getint("charge_gpio", profile["charge_gpio"],
                               minval=0)
        rail = config.getint("rail_gpio", profile["rail_gpio"], minval=0)
        if sensor is None or charge is None:
            raise config.error(
                "power_loss: printer_model '%s' has no verified default "
                "GPIOs. Set sensor_gpio and charge_gpio explicitly (and "
                "rail_gpio, the supercap-output-to-rail line, so it is "
                "guarded). Wrong pins on a power rail can damage the "
                "board." % self.model)
        if self.model == "cc2":
            logging.warning(
                "power_loss: cc2 GPIOs (sensor=%d charge=%d rail=%d) come "
                "from Elegoo's config. The outage sensor was confirmed by an "
                "unplug test on one CC2; the charge pin is not independently "
                "verified. Check polarity with POWER_LOSS_TEST_READ.",
                sensor, charge, rail)
        if rail is None:
            logging.warning("power_loss: rail_gpio not set; no GPIO is "
                            "guarded against accidental use")
        if sensor == charge:
            raise config.error("power_loss: sensor_gpio and charge_gpio "
                               "must differ")
        # Safety guard: never allow the rail-output pin to be used.
        if rail is not None and rail in (sensor, charge):
            raise config.error(
                "power_loss: GPIO %d is the supercap rail output and "
                "must not be used as the sensor or charge pin" % rail)
        self.sensor_line = sensor
        self.charge_line = charge
        self.rail_line = rail

        self.tray_macro = config.get("tray_macro",
                                     profile["tray_macro"]).strip()
        self.do_wipe = config.getboolean("wipe", profile["wipe"])
        if self.do_wipe and self.model != "cc1":
            raise config.error(
                "power_loss: the built-in wipe pattern uses CC1 "
                "coordinates and is only allowed with printer_model: cc1. "
                "Use tray_macro to run your own sequence instead.")
        self.poll_interval = config.getfloat("poll_interval", 0.01, above=0.)
        self.debounce_time = config.getfloat("debounce_time", 0.05, minval=0.)
        self.charge_time = config.getfloat("charge_time", 30., minval=0.)
        self.invert_sensor = config.getboolean("invert_sensor", False)
        self.z_hop = config.getfloat("z_hop", 10., minval=0.)
        self.travel_speed = config.getfloat("travel_speed", 3000., above=0.)
        self.homing_override_z_lift = config.getfloat(
            "homing_override_z_lift", 5.)
        self.purge_mm = config.getfloat("purge_mm", 15., minval=0.)
        self.move_temp = config.getfloat("move_temp", 200., minval=0.)
        self.save_path = config.get("save_path", "/data/power_loss_state.json")
        self.save_path_ok = None      # result of the startup writability probe
        self.save_path_error = ""

        self.charge_pin = SysfsGpio(self.charge_line)
        self.charge_pin.set_direction("out")
        self.charge_pin.write(0)  # enabled in klippy:ready

        self.charge_done = False
        self.saved_already = False
        self.start_time = None
        self.read_history = []
        self.read_history_max = 200
        self.prompt_attempts_left = 0
        self.last_checkpoint = None
        self.last_seen_layer = None
        self.outage_since = None      # monotonic time outage first seen
        self.debounce_timer = None

        # Edge events must be requested BEFORE any sysfs export of the
        # sensor line: a GPIO line has one exclusive kernel owner.
        self.event_fd = self._setup_ioctl_event()
        if self.event_fd is not None:
            self.detection_method = "ioctl"
            self.sensor_pin = None
            self.reactor.register_fd(self.event_fd, self._handle_gpio_event)
        else:
            self.detection_method = "polling"
            self.sensor_pin = SysfsGpio(self.sensor_line)
            self.sensor_pin.set_direction("in")
        logging.info("power_loss: detection=%s", self.detection_method)

        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._handle_disconnect)

        reg = self.gcode.register_command
        reg("POWER_LOSS_STATUS", self.cmd_STATUS,
            desc="Show power loss monitor status (RECHECK=1 re-tests "
                 "that the state file location is writable)")
        reg("POWER_LOSS_RESUME", self.cmd_RESUME,
            desc="Resume using the precise instant-of-outage position")
        reg("POWER_LOSS_RESUME_SAFE", self.cmd_RESUME_SAFE,
            desc="Resume from the last completed layer")
        reg("POWER_LOSS_DISCARD", self.cmd_DISCARD,
            desc="Discard saved power loss state")
        reg("POWER_LOSS_TEST_READ", self.cmd_TEST_READ,
            desc="Show recent sensor readings")

    # ------------------------------------------------------------------
    # Detection setup
    # ------------------------------------------------------------------
    def _setup_ioctl_event(self):
        try:
            chip_fd = os.open(GPIO_CHIP_PATH, os.O_RDONLY)
        except OSError as e:
            logging.warning("power_loss: can't open %s: %s", GPIO_CHIP_PATH, e)
            return None
        try:
            label = b"power_loss".ljust(32, b"\x00")
            req = struct.pack(_EVREQ_FMT, self.sensor_line, _HANDLE_INPUT,
                              _EVENT_BOTH, label, 0)
            res = fcntl.ioctl(chip_fd, _GET_LINEEVENT_IOCTL, req)
            fd = struct.unpack(_EVREQ_FMT, res)[-1]
            os.set_blocking(fd, False)
            return fd
        except Exception as e:
            logging.warning("power_loss: ioctl edge setup failed (%s); "
                            "falling back to polling", e)
            return None
        finally:
            os.close(chip_fd)

    def _read_level(self):
        """Current logical sensor level (0/1), or None on failure."""
        try:
            if self.sensor_pin is not None:
                return self.sensor_pin.read()
            buf = fcntl.ioctl(self.event_fd, _GET_LINE_VALUES_IOCTL,
                              b"\x00" * _HANDLE_DATA_SIZE)
            return buf[0]
        except Exception as e:
            logging.error("power_loss: sensor read failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # State-file location check
    # ------------------------------------------------------------------
    def _probe_save_path(self):
        """Check that a state file can really be written to save_path:
        create, write 4 KiB, fsync and remove a probe file in its
        directory. Sets save_path_ok / save_path_error; never raises."""
        directory = os.path.dirname(os.path.abspath(self.save_path))
        probe = os.path.join(directory, ".power_loss_probe.%d" % os.getpid())
        ok, err = True, ""
        if os.path.isdir(self.save_path):
            ok, err = False, "%s is a directory, not a file" % self.save_path
        elif not os.path.isdir(directory):
            ok, err = False, "directory %s does not exist" % directory
        else:
            try:
                fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o644)
                try:
                    os.write(fd, b"\0" * 4096)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                ok, err = False, "%s in %s" % (e.strerror or e, directory)
            finally:
                try:
                    os.remove(probe)
                except OSError:
                    pass
        self.save_path_ok, self.save_path_error = ok, err
        return ok

    def _save_path_warning(self):
        return ("power_loss: WARNING: cannot write the state file at %s "
                "(%s). A power outage would NOT be saved. Fix the path or "
                "mount, then run POWER_LOSS_STATUS RECHECK=1."
                % (self.save_path, self.save_path_error))

    def _delayed_save_path_warning(self, eventtime):
        # Console clients often aren't connected at klippy:ready, so repeat
        # the warning to the console a little later.
        if self.save_path_ok is False:
            self.gcode.respond_info(self._save_path_warning())
        return self.reactor.NEVER

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _handle_ready(self):
        if self._probe_save_path():
            logging.info("power_loss: state file location %s is writable",
                         self.save_path)
        else:
            logging.warning(self._save_path_warning())
            self.reactor.register_timer(self._delayed_save_path_warning,
                                        self.reactor.monotonic() + 10.)
        self.charge_pin.write(1)
        self.start_time = self.reactor.monotonic()
        self.timer = self.reactor.register_timer(self._poll, self.reactor.NOW)
        logging.info("power_loss: charging supercap (%.0fs)", self.charge_time)
        if os.path.exists(self.save_path):
            # No reliable "UI connected" signal, so resend the idempotent
            # prompt a few times during the first ~20s.
            self.prompt_attempts_left = 6
            self.reactor.register_timer(self._show_resume_prompt,
                                        self.reactor.monotonic() + 2.)

    def _handle_disconnect(self):
        try:
            self.charge_pin.write(0)
        except Exception:
            pass
        if self.event_fd is not None:
            try:
                self.reactor.unregister_fd(self.event_fd)
                os.close(self.event_fd)
            except Exception:
                pass
            self.event_fd = None

    def _show_resume_prompt(self, eventtime):
        if self.prompt_attempts_left <= 0 or not os.path.exists(self.save_path):
            return self.reactor.NEVER
        self.prompt_attempts_left -= 1
        msgs = [
            "action:prompt_begin Interrupted Print",
            "action:prompt_text A power outage was detected during a print. "
            "Resume precisely, resume from the last completed layer "
            "(safer, repeats part of a layer), or discard?",
            "action:prompt_footer_button Resume Precise|POWER_LOSS_RESUME",
            "action:prompt_footer_button Resume Safe|"
            "POWER_LOSS_RESUME_SAFE|primary",
            "action:prompt_footer_button Discard|POWER_LOSS_DISCARD",
            "action:prompt_show",
        ]
        try:
            for m in msgs:
                self.gcode.run_script_from_command(
                    'RESPOND TYPE=command MSG="%s"' % m)
        except Exception as e:
            logging.error("power_loss: prompt failed: %s", e)
        if self.prompt_attempts_left <= 0:
            return self.reactor.NEVER
        return eventtime + 3.

    # ------------------------------------------------------------------
    # Polling / event handling
    # ------------------------------------------------------------------
    def _record(self, raw):
        self.read_history.append(raw)
        if len(self.read_history) > self.read_history_max:
            self.read_history.pop(0)

    def _is_outage(self, raw):
        return bool(raw) != self.invert_sensor

    def _poll(self, eventtime):
        if (not self.charge_done and self.start_time is not None
                and eventtime - self.start_time > self.charge_time):
            self.charge_done = True
            logging.info("power_loss: supercap charge complete")

        self._check_layer_change()

        if self.sensor_pin is not None and not self.saved_already:
            raw = self._read_level()
            if raw is not None:
                self._record(raw)
                self._evaluate(eventtime, self._is_outage(raw))
        return eventtime + self.poll_interval

    def _handle_gpio_event(self, eventtime):
        try:
            data = os.read(self.event_fd, _EVDATA_SIZE)
            _, ev_id = struct.unpack("<QI", data[:12])
            raw = 1 if ev_id == _EV_ID_RISING else 0
        except BlockingIOError:
            return
        except Exception as e:
            logging.error("power_loss: event read failed: %s", e)
            return
        self._record(raw)
        if self.saved_already:
            return
        if self._is_outage(raw):
            self._evaluate(eventtime, True)
            if self.outage_since is not None and self.debounce_timer is None:
                self.debounce_timer = self.reactor.register_timer(
                    self._debounce_check,
                    self.reactor.monotonic() + self.debounce_time)
        else:
            self.outage_since = None

    def _evaluate(self, eventtime, outage):
        """Debounce for the polling tier: outage must persist."""
        if not outage:
            self.outage_since = None
            return
        if self.outage_since is None:
            self.outage_since = eventtime
        if eventtime - self.outage_since >= self.debounce_time:
            self._on_power_loss()

    def _debounce_check(self, eventtime):
        """Edge tier: re-read the level after debounce_time."""
        self.debounce_timer = None
        if self.saved_already or self.outage_since is None:
            return self.reactor.NEVER
        raw = self._read_level()
        # If we can't read the level, trust the edge (fail towards saving).
        if raw is None or self._is_outage(raw):
            self._on_power_loss()
        else:
            self.outage_since = None
        return self.reactor.NEVER

    # ------------------------------------------------------------------
    # State capture
    # ------------------------------------------------------------------
    def _print_state(self):
        try:
            ps = self.printer.lookup_object("print_stats")
            return ps.get_status(self.reactor.monotonic()).get("state")
        except Exception:
            return None

    def _check_layer_change(self):
        try:
            ps = self.printer.lookup_object("print_stats")
            info = ps.get_status(self.reactor.monotonic()).get("info", {})
            layer = info.get("current_layer")
        except Exception:
            return
        if layer is None or layer == self.last_seen_layer:
            return
        self.last_seen_layer = layer
        snap = self._snapshot_position()
        if snap is not None:
            snap["layer"] = layer
            self.last_checkpoint = snap

    def _snapshot_position(self):
        snap = {}
        now = self.reactor.monotonic()
        try:
            gm = self.printer.lookup_object("gcode_move")
            st = gm.get_status(now)
            snap["gcode_position"] = list(st["gcode_position"])
            snap["speed_factor"] = st.get("speed_factor")
            snap["absolute_extrude"] = st.get("absolute_extrude")
        except Exception:
            try:
                th = self.printer.lookup_object("toolhead")
                snap["gcode_position"] = list(th.get_position())
            except Exception:
                return None
        try:
            vsd = self.printer.lookup_object("virtual_sdcard")
            snap["file_path"] = vsd.get_status(now).get("file_path")
            snap["file_position"] = vsd.file_position
        except Exception:
            return None
        return snap

    def _collect_state(self):
        now = self.reactor.monotonic()
        state = {"timestamp": time.time()}
        try:
            heaters = self.printer.lookup_object("heaters")
            for name in ("extruder", "heater_bed"):
                try:
                    h = heaters.lookup_heater(name)
                    state[name + "_target"] = h.get_temp(now)[1]
                except Exception:
                    pass
        except Exception:
            pass
        try:
            ps = self.printer.lookup_object("print_stats")
            state["print_duration"] = ps.get_status(now).get("print_duration")
        except Exception:
            pass
        try:
            fan = self.printer.lookup_object("fan")
            state["fan_speed"] = fan.get_status(now).get("speed")
        except Exception:
            pass
        state["instant"] = self._snapshot_position()
        state["checkpoint"] = self.last_checkpoint
        return state

    # ------------------------------------------------------------------
    # The outage handler
    # ------------------------------------------------------------------
    def _on_power_loss(self):
        if self.saved_already:
            return
        self.saved_already = True
        logging.warning("power_loss: OUTAGE DETECTED")

        printing = self._print_state() in ("printing", "paused")
        if printing:
            state = self._collect_state()
            tmp = self.save_path + ".tmp"
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                try:
                    os.write(fd, json.dumps(state).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.rename(tmp, self.save_path)  # atomic replace
            except OSError as e:
                logging.error("power_loss: failed to write state: %s", e)
        else:
            logging.info("power_loss: not printing, skipping state save")

        try:
            self.gcode.run_script_from_command("TURN_OFF_HEATERS")
        except Exception as e:
            logging.error("power_loss: heaters off failed: %s", e)

        self._protect_filesystems()

    def _protect_filesystems(self):
        """sync, then remount writable ext*/FAT mounts read-only. No
        shutdown: some PMICs here need a physical replug to boot again."""
        try:
            subprocess.call(["sync"])
        except Exception as e:
            logging.error("power_loss: sync failed: %s", e)
        try:
            with open("/proc/mounts") as f:
                mounts = f.readlines()
        except OSError as e:
            logging.error("power_loss: can't read /proc/mounts: %s", e)
            return
        for line in mounts:
            parts = line.split()
            if len(parts) < 4:
                continue
            mountpoint, fstype, options = parts[1], parts[2], parts[3]
            if fstype not in ("ext2", "ext3", "ext4", "vfat", "fat", "exfat"):
                continue
            if "rw" not in options.split(","):
                continue
            try:
                subprocess.call(["mount", "-o", "remount,ro", mountpoint])
                logging.warning("power_loss: remounted %s read-only",
                                mountpoint)
            except Exception as e:
                logging.error("power_loss: remount %s failed: %s",
                              mountpoint, e)

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------
    def _derive_extrude_state(self, file_path, file_position):
        """Replay the gcode file up to file_position to find the extrude
        mode (M82/M83) and last absolute E. Returns (last_e, absolute)
        or (None, None) if unreadable."""
        absolute = True
        last_e = None
        e_re = re.compile(r"E(-?[0-9.]+)")
        try:
            with open(file_path, "rb") as f:
                read = 0
                for raw in f:
                    read += len(raw)
                    if read > file_position:
                        break
                    line = raw.decode("utf-8", "ignore").strip().upper()
                    if not line or line.startswith(";"):
                        continue
                    if line.startswith("M83"):
                        absolute = False
                    elif line.startswith("M82"):
                        absolute = True
                    elif line.startswith(("G0", "G1", "G92")):
                        m = e_re.search(line)
                        if m and (line.startswith("G92") or absolute):
                            try:
                                last_e = float(m.group(1))
                            except ValueError:
                                pass
        except OSError as e:
            logging.error("power_loss: gcode replay failed: %s", e)
            return None, None
        return last_e, absolute

    def _run(self, script):
        self.gcode.run_script_from_command(script)

    def _do_resume(self, gcmd, package_name):
        if not os.path.exists(self.save_path):
            raise gcmd.error("power_loss: no saved state at %s"
                             % self.save_path)
        with open(self.save_path) as f:
            full = json.load(f)

        pkg = full.get(package_name)
        if package_name == "checkpoint" and not pkg:
            gcmd.respond_info("power_loss: no layer checkpoint (outage before "
                              "first layer) -- using precise capture")
            pkg, package_name = full.get("instant"), "instant"
        if not pkg:
            raise gcmd.error("power_loss: no usable position data")

        missing = [k for k in ("gcode_position", "file_path", "file_position")
                   if k not in pkg]
        if missing:
            raise gcmd.error("power_loss: incomplete '%s' package, missing: %s"
                             % (package_name, ", ".join(missing)))
        if not os.path.exists(pkg["file_path"]):
            raise gcmd.error("power_loss: gcode file not found: %s"
                             % pkg["file_path"])

        gcmd.respond_info("power_loss: resuming from '%s' data" % package_name)
        self.prompt_attempts_left = 0
        self._run('RESPOND TYPE=command MSG="action:prompt_end"')

        x, y, z = pkg["gcode_position"][0:3]

        file_e, file_abs = self._derive_extrude_state(
            pkg["file_path"], pkg["file_position"])
        if file_abs is None:
            absolute = pkg.get("absolute_extrude", True)
            e = (pkg["gcode_position"][3]
                 if len(pkg["gcode_position"]) > 3 else 0.)
        else:
            absolute = file_abs
            e = file_e if file_e is not None else 0.

        ext_t = full.get("extruder_target")
        bed_t = full.get("heater_bed_target")

        # 1. Nozzle to a movement-safe temp (may be stuck to the part).
        gcmd.respond_info("power_loss: heating nozzle before moving...")
        if bed_t:
            self._run("M140 S%.1f" % bed_t)
        if ext_t:
            self._run("M104 S%.1f" % ext_t)
            self._run("TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.1f"
                      % min(self.move_temp, ext_t))

        # 2. Real X/Y homing. Z is trusted from the save, hop for clearance.
        gcmd.respond_info("power_loss: hop + homing X Y...")
        self._run("SET_KINEMATIC_POSITION Z=%.3f" % z)
        self._run("G91")
        self._run("G1 Z%.1f F%.0f" % (self.z_hop, self.travel_speed))
        self._run("G90")
        self._run("G28 X Y")

        # [homing_override] zeroes Z and lifts by a known amount; fix up.
        z_true = z + self.z_hop + self.homing_override_z_lift
        self._run("SET_KINEMATIC_POSITION Z=%.3f" % z_true)
        gcmd.respond_info("power_loss: saved_z=%.3f -> declared Z=%.3f "
                          "(verify physically)" % (z, z_true))

        # 3. Full temps, then purge/wipe at the tray.
        if bed_t:
            self._run("M190 S%.1f" % bed_t)
        if ext_t:
            self._run("M109 S%.1f" % ext_t)
        if self.tray_macro:
            gcmd.respond_info("power_loss: purging at the tray...")
            self._run(self.tray_macro)
            self._run("M83")
            self._run("G1 E%.1f F300" % self.purge_mm)
            self._run("M400")
        else:
            # Never purge in place: the nozzle is over the part/bed.
            gcmd.respond_info("power_loss: no tray_macro set, skipping "
                              "purge and wipe")
        if self.tray_macro and self.do_wipe:
            lines = ["G1 F9000"]
            for _ in range(4):
                for i in range(8):
                    lines.append("G1 X%d Y%d" % (187 - i * 2,
                                                 265 if i % 2 == 0 else 263))
                lines += ["G1 X187", "G1 Y263"]
            lines.append("M400")
            self._run("\n".join(lines))

        # 4. Travel back (elevated), lower, restore extruder/fan/speed.
        gcmd.respond_info("power_loss: moving to X=%.2f Y=%.2f Z=%.3f"
                          % (x, y, z))
        self._run("G1 X%.3f Y%.3f F%.0f" % (x, y, self.travel_speed))
        self._run("G1 Z%.3f F%.0f" % (z, self.travel_speed / 4.))

        if absolute:
            self._run("G92 E%.5f" % e)
            self._run("M82")
        else:
            self._run("G92 E0")
            self._run("M83")

        fan = full.get("fan_speed")
        if fan is not None:
            self._run("M106 S%d" % round(fan * 255))
        sf = pkg.get("speed_factor")
        if sf:
            self._run("M220 S%.1f" % (sf * 100.))

        # 5. Reload file at the saved offset and go.
        self._run("M23 %s" % os.path.basename(pkg["file_path"]))
        self._run("M26 S%d" % pkg["file_position"])
        gcmd.respond_info("power_loss: resuming print")
        self._run("M24")

        try:
            os.remove(self.save_path)
        except OSError as e:
            logging.error("power_loss: couldn't remove state file: %s", e)
        self.saved_already = False
        self.outage_since = None

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def cmd_STATUS(self, gcmd):
        if gcmd.get_int("RECHECK", 0):
            self._probe_save_path()
        gcmd.respond_info(
            "model=%s sensor=GPIO%d charge=GPIO%d rail_guard=%s "
            "charge_done=%s saved=%s pending_resume=%s detection=%s "
            "save_path_writable=%s"
            % (self.model, self.sensor_line, self.charge_line,
               self.rail_line, self.charge_done,
               self.saved_already, os.path.exists(self.save_path),
               self.detection_method, self.save_path_ok))
        if self.save_path_ok is False:
            gcmd.respond_info(self._save_path_warning())

    def cmd_DISCARD(self, gcmd):
        self.prompt_attempts_left = 0
        self._run('RESPOND TYPE=command MSG="action:prompt_end"')
        if os.path.exists(self.save_path):
            os.remove(self.save_path)
            gcmd.respond_info("power_loss: state discarded")
        else:
            gcmd.respond_info("power_loss: no saved state")

    def cmd_RESUME(self, gcmd):
        self._do_resume(gcmd, "instant")

    def cmd_RESUME_SAFE(self, gcmd):
        self._do_resume(gcmd, "checkpoint")

    def cmd_TEST_READ(self, gcmd):
        n = gcmd.get_int("N", 50, minval=1, maxval=self.read_history_max)
        vals = self.read_history[-n:]
        gcmd.respond_info("last %d readings: %s (ones=%d of %d)"
                          % (len(vals), vals, sum(vals), len(vals)))


def load_config(config):
    return PowerLoss(config)
