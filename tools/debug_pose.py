"""Live head-pose viewer with its own camera; see lookfocus/debugview.py (also the tray's Debug view).

Keys:  G guided check   M mirror   V video on/off   R reset traces   Y take camera back   Q / Esc quit
Usage: .venv\\Scripts\\python.exe tools\\debug_pose.py [--backend msmf|dshow] [--fps 15] [--guided]
"""

import sys

import lookfocus  # noqa: F401  must precede cv2: sets OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS

# isort: split
from lookfocus.debugview import main

if __name__ == "__main__":
    sys.exit(main())
