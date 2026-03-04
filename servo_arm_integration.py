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
from rest_positions import move_to_home, move_to_position
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
_SERVO_POWER_LOCK = threading.Lock()
_servo_power_on = None


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
        return bool(data.get("output"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        logger.warning("Shelly set output failed (%s): %s", url, e)
        return None


def _set_servo_power(enabled):
    global _servo_power_on
    with _SERVO_POWER_LOCK:
        for servo_id in ALL_SERVO_IDS:
            try:
                controller.set_torque(servo_id, bool(enabled))
            except Exception as e:
                logger.warning("Set torque failed for servo %s: %s", servo_id, e)

        shelly_state = _shelly_set_output(enabled)
        if shelly_state is None:
            _servo_power_on = bool(enabled) if not _shelly_enabled() else None
        else:
            _servo_power_on = bool(shelly_state)


def power_down_servos():
    _set_servo_power(False)


def power_up_servos():
    _set_servo_power(True)


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


def go_home():
    """Move arm to the calibrated home position and notify the monitor."""
    move_to_home(controller, time_ms=2000)
    thermal_monitor.notify_move()


def relax_arm():
    """Move arm to the compact_fold low-strain rest position."""
    move_to_position(controller, "compact_fold", time_ms=2000)
    thermal_monitor.notify_move()


def get_thermal_status():
    """Return the current thermal monitor status."""
    status = thermal_monitor.get_status()
    status["servo5_deviation"] = controller.get_deviation(5)
    return status


def park_arm():
    """Manually park the arm at the configured thermal rest position."""
    thermal_monitor.park_now()


def resume_arm():
    """Resume thermal monitoring after a manual park."""
    power_up_servos()
    thermal_monitor.resume()
