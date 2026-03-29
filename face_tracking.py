# face_tracking.py – Face tracking via Pi Camera (port 1) with OpenCV Haar Cascade
import os, time, threading
import sys
import cv2
import numpy as np
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)

import robot_brain as brain
import config

# --- Smooth tracking state (exponential moving average) ---
_smooth_x = config.ARM_X_CENTER
_smooth_y = config.ARM_Y_DEFAULT
_smooth_z = config.ARM_Z_DEFAULT

_face_cascade = cv2.CascadeClassifier(config.HAAR_CASCADE_PATH)

# Redirect all stdout/stderr to a log file for debugging
_LOG_PATH = os.path.join(os.path.dirname(__file__), "face_tracking_debug.log")
_log_file = open(_LOG_PATH, "a", buffering=1)
sys.stdout = _log_file
sys.stderr = _log_file
_pipe = None
_running = False
_lock = threading.Lock()


def _map_face_to_arm(cx, cy, frame_w, frame_h):
    """Map face centroid (pixels) to arm (x, y, z) workspace coordinates (metres)."""
    nx = cx / frame_w   # 0..1, left → right
    ny = cy / frame_h   # 0..1, top → bottom
    # Forward reach: face at horizontal centre → ARM_X_CENTER
    x = config.ARM_X_CENTER + config.ARM_X_RANGE * (0.5 - nx)
    # Lateral swing: left half of frame → positive Y, right → negative Y
    y = config.ARM_Y_RANGE * (0.5 - ny)
    # Map vertical face position to Z height (top of frame = max Z, bottom = min Z)
    z = config.ARM_MIN_Z + (1.0 - ny) * (config.ARM_MAX_Z - config.ARM_MIN_Z)
    if getattr(config, "DEBUG_FACE_Z", False):
        print(f"[DEBUG] Face centroid: cx={cx}, cy={cy}, nx={nx:.3f}, ny={ny:.3f}, z={z:.3f}")
    return float(x), float(y), float(z)


def _on_new_sample(sink, _user_data=None):
    """Appsink callback: pull a frame, run Haar detection, update arm position."""
    global _smooth_x, _smooth_y, _smooth_z

    print("[DEBUG] _on_new_sample called")
    print(f"[DEBUG] sink: {sink}, user_data: {_user_data}")
    sample = sink.emit("pull-sample")
    print(f"[DEBUG] sample: {sample}")
    if sample is None:
        print("[DEBUG] No sample pulled from sink")
        return Gst.FlowReturn.OK

    buf = sample.get_buffer()
    print(f"[DEBUG] buffer: {buf}")
    caps = sample.get_caps()
    print(f"[DEBUG] caps: {caps}")
    struct = caps.get_structure(0)
    w = struct.get_value("width")
    h = struct.get_value("height")
    print(f"[DEBUG] frame width: {w}, height: {h}")

    success, mapinfo = buf.map(Gst.MapFlags.READ)
    print(f"[DEBUG] buffer map success: {success}")
    if not success:
        print("[DEBUG] Failed to map buffer")
        return Gst.FlowReturn.OK

    try:
        print(f"[DEBUG] Mapping buffer to frame of size w={w}, h={h}")
        frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        print(f"[DEBUG] Frame shape: {frame.shape}")
        # Convert RGB to BGR for OpenCV display
        frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        faces = _face_cascade.detectMultiScale(
            gray,
            scaleFactor=config.FACE_SCALE_FACTOR,
            minNeighbors=config.FACE_MIN_NEIGHBORS,
            minSize=config.FACE_MIN_SIZE,
        )

        print(f"[DEBUG] faces detected: {len(faces)}")
        if len(faces):
            # Track the largest detected face
            areas = [face_w * face_h for _, _, face_w, face_h in faces]
            face_x, face_y, face_w, face_h = faces[int(np.argmax(areas))]
            cx = face_x + face_w // 2
            cy = face_y + face_h // 2

            # Draw rectangle overlay for the detected face
            cv2.rectangle(frame_bgr, (face_x, face_y), (face_x + face_w, face_y + face_h), (0, 255, 0), 2)

            print(f"[DEBUG] Largest face center: cx={cx}, cy={cy}")
            tx, ty, tz = _map_face_to_arm(cx, cy, w, h)
            ny = cy / h
            print(f"[DEBUG] Detected face: cy={cy}, ny={ny:.3f}, z={tz:.3f} (frame h={h})")

            # Exponential moving average for smooth motion
            a = config.TRACKING_ALPHA
            _smooth_x = a * tx + (1 - a) * _smooth_x
            _smooth_y = a * ty + (1 - a) * _smooth_y
            _smooth_z = a * tz + (1 - a) * _smooth_z

            params = brain.tuner.get_params()
            if not params.get("busy", 1):
                brain.reach_for_coordinate(
                    _smooth_x, _smooth_y, _smooth_z,
                    int(params.get("speed", 800)),
                )
        # Show video preview window with overlays and correct color
        cv2.imshow("Face Tracking Preview", frame_bgr)
        cv2.waitKey(1)
    finally:
        print("[DEBUG] Unmapping buffer")
        buf.unmap(mapinfo)

    return Gst.FlowReturn.OK


def _build_pipeline():
    """Construct and return a GStreamer pipeline for face tracking."""
    launch_str = (
        f"libcamerasrc camera-name={config.PI_CAMERA_DEVICE} ! "
        f"videoconvert ! "
        f"videoscale ! video/x-raw,width={config.MODEL_INPUT_SIZE},height={config.MODEL_INPUT_SIZE} ! "
        f"hailonet hef-path={config.HEF_PATH} force-writable=true ! "
        f"hailofilter name=hailofilter so-path={config.SO_PATH} ! "
        f"hailotracker ! hailooverlay ! "
        f"videoconvert ! videoscale ! video/x-raw,format=RGB,width={config.FRAME_W},height={config.FRAME_H} ! "
        f"videoconvert ! appsink name=preview_sink emit-signals=true sync={config.GST_SYNC}"
    )
    print("[DEBUG] _build_pipeline called (Hailo pose pipeline)")
    print(f"[DEBUG] Building pipeline with device: {config.PI_CAMERA_DEVICE}, width: {config.FRAME_W}, height: {config.FRAME_H}")
    pipe = Gst.parse_launch(launch_str)
    sink = pipe.get_by_name("preview_sink")
    sink.connect("new-sample", _on_new_sample)
    print("[DEBUG] Registered _on_new_sample callback with appsink (preview_sink)")
    return pipe


def start():
    """Start the face-tracking pipeline (non-blocking)."""
    global _pipe, _running
    print("[DEBUG] face_tracking.start() called")
    with _lock:
        if _running:
            print("[DEBUG] face_tracking.start(): already running")
            return
        _pipe = _build_pipeline()
        print(f"[DEBUG] Pipeline object: {_pipe}")
        ret = _pipe.set_state(Gst.State.PLAYING)
        print(f"[DEBUG] Pipeline set_state PLAYING returned: {ret}")
        _running = True
        print("--- Face Tracking: Pipeline Started ---")


def stop():
    """Stop the face-tracking pipeline and release resources."""
    global _pipe, _running
    with _lock:
        if not _running or _pipe is None:
            return
        _pipe.set_state(Gst.State.NULL)
        _pipe = None
        try:
            print(f"[DEBUG] Mapping buffer to frame of size w={w}, h={h}")
            frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
            print(f"[DEBUG] Frame shape: {frame.shape}")
            # Convert RGB to BGR for OpenCV display
            frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

            # Parse pose keypoints from Hailo tracker metadata (if available)
            # This assumes the hailotracker element attaches keypoints as buffer metadata
            # For demonstration, we just show the frame; for real use, parse and overlay keypoints here
            # TODO: Implement keypoint extraction from buffer metadata if available

            # Show video preview window with overlays and correct color
            cv2.imshow("Face Tracking Preview", frame_bgr)
            cv2.waitKey(1)
        except Exception as e:
            print(f"[FaceTracking] Exception in video preview: {e}")
