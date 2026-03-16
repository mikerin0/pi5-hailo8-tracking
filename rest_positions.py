# rest_positions.py
"""Pre-defined low-strain rest positions for the Hiwonder LeArm 6DOF arm.

All position values use the same 500–2500 pulse-width scale as
``robot_brain.py`` and :class:`~lsc6_controller.LSC6Controller`.
"""

from lsc6_controller import LSC6Controller
import config

# Named rest positions
# Updated to keep servo 5 closer to upright at rest and move the bend into
# servos 3/4, reducing shoulder strain at power-on.
HOME = {6: 1883, 5: 1500, 4: 1200, 3: 1050, 2: 1500, 1: 1500}
COMPACT_FOLD = {6: 1500, 5: 1500, 4: 1200, 3: 1050, 2: 1500, 1: 1500}
UPRIGHT_STOW = {6: 1500, 5: 1500, 4: 1500, 3: 1500, 2: 1500, 1: 1500}
LOWERED_REST = {6: 1500, 5: 600, 4: 600, 3: 900, 2: 1500, 1: 1500}

# Registry: name → position dict
POSITION_REGISTRY = {
    "home":         HOME,
    "compact_fold": COMPACT_FOLD,
    "upright_stow": UPRIGHT_STOW,
    "lowered_rest": LOWERED_REST,
}

# Default rest position used by the thermal monitor
DEFAULT_REST = "home"


def get_home_position():
    pose = dict(HOME)
    pose[3] = int(getattr(config, "HOME_PULSE_SERVO3", pose[3]))
    pose[4] = int(getattr(config, "HOME_PULSE_SERVO4", pose[4]))
    pose[5] = int(getattr(config, "HOME_PULSE_SERVO5", pose[5]))
    return pose


def move_to_position(controller, name, time_ms=2000):
    """Move the arm to a named rest position using *controller*."""
    if str(name) == "home":
        positions = get_home_position()
    else:
        positions = POSITION_REGISTRY[name]
    controller.move_servos(positions, time_ms=time_ms)


def move_to_home(controller, time_ms=2000):
    """Convenience wrapper: move to the HOME position."""
    move_to_position(controller, "home", time_ms=time_ms)
