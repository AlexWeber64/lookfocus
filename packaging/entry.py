"""PyInstaller entry point shared by LookFocus.exe (tray, no console) and lookfocus-cli.exe (commands)."""

import sys

import lookfocus  # noqa: F401  sets OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS before cv2 is imported

# isort: split


def self_check() -> int:
    """Run by tools/build_exe.py on the built exe: privacy checks, model, mediapipe and one inference.
    (A missing module only shows up here: the first build lacked matplotlib, which mediapipe imports.)"""
    import numpy as np

    from lookfocus.capture import FaceTracker

    tracker = FaceTracker()
    try:
        tracker.process(np.zeros((480, 640, 3), np.uint8), 0.0)
    finally:
        tracker.close()
    print("self-check OK")
    return 0


if "--self-check" in sys.argv[1:]:
    sys.exit(self_check())
if "--probe-child" in sys.argv[1:]:  # the camera-sharing probe re-runs this exe (see probe_command)
    from lookfocus.camera_sharing import main
else:
    from lookfocus.main import main

sys.exit(main())
