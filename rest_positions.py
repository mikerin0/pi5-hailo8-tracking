# rest_positions.py
"""Pre-defined low-strain rest positions for the Hiwonder LeArm 6DOF arm.

All position values use the same 500–2500 pulse-width scale as
``robot_brain.py`` and :class:`~lsc6_controller.LSC6Controller`.

Position dictionaries map servo ID → target position::

    {servo_id: position, ...}

Servo layout
------------
  Servo 1 – claw        (1500 = open, 2326 = fully closed)
  Servo 2 – wrist rotation
  Servo 3 – wrist tilt
  Servo 4 – elbow
  Servo 5 – shoulder
  Servo 6 – base rotation

Low-strain design
-----------------
Each position is chosen to minimise the gravitational torque that the
servos must resist.  Folding joints inward shortens the lever arms so
servo motors draw less holding current and run cooler.
"""

from lsc6_controller import LSC6Controller

# ---------------------------------------------------------------------------
# Named rest positions
# ---------------------------------------------------------------------------

# Calibrated home position from field tuning (robot_brain.py ``go_home``).
# Arm is folded in with moderate gravitational load.
HOME = {6: 1883, 5: 700, 4: 655, 3: 720, 2: 1500, 1: 1500}

# Compact fold – arm pulled close to the mounting bracket, base centred.
# Uses the same field-calibrated joint angles as HOME so the arm never
# crashes into the table or mounting surface.
COMPACT_FOLD = {6: 1500, 5: 700, 4: 655, 3: 720, 2: 1500, 1: 1500}

# Upright stow – all joints at neutral (1500).  Arm points straight up,
# gravity acts along the joint axes so holding torque is minimal.
UPRIGHT_STOW = {6: 1500, 5: 1500, 4: 1500, 3: 1500, 2: 1500, 1: 1500}

# Lowered rest – arm laid close to the table surface.
# Useful when the arm must be out of the camera field of view.
LOWERED_REST = {6: 1500, 5: 600, 4: 600, 3: 900, 2: 1500, 1: 1500}

# Registry: name → position dict (preferred → fallback order)
POSITION_REGISTRY = {
    "home":         HOME,
    "compact_fold": COMPACT_FOLD,
    "upright_stow": UPRIGHT_STOW,
    "lowered_rest": LOWERED_REST,
}

# Default rest position used by the thermal monitor
DEFAULT_REST = "compact_fold"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def move_to_position(controller, name, time_ms=2000):
    """Move the arm to a named rest position using *controller*.

    Parameters
    ----------
    controller : LSC6Controller
        Open controller instance used to send the movement command.
    name : str
        Key from :data:`POSITION_REGISTRY` (e.g. ``"home"``,
        ``"compact_fold"``).
    time_ms : int
        Duration of the move in milliseconds.  2 000 ms (2 s) is the
        default to ensure a safe, smooth transition.

    Raises
    ------
    KeyError
        If *name* is not found in :data:`POSITION_REGISTRY`.
    """
    positions = POSITION_REGISTRY[name]
    controller.move_servos(positions, time_ms=time_ms)


def move_to_home(controller, time_ms=2000):
    """Convenience wrapper: move to the :data:`HOME` position."""
    move_to_position(controller, "home", time_ms=time_ms)
