"""LookFocus: move keyboard focus to the monitor you are facing."""

import os

# Must be set before cv2 is first imported, and mediapipe imports cv2 itself, so it lives here:
# importing any lookfocus module runs this first. Disabling MSMF hardware transforms avoids
# multi-second opens and colour-conversion failures on some cameras (see DECISIONS.md, D-005).
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

__version__ = "0.0.1"
