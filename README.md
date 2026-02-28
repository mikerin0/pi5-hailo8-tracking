# pi5-hailo8-tracking

Face and hand tracking with Raspberry Pi 5, Hailo8 AI accelerator, and ArduCAM.

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
