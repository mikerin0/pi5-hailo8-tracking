import json, os, serial, time, subprocess, tkinter as tk, threading, socket
import numpy as np 
import ikpy.chain
import ikpy.link

# --- SAFETY FLAG ---
# Set to True to disable ALL servo movement (prevents hardware damage).
# Set to False once the Hailo pipeline is confirmed working and arm limits are safe.
ARM_MOVEMENT_DISABLED = False

# --- Tuner preset persistence ---
# Only numeric slider values are saved/loaded; string keys (e.g. camera_mode) are skipped.
_PRESET_PATH = os.path.expanduser("~/.robot_tuner_preset.json")
_PRESET_FLOAT_KEYS = frozenset({
    "ry_m", "rz_m", "z_off", "speed", "smooth",
    "tune_x", "tune_y", "tune_z",
    "nose_x", "nose_y", "left_hand_x", "left_hand_y", "right_hand_x", "right_hand_y",
})

def _load_preset(params):
    try:
        with open(_PRESET_PATH) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in _PRESET_FLOAT_KEYS:
                params[k] = float(v)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Failed to load tuner preset: {e}")

def _save_preset(params):
    try:
        with open(_PRESET_PATH, "w") as f:
            json.dump({k: params[k] for k in _PRESET_FLOAT_KEYS if k in params}, f)
    except Exception as e:
        print(f"Failed to save tuner preset: {e}")

# --- 1. SETTINGS & HARDWARE ---
# Physical lengths of your 6DOF arm (L4 includes your 19cm claw)
L1, L2, L3, L4 = 0.05, 0.055, 0.091, 0.170
PORT, BAUD = "/dev/ttyAMA10", 9600
CRESTRON_PORT = 50005
last_angles = [0, 0, 0.2, 0.5, 0.1]

# Pluggable thermal management callbacks – set by od.py at startup
on_relax_arm = None          # callable() → moves arm to low-strain rest position
get_thermal_status_fn = None  # callable() → dict with parked/idle/load info

# --- Camera switch callbacks (populated by external modules at runtime) ---
# Keys: "HIGH_CAM", "TABLE_CAM"  →  callable with no arguments
camera_switch_handlers = {}

# Global for the persistent Crestron connection
crestron_conn = None

try:
    ser = serial.Serial(PORT, BAUD, timeout=1)
except:
    print("Serial Error: Check TX/RX cables. Servos will not move.")

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
        s.listen(5)
        print(f"--- Pi Server Active: Waiting for Crestron on {CRESTRON_PORT} ---")
        
        while True:
            try:
                conn, addr = s.accept()
                crestron_conn = conn
                with conn:
                    print(f"Crestron Connected: {addr}")
                    while True:
                        data = conn.recv(1024).decode('utf-8').strip().upper()
                        if not data: break # Disconnect
                        
                        print(f"Incoming from Crestron: {data}")
                        # Handle commands received FROM Crestron (e.g., Alexa)
                        if data == "HOME": go_home()
                        elif data == "OPEN": move_servo(1, 1500, 900)
                        elif data == "CLOSE": move_servo(1, 2300, 900)
                        elif data == "HAND_OPEN":
                            print("Hand open received – gripper open")
                            move_servo(1, 1500, 900)
                        elif data == "HAND_CLOSED":
                            print("Hand closed received – gripper close")
                            move_servo(1, 2300, 900)
                        elif data in ("HIGH_CAM", "TABLE_CAM"):
                            switch_camera(data)
                        elif data == "FLAGPOLE":
                            tuner.shared_params["busy"] = 1
                            reach_for_coordinate(0.05, 0.0, 0.46, speed=500)
                            say("Flagpole Mode. Manual lock engaged.")
                        elif data == "RESUME":
                            tuner.shared_params["busy"] = 0
                            global last_angles
                            last_angles = [0, 0, 0.2, 0.5, 0.1]
                            reach_for_coordinate(0.2, 0.0, 0.25, speed=1000)
                            say("Resuming tracking")
            except Exception as e:
                print(f"Connection Lost: {e}")
            finally:
                crestron_conn = None

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
    if ARM_MOVEMENT_DISABLED:
        print(f"ARM_MOVEMENT_DISABLED: servo {id} pos {pos} suppressed")
        return
    # Servo 1 is the claw – limit its closing range to protect the mechanism
    if id == 1:
        pos = max(1500, min(2326, pos))
    else:
        pos = max(500, min(2500, pos))
    packet = bytearray([0x55, 0x55, 0x08, 0x03, 0x01, time_ms & 0xFF, (time_ms >> 8) & 0xFF, id, pos & 0xFF, (pos >> 8) & 0xFF])
    ser.write(packet)

def go_home():
    # Calibrated safe-rest positions for each servo (from field tuning)
    for id, pos in {6: 1883, 5: 700, 4: 655, 3: 720, 2: 1500, 1: 1500}.items():
        move_servo(id, pos, 2000)

def reach_for_coordinate(x, y, z, speed=800):
    global last_angles
    try:
        angles = my_arm.inverse_kinematics([x, y, z], initial_position=last_angles)
        last_angles = angles
        new_p = {
            6: 1500 + int(angles[1] * 637), 
            5: 1500 + int(angles[2] * 637), 
            4: 1500 + int(angles[3] * 637), 
            3: 1500 + int(angles[4] * 637)
        }
        for id, pos in new_p.items(): move_servo(id, pos, speed)
    except Exception as e: print(f"IK Error: {e}")

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
        }
        self.manual_mode = False 
        self.needs_camera_restart = False

    def get_params(self): return self.shared_params

    def create_gui(self):
        self.root = tk.Tk()
        self.root.title("Robot Master v2 - Pi Server Mode")
        self.root.geometry("420x460+0+0")
        self.root.resizable(True, True)

        # Scrollable canvas so the GUI fits on any display height
        canvas = tk.Canvas(self.root, borderwidth=0)
        vsb = tk.Scrollbar(self.root, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        f = tk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))

        # --- Camera Mode Frame ---
        tk.Label(f, text="--- CAMERA MODE ---", font=("Arial", 12, "bold")).pack(pady=5)
        cam_frame = tk.Frame(f)
        cam_frame.pack()
        tk.Button(cam_frame, text="HIGH CAM\n(Face Tracking)", bg="blue", fg="white",
                  width=14, command=lambda: switch_camera("HIGH_CAM")).pack(side="left", padx=5)
        tk.Button(cam_frame, text="TABLE CAM\n(Manipulation)", bg="green", fg="white",
                  width=14, command=lambda: switch_camera("TABLE_CAM")).pack(side="left", padx=5)
        self.cam_mode_label = tk.Label(f, text="Mode: HIGH CAM", font=("Arial", 10))
        self.cam_mode_label.pack(pady=3)

        # --- Robot Frame ---
        tk.Label(f, text="--- ROBOT CONTROL ---", font=("Arial", 12, "bold")).pack(pady=5)
        self.manual_var = tk.BooleanVar(value=False)
        tk.Checkbutton(f, text="ENABLE MANUAL SLIDERS (AI LOCKED)", variable=self.manual_var, command=self.toggle_manual_mode, font=("Arial", 10, "bold"), fg="blue").pack(pady=10)

        for lbl, k, mn, mx in [("Reach X", "tune_x", 0.1, 0.35), ("Swing Y", "tune_y", -0.2, 0.2), ("Height Z", "tune_z", 0.02, 0.4)]:
            tk.Label(f, text=lbl).pack()
            s = tk.Scale(f, from_=mn, to=mx, resolution=0.005, orient='horizontal', command=lambda v, k=k: self.update_tune(k, v))
            s.set(self.shared_params[k]); s.pack(fill="x", padx=10)

        tk.Button(f, text="HOME ARM", command=go_home, bg="gray", fg="white").pack(pady=10)

        # --- Servo Thermal Frame ---
        tk.Label(f, text="--- SERVO THERMAL ---", font=("Arial", 12, "bold")).pack(pady=5)
        tk.Button(f, text="RELAX ARM (fold to rest)",
                  bg="orange", fg="white", width=22,
                  command=self._do_relax_arm).pack(pady=5)
        self.thermal_label = tk.Label(
            f, text="Thermal monitor: starting…",
            font=("Arial", 9), wraplength=380, justify="left"
        )
        self.thermal_label.pack(pady=3)
        self._refresh_thermal_status()

        # --- Crestron Lights Frame ---
        tk.Label(f, text="--- CRESTRON LIGHTS ---", font=("Arial", 12, "bold")).pack(pady=15)
        btn_frame = tk.Frame(f)
        btn_frame.pack()
        tk.Button(btn_frame, text="LIGHTS ON", bg="yellow", width=12, command=lambda: send_to_crestron("LIGHT_ON")).pack(side="left", padx=5)
        tk.Button(btn_frame, text="LIGHTS OFF", bg="black", fg="white", width=12, command=lambda: send_to_crestron("LIGHT_OFF")).pack(side="left", padx=5)

        # --- Exit ---
        tk.Button(f, text="EXIT", bg="red", fg="white", width=12,
                  command=lambda: os._exit(0)).pack(pady=15)

    def toggle_manual_mode(self):
        self.manual_mode = self.manual_var.get()
        self.shared_params["busy"] = 1 if self.manual_mode else 0

    def update_tune(self, k, v):
        self.shared_params[k] = float(v)
        if self.manual_mode: reach_for_coordinate(self.shared_params["tune_x"], self.shared_params["tune_y"], self.shared_params["tune_z"], 1200)

    def _do_relax_arm(self):
        """Invoke the thermal relax callback, logging any error."""
        if on_relax_arm:
            try:
                on_relax_arm()
            except Exception as exc:
                print(f"RELAX ARM error: {exc}")

    def _refresh_thermal_status(self):
        """Update the thermal status label every 3 seconds via tkinter after()."""
        if get_thermal_status_fn:
            try:
                s = get_thermal_status_fn()
                parked = "YES" if s.get("parked") else "no"
                idle = f"{s.get('idle_secs', 0):.0f}s"
                loads = s.get("high_load_counts", {})
                hot = [str(sid) for sid, c in loads.items() if c > 0]
                hot_str = ", ".join(hot) if hot else "none"
                self.thermal_label.config(
                    text=f"Parked: {parked}  Idle: {idle}  High-load servos: {hot_str}"
                )
            except Exception as exc:
                self.thermal_label.config(text=f"Thermal status error: {exc}")
        else:
            self.thermal_label.config(text="Thermal monitor: not connected")
        try:
            self.root.after(3000, self._refresh_thermal_status)
        except Exception:
            pass  # window has been destroyed

tuner = RobotTuner()

def start_brain_ui():
    # Start the Server thread immediately
    threading.Thread(target=tcp_listener, daemon=True).start()
    _load_preset(tuner.shared_params)
    tuner.create_gui()
    tuner.root.mainloop()
    _save_preset(tuner.shared_params)