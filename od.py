# These imports must come first so that libgomp and GST_PLUGIN_PATH are
# configured BEFORE any other package (hailo, robot_brain, gi) can trigger a
# GStreamer initialisation internally.
import os, sys, platform, glob as _glob, subprocess

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
# Glob patterns at depth 0, 1, and 2 below each lib root to catch all known
# install layouts (e.g. /usr/lib/{arch}-linux-gnu/hailo/gstreamer-1.0/)
# without traversing the entire tree.  Shared by module-level setup and
# _check_hailo_plugins() so there is a single source of truth.
_HAILO_SO_GLOB_PATTERNS = (
    f"/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
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

# dpkg fallback: if the globs found no new directories, the package may have
# installed libgsthailotools.so to a non-standard path (e.g.
# /usr/lib/hailo-tappas/gstreamer/ from hailo-tappas-core).  Ask the Debian
# package manager for the authoritative install location and add it to
# GST_PLUGIN_PATH before Gst.init() is called below.
#
# _DPKG_SO_REPORTED accumulates every path that dpkg says the file should be
# at, regardless of whether the file physically exists.  This is used later by
# _check_hailo_plugins() to distinguish a "broken install" (dpkg knows the
# path but the file is absent) from a "never installed" scenario.
_DPKG_SO_REPORTED = set()
if not _new_dirs:
    try:
        _dpkg_out = subprocess.run(
            ["dpkg", "-S", "libgsthailotools.so"],
            capture_output=True, text=True, check=False, timeout=5
        ).stdout
        for _dpkg_line in _dpkg_out.splitlines():
            # Each line has the form "package-name: /absolute/path/to/file"
            if ":" not in _dpkg_line:
                continue
            _dpkg_so = _dpkg_line.split(":", 1)[1].strip()
            if not _dpkg_so:
                continue
            # Record every path dpkg reports (file may or may not exist on disk).
            _DPKG_SO_REPORTED.add(_dpkg_so)
            _dpkg_dir = os.path.dirname(_dpkg_so)
            _is_new = _dpkg_dir and _dpkg_dir not in _existing_gst_dirs
            if _is_new and os.path.isfile(_dpkg_so):
                _cur_gst = os.environ.get("GST_PLUGIN_PATH", "")
                os.environ["GST_PLUGIN_PATH"] = (
                    f"{_dpkg_dir}:{_cur_gst}" if _cur_gst else _dpkg_dir
                )
                _existing_gst_dirs.add(_dpkg_dir)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

# Increment this whenever a new version is pushed so users can confirm they
# are running the latest code after a git pull.
_VERSION = "2026.02.28-11"

# Maximum number of GST_DEBUG log lines to embed in the runtime-failure error.
_GST_DEBUG_MAX_LINES = 25
# Timeout (seconds) for gst-inspect-1.0 when collecting runtime diagnostics.
_GST_INSPECT_TIMEOUT = 20

# All remaining imports come after the environment is prepared.
import time, threading, gi, hailo, numpy as np, cv2, robot_brain as brain
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

    # Still missing after rescan.  Gather as much diagnostic information as
    # possible so the user can identify the root cause immediately.
    # Search both the known glob patterns and every directory in the current
    # GST_PLUGIN_PATH (which may include the dpkg-discovered path).
    _found_so = [_hit for _pat in _HAILO_SO_GLOB_PATTERNS for _hit in _glob.glob(_pat)]
    # Also search every directory in GST_PLUGIN_PATH (includes dpkg-discovered paths).
    for _d in [d for d in os.environ.get("GST_PLUGIN_PATH", "").split(":") if d]:
        _candidate = os.path.join(_d, "libgsthailotools.so")
        if os.path.isfile(_candidate) and _candidate not in _found_so:
            _found_so.append(_candidate)
    _gst_path = os.environ.get("GST_PLUGIN_PATH", "(not set)")

    # Ask the system-level gst-inspect-1.0 tool whether it can see the plugin.
    # This tool runs in the same environment so it is the most reliable oracle.
    try:
        _inspect_rc = subprocess.run(
            ["gst-inspect-1.0", "--exists", "hailonet"],
            capture_output=True, check=False, timeout=10
        ).returncode
        _inspect_available = (_inspect_rc == 0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _inspect_available = False

    if not _found_so:
        if _DPKG_SO_REPORTED:
            # dpkg knows where the file should be, but it is absent from disk.
            # The hailo-tappas-core package is broken (registered in dpkg's
            # database but the files were never unpacked or were later deleted).
            _dpkg_paths = "\n".join(f"    {p}" for p in sorted(_DPKG_SO_REPORTED))
            print(
                f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
                f"  GST_PLUGIN_PATH = {_gst_path}\n"
                "  dpkg reports libgsthailotools.so should be at:\n"
                f"{_dpkg_paths}\n"
                "  but none of those files exist on disk.  The hailo-tappas-core\n"
                "  package is broken (registered in dpkg but files absent).\n"
                "  Reinstall the package to restore missing files:\n"
                "    sudo apt reinstall hailo-tappas-core && sudo ldconfig\n"
                "  If that is not enough, do a full reinstall and reboot:\n"
                "    sudo apt install --reinstall hailo-all && sudo reboot"
            )
        else:
            # libgsthailotools.so not found by globs, dpkg, or GST_PLUGIN_PATH scan.
            print(
                f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
                f"  GST_PLUGIN_PATH = {_gst_path}\n"
                "  libgsthailotools.so could not be located on this system.\n"
                "  Diagnostic steps:\n"
                "  1. Check which hailo packages are installed:\n"
                "       dpkg -l | grep hailo\n"
                "  2. Find the plugin file across all installed hailo packages:\n"
                "       dpkg -S libgsthailotools.so\n"
                "  3. Search the whole filesystem as a last resort:\n"
                "       find / -name 'libgsthailotools.so' 2>/dev/null\n"
                "  If the file is genuinely missing, install and reboot:\n"
                "    sudo apt install hailo-all && sudo reboot"
            )
    elif _inspect_available:
        # gst-inspect-1.0 found the element but our in-process GStreamer didn't.
        # This usually means the plugin loaded in a clean shell but not inside
        # the current virtualenv / environment.
        _so_path = _found_so[0]
        print(
            f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
            f"  GST_PLUGIN_PATH = {_gst_path}\n"
            f"  Plugin file found at: {_so_path}\n"
            "  gst-inspect-1.0 reports the element IS available at the system level,\n"
            "  but the Python process could not load it.  Likely causes:\n"
            "  1. A stale virtualenv GStreamer cache — try deleting it:\n"
            "       rm -rf ~/.cache/gstreamer-1.0\n"
            "  2. LD_LIBRARY_PATH inside the virtualenv may be hiding a needed .so —\n"
            "     check for hailo or gstreamer entries that shadow system libraries:\n"
            f"       echo $LD_LIBRARY_PATH\n"
            "  3. Re-run without the virtualenv to confirm: python3 od.py"
        )
    else:
        # The .so file exists but the plugin won't load system-wide either.
        # Run ldd now so we can embed the specific missing libraries in the
        # error output rather than asking the user to run it manually.
        _so_path = _found_so[0]
        _run_cmd = sys.argv[0]
        _ldd_missing = []
        _ldd_stderr = ""
        try:
            _ldd_result = subprocess.run(
                ["ldd", _so_path],
                capture_output=True, text=True, check=False, timeout=15
            )
            for _ldd_line in _ldd_result.stdout.splitlines():
                if "not found" in _ldd_line:
                    _ldd_missing.append(_ldd_line.strip())
            if _ldd_result.returncode != 0 and _ldd_result.stderr.strip():
                _ldd_stderr = _ldd_result.stderr.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        _ldd_section = ""
        if _ldd_missing:
            _ldd_section = (
                "  Missing shared-library dependencies reported by ldd:\n"
                + "\n".join(f"    {ln}" for ln in _ldd_missing)
                + "\n"
            )
            _gomp_missing = any("libgomp" in ln for ln in _ldd_missing)
        elif _ldd_stderr:
            _ldd_section = f"  ldd reported an error: {_ldd_stderr}\n"
            # ldd couldn't run — fall back to checking whether libgomp exists,
            # since that is the most common reason for load failure.
            _gomp_missing = not os.path.isfile(_LIBGOMP)
        else:
            # ldd ran cleanly — all shared-library dependencies are resolved.
            # The load failure is a runtime error.  Use ctypes.CDLL() to call
            # dlopen() directly: unlike ldd it catches undefined-symbol errors
            # (ABI / version-tag mismatches inside present .so files).
            import ctypes as _ctypes
            _dlopen_error = ""
            try:
                _ctypes.CDLL(_so_path)
            except OSError as _dlopen_exc:
                _dlopen_error = str(_dlopen_exc)

            _gst_debug_lines = []
            try:
                # Build a minimal environment: keep standard path/library vars
                # and the hailo plugin path we set earlier, but avoid
                # propagating any credentials or tokens from the current env.
                _gst_debug_env = {
                    k: v for k, v in os.environ.items()
                    if k in (
                        "PATH", "HOME", "USER", "LOGNAME",
                        "LD_PRELOAD", "LD_LIBRARY_PATH",
                        "GST_PLUGIN_PATH", "GST_REGISTRY",
                        "XDG_RUNTIME_DIR", "XDG_CACHE_HOME",
                        "DBUS_SESSION_BUS_ADDRESS",
                    )
                }
                _gst_debug_env["GST_DEBUG"] = "3"
                _gst_debug_result = subprocess.run(
                    ["gst-inspect-1.0", "hailonet"],
                    capture_output=True, text=True, check=False,
                    timeout=_GST_INSPECT_TIMEOUT, env=_gst_debug_env
                )
                _gst_combined = (
                    _gst_debug_result.stdout + _gst_debug_result.stderr
                ).strip()
                if _gst_combined:
                    _gst_debug_lines = _gst_combined.splitlines()[:_GST_DEBUG_MAX_LINES]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

            _hailo_pkgs = []
            try:
                _dpkg_list_out = subprocess.run(
                    ["dpkg", "-l"],
                    capture_output=True, text=True, check=False, timeout=5
                ).stdout
                for _pl in _dpkg_list_out.splitlines():
                    _cols = _pl.split()
                    # Column layout: status, name, version, ...
                    # Only include rows whose package name starts with 'hailo'.
                    if len(_cols) >= 3 and _cols[1].startswith("hailo"):
                        _hailo_pkgs.append(f"{_cols[1]}  {_cols[2]}")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

            _ldd_section = (
                "  All shared-library dependencies are resolved (ldd: no missing libs).\n"
                "  The plugin fails to load at runtime.\n"
            )
            if _dlopen_error:
                _ldd_section += (
                    "  dlopen() error (ctypes):\n"
                    f"    {_dlopen_error}\n"
                )
            elif _gst_debug_lines:
                _ldd_section += (
                    "  GStreamer plugin diagnostics (GST_DEBUG=3):\n"
                    + "\n".join(f"    {ln}" for ln in _gst_debug_lines)
                    + "\n"
                )
            if _hailo_pkgs:
                _ldd_section += (
                    "  Installed Hailo packages:\n"
                    + "\n".join(f"    {p}" for p in _hailo_pkgs)
                    + "\n"
                )
            _gomp_missing = False

        if _gomp_missing:
            if os.path.isfile(_LIBGOMP):
                # libgomp is on disk but ldd still shows it missing — the
                # LD_PRELOAD re-exec at module init should have resolved this.
                # Most likely the re-exec ran but the GStreamer registry cache
                # still records the plugin as unloadable.  Clear it and retry.
                _gomp_hint = (
                    "  libgomp is present but the GStreamer cache may be stale.\n"
                    "  Clear the cache and re-run:\n"
                    f"    rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                )
            else:
                # libgomp.so.1 is not installed at all.
                _gomp_hint = (
                    "  libgomp (OpenMP runtime) is not installed.  Install it:\n"
                    "    sudo apt install libgomp1\n"
                    "  Then clear the GStreamer cache and re-run:\n"
                    f"    rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                )
        else:
            if not _ldd_missing and not _ldd_stderr:
                # Clean ldd — runtime dlopen failure.  Give targeted advice
                # based on whether ctypes could identify the error.
                if _dlopen_error and "undefined symbol" in _dlopen_error:
                    # Extract the symbol name for a targeted nm lookup command.
                    # dlerror format: "path.so: undefined symbol: sym_name"
                    # Sanitise to safe characters (C symbol chars + version tags)
                    # before embedding in the suggested shell command.
                    _sym_raw = _dlopen_error.rsplit("undefined symbol:", 1)[-1].strip()
                    _sym = "".join(
                        c for c in _sym_raw if c.isalnum() or c in "_@."
                    )[:120]
                    _gomp_hint = (
                        "  Undefined symbol at dlopen — the .so was built against\n"
                        "  a different libhailort ABI than the one installed.\n"
                        "  Steps to try:\n"
                        "  1. Verify the symbol exists in the installed libhailort:\n"
                        f"       nm -D /usr/lib/aarch64-linux-gnu/libhailort.so"
                        f" | grep '{_sym}'\n"
                        "  2. Reload the kernel module:\n"
                        "       sudo modprobe -r hailo_pci && sudo modprobe hailo_pci\n"
                        "  3. Re-run with a clean GStreamer cache:\n"
                        f"       rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                        "  4. If the symbol is absent, the tappas-core and hailort\n"
                        "     packages are mismatched — contact Hailo support.\n"
                    )
                elif _dlopen_error:
                    _gomp_hint = (
                        "  dlopen failed (see error above).\n"
                        "  Steps to try:\n"
                        "  1. Reload the kernel module:\n"
                        "       sudo modprobe -r hailo_pci && sudo modprobe hailo_pci\n"
                        "  2. Re-run with a clean GStreamer cache:\n"
                        f"       rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                        "  3. Check device node permissions:\n"
                        "       ls -la /dev/hailo*\n"
                    )
                else:
                    # ctypes loaded the .so fine; plugin init may be failing.
                    _gomp_hint = (
                        "  The .so loads (ctypes OK) but GStreamer cannot\n"
                        "  initialise the plugin.  Run with GST_DEBUG=5 for details:\n"
                        f"    GST_DEBUG=5 gst-inspect-1.0 hailonet 2>&1 | head -60\n"
                        "  Then reload the kernel module and retry:\n"
                        "    sudo modprobe -r hailo_pci && sudo modprobe hailo_pci\n"
                        f"    rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                    )
            else:
                # ldd found missing deps not involving libgomp (or ldd errored).
                _gomp_hint = (
                    "  If libgomp appears in the list above, set LD_PRELOAD:\n"
                    f"    export LD_PRELOAD={_LIBGOMP}\n"
                    "  Then clear the GStreamer cache and re-run:\n"
                    f"    rm -rf ~/.cache/gstreamer-1.0 && python {_run_cmd}\n"
                )

        print(
            f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
            f"  GST_PLUGIN_PATH = {_gst_path}\n"
            f"  Plugin file found at: {_so_path}\n"
            "  The plugin file exists but GStreamer cannot load it.\n"
            + _ldd_section
            + _gomp_hint
            + "  Verify the Hailo device is connected:\n"
            "    hailortcli fw-control identify"
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

def _cpu_fallback_loop():
    """CPU-only fallback: live video window with Haar-cascade face detection.

    Called when the Hailo GStreamer plugin is unavailable.  Uses the same
    libcamerasrc pipeline as face_tracking.py (no hailonet required) and
    displays detected faces in an OpenCV window so the user always gets a
    live feed with AI bounding-box indicators regardless of Hailo status.
    """
    print("--- CPU Fallback Mode: live video + Haar face detection ---")
    face_cascade = cv2.CascadeClassifier(config.HAAR_CASCADE_PATH)
    launch_str = (
        f"libcamerasrc camera-name={config.PI_CAMERA_DEVICE} ! "
        f"videoconvert ! "
        f"video/x-raw,format=RGB,width={config.FRAME_W},height={config.FRAME_H} ! "
        f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
        f"appsink name=sink emit-signals=false sync=false drop=true max-buffers=1"
    )
    try:
        pipe = Gst.parse_launch(launch_str)
    except Exception as e:
        print(f"CPU Fallback: pipeline build failed: {e}")
        return
    sink = pipe.get_by_name("sink")
    pipe.set_state(Gst.State.PLAYING)
    print("--- CPU Fallback Active: Pi5 AI Vision window (press Q to quit) ---")
    try:
        while True:
            sample = sink.emit("try-pull-sample", int(0.1 * Gst.SECOND))
            if sample is None:
                continue
            buf = sample.get_buffer()
            caps = sample.get_caps()
            struct = caps.get_structure(0)
            w = struct.get_value("width")
            h = struct.get_value("height")
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
                frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
                faces = face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=config.FACE_SCALE_FACTOR,
                    minNeighbors=config.FACE_MIN_NEIGHBORS,
                    minSize=config.FACE_MIN_SIZE,
                )
                for (fx, fy, fw, fh) in faces:
                    cv2.rectangle(frame, (fx, fy), (fx + fw, fy + fh), (0, 255, 0), 2)
                    cv2.putText(frame, "FACE", (fx, fy - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                label = f"Faces: {len(faces)}  |  Hailo: UNAVAILABLE (CPU mode)"
                cv2.putText(frame, label, (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 80, 255), 2)
                cv2.imshow("Pi5 AI Vision", frame)
            finally:
                buf.unmap(mapinfo)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        pipe.set_state(Gst.State.NULL)
        cv2.destroyAllWindows()


def camera_loop():
    # Force the device type for the Pro chip before starting
    os.environ["hailort_device_type"] = "hailo8"

    if not _check_hailo_plugins():
        _cpu_fallback_loop()
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
    print(f"--- od.py version {_VERSION} ---")
    threading.Thread(target=brain.start_brain_ui, daemon=True).start()
    camera_loop()