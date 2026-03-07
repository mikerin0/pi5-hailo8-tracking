# config.py – Central configuration for pi5-hailo8-tracking

# --- Camera Paths ---
# Pi Camera on port 1 (high camera, used for pose tracking via Hailo)
PI_CAMERA_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
# Second IMX708 on port 0 (table / manipulation view)
ARDUCAM_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a"
# USB ArduCAM fallback device index (used only when CSI port 0 is unavailable)
ARDUCAM_DEVICE_ID = 0
# Camera backend for GStreamer source: "libcamera" (default) or "v4l2".
# If you hit libcamera native segfaults, switch to "v4l2".
CAMERA_BACKEND = "libcamera"
PI_CAMERA_V4L2_DEVICE = "/dev/video0"
ARDUCAM_V4L2_DEVICE = "/dev/video1"
# Hard safety gate for unstable dual-camera/libcamera combinations.
# Keep False unless your specific image + hardware proves stable.
ALLOW_DUAL_CAM = False
# DUAL_CAM preview safety:
# Running an extra rpicam/libcamera preview process can trigger libcamera
# segfaults on some Pi5 + dual-IMX708 setups. Keep disabled by default.
DUAL_CAM_TABLE_PREVIEW_ENABLED = False

# --- Hailo Pose Model Paths (yolov8m_pose + post-processing .so) ---
HEF_PATH = "/usr/local/hailo/resources/models/hailo8/yolov8m_pose.hef"
SO_PATH  = "/usr/local/hailo/resources/so/libyolov8pose_postprocess.so"

# --- Camera / Model Resolution ---
# libcamerasrc captures at the IMX708 native 16:9 resolution and the
# pipeline downscales to MODEL_INPUT_SIZE for the yolov8m_pose network.
CAM_SENSOR_W = 1536        # libcamerasrc capture width
CAM_SENSOR_H = 864         # libcamerasrc capture height
MODEL_INPUT_SIZE = 640     # hailonet input size (yolov8 expects 640×640)

# --- Pose Tracking Parameters ---
TRACKING_TARGET = "nose"   # default keypoint to follow: "nose" | "left_hand" | "right_hand"
KEYPOINTS = {              # Keypoint indices from the yolov8m_pose COCO skeleton
                           # COCO names them 'left_wrist' (9) and 'right_wrist' (10);
                           # the keys 'left_hand'/'right_hand' are kept so that the
                           # robot_brain.py params (left_hand_x etc.) stay consistent
                           # with the old working configuration.
    'nose': 0,
    'left_hand': 9,        # COCO: left_wrist
    'right_hand': 10,      # COCO: right_wrist
}
MOVE_COOLDOWN = 0.30       # minimum seconds between consecutive arm-move commands
FLAGPOLE_TIMEOUT = 2.0     # seconds without a detected person before entering standby

# --- Pose-to-arm coordinate constants ---
# Hailo returns normalised (0-1) coordinates.  The constants below map them
# to metres in the robot arm's workspace.
POSE_TELEPORT_THRESHOLD = 0.30  # max normalised jump between frames – larger deltas
                                 # are treated as noisy/invalid detections and dropped
ARM_REACH_X = 0.25         # fixed forward reach (metres) while pose tracking
ARM_RZ_BASE = 0.18         # arm height at the vertical centre of the frame (metres)
ARM_MIN_Z   = 0.05         # minimum safe arm height (metres)
ARM_MAX_Z   = 0.50         # maximum arm height (metres)

# --- Face Detection Parameters (OpenCV Haar Cascade) – used in CPU fallback ---
HAAR_CASCADE_PATH = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
FACE_SCALE_FACTOR = 1.1    # scaleFactor for detectMultiScale (lower = more thorough scan)
FACE_MIN_NEIGHBORS = 5     # minNeighbors for detectMultiScale (higher = fewer false positives)
FACE_MIN_SIZE = (50, 50)   # minimum face size in pixels – small enough for ~2 m distance

# --- Arm Coordinate Mapping (face position → arm reach, metres) ---
FRAME_W = 640              # expected frame width for face tracking pipeline
FRAME_H = 360              # expected frame height – 16:9 matches the IMX708 native
                           # aspect ratio so libcamera uses the full sensor FOV
                           # (480 causes a 4:3 crop, losing the wide field of view)
ARM_X_CENTER = 0.20        # arm reach when face is at horizontal frame centre
ARM_X_RANGE = 0.10         # ± reach variation across full frame width
ARM_Y_RANGE = 0.15         # ± lateral swing across full frame height
ARM_Z_DEFAULT = 0.15       # fixed height while face tracking

# --- Smooth Tracking ---
TRACKING_ALPHA = 0.15      # EMA coefficient (lower = smoother but more lag)
ARM_Y_DEFAULT = 0.0        # starting lateral position for smooth tracking

# --- Home / park position ---
# Safer default: use IK coordinates for homing so it is less sensitive to
# per-servo calibration drift than hardcoded absolute pulse values.
HOME_USE_COORDINATES = True
HOME_X = 0.06
HOME_Y = 0.0
HOME_Z = 0.40
HOME_SPEED = 700

# --- Manual slider safety bounds ---
# Manual mode sends coordinates directly to IK; keep these conservative to
# avoid kinematic branch flips and below-table targets.
MANUAL_X_MIN = 0.14
MANUAL_X_MAX = 0.30
MANUAL_Y_MIN = -0.12
MANUAL_Y_MAX = 0.12
MANUAL_Z_MIN = 0.18
MANUAL_Z_MAX = 0.40
MANUAL_TRAVEL_Z = 0.32

# --- Manual IK safety validation ---
# Reject manual IK solutions that are inconsistent with the requested target
# or that imply a dangerous branch flip.
MANUAL_MIN_SOLVED_Z = 0.10
MANUAL_IK_MAX_POSITION_ERROR_M = 0.10
MANUAL_IK_MAX_JOINT_STEP_RAD = 1.20
MANUAL_JOG_STEP_M = 0.002
MANUAL_JOG_SPEED = 700

# --- GStreamer Pipeline Parameters ---
# GST_SYNC is used as a string literal inside GStreamer launch strings ("true"/"false")
GST_LEAKY_QUEUE_SIZE = 5   # max-size-buffers for leaky downstream queue
GST_SYNC = "false"         # appsink / ximagesink sync flag

# --- Gripper microswitch safety (servo 1 close-stop) ---
# Set GRIPPER_SWITCH_PIN_BCM to your Raspberry Pi BCM GPIO number (e.g. 17),
# then set the electrical polarity fields to match your wiring.
# Physical header wiring: pin 13 (GPIO27) signal, pin 14 (GND) return.
GRIPPER_SWITCH_PIN_BCM = 27
GRIPPER_SWITCH_PULL_UP = True
GRIPPER_SWITCH_PRESSED_STATE = 0
# Close motion runs in short increments so the switch can stop the gripper early.
GRIPPER_CLOSE_STEP_US = 35
GRIPPER_CLOSE_STEP_TIME_MS = 70

# --- Bottom-camera handoff release (automatic give-to-user) ---
# When enabled, TABLE_CAM mode watches wrist keypoints and opens the gripper
# when a hand enters the claw zone.
TABLE_HANDOFF_RELEASE_ENABLED = True
TABLE_HANDOFF_CLAW_X_NORM = 0.50      # 0..1 horizontal claw center in TABLE_CAM frame
TABLE_HANDOFF_CLAW_Y_NORM = 0.82      # 0..1 vertical claw center in TABLE_CAM frame
TABLE_HANDOFF_RADIUS_NORM = 0.14      # trigger distance from claw center (normalized)
TABLE_HANDOFF_RELEASE_COOLDOWN = 2.5  # seconds between release triggers
# Minimum time item must be held before auto-release is allowed after a close.
TABLE_HANDOFF_MIN_HOLD_SEC = 1.5
# Longer lockout after TAKE ITEM sequence close/lift phase.
TABLE_HANDOFF_TAKE_LOCKOUT_SEC = 4.0
# Require this many consecutive qualifying frames before releasing.
TABLE_HANDOFF_FRAMES_REQUIRED = 5
# Minimum wrist keypoint confidence to consider a hand valid.
TABLE_HANDOFF_MIN_CONFIDENCE = 0.45
# Optional visual marker for release zone (uses cairooverlay). Disabled by
# default for maximum runtime stability on Pi images where cairooverlay can be
# unstable with some camera/display combinations.
TABLE_HANDOFF_OVERLAY_ENABLED = False

# --- TABLE_CAM object pickup (MVP) ---
# Detects the largest blob on the lower camera feed and triggers the
# existing take-item sequence using tuned take_* values.
TABLE_OBJECT_PICKUP_ENABLED = False
TABLE_OBJECT_MIN_AREA_PX = 1200
TABLE_OBJECT_FRAMES_REQUIRED = 3
TABLE_OBJECT_COOLDOWN_SEC = 20.0
TABLE_OBJECT_Y_GAIN = 0.24
TABLE_OBJECT_X_BIAS_GAIN = 0.03
TABLE_OBJECT_MAX_AREA_FRAC = 0.80
TABLE_OBJECT_X_MIN_NORM = 0.10
TABLE_OBJECT_X_MAX_NORM = 0.90
TABLE_OBJECT_Y_MIN_NORM = 0.20
TABLE_OBJECT_TAKE_Z = 0.18
TABLE_OBJECT_CENTER_X_NORM = 0.50
TABLE_OBJECT_CENTER_Y_NORM = 0.62
TABLE_OBJECT_CENTER_TOL_NORM = 0.10
TABLE_OBJECT_CENTER_FRAMES_REQUIRED = 4
TABLE_OBJECT_ALIGN_ALPHA = 0.35
# Target type filter for pickup worker:
#   any | red | green | blue | yellow | orange | white | black
TABLE_OBJECT_TARGET_TYPE = "any"
# Safety delay after entering TABLE_CAM before auto-pick is allowed.
TABLE_OBJECT_ARM_DELAY_SEC = 4.0
# Extra Z offset applied only to autonomous TABLE PICK (meters).
TABLE_PICK_Z_OFFSET_M = -0.01
# If servo 1 closes beyond this pulse during TABLE PICK, treat as a miss.
TABLE_PICK_MISS_SERVO1_POS = 2140
# When TABLE PICK is manually armed, ignore strict model class label filtering
# (helps when small objects are misclassified, e.g., bottle -> toothbrush).
TABLE_PICK_IGNORE_LABEL_FILTER = True
# Use stepped/manual IK safety path for TABLE PICK steering updates.
# False uses direct coordinate IK to avoid strict pose-error rejections
# during small tracking corrections.
TABLE_PICK_STEER_USE_STEPPED_IK = False

# --- TABLE_CAM model-based object detection (Hailo) ---
TABLE_OBJECT_MODEL_ENABLED = True
TABLE_OBJECT_HEF_PATH = "/usr/local/hailo/resources/models/hailo8/yolov8n.hef"
TABLE_OBJECT_SO_PATH = "/usr/local/hailo/resources/so/libyolo_hailortpp_postprocess.so"
TABLE_OBJECT_MIN_CONFIDENCE = 0.35
# Optional class filter by detector label text (empty => accept all labels).
TABLE_OBJECT_TARGET_LABEL = ""

# --- Startup safety ---
# Prevent any automatic arm movement during application startup.
SAFE_STARTUP_NO_MOTION = True
# Cap speed of the very first commanded move after startup (set <=0 to disable).
SAFE_STARTUP_FIRST_MOVE_SPEED_CAP = 1500
# When SAFE_STARTUP_NO_MOTION is True, keep servo torque/power off until first
# explicit motion command (or RESUME).
SAFE_STARTUP_POWER_ON = False

# Optional startup choreography: move slowly to HOME before tracking starts.
# This helps avoid aggressive first motion when the arm is in an unknown pose.
STARTUP_SLOW_HOME_ENABLED = True
STARTUP_SLOW_HOME_TIME_MS = 5000
STARTUP_SLOW_HOME_SETTLE_SEC = 0.4
# Before startup slow-home, clear any queued controller motion and wait briefly
# after torque/power enable to avoid snap/jerk from stale targets.
STARTUP_CLEAR_MOTION_QUEUE = True
STARTUP_POWER_SETTLE_SEC = 0.8

# --- Pose gesture events to Crestron (outbound) ---
# Uses yolov8 pose keypoints (wrists + shoulders). These are coarse gestures,
# not finger-level hand-pose classification.
POSE_GESTURE_EVENTS_ENABLED = True
POSE_GESTURE_Y_MARGIN = 0.025         # wrist must be this much above shoulder (normalized y)
POSE_GESTURE_MIN_CONFIDENCE = 0.20    # minimum keypoint confidence
POSE_GESTURE_COOLDOWN_SEC = 0.8       # debounce per event
# Keep physical left/right mapping by default. Set True only if your camera
# orientation makes events feel swapped in your installation.
POSE_GESTURE_MIRROR_LEFT_RIGHT = False
# Consecutive-frame confirmation before emitting a pose gesture event.
POSE_GESTURE_FRAMES_REQUIRED = 1
# After BOTH_HANDS_UP, suppress single-hand events briefly to avoid double-firing.
POSE_GESTURE_BOTH_SUPPRESS_SEC = 0.9
# Require this many neutral frames before a held gesture can re-trigger.
POSE_GESTURE_RESET_FRAMES = 2
# Runtime debug logging for gesture classifier state (NONE/LEFT/RIGHT/BOTH).
POSE_GESTURE_DEBUG = False
POSE_GESTURE_DEBUG_LOG_INTERVAL_SEC = 0.5

# --- MediaPipe finger-count gesture events (outbound, HIGH_CAM) ---
# NOTE: keep disabled by default for runtime stability on Pi5 + Hailo pipeline.
# Enable only after validating performance on your setup.
FINGER_GESTURE_EVENTS_ENABLED = False
FINGER_GESTURE_MIN_DET_CONF = 0.45
FINGER_GESTURE_MIN_TRACK_CONF = 0.45
FINGER_GESTURE_COOLDOWN_SEC = 1.0
FINGER_GESTURE_FRAMES_REQUIRED = 2
# Vertical margin for counting a finger as raised (tip.y < pip.y - margin)
FINGER_GESTURE_Y_MARGIN = 0.02
FINGER_GESTURE_DEBUG = False

# --- Shelly smart plug power control for servo/controller supply ---
# Uses Shelly Gen2 RPC API, e.g. /rpc/Switch.Set?id=0&on=true
SHELLY_ARM_POWER_ENABLED = True
SHELLY_ARM_POWER_HOST = "172.31.31.166"
SHELLY_ARM_POWER_SWITCH_ID = 0
SHELLY_ARM_POWER_TIMEOUT_S = 2.0
# Delay after turning Shelly output on before sending controller commands.
SHELLY_ARM_POWER_BOOT_SETTLE_SEC = 1.2
# Optional inter-servo delay when enabling/disabling torque.
SERVO_TORQUE_STEP_SEC = 0.03

# --- Resume motion behavior ---
# On RESUME, move to a known absolute HOME pose slowly before tracking.
RESUME_USE_HOME_POSE = True
RESUME_HOME_TIME_MS = 3500
RESUME_SETTLE_SEC = 0.2
