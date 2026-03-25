# lsc6_controller.py
"""LSC-6 servo controller serial communication library.

Implements the Hiwonder LSC Series packet protocol for the 6-channel
LSC-6 controller board with LDX-218 bus servos.

Packet structure
----------------
  Header  : 0x55 0x55
  Length  : number of bytes from the length byte to the end of the packet
            (i.e.  2 + len(data))
  Command : 1-byte command identifier
  Data    : variable-length parameters (no trailing checksum for write
            commands in the board-level protocol used here)

Servo position scale
--------------------
Positions are expressed as pulse-width integers in the range 500–2500,
matching the convention already used in robot_brain.py.  Servo 1 (the
claw) has a tighter closing limit (1500–2326) to protect the mechanism.

Usage example::

    ctrl = LSC6Controller("/dev/ttyAMA10", 9600)
    ctrl.move_servo(6, 1883, time_ms=2000)   # move base servo to home
    pos = ctrl.read_position(6)              # read back position
    ctrl.close()
"""

import serial
import threading
import time
import logging
import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

_HEADER = bytes([0x55, 0x55])

CMD_SERVO_MOVE       = 0x03
CMD_SERVO_STOP       = 0x04
CMD_GET_BATTERY_VOLT = 0x0F
CMD_GET_SERVO_POS    = 0x15
CMD_SERVO_TORQUE     = 0x1F

# Safe position limits (pulse-width microseconds, matching robot_brain.py)
SERVO_POS_MIN     = 500
SERVO_POS_MAX     = 2500
SERVO_POS_NEUTRAL = 1500

# Claw (servo ID 1) has a tighter closing limit to protect the mechanism
SERVO_CLAW_ID  = 1
SERVO_CLAW_MIN = 1500
SERVO_CLAW_MAX = 2326

# All servo IDs on the Hiwonder Learm 6DOF arm
ALL_SERVO_IDS = [1, 2, 3, 4, 5, 6]


class LSC6Controller:
    """Serial interface for the Hiwonder LSC-6 servo controller board."""

    def __init__(self, port="/dev/ttyAMA10", baud=9600, timeout=1.0,
                 arm_disabled=False, ser=None):
        self.arm_disabled = arm_disabled
        self._lock = threading.Lock()
        self._cmd_lock = threading.Lock()
        self._commanded = {}
        self._owns_ser = ser is None

        if ser is not None:
            self._ser = ser
            logger.info("LSC6Controller: using shared serial port")
        else:
            try:
                self._ser = serial.Serial(port, baud, timeout=timeout)
                logger.info("LSC6Controller: opened %s @ %d baud", port, baud)
            except serial.SerialException as exc:
                logger.error("LSC6Controller: cannot open serial port – %s", exc)
                self._ser = None

    def _guard_target(self, servo_id, target_pos):
        """Apply configured max-delta guard to a commanded target pulse."""
        sid = int(servo_id)
        guarded = self.clamp(sid, int(target_pos))
        exempt_ids = getattr(config, "SERVO_MOVE_DELTA_GUARD_EXEMPT_IDS", [1])
        try:
            exempt = {int(x) for x in (exempt_ids or [])}
        except Exception:
            exempt = {1}
        if sid in exempt:
            return guarded
        if not bool(getattr(config, "SERVO_MOVE_DELTA_GUARD_ENABLED", True)):
            return guarded

        max_delta = max(10, int(getattr(config, "SERVO_MOVE_MAX_DELTA_US", 90)))
        mode = str(getattr(config, "SERVO_MOVE_DELTA_MODE", "clamp")).strip().lower()

        with self._cmd_lock:
            prev = self._commanded.get(sid, SERVO_POS_NEUTRAL)
        delta = int(guarded) - int(prev)
        if abs(delta) <= max_delta:
            return guarded

        if mode == "reject":
            logger.warning(
                "Servo %d command rejected by delta guard: prev=%d target=%d max=%d",
                int(servo_id), int(prev), int(guarded), int(max_delta)
            )
            return None

        step = max_delta if delta > 0 else -max_delta
        clamped = int(prev) + int(step)
        clamped = self.clamp(int(servo_id), clamped)
        logger.warning(
            "Servo %d command clamped by delta guard: prev=%d target=%d clamped=%d max=%d",
            int(servo_id), int(prev), int(guarded), int(clamped), int(max_delta)
        )
        return clamped

    def _build_packet(self, cmd, data):
        """Return a bytearray for *cmd* with *data* payload."""
        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)
        length = 2 + len(data)
        return bytearray(_HEADER) + bytearray([length, cmd]) + bytearray(data)

    def _write(self, packet):
        """Write *packet* to the serial port under the thread lock."""
        if self._ser is None:
            logger.warning("Serial not open – packet dropped")
            return
        with self._lock:
            self._ser.write(packet)

    def _query(self, packet, expected_len, retries=3, timeout_s=None):
        """Send *packet* and return the raw response bytes, or None."""
        if self._ser is None:
            return None
        with self._lock:
            original_timeout = getattr(self._ser, "timeout", None)
            if timeout_s is not None:
                try:
                    self._ser.timeout = timeout_s
                except Exception:
                    pass
            for attempt in range(retries):
                self._ser.reset_input_buffer()
                self._ser.write(packet)
                time.sleep(0.025)
                response = self._ser.read(expected_len)
                if (len(response) == expected_len
                        and response[:2] == b'\x55\x55'):
                    if timeout_s is not None:
                        try:
                            self._ser.timeout = original_timeout
                        except Exception:
                            pass
                    return response
                logger.debug(
                    "Query attempt %d/%d: %d/%d bytes received",
                    attempt + 1, retries, len(response), expected_len,
                )
            if timeout_s is not None:
                try:
                    self._ser.timeout = original_timeout
                except Exception:
                    pass
        return None

    @staticmethod
    def clamp(servo_id, pos):
        """Return *pos* clamped to the safe range for *servo_id*."""
        if servo_id == SERVO_CLAW_ID:
            return max(SERVO_CLAW_MIN, min(SERVO_CLAW_MAX, pos))
        return max(SERVO_POS_MIN, min(SERVO_POS_MAX, pos))

    def move_servo(self, servo_id, pos, time_ms=800):
        """Move *servo_id* to *pos* (500–2500) over *time_ms* milliseconds."""
        if self.arm_disabled:
            logger.info(
                "arm_disabled: servo %d pos %d suppressed", servo_id, pos
            )
            return
        guarded_pos = self._guard_target(servo_id, pos)
        if guarded_pos is None:
            return
        pos = guarded_pos
        with self._cmd_lock:
            self._commanded[servo_id] = pos
        data = [
            0x01,
            time_ms & 0xFF, (time_ms >> 8) & 0xFF,
            servo_id,
            pos & 0xFF, (pos >> 8) & 0xFF,
        ]
        self._write(self._build_packet(CMD_SERVO_MOVE, data))

    def note_commanded_position(self, servo_id, pos):
        """Record a commanded position without transmitting a move packet."""
        pos = self.clamp(servo_id, pos)
        with self._cmd_lock:
            self._commanded[servo_id] = pos

    def move_servos(self, positions, time_ms=800):
        """Move multiple servos simultaneously."""
        if self.arm_disabled:
            logger.info("arm_disabled: multi-servo move suppressed")
            return
        n = len(positions)
        data = [n, time_ms & 0xFF, (time_ms >> 8) & 0xFF]
        guarded_positions = {}
        for sid, pos in positions.items():
            guarded_pos = self._guard_target(sid, pos)
            if guarded_pos is None:
                continue
            pos = guarded_pos
            with self._cmd_lock:
                self._commanded[sid] = pos
            guarded_positions[sid] = pos
            data += [sid, pos & 0xFF, (pos >> 8) & 0xFF]
        if not guarded_positions:
            return
        data[0] = len(guarded_positions)
        self._write(self._build_packet(CMD_SERVO_MOVE, data))

    def stop_all(self):
        """Send an emergency-stop command to all servos."""
        self._write(self._build_packet(CMD_SERVO_STOP, []))

    def set_torque(self, servo_id, enabled):
        """Enable or disable torque for *servo_id*."""
        self._write(
            self._build_packet(CMD_SERVO_TORQUE,
                               [servo_id, 1 if enabled else 0])
        )

    def read_position(self, servo_id, fast=False):
        """Query the current physical position of *servo_id*."""
        packet = self._build_packet(CMD_GET_SERVO_POS, [0x01, servo_id])
        retries = 1 if fast else 3
        timeout_s = 0.05 if fast else None
        response = self._query(packet, expected_len=8, retries=retries, timeout_s=timeout_s)
        if response is None:
            return None
        pos = response[6] | (response[7] << 8)
        return pos

    def read_positions(self, servo_ids=None):
        """Read positions for multiple servos."""
        if servo_ids is None:
            servo_ids = ALL_SERVO_IDS
        return {sid: self.read_position(sid) for sid in servo_ids}

    def get_deviation(self, servo_id):
        """Return |commanded_pos – actual_pos| for *servo_id*."""
        actual = self.read_position(servo_id, fast=True)
        with self._cmd_lock:
            commanded = self._commanded.get(servo_id)
        if actual is None or commanded is None:
            return None
        return abs(actual - commanded)

    def get_all_deviations(self):
        """Return {servo_id: deviation_or_None} for all servos."""
        return {sid: self.get_deviation(sid) for sid in ALL_SERVO_IDS}

    def get_commanded_positions(self):
        """Return a snapshot of the last commanded pulse for each servo."""
        with self._cmd_lock:
            return dict(self._commanded)

    def close(self):
        """Close the serial port (only if this instance owns it)."""
        if self._owns_ser and self._ser and self._ser.is_open:
            self._ser.close()
            logger.info("LSC6Controller: serial port closed")
