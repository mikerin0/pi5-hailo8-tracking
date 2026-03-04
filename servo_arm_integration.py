# servo_arm_integration.py
"""Integration layer connecting the LSC-6 library and thermal management
to the pi5-hailo8-tracking system.

Do not run this file directly. Launch with python od.py as before.
"""

import logging
import threading

import robot_brain as brain
from lsc6_controller import LSC6Controller
from rest_positions import move_to_home, move_to_position
from servo_thermal_monitor import ServoThermalMonitor

logger = logging.getLogger(__name__)

# Reuse the serial port already opened by robot_brain so that both modules
# share the same connection without a conflict.
controller = LSC6Controller(
    ser=getattr(brain, 'ser', None),
    arm_disabled=brain.ARM_MOVEMENT_DISABLED,
)

thermal_monitor = ServoThermalMonitor(controller)


def move_servo(servo_id, pos, time_ms=800):
    """Move a single servo and notify the thermal monitor."""
    controller.move_servo(servo_id, pos, time_ms=time_ms)
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
    return thermal_monitor.get_status()
