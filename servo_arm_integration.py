# servo_arm_integration.py
"""Integration layer connecting the LSC-6 library and thermal management
to the pi5-hailo8-tracking system.

This module wraps the movement functions from ``robot_brain.py`` so that
every arm movement automatically notifies the :class:`ServoThermalMonitor`,
keeping the idle timer accurate and enabling safe auto-parking.

It reuses the serial port already opened by ``robot_brain`` (to avoid
opening the same ``/dev/ttyAMA10`` twice) and exposes drop-in replacements
for the most commonly used movement helpers.

Usage – standalone launch (brain UI + thermal monitor)::

    python servo_arm_integration.py

Usage – as an imported library::

    import servo_arm_integration as arm_sys

    arm_sys.move_servo(6, 1883, time_ms=2000)
    arm_sys.go_home()

    status = arm_sys.thermal_monitor.get_status()
    print(status)
"""

import logging
import threading

import robot_brain as brain
from lsc6_controller import LSC6Controller
from rest_positions import move_to_home
from servo_thermal_monitor import ServoThermalMonitor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared controller and monitor (created once at import time)
# ---------------------------------------------------------------------------

# Reuse the serial port already opened by robot_brain so that both modules
# share the same connection without a conflict.
controller = LSC6Controller(
    ser=brain.ser,
    arm_disabled=brain.ARM_MOVEMENT_DISABLED,
)

thermal_monitor = ServoThermalMonitor(controller)

# ---------------------------------------------------------------------------
# Wrapped movement helpers
# ---------------------------------------------------------------------------

def move_servo(servo_id, pos, time_ms=800):
    """Move a single servo and notify the thermal monitor.

    Drop-in replacement for :func:`robot_brain.move_servo`.
    """
    controller.move_servo(servo_id, pos, time_ms=time_ms)
    thermal_monitor.notify_move()


def move_servos(positions, time_ms=800):
    """Move multiple servos simultaneously and notify the thermal monitor.

    Parameters
    ----------
    positions : dict
        ``{servo_id: target_position}`` mapping.
    time_ms : int
        Move duration in milliseconds.
    """
    controller.move_servos(positions, time_ms=time_ms)
    thermal_monitor.notify_move()


def go_home():
    """Move arm to the calibrated home position and notify the monitor."""
    move_to_home(controller, time_ms=2000)
    thermal_monitor.notify_move()


def get_thermal_status():
    """Return the current thermal monitor status.

    Returns
    -------
    dict
        See :meth:`ServoThermalMonitor.get_status`.
    """
    return thermal_monitor.get_status()


# ---------------------------------------------------------------------------
# Start-up
# ---------------------------------------------------------------------------

def start_integration():
    """Start the thermal monitor and the robot brain GUI.

    Starts :class:`ServoThermalMonitor` in its background thread, then
    calls :func:`robot_brain.start_brain_ui` which blocks until the
    tkinter window is closed.  Cleans up on exit.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("Servo arm integration layer starting")
    thermal_monitor.start()
    try:
        brain.start_brain_ui()
    finally:
        thermal_monitor.stop()
        controller.close()
        logger.info("Servo arm integration layer stopped")


if __name__ == "__main__":
    start_integration()
