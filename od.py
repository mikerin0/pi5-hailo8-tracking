import ctypes
import os, sys, time, threading, platform, gi, hailo, numpy as np, robot_brain as brain
import config

# On aarch64 Linux, libgomp.so.1 has a static TLS block that must be allocated
# before any shared library that depends on it is loaded via dlopen().  If it
# is not already mapped when Gst.init() calls dlopen("libgsthailotools.so"),
# the dynamic linker cannot satisfy the TLS requirement and the plugin silently
# fails to load.
#
# Setting os.environ["LD_PRELOAD"] only affects *new subprocesses* (it calls
# putenv in C, which the dynamic linker reads at process startup).  It does
# not retroactively load libgomp into the *current* Python process.
#
# The correct fix is to call ctypes.CDLL() which uses dlopen() immediately,
# pre-allocating the static TLS before Gst.init() runs.  We also keep the
# os.environ setting so that subprocesses (gst-plugin-scanner) inherit it.
# See: https://github.com/hailo-ai/hailo-rpi5-examples/blob/main/doc/install-raspberry-pi5.md
_arch = platform.machine()
_LIBGOMP = f"/usr/lib/{_arch}-linux-gnu/libgomp.so.1"
if os.path.isfile(_LIBGOMP):
    try:
        ctypes.CDLL(_LIBGOMP)  # load into the current process immediately
    except OSError:
        pass
    _ld_preload = os.environ.get("LD_PRELOAD", "")
    if _LIBGOMP not in _ld_preload.split(":"):
        os.environ["LD_PRELOAD"] = f"{_LIBGOMP}:{_ld_preload}" if _ld_preload else _LIBGOMP

# Also ensure the standard GStreamer plugin directory is on the search path
# in case GST_PLUGIN_PATH was overridden (e.g. inside a venv).
_HAILO_GST_DIR = f"/usr/lib/{_arch}-linux-gnu/gstreamer-1.0"
_existing_gst_path = os.environ.get("GST_PLUGIN_PATH", "")
if _HAILO_GST_DIR not in _existing_gst_path.split(":"):
    os.environ["GST_PLUGIN_PATH"] = (
        f"{_HAILO_GST_DIR}:{_existing_gst_path}" if _existing_gst_path else _HAILO_GST_DIR
    )

gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

_HAILO_ELEMENTS = ("hailonet", "hailofilter", "hailooverlay")

def _clear_gst_registry():
    """Delete stale GStreamer registry cache files so the next scan starts fresh."""
    import glob
    cache_dir = os.path.expanduser("~/.cache/gstreamer-1.0")
    for path in glob.glob(os.path.join(cache_dir, "registry.*.bin")):
        try:
            os.remove(path)
        except OSError:
            pass

def _check_hailo_plugins():
    """Return True if all Hailo GStreamer elements are registered.

    If the initial check fails, automatically clears the GStreamer registry
    cache and forces a fresh plugin scan (via Gst.update_registry()).  The
    rescan inherits the LD_PRELOAD set at module level (_LIBGOMP), so
    gst-plugin-scanner can load libgomp and, in turn, the Hailo plugin shared
    libraries.  This recovers the common case where the cache was rebuilt
    without LD_PRELOAD (e.g. by running gst-inspect-1.0 before configuring
    ~/.bashrc).
    """
    missing = [e for e in _HAILO_ELEMENTS if Gst.ElementFactory.find(e) is None]
    if not missing:
        return True

    # Plugins not found — the registry cache may pre-date our LD_PRELOAD
    # configuration.  Clear the cache files and force a full rescan.
    _clear_gst_registry()
    Gst.update_registry()

    missing = [e for e in _HAILO_ELEMENTS if Gst.ElementFactory.find(e) is None]
    if not missing:
        return True

    # Still missing after rescan — this is a genuine installation problem.
    print(
        f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
        "  The Hailo GStreamer plugins could not be loaded.\n"
        "  1. Confirm the package is installed: sudo apt install hailo-all\n"
        "  2. Check for missing shared-library dependencies:\n"
        f"     ldd /lib/{_arch}-linux-gnu/gstreamer-1.0/libgsthailotools.so | grep 'not found'\n"
        "  3. If libgomp appears missing, add to ~/.bashrc and open a new terminal:\n"
        f"     export LD_PRELOAD={_LIBGOMP}\n"
        "  4. Verify the Hailo device is connected: hailortcli fw-control identify"
    )
    return False

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