# face_tracking.py – Face tracking via Pi Camera (port 1) with OpenCV Haar Cascade
import os, time, threading
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
    return float(x), float(y), float(z)


def _on_new_sample(sink, _user_data=None):
    """Appsink callback: pull a frame, run Haar detection, update arm position."""
    global _smooth_x, _smooth_y, _smooth_z

    sample = sink.emit("pull-sample")
    if sample is None:
        return Gst.FlowReturn.OK

    buf = sample.get_buffer()
    caps = sample.get_caps()
    struct = caps.get_structure(0)
    w = struct.get_value("width")
    h = struct.get_value("height")

    success, mapinfo = buf.map(Gst.MapFlags.READ)
    if not success:
        return Gst.FlowReturn.OK

    try:
        frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        faces = _face_cascade.detectMultiScale(
            gray,
            scaleFactor=config.FACE_SCALE_FACTOR,
            minNeighbors=config.FACE_MIN_NEIGHBORS,
            minSize=config.FACE_MIN_SIZE,
        )

        if len(faces):
            # Track the largest detected face
            areas = [face_w * face_h for _, _, face_w, face_h in faces]
            face_x, face_y, face_w, face_h = faces[int(np.argmax(areas))]
            cx = face_x + face_w // 2
            cy = face_y + face_h // 2

            tx, ty, tz = _map_face_to_arm(cx, cy, w, h)

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
    finally:
        buf.unmap(mapinfo)

    return Gst.FlowReturn.OK


def _build_pipeline():
    """Construct and return a GStreamer pipeline for face tracking."""
    launch_str = (
        f"libcamerasrc camera-name={config.PI_CAMERA_DEVICE} ! "
        f"videoconvert ! "
        f"video/x-raw,format=RGB,width={config.FRAME_W},height={config.FRAME_H} ! "
        f"queue leaky=downstream max-size-buffers={config.GST_LEAKY_QUEUE_SIZE} ! "
        f"appsink name=sink emit-signals=true sync={config.GST_SYNC}"
    )
    pipe = Gst.parse_launch(launch_str)
    sink = pipe.get_by_name("sink")
    sink.connect("new-sample", _on_new_sample)
    return pipe


def start():
    """Start the face-tracking pipeline (non-blocking)."""
    global _pipe, _running
    with _lock:
        if _running:
            return
        _pipe = _build_pipeline()
        _pipe.set_state(Gst.State.PLAYING)
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
        _running = False
        print("--- Face Tracking: Pipeline Stopped ---")


def camera_loop():
    """Blocking loop that starts face tracking and restarts on failure."""
    while True:
        start()
        try:
            while True:
                time.sleep(1)
                with _lock:
                    if not _running:
                        break
        except KeyboardInterrupt:
            stop()
            break
        except Exception as e:
            print(f"Face Tracking Error: {e}")
            stop()
            time.sleep(2)


if __name__ == "__main__":
    import threading
    threading.Thread(target=brain.start_brain_ui, daemon=True).start()
    camera_loop()
