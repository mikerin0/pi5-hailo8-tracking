# pi5-hailo8-tracking

Face and hand tracking with Raspberry Pi 5, Hailo8 AI accelerator, and ArduCAM.

---

## What You'll Need

### Hardware

| Item | Notes |
|------|-------|
| Raspberry Pi 5 | 4 GB or 8 GB RAM recommended |
| Hailo8 AI HAT+ | Plugs into the Pi 5 PCIe slot |
| Raspberry Pi Camera Module 3 | Connected to Camera port 1 (the far port from USB); this is the official Pi camera |
| ArduCAM USB Camera Module 3 | USB camera used as the table/bottom view; this is ArduCAM's separate product |
| 6DOF robot arm (Hiwonder) | Connected via `/dev/ttyAMA10` serial port |
| MicroSD card | 16 GB+ with Raspberry Pi OS (64-bit, Bookworm) |

### Software (pre-installed on Raspberry Pi OS Bookworm)

- **Python 3.11+** – `python3 --version`
- **Git** – `git --version` (install with `sudo apt install -y git` if missing)
- **Hailo Pi 5 SDK** – follow the [Hailo documentation](https://hailo.ai/developer-zone/) to install `hailort` and the GStreamer plug-ins
- **GStreamer 1.0** – installed in Step 1 below

---

## Quick Start

Follow these steps in order to go from a fresh clone to a running system.

### Step 0 – Get the code onto your Pi

Open a terminal on your Raspberry Pi (or SSH into it) and run:

```bash
git clone https://github.com/mikerin0/pi5-hailo8-tracking.git
cd pi5-hailo8-tracking
git checkout copilot/add-face-tracking-module
```

> **Why `git checkout`?**  
> The full codebase (`config.py`, `face_tracking.py`, and the updated scripts) lives on
> the `copilot/add-face-tracking-module` branch while the PR is open.  
> The `git checkout` line switches you to that branch so you have all five files.

> **Already cloned before?**  
> If you see `fatal: destination path 'pi5-hailo8-tracking' already exists`, the
> folder is already there. Enter it, fetch, and switch to the right branch:
> ```bash
> cd pi5-hailo8-tracking
> git fetch
> git checkout copilot/add-face-tracking-module
> git pull
> ```

> **No `git` installed?**  
> `sudo apt install -y git` then repeat the commands above.
>
> **Prefer a zip download?**  
> On the GitHub page use the **branch dropdown** (top-left of the file list, currently showing `main`) to select **`copilot/add-face-tracking-module`**, then click
> **Code → Download ZIP**, copy the zip to the Pi (e.g. with `scp`), and
> `unzip pi5-hailo8-tracking-copilot-add-face-tracking-module.zip`.

#### ✅ Verify the files are there

After cloning and checking out the branch, run:

```bash
ls
```

You should see exactly these five files:

```
README.md
config.py
face_tracking.py
od.py
robot_brain.py
```

If `config.py` or `face_tracking.py` are missing, you might still be on the `main` branch (or the clone may have been incomplete).  
Fix it with:

```bash
git fetch
git checkout copilot/add-face-tracking-module
```

Then run `ls` again — all five files should be there.

### Step 1 – Install system packages

```bash
sudo apt update
sudo apt install -y python3-gi python3-opencv \
    gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-libcamera libcamera-tools rpicam-apps \
    hailo-all
```

> **`hailo-all`** installs the Hailo8 firmware, runtime (`hailort`), Python bindings, and
> the GStreamer plug-ins (`hailonet`, `hailofilter`, `hailooverlay`) that `od.py` requires.
>
> **`od.py` handles GStreamer plugin discovery automatically.**
> At startup it uses `ctypes.CDLL()` to load `libgomp.so.1` directly into the running
> Python process (which pre-allocates its static TLS block), then calls `Gst.init()`.
> If the plugins are still not found it also clears the registry cache and forces a
> fresh rescan — so in most cases just running `python od.py` is enough with no
> extra setup.
>
> If `od.py` still prints `ERROR: GStreamer element(s) not found`, there is a genuine
> installation problem.  Check the details it prints; the most common causes are:
>
> **a) Missing or broken package** – Reinstall:
> ```bash
> sudo apt update && sudo apt install --reinstall hailo-all
> ```
>
> **b) Missing shared-library dependencies** – Run:
> ```bash
> ldd /lib/aarch64-linux-gnu/gstreamer-1.0/libgsthailotools.so | grep 'not found'
> ```
> Any library listed as "not found" must be installed before the plugin can load.
>
> **c) Hailo device not connected or driver not loaded** – Run:
> ```bash
> hailortcli fw-control identify
> ```
> If this fails, check the HAT connection and that `hailort` service is running.
>
> **d) Hailo apt repository not configured** – Follow the
> [Hailo Raspberry Pi 5 setup guide](https://hailo.ai/developer-zone/) to add the
> repository, then re-run `sudo apt update && sudo apt install hailo-all`.

> **`rpicam-apps` provides `rpicam-hello`** (and `rpicam-still`, `rpicam-vid`, etc.) on Pi OS Bookworm.  
> It also installs transitional `libcamera-*` symlinks so old scripts keep working.  
> On Pi OS Bullseye (the older release) the package was called `libcamera-apps`; replace
> `rpicam-apps` with `libcamera-apps` if you are on Bullseye.

### Step 2 – Create a virtual environment and install Python packages

Raspberry Pi OS Bookworm (Debian 12) enforces [PEP 668](http://rptl.io/venv) and
blocks bare `pip install` system-wide.  
Use a virtual environment instead.

```bash
python3 -m venv --system-site-packages ~/hailo-venv
source ~/hailo-venv/bin/activate
pip install numpy ikpy pyserial
```

> **What `--system-site-packages` does**  
> It lets the venv reuse packages installed via `apt` (like `gi`, `cv2`, and `hailo`
> from the Hailo SDK) while still allowing `pip` to install `numpy`, `ikpy`, and
> `pyserial` cleanly inside the venv.

> **GStreamer plugin discovery is fully automatic in `od.py`**  
> At startup, `od.py` uses `ctypes.CDLL()` to load `libgomp.so.1` directly into the
> running process, sets `GST_PLUGIN_PATH`, and then calls `Gst.init()`.  If plugins
> are still not found it also clears the stale registry cache and forces a full
> rescan.  No `LD_PRELOAD` changes to `~/.bashrc` are needed — just run:
> ```bash
> python od.py
> ```
> If you want `gst-inspect-1.0` to work from the shell separately, you still need:
> ```bash
> export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libgomp.so.1
> gst-inspect-1.0 hailotools
> ```

> **Activate the venv every session**  
> Whenever you open a new terminal to run the code, run:
> ```bash
> source ~/hailo-venv/bin/activate
> ```
> You'll see `(hailo-venv)` at the start of your prompt when it's active.

### Step 3 – Place the Hailo8 model

Copy the hand-landmark HEF from the Hailo Pi 5 SDK into the expected location:

```bash
mkdir -p /home/arm/models
cp /path/to/hand_landmark_lite.hef /home/arm/models/
```

### Step 4 – Verify cameras are visible

```bash
# Pi Camera (port 1) – should list the imx708 sensor
rpicam-hello --list-cameras

# On Pi OS Bullseye (the older release) the command is still called:
# libcamera-hello --list-cameras

# All V4L2 devices – look for an "ArduCAM" or "USB Video" entry for the table camera
v4l2-ctl --list-devices
```

> If you see `bash: rpicam-hello: command not found`, install the missing package:
> ```bash
> sudo apt install -y rpicam-apps
> ```
> On Pi OS Bullseye, use `sudo apt install -y libcamera-apps` and `libcamera-hello` instead.

> **Understanding the `v4l2-ctl --list-devices` output on Pi 5:**  
> On Pi 5 Bookworm you will always see two `rp1-cfe` entries (each owning `/dev/video0`–`7`
> and `/dev/video8`–`15`) plus `pispbe` and `rpi-hevc-dec` — these are the built-in CSI
> camera interfaces and ISP; they are **not** the ArduCAM USB camera.  
> The USB ArduCAM appears as a separate named entry (e.g. `ArduCAM USB Camera` or
> `USB Video Device`) at a higher device number (typically `/dev/video16` or above).  
> Note its `/dev/videoX` number and update `ARDUCAM_DEVICE_ID` in `config.py` if it
> differs from `0`.

If the libcamera path shown by `rpicam-hello --list-cameras` differs from the default
`/base/axi/.../imx708@1a` in `config.py`, update `PI_CAMERA_DEVICE` accordingly.

### Step 5 – Check serial port for the arm

```bash
ls /dev/ttyAMA*
```

If the port is not `/dev/ttyAMA10`, edit `PORT` in `robot_brain.py` or set the correct
value before running.

### Step 6 – Run the full system

```bash
# Activate the virtual environment first (if not already active)
source ~/hailo-venv/bin/activate

# Starts both the GUI brain and the hand-gesture AI pipeline
python od.py
```

The Tkinter GUI window opens automatically.  Use the **HIGH CAM** / **TABLE CAM**
buttons in the GUI to switch camera modes at any time.

### Step 7 – Verify each feature

| Feature | How to test |
|---------|-------------|
| Arm moves to home | Click **HOME ARM** in the GUI |
| Manual arm control | Enable manual sliders, drag the **Reach X / Swing Y / Height Z** sliders |
| Face tracking | Click **HIGH CAM** – the arm should follow your face smoothly |
| Hand open event | Show an open flat hand to the Pi Camera – terminal prints `HAND_OPEN` |
| Hand closed event | Make a fist – terminal prints `HAND_CLOSED` |
| Index-finger light | Point index finger up – terminal prints `LIGHTS ON` |
| Crestron connection | Connect Crestron client to port **50005**; send `HOME` to confirm two-way comms |

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                      robot_brain.py                      │
│  • 6DOF inverse-kinematics arm control                   │
│  • Crestron TCP server (sends & receives events)         │
│  • GUI: manual sliders, camera-mode buttons              │
│  • camera_switch_handlers registry for live switching    │
└──────────┬───────────────────────────┬───────────────────┘
           │ HIGH CAM mode             │ TABLE CAM mode
           ▼                           ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│   face_tracking.py   │   │  od.py (hand gestures)       │
│  Pi Camera port 1    │   │  ArduCAM / Pi Camera port 1  │
│  Haar Cascade faces  │   │  Hailo8 hand-landmark model  │
│  EMA smooth tracking │   │  Open / closed hand gestures │
│  → arm coordinates   │   │  → Crestron events           │
└──────────────────────┘   └──────────────────────────────┘
```

### Dual-Camera Modes

| Mode | Camera | Purpose |
|------|--------|---------|
| **HIGH CAM** | Pi Camera (port 1) | Face tracking – arm follows detected face |
| **TABLE CAM** | ArduCAM Module 3 | Close-up table view for manipulation tasks |

Only one pipeline is active at a time.  
Switching is triggered by a GUI button or a Crestron `HIGH_CAM` / `TABLE_CAM` command.

### Hand Gesture Detection

```
Hand landmarks (Hailo8)
  → Euclidean distance: finger tips → wrist
  → OPEN  (all distances > HAND_OPEN_THRESHOLD)
  → CLOSED (all distances < HAND_CLOSED_THRESHOLD)
  → send_to_crestron("HAND_OPEN" | "HAND_CLOSED")
```

---

## File Layout

| File | Description |
|------|-------------|
| `config.py` | All tunable parameters (cameras, thresholds, arm mapping) |
| `face_tracking.py` | Face detection pipeline and smooth arm tracking |
| `od.py` | Hand-landmark AI pipeline with gesture detection |
| `robot_brain.py` | IK arm control, Crestron server, Tkinter GUI |

---

## Setup

### Requirements

```bash
# System packages
sudo apt install python3-gi python3-opencv gstreamer1.0-tools \
     gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
     libcamera-tools rpicam-apps hailo-all

# Python packages (use a venv to avoid the PEP 668 error on Pi OS Bookworm)
python3 -m venv --system-site-packages ~/hailo-venv
source ~/hailo-venv/bin/activate
pip install numpy ikpy pyserial
```

### Hailo8 Models

Place the hand-landmark HEF at the path set in `config.py`:

```
/home/arm/models/hand_landmark_lite.hef
```

The post-processing shared library ships with the Hailo Pi 5 SDK:

```
/usr/lib/aarch64-linux-gnu/hailo/libhand_landmark_post.so
```

---

## Configuration (`config.py`)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PI_CAMERA_DEVICE` | `/base/axi/…/imx708@1a` | libcamerasrc camera-name for port 1 |
| `ARDUCAM_DEVICE_ID` | `0` | v4l2 device index for the USB ArduCAM (see Step 4) |
| `HEF_PATH` | `/home/arm/models/…` | Hailo8 HEF model path |
| `SO_PATH` | `/usr/lib/…` | Hand-landmark post-processing .so |
| `HAAR_CASCADE_PATH` | `/usr/share/opencv4/…` | OpenCV Haar Cascade XML |
| `FACE_SCALE_FACTOR` | `1.2` | detectMultiScale scaleFactor |
| `FACE_MIN_NEIGHBORS` | `5` | detectMultiScale minNeighbors |
| `FACE_MIN_SIZE` | `(60, 60)` | Minimum face size (px) |
| `HAND_OPEN_THRESHOLD` | `0.15` | Tip-wrist distance for open hand |
| `HAND_CLOSED_THRESHOLD` | `0.10` | Tip-wrist distance for closed hand |
| `GESTURE_COOLDOWN_SEC` | `1.5` | Seconds between gesture events |
| `ARM_X_CENTER` | `0.20` | Forward reach at frame centre (m) |
| `ARM_X_RANGE` | `0.10` | ± reach across frame width (m) |
| `ARM_Y_RANGE` | `0.15` | ± swing across frame height (m) |
| `ARM_Z_DEFAULT` | `0.15` | Fixed arm height during face tracking (m) |
| `TRACKING_ALPHA` | `0.15` | EMA coefficient for smooth motion |
| `GST_LEAKY_QUEUE_SIZE` | `5` | GStreamer leaky queue depth |

---

## Usage

### Run hand-gesture tracking (default / Hailo8)

```bash
source ~/hailo-venv/bin/activate
python od.py
```

### Run face tracking only

```bash
source ~/hailo-venv/bin/activate
python face_tracking.py
```

### GUI camera switching

Launch `od.py` (or `robot_brain.py` standalone), then click:

- **HIGH CAM (Face Tracking)** – starts the Pi Camera face-tracking pipeline  
- **TABLE CAM (Manipulation)** – stops face tracking; table camera view is used

The current mode is shown beneath the buttons.

### Crestron commands (sent **to** the Pi)

| Command | Effect |
|---------|--------|
| `HOME` | Move arm to home position |
| `OPEN` | Open gripper |
| `CLOSE` | Close gripper |
| `HIGH_CAM` | Switch to face-tracking camera |
| `TABLE_CAM` | Switch to table camera |
| `HAND_OPEN` | Open gripper (mirroring gesture) |
| `HAND_CLOSED` | Close gripper (mirroring gesture) |

### Events pushed **from** the Pi to Crestron

| Event | Trigger |
|-------|---------|
| `LIGHT_ON` | Index finger pointing up |
| `HAND_OPEN` | All five fingers extended |
| `HAND_CLOSED` | All five fingers folded |
