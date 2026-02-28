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
```

> **Already cloned before?**  
> If you see `fatal: destination path 'pi5-hailo8-tracking' already exists`, the
> folder is already there. Just enter it and pull the latest changes:
> ```bash
> cd pi5-hailo8-tracking
> git pull
> ```

> **No `git` installed?**  
> `sudo apt install -y git` then repeat the commands above.
>
> **Prefer a zip download?**  
> On the GitHub page click **Code → Download ZIP**, copy the zip to the Pi
> (e.g. with `scp`), then `unzip pi5-hailo8-tracking-main.zip`.

#### ✅ Verify the files are there

After cloning (or pulling), run:

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

If the files are listed, you're ready to move on. If the folder looks empty or the command gives an error, check that you are inside the `pi5-hailo8-tracking` directory (`pwd` prints your current path).

### Step 1 – Install system packages

```bash
sudo apt update
sudo apt install -y python3-gi python3-opencv \
    gstreamer1.0-tools gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-libcamera libcamera-tools
```

### Step 2 – Install Python packages

```bash
pip install numpy ikpy pyserial
```

### Step 3 – Place the Hailo8 model

Copy the hand-landmark HEF from the Hailo Pi 5 SDK into the expected location:

```bash
mkdir -p /home/arm/models
cp /path/to/hand_landmark_lite.hef /home/arm/models/
```

### Step 4 – Verify cameras are visible

```bash
# Pi Camera (port 1) – should list the imx708 sensor
libcamera-hello --list-cameras

# ArduCAM Module 3 – should show /dev/video0 (or similar)
v4l2-ctl --list-devices
```

If the Pi Camera device name in the output differs from the default in `config.py`,
update `PI_CAMERA_DEVICE` accordingly.

### Step 5 – Check serial port for the arm

```bash
ls /dev/ttyAMA*
```

If the port is not `/dev/ttyAMA10`, edit `PORT` in `robot_brain.py` or set the correct
value before running.

### Step 6 – Run the full system

```bash
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
     gstreamer1.0-plugins-good gstreamer1.0-plugins-bad

# Python packages
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
| `ARDUCAM_DEVICE_ID` | `0` | v4l2 device index for ArduCAM |
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
python od.py
```

### Run face tracking only

```bash
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
