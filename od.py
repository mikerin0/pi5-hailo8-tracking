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

# Qt warns "wrong permissions on runtime directory … 0770 instead of 0700" when
# /run/user/<uid> is group-writable (common on Raspberry Pi OS multi-user setups).
# We create our own runtime dir in /tmp with the correct 0700 mode and point Qt
# there, preventing the noisy startup warning.  Done here – before any import
# that may call Qt/GLib init – so the variable is inherited by every sub-system.
_qt_rt = f"/tmp/runtime-{os.getuid()}"
os.makedirs(_qt_rt, mode=0o700, exist_ok=True)
# Enforce permissions even if the directory already existed, and verify ownership
# before using it (TOCTOU guard: only trust directories we own).
_qt_rt_stat = os.stat(_qt_rt)
if _qt_rt_stat.st_uid == os.getuid():
    os.chmod(_qt_rt, 0o700)
    if not os.environ.get("XDG_RUNTIME_DIR"):
        os.environ["XDG_RUNTIME_DIR"] = _qt_rt

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
#
# Two separate plugin files must be located:
#   libgsthailotools.so  – hailo-tappas-core package (hailofilter, hailooverlay, hailotracker)
#   libgsthailo.so       – hailort package            (hailonet)
# Both must be in GST_PLUGIN_PATH for the full pipeline to work.
_HAILO_SO_GLOB_PATTERNS = (
    # hailo-tappas-core: hailofilter, hailooverlay, hailotracker
    f"/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailotools.so",
    f"/usr/lib/{_arch}-linux-gnu/*/*/libgsthailotools.so",
    # hailort: hailonet
    f"/lib/{_arch}-linux-gnu/libgsthailo.so",
    f"/lib/{_arch}-linux-gnu/*/libgsthailo.so",
    f"/lib/{_arch}-linux-gnu/*/*/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/*/libgsthailo.so",
    f"/usr/lib/{_arch}-linux-gnu/*/*/libgsthailo.so",
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

# hailonet lives in libgsthailo.so (hailort package), which is a separate file
# from libgsthailotools.so (hailo-tappas-core).  Its directory may differ from
# the tappas path and might not have been found by the globs above (e.g. if
# libgsthailotools.so is already at a standard path so _new_dirs was non-empty
# and the dpkg fallback above was skipped).  Search for libgsthailo.so
# unconditionally so hailonet's directory is always added when needed.
try:
    _dpkg_hailo_out = subprocess.run(
        ["dpkg", "-S", "libgsthailo.so"],
        capture_output=True, text=True, check=False, timeout=5
    ).stdout
    for _dpkg_line in _dpkg_hailo_out.splitlines():
        if ":" not in _dpkg_line:
            continue
        _dpkg_so = _dpkg_line.split(":", 1)[1].strip()
        if not _dpkg_so:
            continue
        _DPKG_SO_REPORTED.add(_dpkg_so)
        _dpkg_dir = os.path.dirname(_dpkg_so)
        _cur_gst = os.environ.get("GST_PLUGIN_PATH", "")
        _cur_gst_dirs = set(filter(None, _cur_gst.split(":")))
        if _dpkg_dir and _dpkg_dir not in _cur_gst_dirs and os.path.isfile(_dpkg_so):
            os.environ["GST_PLUGIN_PATH"] = (
                f"{_dpkg_dir}:{_cur_gst}" if _cur_gst else _dpkg_dir
            )
except (FileNotFoundError, subprocess.TimeoutExpired):
    pass

# Increment this whenever a new version is pushed so users can confirm they
# are running the latest code after a git pull.
_VERSION = "2026.03.01-03"

# Maximum number of GST_DEBUG log lines to embed in the runtime-failure error.
_GST_DEBUG_MAX_LINES = 25
# Timeout (seconds) for gst-inspect-1.0 when collecting runtime diagnostics.
_GST_INSPECT_TIMEOUT = 20

# All remaining imports come after the environment is prepared.
import time, threading, gi, hailo, numpy as np, cv2, robot_brain as brain
import config
import servo_arm_integration as servo_integration
try:
    import mediapipe as mp
except Exception:
    mp = None

gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

_startup_t0 = time.monotonic()


def _startup_log(message):
    if not bool(getattr(config, "STARTUP_DEBUG_TIMESTAMPS", False)):
        return
    dt = time.monotonic() - _startup_t0
    print(f"[STARTUP +{dt:7.3f}s] {message}")


def _wait_for_space(step_label):
    """Block until user presses SPACE then Enter (interactive terminals)."""
    prompts_enabled = bool(getattr(config, "STARTUP_STEP_PROMPTS_ENABLED", False))
    if not prompts_enabled:
        return
    if not sys.stdin or not sys.stdin.isatty():
        print(f"{step_label}: non-interactive stdin, continuing without prompt")
        return

    while True:
        try:
            reply = input(f"{step_label} — press SPACE then Enter to continue: ")
        except EOFError:
            print(f"{step_label}: input unavailable, continuing")
            return
        if reply == " ":
            return
        print("Input ignored. Press SPACE then Enter to continue.")


def _seed_brain_ik_from_servo_readback(retry_sec=0.0, retry_interval_sec=0.25):
    """Align brain.last_angles with physical servo pose before first IK move."""
    deadline = time.monotonic() + max(0.0, float(retry_sec))
    interval = max(0.05, float(retry_interval_sec))
    attempt = 0

    while True:
        attempt += 1
        try:
            readback = {
                6: servo_integration.controller.read_position(6, fast=True),
                5: servo_integration.controller.read_position(5, fast=True),
                4: servo_integration.controller.read_position(4, fast=True),
                3: servo_integration.controller.read_position(3, fast=True),
            }
        except Exception as e:
            _startup_log(f"IK seed: readback failed attempt {attempt}: {e}")
            readback = None

        if isinstance(readback, dict):
            try:
                p6 = readback.get(6)
                p5 = readback.get(5)
                p4 = readback.get(4)
                p3 = readback.get(3)
                if None not in (p6, p5, p4, p3):
                    a1 = (float(p6) - 1500.0) / 637.0
                    a2 = (float(p5) - 1500.0) / 637.0
                    a3 = (float(p4) - 1500.0) / 637.0
                    a4 = (float(p3) - 1500.0) / 637.0
                    brain.last_angles = [0.0, a1, a2, a3, a4]
                    _startup_log(f"IK seed: last_angles set from readback {readback} (attempt {attempt})")
                    return True
                _startup_log(f"IK seed: incomplete readback attempt {attempt}: {readback}")
            except Exception as e:
                _startup_log(f"IK seed: conversion failed attempt {attempt}: {e}")

        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _seed_brain_ik_from_last_commanded():
    """Fallback IK seeding from last commanded servo pulses."""
    try:
        commanded = servo_integration.get_last_commanded_pose() or {}
    except Exception as e:
        _startup_log(f"IK seed (commanded): failed: {e}")
        return False

    try:
        p6 = commanded.get(6)
        p5 = commanded.get(5)
        p4 = commanded.get(4)
        p3 = commanded.get(3)
        if None in (p6, p5, p4, p3):
            _startup_log(f"IK seed (commanded): incomplete {commanded}")
            return False
        a1 = (float(p6) - 1500.0) / 637.0
        a2 = (float(p5) - 1500.0) / 637.0
        a3 = (float(p4) - 1500.0) / 637.0
        a4 = (float(p3) - 1500.0) / 637.0
        brain.last_angles = [0.0, a1, a2, a3, a4]
        _startup_log(f"IK seed (commanded): last_angles set from {commanded}")
        return True
    except Exception as e:
        _startup_log(f"IK seed (commanded): conversion failed: {e}")
        return False

_HAILO_ELEMENTS = ("hailonet", "hailofilter", "hailooverlay", "hailotracker")

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
        for _so_name in ("libgsthailotools.so", "libgsthailo.so"):
            _candidate = os.path.join(_d, _so_name)
            if os.path.isfile(_candidate) and _candidate not in _found_so:
                _found_so.append(_candidate)
    _gst_path = os.environ.get("GST_PLUGIN_PATH", "(not set)")

    # Special case: hailonet is in libgsthailo.so (hailort package), not in
    # libgsthailotools.so (hailo-tappas-core).  If hailonet is missing but
    # libgsthailo.so is nowhere on disk, give a targeted diagnosis pointing to
    # the correct package instead of running ldd against the wrong file.
    _found_hailo_so = [p for p in _found_so if os.path.basename(p) == "libgsthailo.so"]
    if "hailonet" in missing and not _found_hailo_so:
        _dpkg_paths = "\n".join(f"    {p}" for p in sorted(_DPKG_SO_REPORTED))
        if _dpkg_paths:
            print(
                f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
                f"  GST_PLUGIN_PATH = {_gst_path}\n"
                "  libgsthailo.so (hailort package, provides 'hailonet') not found.\n"
                "  dpkg reports it should be at:\n"
                f"{_dpkg_paths}\n"
                "  but the file is absent from disk.  Reinstall hailort:\n"
                "    sudo apt reinstall hailort hailo-all && sudo ldconfig && sudo reboot"
            )
        else:
            print(
                f"ERROR: GStreamer element(s) not found: {', '.join(missing)}\n"
                f"  GST_PLUGIN_PATH = {_gst_path}\n"
                "  libgsthailo.so (hailort package, provides 'hailonet') could not\n"
                "  be located on this system.  The hailort GStreamer plugin is\n"
                "  separate from hailo-tappas-core; it may not have been installed.\n"
                "  Diagnostic steps:\n"
                "  1. Check if hailort is installed:\n"
                "       dpkg -l | grep hailo\n"
                "  2. Find the plugin file:\n"
                "       dpkg -S libgsthailo.so\n"
                "       find / -name 'libgsthailo.so' 2>/dev/null\n"
                "  3. Install/reinstall the full stack and reboot:\n"
                "       sudo apt install hailo-all && sudo ldconfig && sudo reboot"
            )
        return False

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

# --- Pose tracking state ---
_smooth_x = 0.5
_smooth_y = 0.5
_last_move_time = 0.0
_tracking_target = config.TRACKING_TARGET  # "nose" | "left_hand" | "right_hand"
# False = actively tracking; True = standby (person lost); "MANUAL" = manual override
_search_mode = False
_last_seen_time = 0.0
_last_table_release_time = 0.0
_overlay_frame_w = config.MODEL_INPUT_SIZE
_overlay_frame_h = config.MODEL_INPUT_SIZE
_table_release_hits = 0
_last_pose_event_times = {}
_pose_state_streak = {"LEFT": 0, "RIGHT": 0, "BOTH": 0}
_suppress_single_until = 0.0
_pose_latched = False
_pose_neutral_streak = 0
_last_pose_debug_log_time = 0.0
_last_pose_debug_state = None
_mp_hands = None
_last_finger_event_times = {}
_finger_state_streak = {"ONE": 0, "TWO": 0}
_table_obj_hits = 0
_last_table_obj_trigger_time = 0.0
_last_table_obj_block_log_time = 0.0
_table_obj_center_hits = 0
_last_table_obj_align_log_time = 0.0
_table_cam_enter_time = 0.0
_person_lost_park_in_progress = False
_last_table_pick_steer_time = 0.0
_table_pick_steer_lock = threading.Lock()
_table_pick_steer_target = None
_table_pick_steer_stop = threading.Event()
_table_pick_steer_thread = None
_last_table_pick_target_update = 0.0
_vision_summary_lock = threading.Lock()
_vision_summary_state = {
    "updated_at": 0.0,
    "mode": "HIGH_CAM",
    "labels": {},
}
_tracking_prev_busy = 1
_tracking_resume_warmup_until = 0.0


def _build_camera_src_segment(mode, cam_path):
    backend = str(getattr(config, "CAMERA_BACKEND", "libcamera")).strip().lower()
    if backend == "v4l2":
        if mode == "TABLE_CAM":
            dev = str(getattr(config, "ARDUCAM_V4L2_DEVICE", "/dev/video1"))
        else:
            dev = str(getattr(config, "PI_CAMERA_V4L2_DEVICE", "/dev/video0"))
        return (
            f"v4l2src device={dev} ! "
            f"video/x-raw,width={config.CAM_SENSOR_W},height={config.CAM_SENSOR_H} ! "
        )

    return (
        f"libcamerasrc camera-name={cam_path} ! "
        f"video/x-raw,format=NV12,width={config.CAM_SENSOR_W},height={config.CAM_SENSOR_H} ! "
    )


def _update_vision_summary(labels, mode=None):
    now = time.time()
    mode_name = mode or brain.tuner.shared_params.get("camera_mode", "HIGH_CAM")
    norm = {}
    for raw_label in labels or []:
        label = str(raw_label or "").strip().lower()
        if not label:
            continue
        norm[label] = int(norm.get(label, 0)) + 1
    with _vision_summary_lock:
        _vision_summary_state["updated_at"] = now
        _vision_summary_state["mode"] = str(mode_name)
        _vision_summary_state["labels"] = norm


def _extract_detection_labels(detections):
    labels = []
    for det in detections or []:
        try:
            labels.append(det.get_label())
        except Exception:
            continue
    return labels


def _maybe_update_table_pick_approach_pose(take_x, take_y, take_lift_z, now):
    """Queue TABLE PICK approach updates without blocking camera callback thread."""
    global _last_table_pick_steer_time, _table_pick_steer_target, _last_table_pick_target_update
    if not bool(brain.tuner.shared_params.get("table_pick_request_active", 0)):
        return
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
        return
    if brain.tuner.shared_params.get("busy", 0) != 0 or brain.is_holding_item():
        return

    min_period = max(0.12, float(getattr(config, "TABLE_PICK_STEER_PERIOD_SEC", 0.25)))
    if (now - _last_table_pick_steer_time) < min_period:
        return

    with _table_pick_steer_lock:
        _table_pick_steer_target = (float(take_x), float(take_y), float(take_lift_z))
        _last_table_pick_target_update = now
    _last_table_pick_steer_time = now


def _table_pick_steer_worker():
    """Send low-rate arm steering updates for TABLE PICK on a background thread."""
    global _table_pick_steer_target
    last_sent = None
    while not _table_pick_steer_stop.is_set() and not brain.shutdown_event.is_set():
        now = time.time()
        if not bool(brain.tuner.shared_params.get("table_pick_request_active", 0)):
            with _table_pick_steer_lock:
                _table_pick_steer_target = None
            last_sent = None
            time.sleep(0.05)
            continue
        if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
            time.sleep(0.05)
            continue
        if brain.tuner.shared_params.get("busy", 0) != 0 or brain.is_holding_item():
            time.sleep(0.05)
            continue

        with _table_pick_steer_lock:
            target = _table_pick_steer_target
        if target is None:
            if bool(getattr(config, "TABLE_PICK_MANUAL_HUNT_ENABLED", True)):
                stale_after = max(0.2, float(getattr(config, "TABLE_PICK_STEER_PERIOD_SEC", 0.25)) * 2.0)
                age = now - float(_last_table_pick_target_update)
                if age >= stale_after:
                    p = brain.tuner.get_params()
                    base_x = max(0.12, min(0.28, float(p.get("take_x", 0.16))))
                    base_z = max(0.18, min(0.45, float(p.get("take_lift_z", 0.36))))
                    amp = max(0.0, min(0.11, float(getattr(config, "TABLE_PICK_MANUAL_HUNT_AMPLITUDE_Y", 0.07))))
                    period = max(1.0, float(getattr(config, "TABLE_PICK_MANUAL_HUNT_PERIOD_SEC", 3.0)))
                    phase = (now % period) / period
                    hunt_y = amp * np.sin(2.0 * np.pi * phase)
                    target = (base_x, hunt_y, base_z)
                else:
                    time.sleep(0.05)
                    continue
            else:
                time.sleep(0.05)
                continue
        tx, ty, tz = target
        if last_sent is not None:
            if abs(tx - last_sent[0]) < 0.002 and abs(ty - last_sent[1]) < 0.002 and abs(tz - last_sent[2]) < 0.002:
                time.sleep(0.04)
                continue

        speed = int(getattr(config, "TABLE_PICK_STEER_SPEED", 800))
        try:
            use_stepped = bool(getattr(config, "TABLE_PICK_STEER_USE_STEPPED_IK", False))
            if use_stepped:
                moved = bool(brain.reach_for_manual_coordinate(tx, ty, tz, speed=speed))
                if not moved:
                    brain.reach_for_coordinate(tx, ty, tz, speed=speed)
            else:
                brain.reach_for_coordinate(tx, ty, tz, speed=speed)
            last_sent = (tx, ty, tz)
        except Exception as e:
            print(f"TABLE_PICK steer skipped: {e}")
        time.sleep(0.04)


def _start_table_pick_steer_worker():
    global _table_pick_steer_thread
    if _table_pick_steer_thread and _table_pick_steer_thread.is_alive():
        return
    _table_pick_steer_stop.clear()
    _table_pick_steer_thread = threading.Thread(target=_table_pick_steer_worker, daemon=True, name="TablePickSteer")
    _table_pick_steer_thread.start()


def _stop_table_pick_steer_worker():
    global _table_pick_steer_thread
    _table_pick_steer_stop.set()
    if _table_pick_steer_thread and _table_pick_steer_thread.is_alive():
        _table_pick_steer_thread.join(timeout=1.0)
    _table_pick_steer_thread = None


def update_table_model_paths(hef_path, so_path):
    """Update TABLE_CAM model paths at runtime and request pipeline restart."""
    hef = os.path.abspath(str(hef_path or "").strip())
    so = os.path.abspath(str(so_path or "").strip())
    if not hef or not os.path.isfile(hef):
        return False, f"Invalid HEF path: {hef or '(empty)'}"
    if not so or not os.path.isfile(so):
        return False, f"Invalid postprocess .so path: {so or '(empty)'}"

    config.TABLE_OBJECT_HEF_PATH = hef
    config.TABLE_OBJECT_SO_PATH = so
    config.TABLE_OBJECT_MODEL_ENABLED = True
    config.TABLE_OBJECT_PICKUP_ENABLED = True

    _restart_event.set()
    return True, "Table model updated; restarting camera pipeline"


def arm_table_pick_tracking():
    """Arm TABLE_CAM object tracking so pickup starts after stable alignment."""
    global _table_obj_hits, _table_obj_center_hits
    global _last_table_obj_trigger_time, _last_table_obj_block_log_time, _last_table_obj_align_log_time
    global _table_cam_enter_time

    config.TABLE_OBJECT_PICKUP_ENABLED = True
    _table_obj_hits = 0
    _table_obj_center_hits = 0
    _last_table_obj_trigger_time = 0.0
    _last_table_obj_block_log_time = 0.0
    _last_table_obj_align_log_time = 0.0

    arm_delay = max(0.0, float(getattr(config, "TABLE_OBJECT_ARM_DELAY_SEC", 4.0)))
    _table_cam_enter_time = time.time() - arm_delay - 0.1
    return "TABLE PICK armed: tracking object for alignment before grab"


def get_vision_summary_text():
    with _vision_summary_lock:
        updated_at = float(_vision_summary_state.get("updated_at", 0.0))
        mode = str(_vision_summary_state.get("mode", "HIGH_CAM"))
        labels = dict(_vision_summary_state.get("labels", {}))

    age = max(0.0, time.time() - updated_at)
    if not labels:
        if age > 3.0:
            return f"I do not see anything right now on {mode}."
        return f"I do not currently detect objects on {mode}."

    ordered = sorted(labels.items(), key=lambda item: (-int(item[1]), item[0]))
    top_items = ordered[:4]
    parts = []
    for name, count in top_items:
        if int(count) > 1:
            parts.append(f"{name} x{int(count)}")
        else:
            parts.append(name)
    summary = ", ".join(parts)
    if age > 2.5:
        return f"Last seen on {mode}: {summary}."
    return f"I see {summary} on {mode}."


def _detection_bbox_center_norm(detection):
    """Best-effort extraction of normalized bbox center/area from Hailo detection."""
    try:
        bbox = detection.get_bbox()
    except Exception:
        return None
    if bbox is None:
        return None

    def _get_num(obj, names):
        for name in names:
            attr = getattr(obj, name, None)
            if callable(attr):
                try:
                    return float(attr())
                except Exception:
                    continue
            if attr is not None:
                try:
                    return float(attr)
                except Exception:
                    continue
        return None

    x = _get_num(bbox, ("xmin", "x_min", "left", "x"))
    y = _get_num(bbox, ("ymin", "y_min", "top", "y"))
    w = _get_num(bbox, ("width", "w"))
    h = _get_num(bbox, ("height", "h"))

    if x is not None and y is not None and w is not None and h is not None:
        cx = x + (w * 0.5)
        cy = y + (h * 0.5)
        area = max(0.0, w * h)
        return (
            max(0.0, min(1.0, cx)),
            max(0.0, min(1.0, cy)),
            area,
        )

    x2 = _get_num(bbox, ("xmax", "x_max", "right"))
    y2 = _get_num(bbox, ("ymax", "y_max", "bottom"))
    if x is not None and y is not None and x2 is not None and y2 is not None:
        cx = (x + x2) * 0.5
        cy = (y + y2) * 0.5
        area = max(0.0, (x2 - x) * (y2 - y))
        return (
            max(0.0, min(1.0, cx)),
            max(0.0, min(1.0, cy)),
            area,
        )
    return None


def _table_object_model_callback(_pad, info, _user_data):
    """Model-based TABLE_CAM detection callback using Hailo detection objects."""
    global _table_obj_hits, _table_obj_center_hits
    global _last_table_obj_trigger_time, _last_table_obj_block_log_time, _last_table_obj_align_log_time

    if not bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False)):
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK

    now = time.time()
    arm_delay = max(0.0, float(getattr(config, "TABLE_OBJECT_ARM_DELAY_SEC", 4.0)))
    if now - _table_cam_enter_time < arm_delay:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK
    if brain.tuner.shared_params.get("busy", 0) != 0 or brain.is_holding_item():
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK

    cooldown = max(2.0, float(getattr(config, "TABLE_OBJECT_COOLDOWN_SEC", 20.0)))
    if now - _last_table_obj_trigger_time < cooldown:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK

    buffer = info.get_buffer()
    if not buffer:
        return Gst.PadProbeReturn.OK

    try:
        roi = hailo.get_roi_from_buffer(buffer)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    except Exception:
        return Gst.PadProbeReturn.OK

    _update_vision_summary(_extract_detection_labels(detections), mode="TABLE_CAM")

    target_label = str(
        brain.tuner.shared_params.get(
            "table_object_target_label",
            getattr(config, "TABLE_OBJECT_TARGET_LABEL", ""),
        )
    ).strip().lower()
    manual_pick_active = bool(brain.tuner.shared_params.get("table_pick_request_active", 0))
    if manual_pick_active and bool(getattr(config, "TABLE_PICK_IGNORE_LABEL_FILTER", True)):
        target_label = ""
    min_conf = float(getattr(config, "TABLE_OBJECT_MIN_CONFIDENCE", 0.35))
    if manual_pick_active:
        min_conf = min(min_conf, float(getattr(config, "TABLE_PICK_MANUAL_MIN_CONFIDENCE", 0.20)))

    best = None
    best_conf = -1.0
    best_label = ""
    best_center = None
    for det in detections:
        try:
            label = str(det.get_label()).strip().lower()
        except Exception:
            label = ""
        if target_label and label != target_label:
            continue

        conf = 1.0
        cattr = getattr(det, "get_confidence", None)
        if callable(cattr):
            try:
                conf = float(cattr())
            except Exception:
                conf = 1.0
        if conf < min_conf:
            continue

        center = _detection_bbox_center_norm(det)
        if center is None:
            continue
        if conf > best_conf:
            best = det
            best_conf = conf
            best_label = label
            best_center = center

    if best is None or best_center is None:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK

    x_norm, y_norm, area_norm = best_center
    x_min = float(getattr(config, "TABLE_OBJECT_X_MIN_NORM", 0.25))
    x_max = float(getattr(config, "TABLE_OBJECT_X_MAX_NORM", 0.75))
    y_min = float(getattr(config, "TABLE_OBJECT_Y_MIN_NORM", 0.45))
    max_area_frac = float(getattr(config, "TABLE_OBJECT_MAX_AREA_FRAC", 0.80))
    if manual_pick_active:
        x_min, x_max, y_min, max_area_frac = 0.0, 1.0, 0.0, 0.98
    if not (x_min <= x_norm <= x_max and y_norm >= y_min and area_norm <= max_area_frac):
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return Gst.PadProbeReturn.OK

    _table_obj_hits += 1
    frames_required = max(1, int(getattr(config, "TABLE_OBJECT_FRAMES_REQUIRED", 3)))
    if manual_pick_active:
        frames_required = max(1, int(getattr(config, "TABLE_PICK_MANUAL_FRAMES_REQUIRED", 1)))
    if _table_obj_hits < frames_required:
        return Gst.PadProbeReturn.OK

    p = brain.tuner.get_params()
    y_gain = float(getattr(config, "TABLE_OBJECT_Y_GAIN", 0.24))
    x_bias_gain = float(getattr(config, "TABLE_OBJECT_X_BIAS_GAIN", 0.03))
    center_x = float(getattr(config, "TABLE_OBJECT_CENTER_X_NORM", 0.50))
    center_y = float(getattr(config, "TABLE_OBJECT_CENTER_Y_NORM", 0.62))
    center_tol = float(getattr(config, "TABLE_OBJECT_CENTER_TOL_NORM", 0.10))
    align_alpha = float(getattr(config, "TABLE_OBJECT_ALIGN_ALPHA", 0.35))
    center_frames_required = max(1, int(getattr(config, "TABLE_OBJECT_CENTER_FRAMES_REQUIRED", 4)))
    if manual_pick_active:
        center_frames_required = max(1, int(getattr(config, "TABLE_PICK_MANUAL_CENTER_FRAMES_REQUIRED", 1)))
        center_tol = max(center_tol, float(getattr(config, "TABLE_PICK_MANUAL_CENTER_TOL_NORM", 0.20)))

    target_take_y = max(-0.12, min(0.12, (center_x - x_norm) * y_gain))
    target_take_x = float(p.get("take_x", 0.16)) + ((center_y - y_norm) * x_bias_gain)
    target_take_x = max(0.12, min(0.28, target_take_x))

    prev_take_x = float(p.get("take_x", 0.16))
    prev_take_y = float(p.get("take_y", 0.0))
    take_x = prev_take_x + (target_take_x - prev_take_x) * max(0.05, min(1.0, align_alpha))
    take_y = prev_take_y + (target_take_y - prev_take_y) * max(0.05, min(1.0, align_alpha))

    err = ((x_norm - center_x) ** 2 + (y_norm - center_y) ** 2) ** 0.5
    if err <= max(0.02, center_tol):
        _table_obj_center_hits += 1
    else:
        _table_obj_center_hits = 0

    if now - _last_table_obj_align_log_time > 1.0:
        print(
            "TABLE_CAM model align: "
            f"label={best_label or 'unknown'} conf={best_conf:.2f} "
            f"x={x_norm:.2f} y={y_norm:.2f} err={err:.3f} "
            f"center_hits={_table_obj_center_hits}/{center_frames_required}"
        )
        _last_table_obj_align_log_time = now

    take_z = float(getattr(config, "TABLE_OBJECT_TAKE_Z", 0.27))
    take_z = max(0.18, min(0.40, take_z))
    take_lift_z = max(take_z + 0.05, min(0.45, float(p.get("take_lift_z", 0.36))))
    brain.tuner.shared_params["take_x"] = take_x
    brain.tuner.shared_params["take_y"] = take_y
    brain.tuner.shared_params["take_z"] = take_z
    brain.tuner.shared_params["take_lift_z"] = take_lift_z
    _maybe_update_table_pick_approach_pose(take_x, take_y, take_lift_z, now)

    if _table_obj_center_hits < center_frames_required:
        return Gst.PadProbeReturn.OK

    print(
        "TABLE_CAM model object detected: "
        f"label={best_label or 'unknown'} conf={best_conf:.2f} "
        f"x={x_norm:.2f} y={y_norm:.2f} -> starting pickup"
    )
    brain.tuner.shared_params["table_pick_request_active"] = 0
    brain.start_take_item_sequence(auto_pick=True)
    _last_table_obj_trigger_time = now
    _table_obj_hits = 0
    _table_obj_center_hits = 0
    return Gst.PadProbeReturn.OK


def _person_lost_park_async():
    global _person_lost_park_in_progress
    if _person_lost_park_in_progress:
        return
    park_cb = getattr(brain, "thermal_park_callback", None)
    if not callable(park_cb):
        print("WARNING: person-lost park callback unavailable")
        return

    _person_lost_park_in_progress = True

    def _worker():
        global _person_lost_park_in_progress
        try:
            park_cb()
            print("Person-lost safety park complete")
        except Exception as e:
            print(f"Person-lost safety park failed: {e}")
        finally:
            _person_lost_park_in_progress = False

    threading.Thread(target=_worker, daemon=True).start()


def _point_confidence(point):
    """Best-effort confidence extraction across Hailo point API variants."""
    for name in ("confidence", "score", "probability"):
        member = getattr(point, name, None)
        if callable(member):
            try:
                return float(member())
            except Exception:
                pass
    return 1.0


def _pose_event_allowed(event_name, now):
    cooldown = max(0.2, float(getattr(config, "POSE_GESTURE_COOLDOWN_SEC", 1.5)))
    last_t = _last_pose_event_times.get(event_name, 0.0)
    if now - last_t < cooldown:
        return False
    _last_pose_event_times[event_name] = now
    return True


def _finger_event_allowed(event_name, now):
    cooldown = max(0.2, float(getattr(config, "FINGER_GESTURE_COOLDOWN_SEC", 1.0)))
    last_t = _last_finger_event_times.get(event_name, 0.0)
    if now - last_t < cooldown:
        return False
    _last_finger_event_times[event_name] = now
    return True


def _get_hands_detector():
    global _mp_hands
    if _mp_hands is not None:
        return _mp_hands
    if mp is None:
        return None
    try:
        _mp_hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=float(getattr(config, "FINGER_GESTURE_MIN_DET_CONF", 0.55)),
            min_tracking_confidence=float(getattr(config, "FINGER_GESTURE_MIN_TRACK_CONF", 0.55)),
        )
    except Exception as e:
        print(f"Finger gesture detector unavailable: {e}")
        _mp_hands = None
    return _mp_hands


def _classify_finger_count_gesture(hand_landmarks):
    """Return 'ONE', 'TWO', or None using simple fingertip-vs-PIP geometry."""
    lm = hand_landmarks.landmark
    y_margin = float(getattr(config, "FINGER_GESTURE_Y_MARGIN", 0.02))

    index_up = lm[8].y < (lm[6].y - y_margin)
    middle_up = lm[12].y < (lm[10].y - y_margin)
    ring_up = lm[16].y < (lm[14].y - y_margin)
    pinky_up = lm[20].y < (lm[18].y - y_margin)

    # "Down" uses a softer threshold to reduce false negatives when fingers are curled.
    ring_down = lm[16].y > (lm[14].y + y_margin * 0.5)
    pinky_down = lm[20].y > (lm[18].y + y_margin * 0.5)

    if index_up and not middle_up and ring_down and pinky_down:
        return "ONE"
    if index_up and middle_up and ring_down and pinky_down:
        return "TWO"
    return None


def _maybe_send_finger_gesture_events_from_sample(sample, now):
    """Run MediaPipe Hands on side-branch sample and emit one/two-finger events."""
    if not bool(getattr(config, "FINGER_GESTURE_EVENTS_ENABLED", True)):
        return
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "HIGH_CAM":
        return

    hands = _get_hands_detector()
    if hands is None:
        return

    buf = sample.get_buffer()
    caps = sample.get_caps()
    struct = caps.get_structure(0)
    w = struct.get_value("width")
    h = struct.get_value("height")
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return
    try:
        frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        result = hands.process(frame_rgb)
    except Exception:
        return
    finally:
        buf.unmap(mapinfo)

    state = None
    if result and result.multi_hand_landmarks:
        state = _classify_finger_count_gesture(result.multi_hand_landmarks[0])

    for key in ("ONE", "TWO"):
        if state == key:
            _finger_state_streak[key] += 1
        else:
            _finger_state_streak[key] = 0

    frames_required = max(1, int(getattr(config, "FINGER_GESTURE_FRAMES_REQUIRED", 3)))
    if state == "ONE" and _finger_state_streak["ONE"] >= frames_required:
        if _finger_event_allowed("ONE_FINGER_UP", now):
            brain.send_to_crestron("ONE_FINGER_UP")
            if bool(getattr(config, "FINGER_GESTURE_DEBUG", False)):
                print("Finger gesture: ONE_FINGER_UP")
            _finger_state_streak["ONE"] = 0
    elif state == "TWO" and _finger_state_streak["TWO"] >= frames_required:
        if _finger_event_allowed("TWO_FINGERS_UP", now):
            brain.send_to_crestron("TWO_FINGERS_UP")
            if bool(getattr(config, "FINGER_GESTURE_DEBUG", False)):
                print("Finger gesture: TWO_FINGERS_UP")
            _finger_state_streak["TWO"] = 0


def _maybe_send_pose_gesture_events(points, now):
    """Send coarse hand-raise gesture events to Crestron using pose keypoints."""
    global _suppress_single_until, _pose_latched, _pose_neutral_streak
    global _last_pose_debug_log_time, _last_pose_debug_state
    if not bool(getattr(config, "POSE_GESTURE_EVENTS_ENABLED", True)):
        return
    # Allow outbound gesture events in HIGH_CAM and DUAL_CAM.
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") not in ("HIGH_CAM", "DUAL_CAM"):
        return

    min_conf = max(0.0, min(1.0, float(getattr(config, "POSE_GESTURE_MIN_CONFIDENCE", 0.45))))
    y_margin = max(0.0, float(getattr(config, "POSE_GESTURE_Y_MARGIN", 0.05)))
    mirror_lr = bool(getattr(config, "POSE_GESTURE_MIRROR_LEFT_RIGHT", True))
    frames_required = max(1, int(getattr(config, "POSE_GESTURE_FRAMES_REQUIRED", 2)))
    suppress_sec = max(0.0, float(getattr(config, "POSE_GESTURE_BOTH_SUPPRESS_SEC", 0.9)))
    reset_frames = max(1, int(getattr(config, "POSE_GESTURE_RESET_FRAMES", 2)))
    debug_enabled = bool(round(float(
        brain.tuner.shared_params.get(
            "pose_gesture_debug",
            1.0 if getattr(config, "POSE_GESTURE_DEBUG", False) else 0.0,
        )
    )))
    debug_interval = max(0.1, float(getattr(config, "POSE_GESTURE_DEBUG_LOG_INTERVAL_SEC", 0.5)))

    def _debug_log(state_name):
        nonlocal now
        global _last_pose_debug_log_time, _last_pose_debug_state
        if not debug_enabled:
            return
        if state_name != _last_pose_debug_state or (now - _last_pose_debug_log_time) >= debug_interval:
            print(f"Gesture state: {state_name} (latched={int(_pose_latched)}, neutral={_pose_neutral_streak})")
            _last_pose_debug_state = state_name
            _last_pose_debug_log_time = now

    if points is None:
        _debug_log("NONE")
        _pose_neutral_streak += 1
        if _pose_neutral_streak >= reset_frames:
            _pose_latched = False
            _pose_state_streak["LEFT"] = 0
            _pose_state_streak["RIGHT"] = 0
            _pose_state_streak["BOTH"] = 0
        return

    # COCO keypoints for yolov8 pose
    left_shoulder_idx = 5
    right_shoulder_idx = 6
    left_wrist_idx = config.KEYPOINTS.get("left_hand", 9)
    right_wrist_idx = config.KEYPOINTS.get("right_hand", 10)

    idxs = [left_shoulder_idx, right_shoulder_idx, left_wrist_idx, right_wrist_idx]
    if any(i >= len(points) for i in idxs):
        _debug_log("NONE")
        _pose_neutral_streak += 1
        if _pose_neutral_streak >= reset_frames:
            _pose_latched = False
        return

    ls, rs = points[left_shoulder_idx], points[right_shoulder_idx]
    lw, rw = points[left_wrist_idx], points[right_wrist_idx]
    ls_conf = _point_confidence(ls)
    rs_conf = _point_confidence(rs)
    lw_conf = _point_confidence(lw)
    rw_conf = _point_confidence(rw)

    left_valid = ls_conf >= min_conf and lw_conf >= min_conf
    right_valid = rs_conf >= min_conf and rw_conf >= min_conf

    left_raised = left_valid and (lw.y() < (ls.y() - y_margin))
    right_raised = right_valid and (rw.y() < (rs.y() - y_margin))

    state = "NONE"
    if left_raised and right_raised:
        state = "BOTH"
    elif left_raised:
        state = "LEFT"
    elif right_raised:
        state = "RIGHT"
    _debug_log(state)

    # Update streak counters for simple temporal smoothing.
    for key in ("LEFT", "RIGHT", "BOTH"):
        if state == key:
            _pose_state_streak[key] += 1
        else:
            _pose_state_streak[key] = 0

    # Latch reset: require a short neutral period before allowing re-trigger.
    if state == "NONE":
        _pose_neutral_streak += 1
        if _pose_neutral_streak >= reset_frames:
            _pose_latched = False
        return
    _pose_neutral_streak = 0

    # One-shot behavior: once any gesture fires, do not emit again until
    # we see a neutral window (state == NONE for reset_frames).
    if _pose_latched:
        return

    if state == "BOTH":
        if _pose_state_streak["BOTH"] >= frames_required and _pose_event_allowed("BOTH_HANDS_UP", now):
            brain.send_to_crestron("BOTH_HANDS_UP")
            _suppress_single_until = now + suppress_sec
            _pose_latched = True
            _pose_state_streak["LEFT"] = 0
            _pose_state_streak["RIGHT"] = 0
        return

    if state in ("LEFT", "RIGHT") and now < _suppress_single_until:
        return

    if state == "LEFT":
        if _pose_state_streak["LEFT"] < frames_required:
            return
        event_name = "RIGHT_HAND_UP" if mirror_lr else "LEFT_HAND_UP"
        if _pose_event_allowed(event_name, now):
            brain.send_to_crestron(event_name)
            _pose_latched = True
        return
    if state == "RIGHT":
        if _pose_state_streak["RIGHT"] < frames_required:
            return
        event_name = "LEFT_HAND_UP" if mirror_lr else "RIGHT_HAND_UP"
        if _pose_event_allowed(event_name, now):
            brain.send_to_crestron(event_name)
            _pose_latched = True
        return


def _maybe_release_to_user_from_table(points, now):
    """Open gripper when a detected wrist is near the claw in TABLE_CAM mode."""
    global _last_table_release_time, _table_release_hits

    p = brain.tuner.get_params()
    enabled = bool(round(float(p.get("table_release_enabled", 1.0))))

    if not enabled:
        _table_release_hits = 0
        return
    if not brain.is_holding_item():
        _table_release_hits = 0
        return
    if not brain.can_auto_release_now():
        _table_release_hits = 0
        return
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
        _table_release_hits = 0
        return

    cooldown = max(0.5, float(p.get("table_release_cooldown", getattr(config, "TABLE_HANDOFF_RELEASE_COOLDOWN", 2.5))))
    if now - _last_table_release_time < cooldown:
        _table_release_hits = 0
        return

    claw_x = float(p.get("table_claw_x", getattr(config, "TABLE_HANDOFF_CLAW_X_NORM", 0.50)))
    claw_y = float(p.get("table_claw_y", getattr(config, "TABLE_HANDOFF_CLAW_Y_NORM", 0.82)))
    radius = max(0.03, float(p.get("table_release_radius", getattr(config, "TABLE_HANDOFF_RADIUS_NORM", 0.14))))
    min_conf = max(0.0, min(1.0, float(getattr(config, "TABLE_HANDOFF_MIN_CONFIDENCE", 0.45))))
    frames_required = max(1, int(getattr(config, "TABLE_HANDOFF_FRAMES_REQUIRED", 5)))

    near_detected = False
    near_label = None

    for key in ("left_hand", "right_hand"):
        idx = config.KEYPOINTS.get(key)
        if idx is None or idx >= len(points):
            continue
        conf = _point_confidence(points[idx])
        if conf < min_conf:
            continue
        px = points[idx].x()
        py = points[idx].y()
        if px is None or py is None:
            continue
        dist = ((px - claw_x) ** 2 + (py - claw_y) ** 2) ** 0.5
        if dist <= radius:
            near_detected = True
            near_label = key
            break

    if near_detected:
        _table_release_hits += 1
        if _table_release_hits >= frames_required:
            if brain.release_item_to_user(reason=f"table hand proximity ({near_label})"):
                _last_table_release_time = now
            _table_release_hits = 0
        return

    _table_release_hits = 0


def _maybe_pick_table_object_from_sample(sample, now):
    """Decode a table sample and feed unified frame-based pickup detector."""
    buf = sample.get_buffer()
    caps = sample.get_caps()
    struct = caps.get_structure(0)
    w = int(struct.get_value("width"))
    h = int(struct.get_value("height"))
    ok, mapinfo = buf.map(Gst.MapFlags.READ)
    if not ok:
        return
    try:
        frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3)).copy()
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        _maybe_pick_table_object_from_frame(frame_bgr, now)
    except Exception:
        return
    finally:
        buf.unmap(mapinfo)


def _release_overlay_caps_changed(_overlay, caps):
    global _overlay_frame_w, _overlay_frame_h
    try:
        s = caps.get_structure(0)
        _overlay_frame_w = int(s.get_value("width"))
        _overlay_frame_h = int(s.get_value("height"))
    except Exception:
        pass


def _release_overlay_draw(_overlay, cr, _timestamp, _duration):
    try:
        if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
            return

        p = brain.tuner.get_params()
        enabled = bool(round(float(p.get("table_release_enabled", 1.0))))
        if not enabled:
            return

        claw_x = float(p.get("table_claw_x", getattr(config, "TABLE_HANDOFF_CLAW_X_NORM", 0.50)))
        claw_y = float(p.get("table_claw_y", getattr(config, "TABLE_HANDOFF_CLAW_Y_NORM", 0.82)))
        radius_n = max(0.03, float(p.get("table_release_radius", getattr(config, "TABLE_HANDOFF_RADIUS_NORM", 0.14))))

        w, h = max(1, _overlay_frame_w), max(1, _overlay_frame_h)
        cx = claw_x * w
        cy = claw_y * h
        r = max(6.0, radius_n * min(w, h))

        cr.set_source_rgba(1.0, 0.85, 0.1, 0.95)
        cr.set_line_width(3.0)
        cr.arc(cx, cy, r, 0, 2 * np.pi)
        cr.stroke()

        cr.set_source_rgba(1.0, 0.2, 0.2, 0.95)
        cr.set_line_width(2.0)
        cr.move_to(cx - 8, cy)
        cr.line_to(cx + 8, cy)
        cr.move_to(cx, cy - 8)
        cr.line_to(cx, cy + 8)
        cr.stroke()
    except Exception:
        pass


def app_callback(pad, info, user_data):
    """GStreamer pad probe: parse Hailo pose detections and drive the arm."""
    global _smooth_x, _smooth_y, _last_move_time, _search_mode, _last_seen_time
    global _tracking_prev_busy, _tracking_resume_warmup_until

    buffer = info.get_buffer()
    if not buffer:
        return Gst.PadProbeReturn.OK

    try:
        roi = hailo.get_roi_from_buffer(buffer)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
        _update_vision_summary(
            _extract_detection_labels(detections),
            mode=brain.tuner.shared_params.get("camera_mode", "HIGH_CAM"),
        )
        person = next(
            (d for d in detections if d.get_label() == "person"), None
        )
        now = time.time()

        busy_now = int(brain.tuner.shared_params.get("busy", 0))
        if _tracking_prev_busy != 0 and busy_now == 0:
            warmup_s = max(0.0, float(getattr(config, "TRACKING_RESUME_WARMUP_SEC", 1.5)))
            _tracking_resume_warmup_until = now + warmup_s
            _seed_brain_ik_from_last_commanded()
            _last_move_time = now
            print(f"Tracking re-enable warmup: holding arm motion for {warmup_s:.2f}s")
        _tracking_prev_busy = busy_now

        if person:
            _last_seen_time = now

            # Manual override: do not move the arm, but keep last_seen fresh
            if _search_mode == "MANUAL":
                return Gst.PadProbeReturn.OK

            # Wake up from standby when a person reappears and brain is free
            if _search_mode is True:
                p = brain.tuner.get_params()
                if p.get("busy", 0) == 0:
                    print("Pose tracking: person reacquired – resuming")
                    _search_mode = False
                    _smooth_x, _smooth_y = 0.5, 0.5

            # Extract the target keypoint from the pose landmarks
            landmarks = person.get_objects_typed(hailo.HAILO_LANDMARKS)
            if landmarks:
                points = landmarks[0].get_points()
                _maybe_send_pose_gesture_events(points, now)
                _maybe_release_to_user_from_table(points, now)
                target_idx = config.KEYPOINTS.get(_tracking_target, 0)
                raw_x = 1.0 - points[target_idx].x()   # Hailo x=0 is the left edge of the
                raw_y = points[target_idx].y()           # frame; inverting aligns it with the
                                                         # arm's positive-Y direction (its right)

                # Discard frames where the keypoint teleports across more than
                # POSE_TELEPORT_THRESHOLD of the frame – these are noisy detections.
                if (abs(raw_x - _smooth_x) > config.POSE_TELEPORT_THRESHOLD
                        or abs(raw_y - _smooth_y) > config.POSE_TELEPORT_THRESHOLD):
                    return Gst.PadProbeReturn.OK

                p = brain.tuner.get_params()
                sf = p.get("smooth", 0.2)
                _smooth_x = raw_x * sf + _smooth_x * (1.0 - sf)
                _smooth_y = raw_y * sf + _smooth_y * (1.0 - sf)

                if now < _tracking_resume_warmup_until:
                    return Gst.PadProbeReturn.OK

                if (now - _last_move_time > config.MOVE_COOLDOWN
                        and p.get("busy", 0) == 0
                        and _search_mode is False):
                    cx = p.get(f"{_tracking_target}_x", 0.5)
                    cy = p.get(f"{_tracking_target}_y", 0.5)
                    ry = (_smooth_x - cx) * p.get("ry_m", 0.3)
                    rz = (config.ARM_RZ_BASE
                          + (cy - _smooth_y) * p.get("rz_m", 0.3)
                          + p.get("z_off", 0.0))
                    brain.reach_for_coordinate(
                        config.ARM_REACH_X, ry,
                        max(config.ARM_MIN_Z, min(config.ARM_MAX_Z, rz)),
                        speed=int(p.get("speed", 1200)),
                    )
                    _last_move_time = now
            else:
                _maybe_send_pose_gesture_events(None, now)

        else:
            _maybe_send_pose_gesture_events(None, now)
            if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") == "TABLE_CAM":
                return Gst.PadProbeReturn.OK
            # No person in frame – enter standby after timeout
            now = time.time()
            if now - _last_seen_time > config.FLAGPOLE_TIMEOUT:
                p = brain.tuner.get_params()
                if p.get("busy", 0) == 0 and _search_mode is False:
                    print("Pose tracking: person lost – parking arm + power off (RESUME required)")
                    brain.tuner.shared_params["busy"] = 1
                    _search_mode = True
                    _person_lost_park_async()

    except Exception as e:
        print(f"app_callback error: {e}")

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
    if face_cascade.empty():
        # pip-installed OpenCV puts cascades under cv2.data.haarcascades
        _fallback_xml = os.path.join(
            cv2.data.haarcascades, "haarcascade_frontalface_default.xml"
        )
        face_cascade = cv2.CascadeClassifier(_fallback_xml)
        if face_cascade.empty():
            print(f"CPU Fallback: Haar cascade not found at {config.HAAR_CASCADE_PATH}"
                  f" or {_fallback_xml} – face detection disabled")
        else:
            print(f"CPU Fallback: cascade loaded from {_fallback_xml}")
    # CLAHE normalises local contrast without over-amplifying uniform backgrounds,
    # which equalizeHist can do (causing false positives). Create once, reuse per frame.
    _clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
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
            if brain.shutdown_event.is_set():
                break
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
                gray = _clahe.apply(gray)
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


_restart_event = threading.Event()
_table_preview_stop = threading.Event()
_table_preview_thread = None
_table_obj_stop = threading.Event()
_table_obj_thread = None
_table_obj_dualcam_disabled_warned = False


def _maybe_pick_table_object_from_frame(frame_bgr, now):
    """Detect a table object in TABLE_CAM and trigger take-item sequence."""
    global _table_obj_hits, _last_table_obj_trigger_time, _last_table_obj_block_log_time
    global _table_obj_center_hits, _last_table_obj_align_log_time

    if not bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False)):
        _table_obj_hits = 0
        return
    if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") != "TABLE_CAM":
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    arm_delay = max(0.0, float(getattr(config, "TABLE_OBJECT_ARM_DELAY_SEC", 4.0)))
    if now - _table_cam_enter_time < arm_delay:
        if now - _last_table_obj_block_log_time > 1.5:
            print("TABLE_CAM auto-pick blocked: arm delay active")
            _last_table_obj_block_log_time = now
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return
    if brain.is_holding_item():
        if now - _last_table_obj_block_log_time > 2.0:
            print("DUAL_CAM auto-pick blocked: already holding item")
            _last_table_obj_block_log_time = now
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    cooldown = max(2.0, float(getattr(config, "TABLE_OBJECT_COOLDOWN_SEC", 20.0)))
    if now - _last_table_obj_trigger_time < cooldown:
        if now - _last_table_obj_block_log_time > 2.0:
            print("DUAL_CAM auto-pick blocked: cooldown active")
            _last_table_obj_block_log_time = now
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    h, w = frame_bgr.shape[:2]
    target_type = str(
        brain.tuner.shared_params.get(
            "table_object_target_type",
            getattr(config, "TABLE_OBJECT_TARGET_TYPE", "any"),
        )
    ).strip().lower()

    def _contours_from_mask(mask_img):
        kernel = np.ones((5, 5), np.uint8)
        mask_img = cv2.morphologyEx(mask_img, cv2.MORPH_OPEN, kernel)
        mask_img = cv2.morphologyEx(mask_img, cv2.MORPH_CLOSE, kernel)
        cts, _ = cv2.findContours(mask_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return cts

    if target_type == "any":
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        contours = _contours_from_mask(th)
    else:
        hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
        mask = None
        if target_type == "red":
            m1 = cv2.inRange(hsv, np.array([0, 80, 40]), np.array([10, 255, 255]))
            m2 = cv2.inRange(hsv, np.array([170, 80, 40]), np.array([179, 255, 255]))
            mask = cv2.bitwise_or(m1, m2)
        elif target_type == "green":
            mask = cv2.inRange(hsv, np.array([35, 60, 40]), np.array([90, 255, 255]))
        elif target_type == "blue":
            mask = cv2.inRange(hsv, np.array([90, 70, 40]), np.array([130, 255, 255]))
        elif target_type == "yellow":
            mask = cv2.inRange(hsv, np.array([18, 80, 60]), np.array([35, 255, 255]))
        elif target_type == "orange":
            mask = cv2.inRange(hsv, np.array([8, 100, 60]), np.array([20, 255, 255]))
        elif target_type == "white":
            mask = cv2.inRange(hsv, np.array([0, 0, 180]), np.array([179, 50, 255]))
        elif target_type == "black":
            mask = cv2.inRange(hsv, np.array([0, 0, 0]), np.array([179, 255, 60]))
        else:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours = _contours_from_mask(th)
            target_type = "any"
            mask = None

        if mask is not None:
            contours = _contours_from_mask(mask)

    if not contours:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    c = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(c))
    min_area = float(getattr(config, "TABLE_OBJECT_MIN_AREA_PX", 1200))
    if area < min_area:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    frame_area = float(max(1, w * h))
    max_area_frac = float(getattr(config, "TABLE_OBJECT_MAX_AREA_FRAC", 0.35))
    if (area / frame_area) > max(0.05, max_area_frac):
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    m = cv2.moments(c)
    if m.get("m00", 0.0) == 0.0:
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    cx = float(m["m10"] / m["m00"])
    cy = float(m["m01"] / m["m00"])
    x_norm = max(0.0, min(1.0, cx / max(1.0, float(w))))
    y_norm = max(0.0, min(1.0, cy / max(1.0, float(h))))

    x_min = float(getattr(config, "TABLE_OBJECT_X_MIN_NORM", 0.25))
    x_max = float(getattr(config, "TABLE_OBJECT_X_MAX_NORM", 0.75))
    y_min = float(getattr(config, "TABLE_OBJECT_Y_MIN_NORM", 0.45))
    if not (x_min <= x_norm <= x_max and y_norm >= y_min):
        _table_obj_hits = 0
        _table_obj_center_hits = 0
        return

    _update_vision_summary([target_type if target_type != "any" else "object"], mode="TABLE_CAM")

    _table_obj_hits += 1
    frames_required = max(1, int(getattr(config, "TABLE_OBJECT_FRAMES_REQUIRED", 5)))
    if _table_obj_hits < frames_required:
        return

    p = brain.tuner.get_params()
    y_gain = float(getattr(config, "TABLE_OBJECT_Y_GAIN", 0.24))
    x_bias_gain = float(getattr(config, "TABLE_OBJECT_X_BIAS_GAIN", 0.03))
    center_x = float(getattr(config, "TABLE_OBJECT_CENTER_X_NORM", 0.50))
    center_y = float(getattr(config, "TABLE_OBJECT_CENTER_Y_NORM", 0.62))
    center_tol = float(getattr(config, "TABLE_OBJECT_CENTER_TOL_NORM", 0.10))
    align_alpha = float(getattr(config, "TABLE_OBJECT_ALIGN_ALPHA", 0.35))
    center_frames_required = max(1, int(getattr(config, "TABLE_OBJECT_CENTER_FRAMES_REQUIRED", 4)))

    target_take_y = max(-0.12, min(0.12, (center_x - x_norm) * y_gain))
    target_take_x = float(p.get("take_x", 0.16)) + ((center_y - y_norm) * x_bias_gain)
    target_take_x = max(0.12, min(0.28, target_take_x))

    prev_take_x = float(p.get("take_x", 0.16))
    prev_take_y = float(p.get("take_y", 0.0))
    take_x = prev_take_x + (target_take_x - prev_take_x) * max(0.05, min(1.0, align_alpha))
    take_y = prev_take_y + (target_take_y - prev_take_y) * max(0.05, min(1.0, align_alpha))

    err = ((x_norm - center_x) ** 2 + (y_norm - center_y) ** 2) ** 0.5
    if err <= max(0.02, center_tol):
        _table_obj_center_hits += 1
    else:
        _table_obj_center_hits = 0

    if now - _last_table_obj_align_log_time > 1.0:
        print(
            "TABLE_CAM align: "
            f"x={x_norm:.2f} y={y_norm:.2f} err={err:.3f} "
            f"center_hits={_table_obj_center_hits}/{center_frames_required}"
        )
        _last_table_obj_align_log_time = now
    take_z = float(getattr(config, "TABLE_OBJECT_TAKE_Z", 0.27))
    take_z = max(0.18, min(0.40, take_z))
    take_lift_z = max(take_z + 0.05, min(0.45, float(p.get("take_lift_z", 0.36))))

    brain.tuner.shared_params["take_x"] = take_x
    brain.tuner.shared_params["take_y"] = take_y
    brain.tuner.shared_params["take_z"] = take_z
    brain.tuner.shared_params["take_lift_z"] = take_lift_z
    _maybe_update_table_pick_approach_pose(take_x, take_y, take_lift_z, now)

    if _table_obj_center_hits < center_frames_required:
        return

    print(
        f"TABLE_CAM object detected ({target_type}): "
        f"x_norm={x_norm:.2f} y_norm={y_norm:.2f} area={int(area)} "
        f"-> take_x={take_x:.3f} take_y={take_y:.3f} take_z={take_z:.3f}; starting pickup"
    )
    brain.tuner.shared_params["table_pick_request_active"] = 0
    brain.start_take_item_sequence(auto_pick=True)
    _last_table_obj_trigger_time = now
    _table_obj_hits = 0
    _table_obj_center_hits = 0


def _table_object_worker():
    """Separate lower-camera worker for safe object pickup detection in DUAL_CAM."""
    launch_str = (
        f"libcamerasrc camera-name={config.ARDUCAM_DEVICE} ! "
        f"video/x-raw,format=NV12,width={config.CAM_SENSOR_W},height={config.CAM_SENSOR_H} ! "
        f"videoconvert ! videoscale ! "
        f"video/x-raw,format=RGB,width={config.FRAME_W},height={config.FRAME_H} ! "
        "appsink name=table_obj_sink emit-signals=false sync=false drop=true max-buffers=1"
    )
    try:
        pipe = Gst.parse_launch(launch_str)
    except Exception as e:
        print(f"Table object worker unavailable: pipeline build failed: {e}")
        return
    sink = pipe.get_by_name("table_obj_sink")
    if sink is None:
        print("Table object worker unavailable: appsink missing")
        return

    try:
        pipe.set_state(Gst.State.PLAYING)
    except Exception as e:
        print(f"Table object worker unavailable: failed to start: {e}")
        return

    print("--- Table object worker active (DUAL_CAM) ---")
    window_name = "Table Object View"
    window_ready = False
    try:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, config.FRAME_W, config.FRAME_H)
        window_ready = True
    except Exception:
        window_ready = False
    try:
        while not _table_obj_stop.is_set() and not brain.shutdown_event.is_set():
            sample = sink.emit("try-pull-sample", int(0.08 * Gst.SECOND))
            if sample is None:
                time.sleep(0.02)
                continue

            buf = sample.get_buffer()
            caps = sample.get_caps()
            struct = caps.get_structure(0)
            w = int(struct.get_value("width"))
            h = int(struct.get_value("height"))
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                frame_rgb = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
                frame = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            except Exception:
                continue
            finally:
                try:
                    buf.unmap(mapinfo)
                except Exception:
                    pass

            try:
                _maybe_pick_table_object_from_frame(frame, time.time())
            except Exception:
                pass
            if window_ready:
                try:
                    cv2.imshow(window_name, frame)
                    cv2.waitKey(1)
                except Exception:
                    window_ready = False
            time.sleep(0.05)
    finally:
        _graceful_stop_pipeline(pipe)
        if window_ready:
            try:
                cv2.destroyWindow(window_name)
            except Exception:
                pass
        print("--- Table object worker stopped ---")


def _table_preview_worker():
    """Run a table-camera preview in a separate rpicam window for DUAL_CAM mode."""
    preview_cam_id = str(getattr(config, "ARDUCAM_DEVICE_ID", 0))
    preview_cmds = [
        [
            "rpicam-hello",
            "--camera", preview_cam_id,
            "--width", str(config.FRAME_W),
            "--height", str(config.FRAME_H),
            "-t", "0",
        ],
        [
            "libcamera-hello",
            "--camera", preview_cam_id,
            "--width", str(config.FRAME_W),
            "--height", str(config.FRAME_H),
            "-t", "0",
        ],
    ]

    preview_proc = None
    try:
        for cmd in preview_cmds:
            try:
                preview_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.6)
                if preview_proc.poll() is None:
                    print(f"--- Table preview active (DUAL_CAM, via {' '.join(cmd[:1])}) ---")
                    break
                err = (preview_proc.stderr.read() or "").strip()
                print(f"Table preview command failed ({' '.join(cmd[:1])}): {err[:240]}")
                preview_proc = None
            except Exception as e:
                print(f"Table preview command failed ({' '.join(cmd[:1])}): {e}")
                preview_proc = None

        if preview_proc is None:
            print("Table preview unavailable: could not start rpicam/libcamera preview")
            return

        while not _table_preview_stop.is_set() and not brain.shutdown_event.is_set():
            if preview_proc.poll() is not None:
                err_tail = ""
                try:
                    err_tail = (preview_proc.stderr.read() or "").strip()
                except Exception:
                    pass
                if err_tail:
                    print(f"Table preview process exited: {err_tail[:300]}")
                else:
                    print("Table preview process exited")
                break
            time.sleep(0.1)
    finally:
        if preview_proc is not None and preview_proc.poll() is None:
            try:
                preview_proc.terminate()
                preview_proc.wait(timeout=1.5)
            except Exception:
                try:
                    preview_proc.kill()
                except Exception:
                    pass
        print("--- Table preview stopped ---")


def _start_table_preview():
    global _table_preview_thread
    if _table_preview_thread and _table_preview_thread.is_alive():
        return
    _table_preview_stop.clear()
    _table_preview_thread = threading.Thread(target=_table_preview_worker, daemon=True)
    _table_preview_thread.start()


def _stop_table_preview():
    global _table_preview_thread
    _table_preview_stop.set()
    if _table_preview_thread and _table_preview_thread.is_alive():
        _table_preview_thread.join(timeout=3.0)
    _table_preview_thread = None


def _start_table_object_worker():
    global _table_obj_thread
    if _table_obj_thread and _table_obj_thread.is_alive():
        return
    _table_obj_stop.clear()
    _table_obj_thread = threading.Thread(target=_table_object_worker, daemon=True)
    _table_obj_thread.start()


def _stop_table_object_worker():
    global _table_obj_thread
    _table_obj_stop.set()
    if _table_obj_thread and _table_obj_thread.is_alive():
        _table_obj_thread.join(timeout=3.0)
    _table_obj_thread = None


def _graceful_stop_pipeline(pipe):
    if pipe is None:
        return
    try:
        pipe.send_event(Gst.Event.new_eos())
        bus = pipe.get_bus()
        if bus is not None:
            bus.timed_pop_filtered(
                int(0.25 * Gst.SECOND),
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
    except Exception:
        pass
    try:
        pipe.set_state(Gst.State.NULL)
        pipe.get_state(int(0.5 * Gst.SECOND))
    except Exception:
        pass


def camera_loop():
    # Force the device type for the Pro chip before starting
    os.environ["hailort_device_type"] = "hailo8"

    if not _check_hailo_plugins():
        _cpu_fallback_loop()
        return

    # Register handlers so robot_brain.switch_camera() can restart this pipeline
    # when the operator switches camera mode from the GUI or via Crestron.
    # HIGH_CAM and DUAL_CAM share the same main pipeline/camera path, so avoid
    # tearing down/rebuilding libcamerasrc between those two modes.
    brain.camera_switch_handlers["HIGH_CAM"] = lambda: None
    brain.camera_switch_handlers["TABLE_CAM"] = lambda: _restart_event.set()
    brain.camera_switch_handlers["DUAL_CAM"] = lambda: None

    global _last_seen_time, _table_cam_enter_time
    _last_seen_time = time.time() + 5.0  # give Hailo 5 s to find a person at startup
    prev_mode = None

    global _table_obj_dualcam_disabled_warned
    while not brain.shutdown_event.is_set():
        mode = brain.tuner.shared_params.get("camera_mode", "HIGH_CAM")
        if mode == "DUAL_CAM" and not bool(getattr(config, "ALLOW_DUAL_CAM", False)):
            brain.tuner.shared_params["camera_mode"] = "HIGH_CAM"
            mode = "HIGH_CAM"
            print("Camera safety: DUAL_CAM disabled by config, forced HIGH_CAM")
        if mode != prev_mode:
            if mode == "TABLE_CAM":
                _table_cam_enter_time = time.time()
            prev_mode = mode
        if mode == "TABLE_CAM":
            cam_path = config.ARDUCAM_DEVICE
            _stop_table_preview()
            _stop_table_object_worker()
        elif mode == "DUAL_CAM":
            # Keep main Hailo feed in this process; table preview runs in a
            # separate rpicam/libcamera preview process.
            cam_path = config.PI_CAMERA_DEVICE
            _stop_table_object_worker()
            if bool(getattr(config, "DUAL_CAM_TABLE_PREVIEW_ENABLED", False)):
                _start_table_preview()
            else:
                _stop_table_preview()
            if bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False)) and not _table_obj_dualcam_disabled_warned:
                print("WARNING: DUAL_CAM object worker disabled for stability (gst-libcamera crash guard)")
                _table_obj_dualcam_disabled_warned = True
        else:
            cam_path = config.PI_CAMERA_DEVICE
            _stop_table_preview()
            _stop_table_object_worker()

        _restart_event.clear()

        pipe = None
        try:
            src_segment = _build_camera_src_segment(mode, cam_path)
            # Capture at IMX708 native 16:9, downscale to the 640×640 square the
            # yolov8m_pose network expects.  hailotracker provides consistent IDs
            # across frames; hailooverlay draws skeleton overlays on the preview.
            overlay_enabled = (
                mode == "TABLE_CAM"
                and bool(getattr(config, "TABLE_HANDOFF_OVERLAY_ENABLED", False))
            )
            finger_branch_enabled = (
                mode == "HIGH_CAM"
                and bool(getattr(config, "FINGER_GESTURE_EVENTS_ENABLED", True))
                and mp is not None
            )
            table_model_branch_enabled = (
                mode == "TABLE_CAM"
                and bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False))
                and bool(getattr(config, "TABLE_OBJECT_MODEL_ENABLED", False))
                and os.path.isfile(getattr(config, "TABLE_OBJECT_HEF_PATH", ""))
                and os.path.isfile(getattr(config, "TABLE_OBJECT_SO_PATH", ""))
            )
            table_object_branch_enabled = (
                mode == "TABLE_CAM"
                and bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False))
                and not table_model_branch_enabled
            )

            if finger_branch_enabled:
                launch_str = (
                    src_segment +
                    f"videoconvert ! videoscale ! "
                    f"video/x-raw,format=RGB,width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
                    f"hailonet hef-path={HEF_PATH} force-writable=true ! "
                    f"hailofilter name=hailofilter so-path={SO_PATH} ! "
                    f"hailotracker ! hailooverlay ! "
                    f"videoconvert ! tee name=t "
                    f"t. ! queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                    f"autovideosink sync=false "
                    f"t. ! queue leaky=downstream max-size-buffers=1 ! "
                    f"video/x-raw,format=RGB,width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
                    f"appsink name=finger_sink emit-signals=false sync=false drop=true max-buffers=1"
                )
            elif table_model_branch_enabled:
                launch_str = (
                    src_segment +
                    f"videoconvert ! videoscale ! "
                    f"video/x-raw,format=RGB,width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
                    f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                    f"hailonet hef-path={config.TABLE_OBJECT_HEF_PATH} force-writable=true ! "
                    f"hailofilter name=table_hailofilter so-path={config.TABLE_OBJECT_SO_PATH} ! "
                    f"hailooverlay ! videoconvert ! autovideosink sync=false"
                )
            elif table_object_branch_enabled:
                launch_str = (
                    src_segment +
                    f"videoconvert ! videoscale ! "
                    f"video/x-raw,format=RGB,width={config.FRAME_W},height={config.FRAME_H} ! "
                    f"tee name=t "
                    f"t. ! queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                    f"videoconvert ! autovideosink sync=false "
                    f"t. ! queue leaky=downstream max-size-buffers=1 ! "
                    f"appsink name=table_obj_sink emit-signals=false sync=false drop=true max-buffers=1"
                )
            else:
                launch_str = (
                    src_segment +
                    f"videoconvert ! videoscale ! "
                    f"video/x-raw,format=RGB,"
                    f"width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
                    f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                    f"hailonet hef-path={HEF_PATH} force-writable=true ! "
                    f"hailofilter name=hailofilter so-path={SO_PATH} ! "
                    f"hailotracker ! hailooverlay ! "
                    f"videoconvert ! autovideosink sync=false"
                )

            if overlay_enabled:
                launch_str = launch_str.replace(
                    "videoconvert ! autovideosink sync=false",
                    "cairooverlay name=release_overlay ! videoconvert ! autovideosink sync=false",
                )

            try:
                pipe = Gst.parse_launch(launch_str)
            except Exception as e:
                # Some images may lack cairooverlay; fall back without the visual marker.
                fallback = launch_str.replace("cairooverlay name=release_overlay ! ", "")
                pipe = Gst.parse_launch(fallback)
                if overlay_enabled:
                    print(f"WARNING: release overlay disabled ({e})")

            hailofilter = pipe.get_by_name("hailofilter")
            if hailofilter:
                hailofilter.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, app_callback, None
                )
            else:
                if not table_object_branch_enabled:
                    print("WARNING: hailofilter element not found – probe not attached")

            table_hailofilter = pipe.get_by_name("table_hailofilter") if table_model_branch_enabled else None
            if table_hailofilter:
                table_hailofilter.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, _table_object_model_callback, None
                )

            if overlay_enabled:
                release_overlay = pipe.get_by_name("release_overlay")
                if release_overlay:
                    release_overlay.connect("caps-changed", _release_overlay_caps_changed)
                    release_overlay.connect("draw", _release_overlay_draw)

            finger_sink = pipe.get_by_name("finger_sink") if finger_branch_enabled else None
            table_obj_sink = pipe.get_by_name("table_obj_sink") if table_object_branch_enabled else None

            pipe.set_state(Gst.State.PLAYING)
            print("--- Hailo AI Hat Active: yolov8m_pose running ---")

            while not _restart_event.is_set() and not brain.shutdown_event.is_set():
                live_mode = brain.tuner.shared_params.get("camera_mode", "HIGH_CAM")
                if live_mode != mode:
                    if {live_mode, mode} <= {"HIGH_CAM", "DUAL_CAM"}:
                        mode = live_mode
                        if mode == "DUAL_CAM":
                            if bool(getattr(config, "TABLE_OBJECT_PICKUP_ENABLED", False)):
                                _stop_table_preview()
                                _start_table_object_worker()
                            else:
                                _stop_table_object_worker()
                                _start_table_preview()
                        else:
                            _stop_table_preview()
                            _stop_table_object_worker()
                        continue
                    _restart_event.set()
                    continue

                if finger_sink is not None:
                    sample = finger_sink.emit("try-pull-sample", int(0.02 * Gst.SECOND))
                    if sample is not None:
                        _maybe_send_finger_gesture_events_from_sample(sample, time.time())
                if table_obj_sink is not None:
                    sample = table_obj_sink.emit("try-pull-sample", int(0.03 * Gst.SECOND))
                    if sample is not None:
                        _maybe_pick_table_object_from_sample(sample, time.time())
                time.sleep(0.1)

        except Exception as e:
            print(f"Pipeline Error: {e}")
            time.sleep(2)
        finally:
            if pipe is not None:
                _graceful_stop_pipeline(pipe)
        time.sleep(0.5)

    _stop_table_preview()
    _stop_table_object_worker()

if __name__ == "__main__":
    print(f"--- od.py version {_VERSION} ---")
    _startup_log("od.py main: startup begin")
    brain.servo_move_callback = servo_integration.note_servo_move
    brain.thermal_status_provider = servo_integration.get_thermal_status
    brain.thermal_park_callback = servo_integration.park_arm
    brain.thermal_resume_callback = servo_integration.resume_arm
    brain.servo_power_provider = servo_integration.is_servo_power_on
    brain.servo_power_up_callback = servo_integration.power_up_servos
    brain.vision_summary_provider = get_vision_summary_text
    brain.table_model_update_callback = update_table_model_paths
    brain.table_pick_arm_callback = arm_table_pick_tracking
    startup_power_on = bool(getattr(config, "SAFE_STARTUP_POWER_ON", not bool(getattr(config, "SAFE_STARTUP_NO_MOTION", True))))
    startup_coord_enabled = bool(getattr(config, "STARTUP_COORD_MOVE_ENABLED", True))
    startup_coord_x = float(getattr(config, "STARTUP_COORD_X", getattr(config, "HOME_X", 0.06)))
    startup_coord_y = float(getattr(config, "STARTUP_COORD_Y", getattr(config, "HOME_Y", 0.0)))
    startup_coord_z = float(getattr(config, "STARTUP_COORD_Z", getattr(config, "HOME_Z", 0.40)))
    startup_coord_time_ms = max(1200, int(getattr(config, "STARTUP_COORD_TIME_MS", 4500)))
    startup_coord_settle_s = max(0.1, float(getattr(config, "STARTUP_COORD_SETTLE_SEC", 0.6)))
    startup_coord_safe = bool(getattr(config, "STARTUP_COORD_USE_SAFE_STEPPED_IK", True))
    startup_allow_force_no_seed = bool(getattr(config, "STARTUP_ALLOW_FORCE_MOVE_WITHOUT_SEED", True))
    startup_force_move_time_ms = max(startup_coord_time_ms, int(getattr(config, "STARTUP_FORCE_MOVE_TIME_MS", 8000)))
    startup_seed_retry_sec = max(0.0, float(getattr(config, "STARTUP_SEED_RETRY_SEC", 6.0)))
    startup_seed_retry_interval = max(0.05, float(getattr(config, "STARTUP_SEED_RETRY_INTERVAL_SEC", 0.25)))
    startup_abs_prime_enabled = bool(getattr(config, "STARTUP_ABS_SERVO_PRIME_ENABLED", True))
    startup_abs_time_ms = max(1200, int(getattr(config, "STARTUP_ABS_SERVO_TIME_MS", 8000)))
    startup_enable_tracking_on_step3 = bool(getattr(config, "STARTUP_ENABLE_TRACKING_ON_STEP3", False))
    startup_slow_home = bool(getattr(config, "STARTUP_SLOW_HOME_ENABLED", True))
    startup_slow_home_staged = bool(getattr(config, "STARTUP_SLOW_HOME_STAGED", True))
    startup_home_time_ms = max(1200, int(getattr(config, "STARTUP_SLOW_HOME_TIME_MS", 5000)))
    startup_settle_s = max(0.1, float(getattr(config, "STARTUP_SLOW_HOME_SETTLE_SEC", 0.4)))
    _startup_log(
        f"startup config: power_on={startup_power_on} coord_move={startup_coord_enabled} "
        f"slow_home={startup_slow_home}"
    )
    startup_abort_tracking = False

    _startup_log("step 1/3: power up board (auto)")
    startup_power_initialized = False

    if startup_abs_prime_enabled:
        _startup_log("startup pre-step: startup_power_up_quiet")
        servo_integration.startup_power_up_quiet()
        startup_power_initialized = True
        _wait_for_space("Step 1.5/3: Move to absolute servo startup pose")
        _startup_log("step 1.5 confirmed: absolute servo startup pose")
        brain.tuner.shared_params["busy"] = 1
        try:
            servo_integration.move_startup_absolute_pose(time_ms=startup_abs_time_ms)
        except Exception as e:
            print(f"Startup absolute-servo pre-step failed: {e}")
            _startup_log(f"startup absolute-servo pre-step failed: {e}")
        _wait_for_space("Step 1.6/3: Continue normal startup")
        _startup_log("step 1.6 confirmed: continue normal startup")

    if startup_coord_enabled:
        if not startup_power_initialized:
            _startup_log("startup path: startup_power_up_quiet")
            servo_integration.startup_power_up_quiet()
            startup_power_initialized = True
        _startup_log("step 2/3: absolute startup move attempt (auto)")
        print(
            "Startup: slow move to startup coordinates "
            f"({startup_coord_x:.3f}, {startup_coord_y:.3f}, {startup_coord_z:.3f}) "
            f"in {startup_coord_time_ms} ms"
        )
        brain.tuner.shared_params["busy"] = 1
        try:
            _startup_log("startup path: coordinate move begin")
            seed_ok = _seed_brain_ik_from_servo_readback()
            if not seed_ok:
                _startup_log("startup path: IK seed failed; startup coordinate move blocked")
                if startup_allow_force_no_seed:
                    print("Startup seed unavailable. You can force a very slow startup move.")
                    _wait_for_space("Step 2b/3: Force slow move without seed (higher risk)")
                    _wait_for_space("Step 2b.1/3: Send first motion command")
                    _startup_log("startup path: retrying IK seed before any forced move")
                    seed_ok_force = _seed_brain_ik_from_servo_readback(
                        retry_sec=startup_seed_retry_sec,
                        retry_interval_sec=startup_seed_retry_interval,
                    )
                    if not seed_ok_force:
                        _startup_log("startup path: forced move blocked; seed still unavailable after retries")
                        print(
                            "Forced startup move blocked: servo readback still unavailable after retries. "
                            "No motion will be commanded; tracking remains paused."
                        )
                        startup_abort_tracking = True
                    else:
                        _startup_log("startup path: forcing SAFE stepped move after successful seed")
                        moved = bool(
                            brain.reach_for_manual_coordinate(
                                startup_coord_x,
                                startup_coord_y,
                                startup_coord_z,
                                speed=startup_force_move_time_ms,
                                respect_config_speed=False,
                            )
                        )
                        if moved:
                            _wait_for_space("Step 2b.2/3: Continue after motion settle")
                            time.sleep(startup_coord_settle_s)
                            _startup_log("startup path: forced safe move settle complete")
                        else:
                            _startup_log("startup path: forced safe move rejected")
                            print("Forced startup move rejected by IK safety checks; tracking remains paused.")
                            startup_abort_tracking = True
                else:
                    print(
                        "Startup move blocked: IK seed/readback failed. "
                        "No motion will be commanded; tracking remains paused."
                    )
                    startup_abort_tracking = True
            else:
                if startup_coord_safe:
                    moved = bool(
                        brain.reach_for_manual_coordinate(
                            startup_coord_x,
                            startup_coord_y,
                            startup_coord_z,
                            speed=startup_coord_time_ms,
                        )
                    )
                    if not moved:
                        _startup_log("startup path: safe stepped coordinate move rejected")
                        print("Startup move rejected by IK safety checks; tracking remains paused")
                        brain.tuner.shared_params["busy"] = 1
                        startup_abort_tracking = True
                else:
                    brain.reach_for_coordinate(
                        startup_coord_x,
                        startup_coord_y,
                        startup_coord_z,
                        speed=startup_coord_time_ms,
                    )
                time.sleep(startup_coord_settle_s)
                _startup_log("startup path: coordinate move settle complete")
        except Exception as e:
            print(f"Startup coordinate move failed: {e}")
            _startup_log(f"startup path: coordinate move failed: {e}")
        finally:
            brain.tuner.shared_params["busy"] = 0
    elif startup_slow_home:
        if not startup_power_initialized:
            _startup_log("startup path: startup_power_up_quiet")
            servo_integration.startup_power_up_quiet()
            startup_power_initialized = True
        _wait_for_space("Step 2/3: Move slowly to startup pose")
        _startup_log("step 2 confirmed: startup pose move")
        print(f"Startup: slow move to HOME ({startup_home_time_ms} ms)")
        brain.tuner.shared_params["busy"] = 1
        try:
            if startup_slow_home_staged:
                _startup_log("startup path: go_home_staged begin")
                servo_integration.go_home_staged(time_ms=startup_home_time_ms)
            else:
                _startup_log("startup path: go_home begin")
                servo_integration.go_home(time_ms=startup_home_time_ms)
            time.sleep(startup_settle_s)
            _startup_log("startup path: go_home settle complete")
        except Exception as e:
            print(f"Startup slow-home failed: {e}")
            _startup_log(f"startup path: go_home failed: {e}")
        finally:
            brain.tuner.shared_params["busy"] = 0
    elif startup_power_on:
        _startup_log("startup path: immediate power_up_servos")
        if not startup_power_initialized:
            servo_integration.power_up_servos()
            startup_power_initialized = True
    else:
        print("Startup safety: servo power remains OFF until an explicit motion command")
        _startup_log("startup path: power remains off")

    if startup_abort_tracking:
        print("Step 3/3 skipped: startup move failed safety checks; tracking remains paused")
        _startup_log("step 3 skipped: tracking paused after startup move rejection")
        config.STARTUP_INITIAL_BUSY = 1
        brain.tuner.shared_params["busy"] = 1
    else:
        _wait_for_space("Step 3/3: Start tracking")
        if startup_enable_tracking_on_step3:
            _startup_log("step 3 confirmed: start tracking")
            config.STARTUP_INITIAL_BUSY = 0
            brain.tuner.shared_params["busy"] = 0
        else:
            _startup_log("step 3 confirmed: startup complete, tracking remains paused")
            print("Startup complete. Tracking remains paused; press RESUME when ready.")
            config.STARTUP_INITIAL_BUSY = 1
            brain.tuner.shared_params["busy"] = 1

    servo_integration.thermal_monitor.start()
    servo_integration.start_status_poller()
    _start_table_pick_steer_worker()
    print("--- Servo thermal monitor started ---")
    _startup_log("startup path: thermal/status/steer workers started")
    camera_thread = threading.Thread(target=camera_loop, daemon=True, name="CameraLoop")
    camera_thread.start()
    _startup_log("startup path: camera_thread started")
    try:
        _startup_log("startup path: entering brain UI")
        brain.start_brain_ui()
    except KeyboardInterrupt:
        brain.request_shutdown()
    finally:
        try:
            servo_integration.power_down_servos()
        except Exception as e:
            print(f"WARNING: power_down_servos failed during shutdown: {e}")
        brain.request_shutdown()
        camera_thread.join(timeout=3.0)
        _stop_table_pick_steer_worker()
        servo_integration.stop_status_poller()
        servo_integration.thermal_monitor.stop()
        print("--- Servo thermal monitor stopped ---")