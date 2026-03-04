import json, os, serial, time, subprocess, tkinter as tk, threading, socket
import numpy as np 
import ikpy.chain
import ikpy.link
import config

# --- SAFETY FLAG ---
# Set to True to disable ALL servo movement (prevents hardware damage).
# Set to False once the Hailo pipeline is confirmed working and arm limits are safe.
ARM_MOVEMENT_DISABLED = False

# --- 1. SETTINGS & HARDWARE ---
# Physical lengths of your 6DOF arm (L4 includes your 19cm claw)
L1, L2, L3, L4 = 0.05, 0.055, 0.091, 0.170
PORT, BAUD = "/dev/ttyAMA10", 9600
CRESTRON_PORT = 50005
last_angles = [0, 0, 0.2, 0.5, 0.1]

# --- Camera switch callbacks (populated by external modules at runtime) ---
# Keys: "HIGH_CAM", "TABLE_CAM", "DUAL_CAM"  →  callable with no arguments
camera_switch_handlers = {}

# Global for the persistent Crestron connection
crestron_conn = None
_take_item_lock = threading.Lock()
shutdown_event = threading.Event()
_gripper_motion_lock = threading.Lock()
_gripper_pos_est = 1500
_gripper_switch_ready = False
_gpio_mod = None
_holding_item = False
_holding_item_lock = threading.Lock()
_release_block_until = 0.0
_release_block_lock = threading.Lock()
servo_move_callback = None
thermal_status_provider = None
thermal_park_callback = None
thermal_resume_callback = None
servo_power_provider = None
TUNER_PARAMS_PATH = os.path.join(os.path.dirname(__file__), "tuner_params.json")

try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
except:
    print("Serial Error: Check TX/RX cables. Servos will not move.")


def _init_gripper_switch():
    global _gripper_switch_ready, _gpio_mod
    pin = getattr(config, "GRIPPER_SWITCH_PIN_BCM", None)
    if pin is None:
        print("Gripper microswitch: disabled (GRIPPER_SWITCH_PIN_BCM=None)")
        return
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        pud = GPIO.PUD_UP if getattr(config, "GRIPPER_SWITCH_PULL_UP", True) else GPIO.PUD_DOWN
        GPIO.setup(pin, GPIO.IN, pull_up_down=pud)
        _gpio_mod = GPIO
        _gripper_switch_ready = True
        print(f"Gripper microswitch: enabled on BCM {pin}")
    except Exception as e:
        print(f"Gripper microswitch disabled (GPIO init failed): {e}")


def _gripper_switch_pressed():
    if not _gripper_switch_ready:
        return False
    try:
        pin = getattr(config, "GRIPPER_SWITCH_PIN_BCM", None)
        pressed_state = int(getattr(config, "GRIPPER_SWITCH_PRESSED_STATE", 0))
        return int(_gpio_mod.input(pin)) == pressed_state
    except Exception:
        return False


def _send_servo_packet(id, pos, time_ms=800):
    packet = bytearray([0x55, 0x55, 0x08, 0x03, 0x01, time_ms & 0xFF, (time_ms >> 8) & 0xFF, id, pos & 0xFF, (pos >> 8) & 0xFF])
    ser.write(packet)


def _notify_servo_move(servo_id=None, pos=None):
    callback = servo_move_callback
    if callback is None:
        return
    try:
        callback(servo_id, pos)
    except TypeError:
        try:
            callback()
        except Exception:
            pass
    except Exception:
        pass


def set_holding_item(value):
    global _holding_item
    with _holding_item_lock:
        _holding_item = bool(value)


def is_holding_item():
    with _holding_item_lock:
        return _holding_item


def release_item_to_user(reason=""):
    if not is_holding_item():
        return False
    move_servo(1, 1500, 700)
    set_holding_item(False)
    send_to_crestron("ITEM_RELEASED")
    if reason:
        print(f"Item released ({reason})")
    else:
        print("Item released")
    return True


def block_auto_release(seconds):
    global _release_block_until
    lockout = max(0.0, float(seconds))
    with _release_block_lock:
        _release_block_until = max(_release_block_until, time.time() + lockout)


def can_auto_release_now():
    with _release_block_lock:
        return time.time() >= _release_block_until

# --- 2. THE CLEAN IK CHAIN ---
# Handles top-mount inversion natively via rotation=[0, -1, 0]
my_arm = ikpy.chain.Chain(name='hiwonder', links=[
    ikpy.link.OriginLink(), 
    ikpy.link.URDFLink(name="waist", bounds=(-np.pi/2, np.pi/2), origin_translation=[0, 0, L1], origin_orientation=[0, 0, 0], rotation=[0, 0, 1]),
    ikpy.link.URDFLink(name="shoulder", bounds=(0, np.pi/2), origin_translation=[0, 0, L2], origin_orientation=[0, 0, 0], rotation=[0, 1, 0]),
    ikpy.link.URDFLink(name="elbow", bounds=(0.3, np.pi/2), origin_translation=[0, 0, L3], origin_orientation=[0, 0, 0], rotation=[0, -1, 0]),
    ikpy.link.URDFLink(name="wrist", bounds=(-0.6, 0.6), origin_translation=[0, 0, L4], origin_orientation=[0, 0, 0], rotation=[0, -1, 0]),
], active_links_mask=[False, True, True, True, True])

# --- 3. COMMUNICATION HELPERS ---

def tcp_listener():
    """Pi acts as SERVER. Waiting for Crestron Client to connect."""
    global crestron_conn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('0.0.0.0', CRESTRON_PORT))
        s.settimeout(1.0)
        s.listen(5)
        print(f"--- Pi Server Active: Waiting for Crestron on {CRESTRON_PORT} ---")
        
        while not shutdown_event.is_set():
            try:
                conn, addr = s.accept()
                conn.settimeout(1.0)
                crestron_conn = conn
                with conn:
                    print(f"Crestron Connected: {addr}")
                    while not shutdown_event.is_set():
                        try:
                            data = conn.recv(1024).decode('utf-8').strip().upper()
                        except socket.timeout:
                            continue
                        if not data: break # Disconnect
                        
                        print(f"Incoming from Crestron: {data}")
                        # Handle commands received FROM Crestron (e.g., Alexa)
                        if data == "HOME": go_home()
                        elif data == "OPEN":
                            move_servo(1, 1500, 900)
                            set_holding_item(False)
                        elif data == "CLOSE":
                            move_servo(1, 2300, 900)
                            set_holding_item(True)
                            block_auto_release(getattr(config, "TABLE_HANDOFF_MIN_HOLD_SEC", 1.5))
                        elif data == "HAND_OPEN":
                            print("Hand open received – gripper open")
                            move_servo(1, 1500, 900)
                            set_holding_item(False)
                        elif data == "HAND_CLOSED":
                            print("Hand closed received – gripper close")
                            move_servo(1, 2300, 900)
                            set_holding_item(True)
                            block_auto_release(getattr(config, "TABLE_HANDOFF_MIN_HOLD_SEC", 1.5))
                        elif data in ("TAKE_ITEM", "TAKE", "HANDOFF"):
                            print("Take-item sequence requested")
                            start_take_item_sequence()
                        elif data in ("EXIT", "SHUTDOWN", "STOP"):
                            print("Shutdown requested")
                            shutdown_program()
                        elif data in ("HIGH_CAM", "TABLE_CAM", "DUAL_CAM"):
                            switch_camera(data)
                        elif data == "FLAGPOLE":
                            tuner.shared_params["busy"] = 1
                            reach_for_coordinate(0.05, 0.0, 0.46, speed=500)
                            say("Flagpole Mode. Manual lock engaged.")
                        elif data == "RESUME":
                            tuner.shared_params["busy"] = 0
                            if thermal_resume_callback:
                                threading.Thread(target=thermal_resume_callback, daemon=True).start()
                            global last_angles
                            last_angles = [0, 0, 0.2, 0.5, 0.1]
                            reach_for_coordinate(0.2, 0.0, 0.25, speed=1000)
                            say("Resuming tracking")
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Connection Lost: {e}")
            finally:
                crestron_conn = None


def request_shutdown():
    if shutdown_event.is_set():
        return
    shutdown_event.set()
    try:
        if _gpio_mod and getattr(config, "GRIPPER_SWITCH_PIN_BCM", None) is not None:
            _gpio_mod.cleanup(getattr(config, "GRIPPER_SWITCH_PIN_BCM"))
    except Exception:
        pass
    try:
        if 'ser' in globals() and ser and getattr(ser, "is_open", False):
            ser.close()
    except Exception:
        pass


def shutdown_program():
    request_shutdown()
    try:
        tuner.root.after(0, tuner.root.destroy)
    except Exception:
        pass

def send_to_crestron(command):
    """Pushes a string to the house via the open connection."""
    global crestron_conn
    if crestron_conn:
        try:
            # Sending \n ensures Crestron Serial Gather or S-1 triggers
            crestron_conn.sendall(f"{command}\n".encode())
            print(f"Pushed to Crestron: {command}")
        except Exception as e:
            print(f"Push failed: {e}")
            crestron_conn = None
    else:
        print("Error: Crestron is not connected to Pi Server.")


def switch_camera(mode):
    """Switch the active camera pipeline and update the GUI indicator."""
    tuner.shared_params["camera_mode"] = mode
    handler = camera_switch_handlers.get(mode)
    if handler:
        threading.Thread(target=handler, daemon=True).start()
    # Update GUI label from the main thread if the root window exists
    try:
        tuner.root.after(0, lambda: tuner.cam_mode_label.config(
            text=f"Mode: {mode.replace('_', ' ')}"
        ))
    except AttributeError:
        pass
    print(f"Camera switched to: {mode}")

# --- 4. MOVEMENT LOGIC ---

def move_servo(id, pos, time_ms=800):
    global _gripper_pos_est
    if ARM_MOVEMENT_DISABLED:
        print(f"ARM_MOVEMENT_DISABLED: servo {id} pos {pos} suppressed")
        return
    # Servo 1 is the claw – limit its closing range to protect the mechanism
    if id == 1:
        pos = max(1500, min(2326, pos))
        with _gripper_motion_lock:
            closing = pos > _gripper_pos_est
            if closing and _gripper_switch_pressed():
                print("Gripper close blocked: microswitch already active")
                _send_servo_packet(1, _gripper_pos_est, 120)
                _notify_servo_move(1, _gripper_pos_est)
                return

            if closing and _gripper_switch_ready:
                step_us = max(10, int(getattr(config, "GRIPPER_CLOSE_STEP_US", 35)))
                step_time_ms = max(40, int(getattr(config, "GRIPPER_CLOSE_STEP_TIME_MS", 70)))
                current = _gripper_pos_est
                while current < pos and not shutdown_event.is_set():
                    if _gripper_switch_pressed():
                        print(f"Gripper microswitch triggered at pos {current}; stopping close")
                        _send_servo_packet(1, current, 120)
                        _gripper_pos_est = current
                        return
                    current = min(pos, current + step_us)
                    _send_servo_packet(1, current, step_time_ms)
                    _notify_servo_move(1, current)
                    _gripper_pos_est = current
                    time.sleep(step_time_ms / 1000.0)
                return

            _send_servo_packet(id, pos, time_ms)
            _notify_servo_move(id, pos)
            _gripper_pos_est = pos
            return
    else:
        pos = max(500, min(2500, pos))
    _send_servo_packet(id, pos, time_ms)
    _notify_servo_move(id, pos)

def go_home():
    # Open gripper first to reduce collision/load risk while parking.
    move_servo(1, 1500, 900)
    if bool(getattr(config, "HOME_USE_COORDINATES", True)):
        hx = float(getattr(config, "HOME_X", 0.06))
        hy = float(getattr(config, "HOME_Y", 0.0))
        hz = float(getattr(config, "HOME_Z", 0.40))
        hs = int(getattr(config, "HOME_SPEED", 700))
        reach_for_coordinate(hx, hy, hz, speed=hs)
    else:
        # Legacy absolute pulse fallback.
        for id, pos in {6: 1883, 5: 700, 4: 655, 3: 720, 2: 1500, 1: 1500}.items():
            move_servo(id, pos, 2000)
    set_holding_item(False)


def _take_item_sequence():
    if not _take_item_lock.acquire(blocking=False):
        print("Take-item request ignored: sequence already running")
        return

    prev_mode = tuner.shared_params.get("camera_mode", "HIGH_CAM")
    prev_busy = tuner.shared_params.get("busy", 0)
    try:
        tuner.shared_params["busy"] = 1
        switch_camera("TABLE_CAM")

        p = tuner.get_params()
        take_x = max(0.12, min(0.28, float(p.get("take_x", 0.16))))
        take_y = max(-0.12, min(0.12, float(p.get("take_y", 0.0))))
        take_z = max(0.24, min(0.40, float(p.get("take_z", 0.30))))
        take_lift_z = max(take_z + 0.05, min(0.45, float(p.get("take_lift_z", 0.36))))
        take_wait_s = max(1.0, min(8.0, float(p.get("take_wait_s", 3.0))))

        move_servo(1, 1500, 900)
        # Approach from above first to avoid tipping the base by dipping too low.
        reach_for_coordinate(take_x, take_y, take_lift_z, speed=900)
        time.sleep(0.6)
        reach_for_coordinate(take_x, take_y, take_z, speed=800)
        say("Please place the item in my gripper")
        time.sleep(take_wait_s)

        move_servo(1, 2300, 900)
        set_holding_item(True)
        block_auto_release(getattr(config, "TABLE_HANDOFF_TAKE_LOCKOUT_SEC", 4.0))
        time.sleep(1.0)

        reach_for_coordinate(take_x, take_y, take_lift_z, speed=900)
        say("Got it")
        send_to_crestron("ITEM_TAKEN")
        time.sleep(0.6)
    except Exception as e:
        print(f"Take-item sequence error: {e}")
    finally:
        switch_camera(prev_mode)
        tuner.shared_params["busy"] = prev_busy
        _take_item_lock.release()


def start_take_item_sequence():
    threading.Thread(target=_take_item_sequence, daemon=True).start()

def reach_for_coordinate(x, y, z, speed=800):
    global last_angles
    try:
        x = float(x)
        y = float(y)
        z = float(z)
        speed = int(speed)
        angles = my_arm.inverse_kinematics([x, y, z], initial_position=last_angles)
        if not np.all(np.isfinite(angles)):
            print(f"IK rejected (non-finite solution) for target x={x:.3f} y={y:.3f} z={z:.3f}")
            return
        last_angles = angles
        new_p = {
            6: 1500 + int(angles[1] * 637), 
            5: 1500 + int(angles[2] * 637), 
            4: 1500 + int(angles[3] * 637), 
            3: 1500 + int(angles[4] * 637)
        }
        for id, pos in new_p.items(): move_servo(id, pos, speed)
    except Exception as e: print(f"IK Error: {e}")


def reach_for_manual_coordinate(x, y, z, speed=900):
    global last_angles
    x = max(float(getattr(config, "MANUAL_X_MIN", 0.14)),
            min(float(getattr(config, "MANUAL_X_MAX", 0.30)), float(x)))
    y = max(float(getattr(config, "MANUAL_Y_MIN", -0.12)),
            min(float(getattr(config, "MANUAL_Y_MAX", 0.12)), float(y)))
    z = max(float(getattr(config, "MANUAL_Z_MIN", 0.12)),
            min(float(getattr(config, "MANUAL_Z_MAX", 0.40)), float(z)))

    speed = int(getattr(config, "MANUAL_JOG_SPEED", speed))
    step_m = max(0.002, float(getattr(config, "MANUAL_JOG_STEP_M", 0.01)))
    travel_z = max(z, float(getattr(config, "MANUAL_TRAVEL_Z", 0.32)))

    def _safe_step_path(x_start, y_start, z_start, x_end, y_end, z_end):
        nonlocal step_m, speed
        global last_angles
        max_delta = max(abs(x_end - x_start), abs(y_end - y_start), abs(z_end - z_start))
        n_steps = max(1, int(np.ceil(max_delta / step_m)))
        for i in range(1, n_steps + 1):
            t = i / float(n_steps)
            xi = x_start + (x_end - x_start) * t
            yi = y_start + (y_end - y_start) * t
            zi = z_start + (z_end - z_start) * t

            angles = my_arm.inverse_kinematics([xi, yi, zi], initial_position=last_angles)
            if not np.all(np.isfinite(angles)):
                print(f"IK rejected (non-finite solution) for step target x={xi:.3f} y={yi:.3f} z={zi:.3f}")
                return False

            fk = my_arm.forward_kinematics(angles)
            solved_x = float(fk[0, 3])
            solved_y = float(fk[1, 3])
            solved_z = float(fk[2, 3])
            solve_err = float(np.linalg.norm(np.array([solved_x - xi, solved_y - yi, solved_z - zi])))
            max_err = float(getattr(config, "MANUAL_IK_MAX_POSITION_ERROR_M", 0.10))
            if solve_err > max_err:
                print(
                    f"IK rejected (pose error {solve_err:.3f}m > {max_err:.3f}m) "
                    f"step=({xi:.3f},{yi:.3f},{zi:.3f}) solved=({solved_x:.3f},{solved_y:.3f},{solved_z:.3f})"
                )
                return False

            joint_step_limit = float(getattr(config, "MANUAL_IK_MAX_JOINT_STEP_RAD", 1.20))
            prev = np.array(last_angles[1:5], dtype=float)
            curr = np.array(angles[1:5], dtype=float)
            max_step = float(np.max(np.abs(curr - prev)))
            if max_step > joint_step_limit:
                print(
                    f"IK rejected (joint step {max_step:.3f}rad > {joint_step_limit:.3f}rad) "
                    f"step=({xi:.3f},{yi:.3f},{zi:.3f})"
                )
                return False

            min_solved_z = float(getattr(config, "MANUAL_MIN_SOLVED_Z", 0.10))
            if solved_z < min_solved_z:
                print(
                    f"IK rejected (solved z {solved_z:.3f}m below floor {min_solved_z:.3f}m) "
                    f"step=({xi:.3f},{yi:.3f},{zi:.3f})"
                )
                return False

            last_angles = angles
            new_p = {
                6: 1500 + int(angles[1] * 637),
                5: 1500 + int(angles[2] * 637),
                4: 1500 + int(angles[3] * 637),
                3: 1500 + int(angles[4] * 637),
            }
            for servo_id, pos in new_p.items():
                move_servo(servo_id, pos, speed)
        return True

    fk0 = my_arm.forward_kinematics(last_angles)
    x0 = float(fk0[0, 3])
    y0 = float(fk0[1, 3])
    z0 = float(fk0[2, 3])

    try:
        if not _safe_step_path(x0, y0, z0, x0, y0, max(z0, travel_z)):
            return
        if not _safe_step_path(x0, y0, max(z0, travel_z), x, y, travel_z):
            return
        _safe_step_path(x, y, travel_z, x, y, z)
    except Exception as e:
        print(f"IK Error: {e}")
        return

def say(text):
    """Non-blocking Piper TTS speech via PipeWire audio output."""
    PIPER_BIN = "/home/arm/piper/piper/piper"
    MODEL_PATH = "/home/arm/piper/en_US-lessac-medium.onnx"
    if not os.path.exists(PIPER_BIN) or not os.path.exists(MODEL_PATH):
        print(f"Speech (TTS unavailable): {text}")
        return
    try:
        piper_proc = subprocess.Popen(
            [PIPER_BIN, "--model", MODEL_PATH, "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        play_proc = subprocess.Popen(  # noqa: F841
            ["pw-play", "--rate", "22050", "--channels", "1", "--format", "s16", "-"],
            stdin=piper_proc.stdout, stderr=subprocess.DEVNULL
        )
        piper_proc.stdout.close()
        piper_proc.stdin.write((text + "\n").encode())
        piper_proc.stdin.close()
    except Exception as e:
        print(f"Speech Error: {e}")

# --- 5. THE TUNER DASHBOARD ---

class RobotTuner:
    def __init__(self):
        self.shared_params = {
            "ry_m":0.42, "rz_m":0.45, "z_off":0.0, "speed":950, "smooth":0.20,
            "busy": 0, "tune_x":0.20, "tune_y":0.0, "tune_z":0.15,
            "nose_x":0.5, "nose_y":0.5,
            "left_hand_x":0.5, "left_hand_y":0.5,
            "right_hand_x":0.5, "right_hand_y":0.5,
            "camera_mode": "HIGH_CAM",
            "pose_gesture_debug": 1.0 if getattr(config, "POSE_GESTURE_DEBUG", False) else 0.0,
            "take_x": 0.16,
            "take_y": 0.0,
            "take_z": 0.30,
            "take_lift_z": 0.36,
            "take_wait_s": 3.0,
            "table_release_enabled": 1.0 if getattr(config, "TABLE_HANDOFF_RELEASE_ENABLED", True) else 0.0,
            "table_claw_x": float(getattr(config, "TABLE_HANDOFF_CLAW_X_NORM", 0.50)),
            "table_claw_y": float(getattr(config, "TABLE_HANDOFF_CLAW_Y_NORM", 0.82)),
            "table_release_radius": float(getattr(config, "TABLE_HANDOFF_RADIUS_NORM", 0.14)),
            "table_release_cooldown": float(getattr(config, "TABLE_HANDOFF_RELEASE_COOLDOWN", 2.5)),
        }
        self.scale_widgets = {}
        self._syncing_scales = False
        self.manual_mode = False 
        self.needs_camera_restart = False
        self._load_tuner_params(silent=True)

    def get_params(self): return self.shared_params

    def _clamp_manual_target(self):
        self.shared_params["tune_x"] = max(
            float(getattr(config, "MANUAL_X_MIN", 0.14)),
            min(float(getattr(config, "MANUAL_X_MAX", 0.30)), self.shared_params["tune_x"]),
        )
        self.shared_params["tune_y"] = max(
            float(getattr(config, "MANUAL_Y_MIN", -0.12)),
            min(float(getattr(config, "MANUAL_Y_MAX", 0.12)), self.shared_params["tune_y"]),
        )
        self.shared_params["tune_z"] = max(
            float(getattr(config, "MANUAL_Z_MIN", 0.12)),
            min(float(getattr(config, "MANUAL_Z_MAX", 0.40)), self.shared_params["tune_z"]),
        )

    def _load_tuner_params(self, silent=False):
        if not os.path.isfile(TUNER_PARAMS_PATH):
            if not silent:
                print(f"No tuner preset found at {TUNER_PARAMS_PATH}")
            return
        try:
            with open(TUNER_PARAMS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("preset must be a JSON object")
            for key, value in data.items():
                if key in self.shared_params:
                    if key == "busy":
                        continue
                    current = self.shared_params[key]
                    if isinstance(current, str):
                        self.shared_params[key] = str(value)
                    elif isinstance(current, (int, float)):
                        self.shared_params[key] = float(value)
                    else:
                        self.shared_params[key] = value
            if not silent:
                print(f"Loaded tuner preset: {TUNER_PARAMS_PATH}")
            self._sync_scale_widgets()
        except Exception as e:
            print(f"Failed to load tuner preset: {e}")

    def _update_thermal_status(self):
        busy = int(self.shared_params.get("busy", 0))
        if busy == 0 and not self.manual_mode:
            self.tracking_status_var.set("Tracking: Active")
        else:
            self.tracking_status_var.set("Tracking: Paused")

        power_provider = servo_power_provider
        if power_provider is None:
            self.servo_power_var.set("Servo Power: unknown")
        else:
            try:
                power_on = bool(power_provider())
                self.servo_power_var.set("Servo Power: ON" if power_on else "Servo Power: OFF")
            except Exception as e:
                self.servo_power_var.set(f"Servo Power error: {e}")

        provider = thermal_status_provider
        if provider is None:
            self.thermal_status_var.set("Thermal monitor: unavailable")
            self.servo5_load_var.set("Servo 5 Load: n/a")
        else:
            try:
                status = provider() or {}
                parked = bool(status.get("parked", False))
                idle_secs = float(status.get("idle_secs", 0.0))
                servo5_dev = status.get("servo5_deviation", None)
                high_counts = status.get("high_load_counts", {}) or {}
                high_servos = [str(sid) for sid, cnt in high_counts.items() if int(cnt) >= 3]
                high_text = ",".join(high_servos) if high_servos else "none"
                self.thermal_status_var.set(
                    f"Parked: {parked} | Idle: {idle_secs:.1f}s | High load servos: {high_text}"
                )
                if servo5_dev is None:
                    self.servo5_load_var.set("Servo 5 Load: n/a")
                else:
                    self.servo5_load_var.set(f"Servo 5 Load: {int(servo5_dev)}")
            except Exception as e:
                self.thermal_status_var.set(f"Thermal monitor error: {e}")
                self.servo5_load_var.set("Servo 5 Load: error")
        if hasattr(self, "root") and self.root is not None:
            self.root.after(1000, self._update_thermal_status)

    def _run_thermal_action(self, action_name, callback):
        if callback is None:
            print(f"Thermal {action_name.lower()} unavailable")
            return

        def _worker():
            try:
                callback()
                print(f"Thermal {action_name.lower()} requested")
            except Exception as e:
                print(f"Thermal {action_name.lower()} failed: {e}")

        threading.Thread(target=_worker, daemon=True).start()

    def _park_arm_clicked(self):
        self.shared_params["busy"] = 1
        self._run_thermal_action("Park", thermal_park_callback)

    def _resume_arm_clicked(self):
        self.shared_params["busy"] = 0
        self.manual_mode = False
        if hasattr(self, "manual_var"):
            self.manual_var.set(False)
        self._run_thermal_action("Resume", thermal_resume_callback)

    def _park_and_shutdown_clicked(self):
        try:
            if thermal_park_callback:
                self.shared_params["busy"] = 1
                thermal_park_callback()
                print("Thermal park requested before shutdown")
            else:
                print("Thermal park unavailable before shutdown")
        except Exception as e:
            print(f"Thermal park before shutdown failed: {e}")
        shutdown_program()

    def _save_tuner_params(self):
        try:
            with open(TUNER_PARAMS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.shared_params, f, indent=2, sort_keys=True)
            print(f"Saved tuner preset: {TUNER_PARAMS_PATH}")
        except Exception as e:
            print(f"Failed to save tuner preset: {e}")

    def _sync_scale_widgets(self):
        self._syncing_scales = True
        for key, scale in self.scale_widgets.items():
            if key in self.shared_params:
                try:
                    scale.set(self.shared_params[key])
                except Exception:
                    pass
        self._syncing_scales = False

    def create_gui(self):
        self.root = tk.Tk()
        self.root.title("Robot Master - Pi Server Mode")
        self.root.geometry("1180x860")
        self.root.protocol("WM_DELETE_WINDOW", shutdown_program)

        columns = tk.Frame(self.root)
        columns.pack(fill="both", expand=True, padx=10, pady=8)
        left_col = tk.Frame(columns)
        right_col = tk.Frame(columns)
        status_col = tk.Frame(columns)
        left_col.grid(row=0, column=0, sticky="nw", padx=(0, 10))
        right_col.grid(row=0, column=1, sticky="nw")
        status_col.grid(row=0, column=2, sticky="nw", padx=(10, 0))

        # --- Camera Mode Frame (left column) ---
        tk.Label(left_col, text="--- CAMERA MODE ---", font=("Arial", 12, "bold")).pack(pady=5)
        cam_frame = tk.Frame(left_col)
        cam_frame.pack()
        tk.Button(cam_frame, text="HIGH CAM\n(Face Tracking)", bg="blue", fg="white",
                  width=12, command=lambda: switch_camera("HIGH_CAM")).pack(side="left", padx=4)
        tk.Button(cam_frame, text="TABLE CAM\n(Manipulation)", bg="green", fg="white",
                  width=12, command=lambda: switch_camera("TABLE_CAM")).pack(side="left", padx=4)
        tk.Button(cam_frame, text="DUAL CAM\n(Track+Preview)", bg="purple", fg="white",
                  width=12, command=lambda: switch_camera("DUAL_CAM")).pack(side="left", padx=4)
        self.cam_mode_label = tk.Label(left_col, text="Mode: HIGH CAM", font=("Arial", 10))
        self.cam_mode_label.pack(pady=3)

        # --- Calibration Jog (left column) ---
        tk.Label(left_col, text="--- CALIBRATION JOG ---", font=("Arial", 12, "bold")).pack(pady=5)
        self.manual_var = tk.BooleanVar(value=False)
        tk.Checkbutton(left_col, text="ENABLE MANUAL SLIDERS (AI LOCKED)", variable=self.manual_var, command=self.toggle_manual_mode, font=("Arial", 10, "bold"), fg="blue").pack(pady=10)

        preset_frame = tk.Frame(left_col)
        preset_frame.pack(pady=4)
        tk.Button(preset_frame, text="LOAD TUNE", width=12, command=lambda: self._load_tuner_params(silent=False)).pack(side="left", padx=5)
        tk.Button(preset_frame, text="SAVE TUNE", width=12, command=self._save_tuner_params).pack(side="left", padx=5)

        for lbl, k, mn, mx, res in [
              ("Reach X", "tune_x", float(getattr(config, "MANUAL_X_MIN", 0.14)), float(getattr(config, "MANUAL_X_MAX", 0.30)), 0.001),
              ("Swing Y", "tune_y", float(getattr(config, "MANUAL_Y_MIN", -0.12)), float(getattr(config, "MANUAL_Y_MAX", 0.12)), 0.001),
              ("Height Z", "tune_z", float(getattr(config, "MANUAL_Z_MIN", 0.12)), float(getattr(config, "MANUAL_Z_MAX", 0.40)), 0.001),
        ]:
            tk.Label(left_col, text=lbl).pack()
            s = tk.Scale(left_col, from_=mn, to=mx, resolution=res, orient='horizontal', length=320, command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack()
            self.scale_widgets[k] = s

        tk.Button(left_col, text="HOME ARM", command=go_home, bg="gray", fg="white").pack(pady=10)
        tk.Button(left_col, text="TAKE ITEM", command=start_take_item_sequence,
              bg="purple", fg="white").pack(pady=6)

        # --- Handoff Tune Frame (left column) ---
        tk.Label(left_col, text="--- TAKE ITEM TUNE ---", font=("Arial", 12, "bold")).pack(pady=8)
        for lbl, k, mn, mx in [
            ("Take X", "take_x", 0.12, 0.28),
            ("Take Y", "take_y", -0.12, 0.12),
            ("Take Z", "take_z", 0.24, 0.40),
            ("Lift Z", "take_lift_z", 0.28, 0.45),
            ("Wait (s)", "take_wait_s", 1.0, 8.0),
        ]:
            tk.Label(left_col, text=lbl).pack()
            s = tk.Scale(left_col, from_=mn, to=mx, resolution=0.01, orient='horizontal',
                         length=320, command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack()

        tk.Button(left_col, text="EXIT PROGRAM", command=self._park_and_shutdown_clicked,
                  bg="red", fg="white").pack(pady=10)

        # --- Tracking Math (right column) ---
        tk.Label(right_col, text="--- TRACKING MATH ---", font=("Arial", 12, "bold")).pack(pady=8)
        for lbl, k, mn, mx, res in [
            ("Y Gain", "ry_m", 0.10, 0.80, 0.01),
            ("Z Gain", "rz_m", 0.10, 0.80, 0.01),
            ("Z Offset", "z_off", -0.20, 0.20, 0.005),
        ]:
            tk.Label(right_col, text=lbl).pack()
            s = tk.Scale(right_col, from_=mn, to=mx, resolution=res, orient='horizontal',
                         length=320, command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack()
            self.scale_widgets[k] = s

        # --- Tracking Response (right column) ---
        tk.Label(right_col, text="--- TRACKING RESPONSE ---", font=("Arial", 12, "bold")).pack(pady=8)
        for lbl, k, mn, mx, res in [
            ("Speed", "speed", 400.0, 1800.0, 10.0),
            ("Smoothing", "smooth", 0.05, 0.60, 0.01),
        ]:
            tk.Label(right_col, text=lbl).pack()
            s = tk.Scale(right_col, from_=mn, to=mx, resolution=res, orient='horizontal',
                         length=320, command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack()
            self.scale_widgets[k] = s

        # --- Gesture Debug (right column) ---
        tk.Label(right_col, text="--- GESTURE DEBUG ---", font=("Arial", 12, "bold")).pack(pady=8)
        tk.Label(right_col, text="Debug Log (0/1)").pack()
        s = tk.Scale(right_col, from_=0.0, to=1.0, resolution=1.0, orient='horizontal',
                 length=320, command=lambda v, k="pose_gesture_debug": self.update_tune(k, v))
        s.set(self.shared_params["pose_gesture_debug"])
        s.pack()
        self.scale_widgets["pose_gesture_debug"] = s

        # --- Auto Release Tune Frame (right column) ---
        tk.Label(right_col, text="--- AUTO RELEASE TUNE ---", font=("Arial", 12, "bold")).pack(pady=8)
        for lbl, k, mn, mx, res in [
            ("Enabled (0/1)", "table_release_enabled", 0.0, 1.0, 1.0),
            ("Claw X (norm)", "table_claw_x", 0.0, 1.0, 0.01),
            ("Claw Y (norm)", "table_claw_y", 0.0, 1.0, 0.01),
            ("Radius (norm)", "table_release_radius", 0.03, 0.35, 0.01),
            ("Cooldown (s)", "table_release_cooldown", 0.5, 6.0, 0.1),
        ]:
            tk.Label(right_col, text=lbl).pack()
            s = tk.Scale(right_col, from_=mn, to=mx, resolution=res, orient='horizontal',
                         length=320, command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack()
            self.scale_widgets[k] = s

        # --- Thermal Monitor Frame (status column) ---
        tk.Label(status_col, text="--- THERMAL STATUS ---", font=("Arial", 12, "bold")).pack(pady=12)
        self.tracking_status_var = tk.StringVar(value="Tracking: initializing...")
        tk.Label(status_col, textvariable=self.tracking_status_var, justify="left").pack(pady=2)
        self.servo_power_var = tk.StringVar(value="Servo Power: initializing...")
        tk.Label(status_col, textvariable=self.servo_power_var, justify="left").pack(pady=2)
        self.servo5_load_var = tk.StringVar(value="Servo 5 Load: initializing...")
        tk.Label(status_col, textvariable=self.servo5_load_var, justify="left").pack(pady=2)
        self.thermal_status_var = tk.StringVar(value="Thermal monitor: initializing...")
        tk.Label(status_col, textvariable=self.thermal_status_var, wraplength=260, justify="left").pack(pady=4)

        thermal_btn_frame = tk.Frame(status_col)
        thermal_btn_frame.pack(pady=6)
        tk.Button(thermal_btn_frame, text="PARK ARM", width=12,
              bg="orange", command=self._park_arm_clicked).pack(side="left", padx=5)
        tk.Button(thermal_btn_frame, text="RESUME", width=12,
              bg="lightgreen", command=self._resume_arm_clicked).pack(side="left", padx=5)

        # --- Crestron Lights Frame (status column) ---
        tk.Label(status_col, text="--- CRESTRON LIGHTS ---", font=("Arial", 12, "bold")).pack(pady=15)
        btn_frame = tk.Frame(status_col)
        btn_frame.pack()

        tk.Button(btn_frame, text="LIGHTS ON", bg="yellow", width=12, command=lambda: send_to_crestron("LIGHT_ON")).pack(side="left", padx=5)
        tk.Button(btn_frame, text="LIGHTS OFF", bg="black", fg="white", width=12, command=lambda: send_to_crestron("LIGHT_OFF")).pack(side="left", padx=5)
        self._update_thermal_status()

    def toggle_manual_mode(self):
        self.manual_mode = self.manual_var.get()
        self.shared_params["busy"] = 1 if self.manual_mode else 0
        if self.manual_mode:
            self._clamp_manual_target()
            reach_for_manual_coordinate(
                self.shared_params["tune_x"],
                self.shared_params["tune_y"],
                self.shared_params["tune_z"],
                1200,
            )

    def update_tune(self, k, v):
        if self._syncing_scales:
            return
        self.shared_params[k] = float(v)
        if k in ("tune_x", "tune_y", "tune_z"):
            self._clamp_manual_target()
            self._sync_scale_widgets()
        if self.manual_mode and k in ("tune_x", "tune_y", "tune_z"):
            self._clamp_manual_target()
            reach_for_manual_coordinate(
                self.shared_params["tune_x"],
                self.shared_params["tune_y"],
                self.shared_params["tune_z"],
                1200,
            )

tuner = RobotTuner()

def start_brain_ui():
    # Start the Server thread immediately
    _init_gripper_switch()
    tuner.shared_params["busy"] = 0
    if thermal_resume_callback:
        try:
            thermal_resume_callback()
        except Exception:
            pass
    threading.Thread(target=tcp_listener, daemon=True).start()
    try:
        tuner.create_gui()
        tuner.root.mainloop()
        request_shutdown()
    except tk.TclError as e:
        print(f"GUI disabled (no display): {e}")
        while not shutdown_event.is_set():
            time.sleep(0.5)