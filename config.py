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
TABLE_PICK_Z_OFFSET_M = 0.00
# If servo 1 closes beyond this pulse during TABLE PICK, treat as a miss.
TABLE_PICK_MISS_SERVO1_POS = 2140
# When TABLE PICK is manually armed, ignore strict model class label filtering
# (helps when small objects are misclassified, e.g., bottle -> toothbrush).
TABLE_PICK_IGNORE_LABEL_FILTER = True
# Use stepped/manual IK safety path for TABLE PICK steering updates.
# False uses direct coordinate IK to avoid strict pose-error rejections
# during small tracking corrections.
TABLE_PICK_STEER_USE_STEPPED_IK = False
# When operator manually arms TABLE PICK, allow faster trigger behavior.
TABLE_PICK_MANUAL_FRAMES_REQUIRED = 1
TABLE_PICK_MANUAL_CENTER_FRAMES_REQUIRED = 2
TABLE_PICK_MANUAL_CENTER_TOL_NORM = 0.10
TABLE_PICK_MANUAL_MIN_CONFIDENCE = 0.20
TABLE_PICK_MANUAL_MIN_HUNT_SEC = 0.8
TABLE_PICK_MANUAL_HUNT_ENABLED = True
TABLE_PICK_MANUAL_HUNT_AMPLITUDE_Y = 0.04
TABLE_PICK_MANUAL_HUNT_PERIOD_SEC = 4.0
TABLE_PICK_MANUAL_FORCE_GRAB_ENABLED = False
TABLE_PICK_MANUAL_FORCE_GRAB_AFTER_SEC = 3.0
TABLE_PICK_MANUAL_COMMIT_ENABLED = True
TABLE_PICK_MANUAL_COMMIT_ERR_NORM = 0.22
TABLE_PICK_MANUAL_COMMIT_STABLE_SEC = 1.2
TABLE_PICK_MANUAL_Y_GAIN = 0.30
TABLE_PICK_MANUAL_X_GAIN = 0.14
TABLE_PICK_MANUAL_ALIGN_ALPHA = 0.55
TABLE_PICK_MANUAL_IGNORE_PERSON = True
TABLE_PICK_MANUAL_MAX_TARGET_STEP_X = 0.015
TABLE_PICK_MANUAL_MAX_TARGET_STEP_Y = 0.020
# Keep autonomous pickup from descending too low.
TABLE_PICK_MIN_AUTO_TAKE_Z = 0.24

# After returning from TABLE_CAM to HIGH_CAM, wait this long before applying
# person-lost timeout logic so reacquisition has time to lock.
HIGH_CAM_REACQUIRE_GRACE_SEC = 2.5

# --- TABLE_CAM model-based object detection (Hailo) ---
TABLE_OBJECT_MODEL_ENABLED = True
# Run TABLE_CAM model for vision summary even when pickup is not armed.
TABLE_OBJECT_SUMMARY_ENABLED = True
TABLE_OBJECT_HEF_PATH = "/usr/local/hailo/resources/models/hailo8/yolov8n.hef"
TABLE_OBJECT_SO_PATH = "/usr/local/hailo/resources/so/libyolo_hailortpp_postprocess.so"
TABLE_OBJECT_MIN_CONFIDENCE = 0.35
# Minimum normalized bbox area for model-based pickup candidates.
# Helps reject far-away tiny detections that are likely not reachable.
TABLE_OBJECT_MIN_AREA_FRAC = 0.004
# Optional class filter by detector label text (empty => accept all labels).
TABLE_OBJECT_TARGET_LABEL = ""
# Never trigger pickup from these detector labels (prevents tabletop lock-on).
TABLE_OBJECT_IGNORED_LABELS = ("table", "dining table")

# Manual TABLE PICK proximity guards for model detections.
# Keep some forward-depth requirement even in manual mode.
TABLE_PICK_MANUAL_Y_MIN_NORM = 0.30
# Require larger target when manually armed so far-away detections don't trigger.
TABLE_PICK_MANUAL_MIN_AREA_FRAC = 0.006

# Speech behavior: announce generic target object by default instead of raw
# detector class names, which can be noisy (e.g., banana/toothbrush).
TABLE_PICK_ANNOUNCE_DETECTOR_LABEL = False

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
STARTUP_SLOW_HOME_ENABLED = False
STARTUP_SLOW_HOME_TIME_MS = 5000
STARTUP_SLOW_HOME_SETTLE_SEC = 0.4
STARTUP_SLOW_HOME_STAGED = True
STARTUP_SLOW_HOME_STEPS = 6
STARTUP_SLOW_HOME_STEP_PAUSE_SEC = 0.08
# Hard safety: cap pulse change per staged-home command to avoid violent flips
# if initial readback is noisy or far from HOME.
STARTUP_SLOW_HOME_MAX_STEP_DELTA_US = 90
# Require at least this many valid servo readbacks before any startup motion.
STARTUP_MIN_VALID_READBACK_SERVOS = 5
# Before startup slow-home, clear any queued controller motion and wait briefly
# after torque/power enable to avoid snap/jerk from stale targets.
STARTUP_CLEAR_MOTION_QUEUE = True
STARTUP_POWER_SETTLE_SEC = 0.8
# Emit detailed startup timing logs for diagnosing boot-time jerks.
STARTUP_DEBUG_TIMESTAMPS = True

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
SHELLY_ARM_POWER_BOOT_SETTLE_SEC = 2.0
# Optional inter-servo delay when enabling/disabling torque.
SERVO_TORQUE_STEP_SEC = 0.15
# On power-up, read current servo positions and seed those as motion targets
# before enabling torque. This prevents snapping to stale prior targets.
STARTUP_SEED_CURRENT_POSE = True
STARTUP_SEED_TIME_MS = 1200

# --- Global servo jump guard ---
# Applies a hard guard to commanded pulse jumps to reduce jerk risk.
# mode: "clamp" (limit to max delta) or "reject" (drop command)
SERVO_MOVE_DELTA_GUARD_ENABLED = True
SERVO_MOVE_MAX_DELTA_US = 160
SERVO_MOVE_DELTA_MODE = "clamp"
# Servo IDs exempt from delta guard (claw should close fully on one command).
SERVO_MOVE_DELTA_GUARD_EXEMPT_IDS = [1]

# Preferred startup motion: go to absolute IK startup pose slowly, then begin tracking.
# Uses the same coordinate frame as HOME_X/HOME_Y/HOME_Z.
STARTUP_COORD_MOVE_ENABLED = False
STARTUP_COORD_X = HOME_X
STARTUP_COORD_Y = HOME_Y
STARTUP_COORD_Z = HOME_Z
STARTUP_COORD_TIME_MS = 4500
STARTUP_COORD_SETTLE_SEC = 0.6
STARTUP_COORD_USE_SAFE_STEPPED_IK = True
# If IK seed/readback fails at startup, allow an operator-confirmed fallback
# slow coordinate move (higher risk than seeded move, but avoids total block).
STARTUP_ALLOW_FORCE_MOVE_WITHOUT_SEED = True
STARTUP_FORCE_MOVE_TIME_MS = 8000
STARTUP_SEED_RETRY_SEC = 6.0
STARTUP_SEED_RETRY_INTERVAL_SEC = 0.25
# Interactive startup wizard: require operator confirmation between
# power-up, absolute-start move, and tracking start.
STARTUP_STEP_PROMPTS_ENABLED = False
# Initial busy state applied when UI starts (1 keeps tracking paused).
STARTUP_INITIAL_BUSY = 1

# Optional pre-step before IK/coordinate startup: move to known absolute
# servo pulses first, then continue normal startup flow.
STARTUP_ABS_SERVO_PRIME_ENABLED = False
STARTUP_ABS_SERVO_TIME_MS = 8000
STARTUP_ABS_SERVO_POSITIONS = {
    6: 1883,
    5: 700,
    4: 655,
    3: 720,
    2: 1500,
    1: 1500,
}

# If False, step 3 will start camera/UI but keep arm tracking paused
# (busy=1) until operator explicitly presses RESUME.
STARTUP_ENABLE_TRACKING_ON_STEP3 = False

# Tracking re-enable safety: hold arm motion briefly after busy goes 1->0
# so IK state can be re-seeded and first-frame jumps are avoided.
TRACKING_RESUME_WARMUP_SEC = 2.5

# --- Resume motion behavior ---
# On RESUME, move to a known absolute HOME pose slowly before tracking.
RESUME_USE_HOME_POSE = True
RESUME_HOME_TIME_MS = 3500
RESUME_SETTLE_SEC = 0.2
# When enabled, RESUME powers servos but does not command any reposition move.
# This keeps the arm at shutdown/current pose to avoid wake-up jumps.
RESUME_HOLD_CURRENT_POSE = True
# Two-step resume safety: first RESUME powers/holds while paused, second RESUME
# enables tracking motion.
RESUME_TWO_STEP_ENABLE = False
# If two-step is disabled, this controls whether tracking auto-enables after
# thermal resume callback completes.
RESUME_AUTO_ENABLE_TRACKING = True
# Use sequential per-servo RESUME pattern (proven jerk-free on this setup).
RESUME_SAFE_SEQUENCE_ENABLED = True
RESUME_SAFE_STEP_TIME_MS = 500
RESUME_SAFE_STEP_PAUSE_SEC = 1.0
RESUME_SAFE_SEQUENCE = [
    (1, 1500),  # gripper
    (2, 1500),  # wrist rotation
    (3, 1497),  # wrist
    (4, 1782),  # elbow
    (5, 2078),  # shoulder
    (6, 1183),  # waist
]
