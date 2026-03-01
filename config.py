# config.py – Central configuration for pi5-hailo8-tracking

# --- Camera Paths ---
# Pi Camera on port 1 (high camera, used for pose tracking via Hailo)
PI_CAMERA_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
# Second IMX708 on port 0 (table / manipulation view)
ARDUCAM_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@88000/imx708@1a"
# USB ArduCAM fallback device index (used only when CSI port 0 is unavailable)
ARDUCAM_DEVICE_ID = 0

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

# --- GStreamer Pipeline Parameters ---
# GST_SYNC is used as a string literal inside GStreamer launch strings ("true"/"false")
GST_LEAKY_QUEUE_SIZE = 5   # max-size-buffers for leaky downstream queue
GST_SYNC = "false"         # appsink / ximagesink sync flag
