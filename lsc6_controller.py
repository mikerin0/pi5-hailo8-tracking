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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

_HEADER = bytes([0x55, 0x55])

CMD_SERVO_MOVE       = 0x03   # Move n servos: [n, t_L, t_H, id, pos_L, pos_H, ...]
CMD_SERVO_STOP       = 0x04   # Emergency-stop all servos
CMD_GET_BATTERY_VOLT = 0x0F   # Read supply voltage (response: [volt_L, volt_H])
CMD_GET_SERVO_POS    = 0x15   # Query servo position(s): [n, id, ...]
CMD_SERVO_TORQUE     = 0x1F   # Enable/disable torque: [id, on_off (1/0)]

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


# ---------------------------------------------------------------------------
# Controller class
# ---------------------------------------------------------------------------

class LSC6Controller:
    """Serial interface for the Hiwonder LSC-6 servo controller board.

    Parameters
    ----------
    port : str
        Serial device path, e.g. '/dev/ttyAMA10'.  Ignored when *ser* is
        provided.
    baud : int
        Baud rate – 9600 is the LSC-6 factory default.
    timeout : float
        Read timeout in seconds for position-query responses.
    arm_disabled : bool
        When True all movement commands are silently suppressed (safe mode).
    ser : serial.Serial or None
        Pass an already-open :class:`serial.Serial` instance to share an
        existing port (e.g. the one opened by ``robot_brain.py``).  When
        supplied the controller does **not** close the port on
        :meth:`close`.
    """

    def __init__(self, port="/dev/ttyAMA10", baud=9600, timeout=1.0,
                 arm_disabled=False, ser=None):
        self.arm_disabled = arm_disabled
        self._lock = threading.Lock()
        self._cmd_lock = threading.Lock()   # guards _commanded dict
        self._commanded = {}                # {servo_id: last commanded position}
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

    # ------------------------------------------------------------------
    # Low-level packet helpers
    # ------------------------------------------------------------------

    def _build_packet(self, cmd, data):
        """Return a bytearray for *cmd* with *data* payload.

        Length byte = 2 (length byte itself + cmd byte) + len(data).
        """
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

    def _query(self, packet, expected_len, retries=3):
        """Send *packet* and return the raw response bytes, or None.

        Retries up to *retries* times if the response is missing or
        has an unexpected length / header.
        """
        if self._ser is None:
            return None
        with self._lock:
            for attempt in range(retries):
                self._ser.reset_input_buffer()
                self._ser.write(packet)
                time.sleep(0.025)       # give the controller time to respond
                response = self._ser.read(expected_len)
                if (len(response) == expected_len
                        and response[:2] == b'\x55\x55'):
                    return response
                logger.debug(
                    "Query attempt %d/%d: %d/%d bytes received",
                    attempt + 1, retries, len(response), expected_len,
                )
        return None

    # ------------------------------------------------------------------
    # Position clamping
    # ------------------------------------------------------------------

    @staticmethod
    def clamp(servo_id, pos):
        """Return *pos* clamped to the safe range for *servo_id*."""
        if servo_id == SERVO_CLAW_ID:
            return max(SERVO_CLAW_MIN, min(SERVO_CLAW_MAX, pos))
        return max(SERVO_POS_MIN, min(SERVO_POS_MAX, pos))

    # ------------------------------------------------------------------
    # Public API – movement
    # ------------------------------------------------------------------

    def move_servo(self, servo_id, pos, time_ms=800):
        """Move *servo_id* to *pos* (500–2500) over *time_ms* milliseconds."""
        if self.arm_disabled:
            logger.info(
                "arm_disabled: servo %d pos %d suppressed", servo_id, pos
            )
            return
        pos = self.clamp(servo_id, pos)
        with self._cmd_lock:
            self._commanded[servo_id] = pos
        data = [
            0x01,
            time_ms & 0xFF, (time_ms >> 8) & 0xFF,
            servo_id,
            pos & 0xFF, (pos >> 8) & 0xFF,
        ]
        self._write(self._build_packet(CMD_SERVO_MOVE, data))

    def move_servos(self, positions, time_ms=800):
        """Move multiple servos simultaneously.

        Parameters
        ----------
        positions : dict
            ``{servo_id: target_position}`` mapping.
        time_ms : int
            Duration in milliseconds.
        """
        if self.arm_disabled:
            logger.info("arm_disabled: multi-servo move suppressed")
            return
        n = len(positions)
        data = [n, time_ms & 0xFF, (time_ms >> 8) & 0xFF]
        for sid, pos in positions.items():
            pos = self.clamp(sid, pos)
            with self._cmd_lock:
                self._commanded[sid] = pos
            data += [sid, pos & 0xFF, (pos >> 8) & 0xFF]
        self._write(self._build_packet(CMD_SERVO_MOVE, data))

    def stop_all(self):
        """Send an emergency-stop command to all servos."""
        self._write(self._build_packet(CMD_SERVO_STOP, []))

    def set_torque(self, servo_id, enabled):
        """Enable (``True``) or disable (``False``) torque for *servo_id*.

        Disabling torque lets the servo move freely and stops it from
        drawing holding current – the primary cause of overheating.
        """
        self._write(
            self._build_packet(CMD_SERVO_TORQUE,
                               [servo_id, 1 if enabled else 0])
        )

    # ------------------------------------------------------------------
    # Public API – feedback
    # ------------------------------------------------------------------

    def read_position(self, servo_id):
        """Query the current physical position of *servo_id*.

        Returns position (500–2500) or ``None`` if communication failed.

        Request  packet: ``55 55 04 15 01 [id]``
        Response packet: ``55 55 08 15 01 [id] [pos_L] [pos_H]``
        """
        packet = self._build_packet(CMD_GET_SERVO_POS, [0x01, servo_id])
        response = self._query(packet, expected_len=8)
        if response is None:
            return None
        pos = response[6] | (response[7] << 8)
        return pos

    def read_positions(self, servo_ids=None):
        """Read positions for multiple servos (one query per servo).

        Returns ``{servo_id: position_or_None}``.
        """
        if servo_ids is None:
            servo_ids = ALL_SERVO_IDS
        return {sid: self.read_position(sid) for sid in servo_ids}

    def get_deviation(self, servo_id):
        """Return ``|commanded_pos – actual_pos|`` for *servo_id*.

        A large deviation indicates the servo is straining against load.
        Returns ``None`` if the position cannot be read or no command has
        been issued for this servo yet.
        """
        actual = self.read_position(servo_id)
        with self._cmd_lock:
            commanded = self._commanded.get(servo_id)
        if actual is None or commanded is None:
            return None
        return abs(actual - commanded)

    def get_all_deviations(self):
        """Return ``{servo_id: deviation_or_None}`` for all servos."""
        return {sid: self.get_deviation(sid) for sid in ALL_SERVO_IDS}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self):
        """Close the serial port (only if this instance owns it)."""
        if self._owns_ser and self._ser and self._ser.is_open:
            self._ser.close()
            logger.info("LSC6Controller: serial port closed")
