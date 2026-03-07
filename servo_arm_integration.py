# servo_arm_integration.py
"""Integration layer connecting the LSC-6 library and thermal management
to the pi5-hailo8-tracking system.

Do not run this file directly. Launch with python od.py as before.
"""

import logging
import threading
import time
import json
import urllib.request
import urllib.error

import robot_brain as brain
import config
from lsc6_controller import ALL_SERVO_IDS, LSC6Controller
from rest_positions import HOME, move_to_home, move_to_position
from servo_thermal_monitor import ServoThermalMonitor

logger = logging.getLogger(__name__)

# Reuse the serial port already opened by robot_brain so that both modules
# share the same connection without a conflict.
controller = LSC6Controller(
    ser=getattr(brain, 'ser', None),
    arm_disabled=brain.ARM_MOVEMENT_DISABLED,
)


_THERMAL_PARK_SEQUENCE = (
    (3, 1636),
    (4, 1947),
    (1, 1500),
    (5, 2180),
)
_THERMAL_PARK_STEP_TIME_MS = 900
_TRACKING_READY_POSE = {
    5: 2005,
    4: 1597,
    3: 1694,
}
_TRACKING_READY_TIME_MS = 2200
_SERVO_POWER_LOCK = threading.Lock()
_servo_power_on = None
_STATUS_CACHE_LOCK = threading.Lock()
_status_cache = {
    "parked": False,
    "idle_secs": 0.0,
    "high_load_counts": {},
    "servo5_deviation": None,
    "shelly_apower_w": None,
}
_STATUS_POLL_INTERVAL_S = 1.0
_status_poll_running = False
_status_poll_thread = None
_startup_t0 = time.monotonic()


def _startup_log(message):
    if not bool(getattr(config, "STARTUP_DEBUG_TIMESTAMPS", False)):
        return
    dt = time.monotonic() - _startup_t0
    print(f"[STARTUP +{dt:7.3f}s] {message}")


def _shelly_enabled():
    return bool(getattr(config, "SHELLY_ARM_POWER_ENABLED", False))


def _shelly_base_url():
    host = str(getattr(config, "SHELLY_ARM_POWER_HOST", "")).strip()
    return f"http://{host}" if host else ""


def _shelly_switch_id():
    return int(getattr(config, "SHELLY_ARM_POWER_SWITCH_ID", 0))


def _shelly_timeout():
    return float(getattr(config, "SHELLY_ARM_POWER_TIMEOUT_S", 2.0))


def _shelly_set_output(enabled):
    if not _shelly_enabled():
        return bool(enabled)

    base = _shelly_base_url()
    if not base:
        logger.warning("Shelly power enabled but host is empty")
        return None

    switch_id = _shelly_switch_id()
    on_str = "true" if enabled else "false"
    url = f"{base}/rpc/Switch.Set?id={switch_id}&on={on_str}"
    try:
        with urllib.request.urlopen(url, timeout=_shelly_timeout()) as resp:
            payload = resp.read().decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, dict) and "output" in data:
            return bool(data.get("output"))

        # Some firmware replies to Switch.Set without the current output state.
        # Query status once; if that also fails, trust the requested state.
        status = _shelly_get_status()
        if isinstance(status, dict) and "output" in status:
            return bool(status.get("output"))
        return bool(enabled)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        logger.warning("Shelly set output failed (%s): %s", url, e)
        return None


def _shelly_get_status():
    if not _shelly_enabled():
        return None

    base = _shelly_base_url()
    if not base:
        return None

    switch_id = _shelly_switch_id()
    url = f"{base}/rpc/Switch.GetStatus?id={switch_id}"
    try:
        with urllib.request.urlopen(url, timeout=_shelly_timeout()) as resp:
            payload = resp.read().decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, dict):
            return data
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def _update_status_cache_once():
    status = thermal_monitor.get_status()
    status["servo5_deviation"] = controller.get_deviation(5)
    shelly_status = _shelly_get_status()
    if shelly_status is None:
        status["shelly_apower_w"] = None
    else:
        apower = shelly_status.get("apower", None)
        status["shelly_apower_w"] = float(apower) if apower is not None else None

    with _STATUS_CACHE_LOCK:
        _status_cache.update(status)


def _status_poll_loop():
    while _status_poll_running:
        try:
            _update_status_cache_once()
        except Exception as e:
            logger.debug("Status poller update failed: %s", e)
        time.sleep(_STATUS_POLL_INTERVAL_S)


def start_status_poller():
    global _status_poll_running, _status_poll_thread
    if _status_poll_running:
        return
    _status_poll_running = True
    _status_poll_thread = threading.Thread(
        target=_status_poll_loop,
        name="ArmStatusPoller",
        daemon=True,
    )
    _status_poll_thread.start()


def stop_status_poller():
    global _status_poll_running
    _status_poll_running = False


def _set_servo_power(enabled):
    global _servo_power_on
    with _SERVO_POWER_LOCK:
        step_s = max(0.0, float(getattr(config, "SERVO_TORQUE_STEP_SEC", 0.03)))
        _startup_log(f"_set_servo_power(enabled={bool(enabled)})")

        if bool(enabled):
            # If Shelly controls the arm rail, power it first and give the
            # controller time to boot before sending torque commands.
            _startup_log("power-on: Shelly rail enable begin")
            shelly_state = _shelly_set_output(True)
            _startup_log(f"power-on: Shelly rail state={shelly_state}")
            if _shelly_enabled():
                boot_settle_s = max(0.0, float(getattr(config, "SHELLY_ARM_POWER_BOOT_SETTLE_SEC", 1.2)))
                if boot_settle_s > 0.0:
                    _startup_log(f"power-on: waiting controller boot settle {boot_settle_s:.2f}s")
                    time.sleep(boot_settle_s)

            # Clear any queued/running motion on the controller after rail-up.
            try:
                controller.stop_all()
                _startup_log("power-on: controller stop_all sent")
            except Exception:
                _startup_log("power-on: controller stop_all failed")
                pass

            # Prevent snap-to-old-target by first setting commanded targets to
            # each servo's present physical position before torque enable.
            if bool(getattr(config, "STARTUP_SEED_CURRENT_POSE", True)):
                try:
                    current_positions = {
                        int(sid): controller.read_position(int(sid), fast=True)
                        for sid in ALL_SERVO_IDS
                    }
                    _startup_log(f"power-on: read current positions {current_positions}")
                except Exception:
                    current_positions = {}
                    _startup_log("power-on: read current positions failed")
                seeded = {
                    int(sid): int(pos)
                    for sid, pos in (current_positions or {}).items()
                    if pos is not None
                }
                if seeded:
                    try:
                        seed_time_ms = max(800, int(getattr(config, "STARTUP_SEED_TIME_MS", 1200)))
                        _startup_log(f"power-on: seeding commanded pose {seeded} time_ms={seed_time_ms}")
                        controller.move_servos(seeded, time_ms=seed_time_ms)
                        for sid, pos in seeded.items():
                            try:
                                controller.note_commanded_position(int(sid), int(pos))
                            except Exception:
                                pass
                        time.sleep(0.08)
                    except Exception:
                        _startup_log("power-on: seeding commanded pose failed")
                        pass

            _startup_log(f"power-on: torque enable begin step={step_s:.3f}s")
            for servo_id in ALL_SERVO_IDS:
                try:
                    controller.set_torque(servo_id, True)
                except Exception as e:
                    logger.warning("Set torque failed for servo %s: %s", servo_id, e)
                if step_s > 0.0:
                    time.sleep(step_s)
            _startup_log("power-on: torque enable complete")

            if shelly_state is None:
                _servo_power_on = True if not _shelly_enabled() else None
            else:
                _servo_power_on = bool(shelly_state)
            _startup_log(f"power-on: _servo_power_on={_servo_power_on}")
            return

        _startup_log(f"power-off: torque disable begin step={step_s:.3f}s")
        for servo_id in ALL_SERVO_IDS:
            try:
                controller.set_torque(servo_id, False)
            except Exception as e:
                logger.warning("Set torque failed for servo %s: %s", servo_id, e)
            if step_s > 0.0:
                time.sleep(step_s)

        shelly_state = _shelly_set_output(False)
        if shelly_state is None:
            _servo_power_on = False if not _shelly_enabled() else None
        else:
            _servo_power_on = bool(shelly_state)
        _startup_log(f"power-off: _servo_power_on={_servo_power_on}")


def power_down_servos():
    _set_servo_power(False)


def power_up_servos():
    _set_servo_power(True)


def startup_power_up_quiet():
    """Power/torque up in a startup-safe way to reduce first-motion jerk."""
    _startup_log("startup_power_up_quiet: begin")
    if bool(getattr(config, "STARTUP_CLEAR_MOTION_QUEUE", True)):
        try:
            controller.stop_all()
            _startup_log("startup_power_up_quiet: pre-power stop_all sent")
        except Exception:
            _startup_log("startup_power_up_quiet: pre-power stop_all failed")
            pass
    _set_servo_power(True)
    settle_s = max(0.0, float(getattr(config, "STARTUP_POWER_SETTLE_SEC", 0.8)))
    if settle_s > 0.0:
        _startup_log(f"startup_power_up_quiet: post-power settle {settle_s:.2f}s")
        time.sleep(settle_s)
    _startup_log("startup_power_up_quiet: complete")


def is_servo_power_on():
    with _SERVO_POWER_LOCK:
        return _servo_power_on


def _safe_park_via_sequence():
    try:
        for servo_id, pos in _THERMAL_PARK_SEQUENCE:
            controller.move_servo(servo_id, pos, time_ms=_THERMAL_PARK_STEP_TIME_MS)
            time.sleep((_THERMAL_PARK_STEP_TIME_MS / 1000.0) + 0.05)
        power_down_servos()
    except Exception as e:
        logger.warning(
            "Thermal park sequence failed (%s); using HOME pulses fallback",
            e,
        )
        move_to_home(controller, time_ms=2000)
        power_down_servos()


thermal_monitor = ServoThermalMonitor(
    controller,
    rest_position="home",
    park_callback=_safe_park_via_sequence,
)


def move_servo(servo_id, pos, time_ms=800):
    """Move a single servo and notify the thermal monitor."""
    controller.move_servo(servo_id, pos, time_ms=time_ms)
    thermal_monitor.notify_move()


def note_servo_move(servo_id=None, pos=None):
    """Track externally-issued servo commands for thermal/load estimation."""
    if servo_id is not None and pos is not None:
        try:
            controller.note_commanded_position(int(servo_id), int(pos))
        except Exception:
            pass
    thermal_monitor.notify_move()


def move_servos(positions, time_ms=800):
    """Move multiple servos simultaneously and notify the thermal monitor."""
    controller.move_servos(positions, time_ms=time_ms)
    thermal_monitor.notify_move()


def move_startup_absolute_pose(time_ms=None):
    """Move to configured absolute startup servo pose."""
    if time_ms is None:
        time_ms = int(getattr(config, "STARTUP_ABS_SERVO_TIME_MS", 8000))
    move_time_ms = max(1200, int(time_ms))

    raw_pose = getattr(config, "STARTUP_ABS_SERVO_POSITIONS", {})
    pose = {}
    if isinstance(raw_pose, dict):
        for sid in ALL_SERVO_IDS:
            val = raw_pose.get(sid, raw_pose.get(str(sid)))
            if val is None:
                continue
            try:
                pose[int(sid)] = controller.clamp(int(sid), int(val))
            except Exception:
                continue

    if not pose:
        pose = {6: 1883, 5: 700, 4: 655, 3: 720, 2: 1500, 1: 1500}

    _startup_log(f"startup absolute pose move: pose={pose} time_ms={move_time_ms}")
    controller.move_servos(pose, time_ms=move_time_ms)
    for sid, pos in pose.items():
        try:
            controller.note_commanded_position(int(sid), int(pos))
        except Exception:
            pass
    thermal_monitor.notify_move()


def go_home(time_ms=None):
    """Move arm to the calibrated home position and notify the monitor."""
    if time_ms is None:
        time_ms = int(getattr(config, "HOME_SPEED", 2000))
    move_time_ms = max(1200, int(time_ms))
    _startup_log(f"go_home: begin time_ms={move_time_ms}")
    move_to_home(controller, time_ms=move_time_ms)
    thermal_monitor.notify_move()
    _startup_log("go_home: command sent")


def go_home_staged(time_ms=None, steps=None):
    """Move to HOME in interpolated stages to reduce initial startup twitch."""
    if time_ms is None:
        time_ms = int(getattr(config, "STARTUP_SLOW_HOME_TIME_MS", 5000))
    total_time_ms = max(1800, int(time_ms))

    if steps is None:
        steps = int(getattr(config, "STARTUP_SLOW_HOME_STEPS", 6))
    steps = max(2, int(steps))

    step_pause_s = max(0.0, float(getattr(config, "STARTUP_SLOW_HOME_STEP_PAUSE_SEC", 0.08)))
    max_step_delta = max(20, int(getattr(config, "STARTUP_SLOW_HOME_MAX_STEP_DELTA_US", 90)))
    min_valid = max(3, int(getattr(config, "STARTUP_MIN_VALID_READBACK_SERVOS", 5)))

    try:
        current = controller.read_positions(ALL_SERVO_IDS)
    except Exception:
        current = {}

    seeded_raw = {
        int(sid): int(pos)
        for sid, pos in (current or {}).items()
        if pos is not None
    }
    seeded = {
        int(sid): int(controller.clamp(int(sid), int(pos)))
        for sid, pos in seeded_raw.items()
        if 400 <= int(pos) <= 2600
    }
    if len(seeded) < min_valid:
        _startup_log(
            f"go_home_staged: abort (valid readbacks {len(seeded)} < required {min_valid}); no auto motion"
        )
        return

    max_delta = 0
    for sid, target in HOME.items():
        src = seeded.get(int(sid), None)
        if src is None:
            continue
        max_delta = max(max_delta, abs(int(target) - int(src)))
    required_steps = max(1, int(np.ceil(max_delta / float(max_step_delta)))) if max_delta > 0 else 1
    steps = max(steps, required_steps)

    _startup_log(
        f"go_home_staged: begin total_time_ms={total_time_ms} steps={steps} "
        f"max_delta={max_delta} max_step_delta={max_step_delta}"
    )
    per_step_ms = max(700, int(total_time_ms / max(1, steps)))

    for step_idx in range(1, steps + 1):
        t = step_idx / float(steps)
        pose = {}
        for sid, target in HOME.items():
            src = seeded.get(int(sid), None)
            if src is None:
                continue
            intended = int(round(src + ((int(target) - int(src)) * t)))
            prev = seeded.get(int(sid), intended)
            low = prev - max_step_delta
            high = prev + max_step_delta
            bounded = max(low, min(high, intended))
            clamped = controller.clamp(int(sid), bounded)
            pose[int(sid)] = clamped
        if not pose:
            continue

        _startup_log(f"go_home_staged: step {step_idx}/{steps} pose={pose} time_ms={per_step_ms}")
        controller.move_servos(pose, time_ms=per_step_ms)
        for sid, pos in pose.items():
            try:
                controller.note_commanded_position(int(sid), int(pos))
            except Exception:
                pass
            seeded[int(sid)] = int(pos)
        thermal_monitor.notify_move()
        time.sleep((per_step_ms / 1000.0) + step_pause_s)

    _startup_log("go_home_staged: complete")


def relax_arm():
    """Move arm to the compact_fold low-strain rest position."""
    move_to_position(controller, "compact_fold", time_ms=2000)
    thermal_monitor.notify_move()


def get_thermal_status():
    """Return the current thermal monitor status."""
    with _STATUS_CACHE_LOCK:
        return dict(_status_cache)


def get_last_commanded_pose():
    """Return last commanded pulse snapshot from the servo controller."""
    try:
        return controller.get_commanded_positions()
    except Exception:
        return {}


def park_arm():
    """Manually park the arm at the configured thermal rest position."""
    thermal_monitor.park_now()


def resume_arm():
    """Resume thermal monitoring after a manual park."""
    power_up_servos()
    if bool(getattr(config, "RESUME_HOLD_CURRENT_POSE", True)):
        settle_s = max(0.05, float(getattr(config, "RESUME_SETTLE_SEC", 0.2)))
        if settle_s > 0.0:
            time.sleep(settle_s)
        thermal_monitor.resume()
        return

    if bool(getattr(config, "RESUME_SAFE_SEQUENCE_ENABLED", True)):
        step_time_ms = max(200, int(getattr(config, "RESUME_SAFE_STEP_TIME_MS", 500)))
        step_pause_s = max(0.0, float(getattr(config, "RESUME_SAFE_STEP_PAUSE_SEC", 1.0)))
        sequence = getattr(config, "RESUME_SAFE_SEQUENCE", None)
        if not isinstance(sequence, (list, tuple)) or not sequence:
            sequence = [(1, 1500), (2, 1500), (3, 1497), (4, 1782), (5, 2078), (6, 1183)]

        for item in sequence:
            try:
                sid, pos = int(item[0]), int(item[1])
            except Exception:
                continue
            controller.move_servo(sid, pos, time_ms=step_time_ms)
            try:
                controller.note_commanded_position(sid, pos)
            except Exception:
                pass
            thermal_monitor.notify_move()
            if step_pause_s > 0.0:
                time.sleep(step_pause_s)

        settle_s = max(0.05, float(getattr(config, "RESUME_SETTLE_SEC", 0.2)))
        time.sleep(settle_s)
        thermal_monitor.resume()
        return

    use_home_pose = bool(getattr(config, "RESUME_USE_HOME_POSE", True))
    settle_s = max(0.05, float(getattr(config, "RESUME_SETTLE_SEC", 0.2)))
    if use_home_pose:
        home_time_ms = max(1200, int(getattr(config, "RESUME_HOME_TIME_MS", 3500)))
        move_to_home(controller, time_ms=home_time_ms)
        time.sleep((home_time_ms / 1000.0) + settle_s)
    else:
        controller.move_servos(_TRACKING_READY_POSE, time_ms=_TRACKING_READY_TIME_MS)
        time.sleep((_TRACKING_READY_TIME_MS / 1000.0) + settle_s)
    thermal_monitor.resume()
