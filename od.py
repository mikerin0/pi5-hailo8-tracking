import os, sys, time, threading, platform, gi, hailo, numpy as np, robot_brain as brain
import config

# Prepend the Hailo GStreamer plugin directory to GST_PLUGIN_PATH before
# Gst.init() so that hailonet / hailofilter / hailooverlay are discovered
# even when the environment variable is unset or overridden (e.g. in a venv).
_HAILO_GST_DIR = f"/usr/lib/{platform.machine()}-linux-gnu/gstreamer-1.0"
_existing_gst_path = os.environ.get("GST_PLUGIN_PATH", "")
if _HAILO_GST_DIR not in _existing_gst_path.split(":"):
    os.environ["GST_PLUGIN_PATH"] = (
        f"{_HAILO_GST_DIR}:{_existing_gst_path}" if _existing_gst_path else _HAILO_GST_DIR
    )

gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

_HAILO_ELEMENTS = ("hailonet", "hailofilter", "hailooverlay")

def _check_hailo_plugins():
    """Return True if all Hailo GStreamer elements are registered; print help if not."""
    missing = [e for e in _HAILO_ELEMENTS if Gst.ElementFactory.find(e) is None]
    if missing:
        print(
            f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
            "  The Hailo GStreamer plugins are missing or not on the plugin path.\n"
            "  1. Install the package:  sudo apt install hailo-all\n"
            "  2. Verify the elements:  gst-inspect-1.0 hailonet\n"
            f"  3. If still missing, check that GST_PLUGIN_PATH includes {_HAILO_GST_DIR}"
        )
        return False
    return True

# --- Configuration (canonical values live in config.py) ---
HEF_PATH = config.HEF_PATH
SO_PATH = config.SO_PATH
last_gesture_time = 0

# --- Hand gesture state ---
_last_hand_state = None  # "OPEN" | "CLOSED" | None
_FINGER_TIP_IDS = [4, 8, 12, 16, 20]  # thumb, index, middle, ring, pinky tips


def _classify_hand(pts):
    """Return 'OPEN', 'CLOSED', or None if the hand state is ambiguous."""
    wx, wy = pts[0].x(), pts[0].y()
    dists = [np.hypot(pts[i].x() - wx, pts[i].y() - wy) for i in _FINGER_TIP_IDS]
    if all(d > config.HAND_OPEN_THRESHOLD for d in dists):
        return "OPEN"
    if all(d < config.HAND_CLOSED_THRESHOLD for d in dists):
        return "CLOSED"
    return None


def app_callback(pad, info, user_data):
    global last_gesture_time, _last_hand_state
    buffer = info.get_buffer()
    if not buffer: return Gst.PadProbeReturn.OK
    
    # Heartbeat print to verify AI is seeing frames
    if time.time() % 3 < 0.1: print("--- AI Pro Chip Heartbeat: Processing ---")

    try:
        roi = hailo.get_roi_from_buffer(buffer)
        if roi:
            landmarks = roi.get_objects_typed(hailo.HAILO_LANDMARKS)
            if landmarks:
                pts = landmarks[0].get_points()
                now = time.time()
                # Index finger check (Point 8 is Tip, Point 6 is Knuckle)
                if pts[8].y() < pts[6].y() and (now - last_gesture_time > 2.0):
                    print(">>> GESTURE DETECTED: LIGHTS ON")
                    brain.send_to_crestron("LIGHT_ON")
                    last_gesture_time = now

                # Open / close hand detection
                if now - last_gesture_time > config.GESTURE_COOLDOWN_SEC:
                    state = _classify_hand(pts)
                    if state and state != _last_hand_state:
                        _last_hand_state = state
                        event = f"HAND_{state}"
                        print(f">>> GESTURE DETECTED: {event}")
                        brain.send_to_crestron(event)
                        last_gesture_time = now
    except Exception: pass
    return Gst.PadProbeReturn.OK

def camera_loop():
    # Force the device type for the Pro chip before starting
    os.environ["hailort_device_type"] = "hailo8"

    if not _check_hailo_plugins():
        return
    
    while True:
        os.system("sudo pkill -9 -f hailonet")
        time.sleep(1)
        try:
            # We use a 'leaky' queue to ensure the high-speed 26 TOPS data 
            # doesn't overflow the Pi's memory
            launch_str = (
                f"libcamerasrc camera-name={config.PI_CAMERA_DEVICE} ! "
                f"videoconvert ! video/x-raw,format=RGB,width=224,height=224 ! "
                f"hailonet name=net hef-path={HEF_PATH} ! "
                f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                f"hailofilter so-path={SO_PATH} ! "
                f"hailooverlay ! videoconvert ! ximagesink sync={config.GST_SYNC}"
            )
            
            pipe = Gst.parse_launch(launch_str)
            net = pipe.get_by_name("net")
            net.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, app_callback, None)
            
            pipe.set_state(Gst.State.PLAYING)
            print("--- System Active: Hand Landmark AI Running ---")
            
            while True: time.sleep(1)
        except Exception as e:
            print(f"Pipeline Error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=brain.start_brain_ui, daemon=True).start()
    camera_loop()