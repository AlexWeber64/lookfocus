"""LookFocus: move keyboard focus to the monitor you are facing."""

import os
import sys

# Must be set before cv2 is first imported, and mediapipe imports cv2 itself, so it lives here:
# importing any lookfocus module runs this first. Disabling MSMF hardware transforms avoids
# multi-second opens and colour-conversion failures on some cameras (see DECISIONS.md, D-005).
os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"
# numpy's OpenBLAS commits a buffer per core at import (~225 MB private on this 8-thread laptop) and
# starts idle threads; LookFocus only does tiny matrix maths, so one thread is plenty (D-041).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
if sys.platform != "win32":
    # tray.py builds its menu from pystray's Menu objects on every platform, but on Linux the icon itself is
    # Qt's (linux/tray.py): pystray's Linux backends would need GTK or an X11 tray at import time (D-050).
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")

__version__ = "1.0.0"
