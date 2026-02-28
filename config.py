# config.py – Central configuration for pi5-hailo8-tracking

# --- Camera Paths ---
# Pi Camera on port 1 (high camera, used for face tracking)
PI_CAMERA_DEVICE = "/base/axi/pcie@1000120000/rp1/i2c@80000/imx708@1a"
# ArduCAM Module 3 (table/bottom camera, used for manipulation)
ARDUCAM_DEVICE_ID = 0  # v4l2 device index (e.g. /dev/video0)

# --- Model Paths ---
HEF_PATH = "/home/arm/models/hand_landmark_lite.hef"
SO_PATH = "/usr/lib/aarch64-linux-gnu/hailo/libhand_landmark_post.so"

# --- Face Detection Parameters (OpenCV Haar Cascade) ---
HAAR_CASCADE_PATH = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
FACE_SCALE_FACTOR = 1.2    # scaleFactor for detectMultiScale
FACE_MIN_NEIGHBORS = 5     # minNeighbors for detectMultiScale
FACE_MIN_SIZE = (60, 60)   # minimum face size in pixels (width, height)

# --- Hand Gesture Thresholds (normalised 0-1 landmark coordinates) ---
HAND_OPEN_THRESHOLD = 0.15    # tip-to-wrist distance > this → finger extended
HAND_CLOSED_THRESHOLD = 0.10  # tip-to-wrist distance < this → finger folded
GESTURE_COOLDOWN_SEC = 1.5    # minimum seconds between consecutive gesture events

# --- Arm Coordinate Mapping (face position → arm reach, metres) ---
FRAME_W = 640              # expected frame width for face tracking pipeline
FRAME_H = 480              # expected frame height for face tracking pipeline
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
