# These imports must come first so that libgomp and GST_PLUGIN_PATH are
# configured BEFORE any other package (hailo, robot_brain, gi) can trigger a
# GStreamer initialisation internally.
import os, sys, platform, glob as _glob

# libgsthailotools.so depends on libgomp.so.1 (OpenMP), which has a static TLS
# block that must be mapped before the dynamic linker exhausts glibc's fixed
# surplus-TLS budget.  A mid-process ctypes.CDLL() call cannot guarantee this
# because Python startup may have already consumed part of that budget.  The
# only fully reliable approach is LD_PRELOAD set *before the process starts*.
#
# Strategy: if LD_PRELOAD does not already contain libgomp, set it and re-exec
# this script.  The replacement process inherits the updated environment and
# starts with libgomp pre-mapped at dynamic-linker init time.  The re-exec is
# one-shot: the new process finds libgomp in LD_PRELOAD and skips this branch.
# See: https://github.com/hailo-ai/hailo-rpi5-examples/blob/main/doc/install-raspberry-pi5.md
_arch = platform.machine()
_LIBGOMP = f"/usr/lib/{_arch}-linux-gnu/libgomp.so.1"
if os.path.isfile(_LIBGOMP):
    _ld_preload = os.environ.get("LD_PRELOAD", "")
    if _LIBGOMP not in _ld_preload.split(":"):
        os.environ["LD_PRELOAD"] = f"{_LIBGOMP}:{_ld_preload}" if _ld_preload else _LIBGOMP
        os.execv(sys.executable, [sys.executable] + sys.argv)

# Ensure the Hailo GStreamer plugin directory is on the search path before
# any import can call Gst.init() (e.g. gi, hailo, robot_brain).
#
# The canonical install path documented by Hailo is:
#   /lib/{arch}-linux-gnu/gstreamer-1.0/libgsthailotools.so
# On some Raspberry Pi OS releases /lib and /usr/lib are separate real
# directories (not symlinks), so we add both.  We also glob-search for
# non-standard locations (e.g. /usr/lib/{arch}-linux-gnu/hailo/gstreamer/)
# so that any installation layout is handled automatically.
_HAILO_GST_CANDIDATES = [
    f"/lib/{_arch}-linux-gnu/gstreamer-1.0",
    f"/usr/lib/{_arch}-linux-gnu/gstreamer-1.0",
]
# Glob patterns at depth 0 and 1 below each lib root to catch non-standard
# install locations (e.g. /usr/lib/{arch}-linux-gnu/hailo/gstreamer/) without
# traversing the entire tree.  Shared by module-level setup and
# _check_hailo_plugins() so there is a single source of truth.
_HAILO_SO_GLOB_PATTERNS = (
    f"/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
)
for _so in (_hit for _pat in _HAILO_SO_GLOB_PATTERNS for _hit in _glob.glob(_pat)):
    _so_dir = os.path.dirname(_so)
    if _so_dir not in _HAILO_GST_CANDIDATES:
        _HAILO_GST_CANDIDATES.append(_so_dir)
_existing_gst_path = os.environ.get("GST_PLUGIN_PATH", "")
_existing_gst_dirs = set(filter(None, _existing_gst_path.split(":"))) if _existing_gst_path else set()
_new_dirs = [d for d in _HAILO_GST_CANDIDATES
             if d not in _existing_gst_dirs and os.path.isdir(d)]
if _new_dirs:
    _prefix = ":".join(_new_dirs)
    os.environ["GST_PLUGIN_PATH"] = f"{_prefix}:{_existing_gst_path}" if _existing_gst_path else _prefix

# All remaining imports come after the environment is prepared.
import time, threading, gi, hailo, numpy as np, robot_brain as brain
import config

gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

_HAILO_ELEMENTS = ("hailonet", "hailofilter", "hailooverlay")

def _clear_gst_registry():
    """Delete stale GStreamer registry cache files so the next scan starts fresh."""
    cache_dir = os.path.expanduser("~/.cache/gstreamer-1.0")
    for path in _glob.glob(os.path.join(cache_dir, "registry.*.bin")):
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
    # Distinguish between "package not installed" and "package installed but
    # the plugin won't load" so the error message is immediately actionable.
    _found_so = [_hit for _pat in _HAILO_SO_GLOB_PATTERNS for _hit in _glob.glob(_pat)]
    if not _found_so:
        # The .so file is absent from both /lib and /usr/lib — package not installed.
        print(
            f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
            "  The Hailo GStreamer plugin file is missing from this system.\n"
            "  Install the package and reboot:\n"
            "    sudo apt install hailo-all && sudo reboot"
        )
    else:
        # The .so file exists but the plugin won't load — a shared-library
        # dependency (most likely libgomp) is missing or inaccessible.
        _so_path = _found_so[0]
        print(
            f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
            "  The Hailo GStreamer plugins could not be loaded.\n"
            f"  Plugin found at: {_so_path}\n"
            "  1. Check for missing shared-library dependencies:\n"
            f"     ldd {_so_path} | grep 'not found'\n"
            "  2. If libgomp appears missing, add to ~/.bashrc and open a new terminal:\n"
            f"     export LD_PRELOAD={_LIBGOMP}\n"
            "  3. Verify the Hailo device is connected: hailortcli fw-control identify"
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