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

gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

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


def app_callback(pad, info, user_data):
    """GStreamer pad probe: parse Hailo pose detections and drive the arm."""
    global _smooth_x, _smooth_y, _last_move_time, _search_mode, _last_seen_time

    buffer = info.get_buffer()
    if not buffer:
        return Gst.PadProbeReturn.OK

    try:
        roi = hailo.get_roi_from_buffer(buffer)
        detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
        person = next(
            (d for d in detections if d.get_label() == "person"), None
        )
        now = time.time()

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
            # No person in frame – enter standby after timeout
            now = time.time()
            if now - _last_seen_time > config.FLAGPOLE_TIMEOUT:
                p = brain.tuner.get_params()
                if p.get("busy", 0) == 0 and _search_mode is False:
                    print("Pose tracking: person lost – entering standby")
                    _search_mode = True
                    brain.reach_for_coordinate(0.06, 0.0, 0.40, speed=500)

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


def camera_loop():
    # Force the device type for the Pro chip before starting
    os.environ["hailort_device_type"] = "hailo8"

    if not _check_hailo_plugins():
        _cpu_fallback_loop()
        return

    # Register handlers so robot_brain.switch_camera() can restart this pipeline
    # when the operator switches between HIGH_CAM and TABLE_CAM from the GUI or
    # via a Crestron command.
    brain.camera_switch_handlers["HIGH_CAM"] = lambda: _restart_event.set()
    brain.camera_switch_handlers["TABLE_CAM"] = lambda: _restart_event.set()

    global _last_seen_time
    _last_seen_time = time.time() + 5.0  # give Hailo 5 s to find a person at startup

    while True:
        cam_path = (
            config.PI_CAMERA_DEVICE
            if brain.tuner.shared_params.get("camera_mode", "HIGH_CAM") == "HIGH_CAM"
            else config.ARDUCAM_DEVICE
        )
        _restart_event.clear()
        os.system("sudo pkill -9 -f hailonet")
        time.sleep(1)

        pipe = None
        try:
            # Capture at IMX708 native 16:9, downscale to the 640×640 square the
            # yolov8m_pose network expects.  hailotracker provides consistent IDs
            # across frames; hailooverlay draws skeleton overlays on the preview.
            launch_str = (
                f"libcamerasrc camera-name={cam_path} ! "
                f"video/x-raw,format=NV12,"
                f"width={config.CAM_SENSOR_W},height={config.CAM_SENSOR_H} ! "
                f"videoconvert ! videoscale ! "
                f"video/x-raw,format=RGB,"
                f"width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
                f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
                f"hailonet hef-path={HEF_PATH} ! "
                f"hailofilter name=hailofilter so-path={SO_PATH} ! "
                f"hailotracker ! hailooverlay ! "
                f"videoconvert ! autovideosink sync=false"
            )

            pipe = Gst.parse_launch(launch_str)
            hailofilter = pipe.get_by_name("hailofilter")
            if hailofilter:
                hailofilter.get_static_pad("src").add_probe(
                    Gst.PadProbeType.BUFFER, app_callback, None
                )
            else:
                print("WARNING: hailofilter element not found – probe not attached")

            pipe.set_state(Gst.State.PLAYING)
            print("--- Hailo AI Hat Active: yolov8m_pose running ---")

            while not _restart_event.is_set():
                time.sleep(0.1)

        except Exception as e:
            print(f"Pipeline Error: {e}")
            time.sleep(2)
        finally:
            if pipe is not None:
                pipe.set_state(Gst.State.NULL)
        time.sleep(0.5)

if __name__ == "__main__":
    print(f"--- od.py version {_VERSION} ---")
    threading.Thread(target=brain.start_brain_ui, daemon=True).start()
    try:
        camera_loop()
    except KeyboardInterrupt:
        pass