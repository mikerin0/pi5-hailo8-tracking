# servo_thermal_monitor.py
"""Real-time servo load monitoring and thermal management.

The LDX-218 servos can overheat when holding the arm against gravity for
extended periods – a condition that has already damaged one servo.  This
module runs a lightweight background thread that:

1. Periodically polls servo positional deviation (commanded vs actual
   position) as a proxy for holding load.
2. Logs a WARNING when a servo has been under high load for several
   consecutive cycles.
3. Automatically moves the arm to a low-strain rest position after the
   arm has been idle for a configurable timeout and at least one servo
   shows a high-load condition.

Usage::

    from lsc6_controller import LSC6Controller
    from servo_thermal_monitor import ServoThermalMonitor

    ctrl = LSC6Controller("/dev/ttyAMA10")
    monitor = ServoThermalMonitor(ctrl)
    monitor.start()

    # ... run tracking / movement code ...
    monitor.notify_move()   # call this on every arm movement

    monitor.stop()
    ctrl.close()
"""

import logging
import threading
import time

from lsc6_controller import ALL_SERVO_IDS, LSC6Controller
from rest_positions import DEFAULT_REST, move_to_position

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuneable defaults  (can be overridden via constructor parameters)
# ---------------------------------------------------------------------------

# Seconds between consecutive load-check cycles
POLL_INTERVAL_S = 2.0

# Absolute positional deviation (pulse-width units) above which a servo
# is considered to be under significant load.
# ~50 units ≈ 5° for a typical LDX-218 servo.
HIGH_LOAD_DEVIATION = 50

# Number of consecutive high-load readings before a WARNING is logged.
HIGH_LOAD_COUNT_WARN = 3

# Seconds of arm inactivity before the thermal manager auto-parks the arm.
IDLE_TIMEOUT_S = 30.0

# Move duration used for the automatic rest transition (milliseconds).
REST_MOVE_TIME_MS = 2500


# ---------------------------------------------------------------------------
# Monitor class
# ---------------------------------------------------------------------------

class ServoThermalMonitor:
    """Monitors servo load and automatically parks the arm when idle.

    Parameters
    ----------
    controller : LSC6Controller
        Open controller instance shared with the rest of the application.
    rest_position : str
        Name of the rest position (key from
        :data:`~rest_positions.POSITION_REGISTRY`) to move to when
        the arm is idle and under load.
    poll_interval : float
        Seconds between load-check cycles.
    idle_timeout : float
        Seconds of inactivity before the arm is auto-parked.
    enabled : bool
        When ``False`` the monitor still logs load warnings but will
        *not* automatically move the arm (monitoring-only mode).
    """

    def __init__(self, controller, rest_position=DEFAULT_REST,
                 poll_interval=POLL_INTERVAL_S, idle_timeout=IDLE_TIMEOUT_S,
                 enabled=True):
        self._ctrl = controller
        self._rest_position = rest_position
        self._poll_interval = poll_interval
        self._idle_timeout = idle_timeout
        self._enabled = enabled
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Per-servo consecutive high-load cycle counters
        self._high_load_counts = {sid: 0 for sid in ALL_SERVO_IDS}

        # Monotonic timestamp of the last externally-triggered arm movement
        self._last_move_time = time.monotonic()

        # True while the arm is sitting at the auto-park rest position
        self._parked = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_move(self):
        """Reset the idle timer.

        Call this whenever a movement command is sent to the arm so that
        the monitor knows the arm is active and should not be auto-parked.
        """
        with self._lock:
            self._last_move_time = time.monotonic()
            self._parked = False

    def start(self):
        """Start the background monitoring thread (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="ServoThermalMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "ServoThermalMonitor: started (rest=%s, idle_timeout=%.0fs, "
            "enabled=%s)",
            self._rest_position, self._idle_timeout, self._enabled,
        )

    def stop(self):
        """Stop the monitoring thread and wait for it to exit."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=self._poll_interval * 2)
        logger.info("ServoThermalMonitor: stopped")

    def get_status(self):
        """Return a snapshot of the current monitoring state.

        Returns
        -------
        dict
            ``'parked'``           – arm is at the auto-park rest position.
            ``'idle_secs'``        – seconds since last movement notification.
            ``'high_load_counts'`` – per-servo consecutive high-load counts.
        """
        with self._lock:
            idle_secs = time.monotonic() - self._last_move_time
            return {
                "parked": self._parked,
                "idle_secs": round(idle_secs, 1),
                "high_load_counts": dict(self._high_load_counts),
            }

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    def _monitor_loop(self):
        while self._running:
            try:
                self._check_load()
                self._check_idle()
            except Exception as exc:
                logger.warning("ServoThermalMonitor loop error: %s", exc)
            time.sleep(self._poll_interval)

    def _check_load(self):
        """Poll deviations and update per-servo high-load counters."""
        for sid in ALL_SERVO_IDS:
            dev = self._ctrl.get_deviation(sid)
            if dev is None:
                # Communication failed or no commanded position yet – skip
                continue

            is_high = dev >= HIGH_LOAD_DEVIATION
            with self._lock:
                if is_high:
                    self._high_load_counts[sid] += 1
                    count = self._high_load_counts[sid]
                else:
                    self._high_load_counts[sid] = 0
                    count = 0

            if is_high and count >= HIGH_LOAD_COUNT_WARN and count % HIGH_LOAD_COUNT_WARN == 0:
                logger.warning(
                    "Servo %d HIGH LOAD: deviation=%d (consecutive_cycles=%d)"
                    " – consider moving arm to a rest position",
                    sid, dev, count,
                )

    def _check_idle(self):
        """Auto-park the arm when idle with high load detected."""
        if not self._enabled:
            return

        with self._lock:
            idle_secs = time.monotonic() - self._last_move_time
            already_parked = self._parked
            any_high_load = any(
                c >= HIGH_LOAD_COUNT_WARN
                for c in self._high_load_counts.values()
            )

        if already_parked:
            return

        if idle_secs >= self._idle_timeout and any_high_load:
            logger.info(
                "ServoThermalMonitor: arm idle %.0fs with high load detected"
                " – moving to '%s' rest position",
                idle_secs, self._rest_position,
            )
            try:
                move_to_position(
                    self._ctrl, self._rest_position,
                    time_ms=REST_MOVE_TIME_MS,
                )
                with self._lock:
                    self._parked = True
                logger.info(
                    "ServoThermalMonitor: arm parked at '%s'",
                    self._rest_position,
                )
            except Exception as exc:
                logger.error(
                    "ServoThermalMonitor: rest move failed – %s", exc
                )
