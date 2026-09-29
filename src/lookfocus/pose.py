"""Head-pose math: facial transformation matrix + face landmarks -> angles.

Pure numpy (no camera, mediapipe or Win32 imports) so it is unit-testable anywhere.

MediaPipe documents neither the memory layout of the 4x4 facial transformation matrix nor its axis sign
conventions. So:
- The layout is detected per matrix: a rigid transform has (0, 0, 0, 1) either as its last row (row-major,
  translation in the last column) or as its last column (the transpose).
- Nothing downstream depends on signs. Calibration records whatever numbers each monitor produces, and
  the classifier compares against those, so an estimator only has to be repeatable and to change
  monotonically as the head turns.

Several estimators are computed side by side because matrix pitch has been reported as unreliable. The
debug viewer shows them all, so the one that behaves best on real hardware can be chosen:
- matrix:  angles of the matrix rotation, equal to Y-X-Z Euler angles (yaw about the vertical axis, then
           pitch, then roll). In this order yaw and pitch are exactly the direction of the face's forward
           axis, so they are unaffected by head roll (tilting the head sideways).
- lm3d:    the same two angles from a face frame built from 3D landmarks (cheek-to-cheek and chin-to-forehead
           vectors); independent of the matrix.
- lm2d:    unitless 2D ratios (where the nose tip sits between the cheeks / between the nasion and chin);
           no depth needed, very stable, but not angles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

Layout = Literal["row", "col", "unknown"]

# MediaPipe Face Mesh landmark indices (the 478-point topology).
NOSE_TIP = 1
NASION = 168  # between the eyes
FOREHEAD = 10  # top of the face oval
CHIN = 152
CHEEK_A = 234  # face-oval extremes, one on each side
CHEEK_B = 454

_UNIT_ROW = np.array([0.0, 0.0, 0.0, 1.0])


@dataclass(frozen=True)
class HeadPose:
    yaw: float  # matrix, degrees
    pitch: float
    roll: float
    lm_yaw: float  # 3D landmark frame, degrees
    lm_pitch: float
    lm2d_yaw: float  # unitless ratios, roughly -0.5..0.5
    lm2d_pitch: float
    tx: float  # matrix translation (canonical-model units, ~cm)
    ty: float
    tz: float
    layout: Layout
    matrix: tuple[float, ...] = ()  # the 16 raw values as returned, row by row (for debugging)

    def features(self, source: str) -> tuple[float, float]:
        """(horizontal, vertical) pair from one estimator: "matrix", "lm3d", "lm2d" or "hybrid"."""
        if source == "matrix":
            return self.yaw, self.pitch
        if source == "lm3d":
            return self.lm_yaw, self.lm_pitch
        if source == "lm2d":
            return self.lm2d_yaw, self.lm2d_pitch
        if source == "hybrid":  # matrix yaw, landmark pitch
            return self.yaw, self.lm_pitch
        raise ValueError(f"unknown pose source {source!r}")


@dataclass(frozen=True)
class PoseSample:
    """What the capture thread publishes for every processed frame."""

    t: float  # monotonic seconds at capture
    status: str  # "opening" | "active" | "no_face" | "error" | "paused" | "stopped"
    pose: HeadPose | None = None
    fps: float = 0.0  # processed frames per second (recent average)
    infer_ms: float = 0.0
    camera: str = ""
    message: str = ""


def detect_layout(m: np.ndarray, tol: float = 1e-3) -> Layout:
    """Which way a 4x4 rigid transform is stored. Pure rotations (no translation) read as "row"."""
    m = np.asarray(m, dtype=float).reshape(4, 4)
    row_ok = bool(np.allclose(m[3, :], _UNIT_ROW, atol=tol))
    col_ok = bool(np.allclose(m[:, 3], _UNIT_ROW, atol=tol))
    if row_ok:
        return "row"
    if col_ok:
        return "col"
    return "unknown"


def rigid_parts(m: np.ndarray) -> tuple[np.ndarray, np.ndarray, Layout]:
    """(R 3x3 proper rotation, t 3-vector, layout). Scale/shear is removed via SVD."""
    m = np.asarray(m, dtype=float).reshape(4, 4)
    layout = detect_layout(m)
    if layout == "col":
        m = m.T
    a = m[:3, :3]
    t = m[:3, 3].copy()
    u, _, vt = np.linalg.svd(a)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r, t, layout


def direction_angles(f: np.ndarray) -> tuple[float, float]:
    """(horizontal, vertical) angles of a face-forward vector in camera space, degrees.

    The vector is first flipped into the +z half-space. A detected face always points roughly along the
    camera axis, just with an unknown sign, so this keeps the angles continuous around a frontal pose
    (near 0, never wrapping at +/-180) whatever sign convention the source uses.
    """
    if f[2] < 0:
        f = -f
    yaw = math.atan2(f[0], f[2])
    pitch = math.atan2(-f[1], math.hypot(f[0], f[2]))
    return math.degrees(yaw), math.degrees(pitch)


def matrix_angles(r: np.ndarray) -> tuple[float, float, float]:
    """(yaw, pitch, roll) degrees from a model->camera rotation.

    yaw/pitch come from the model's z (depth) axis, so they are the head's facing direction and are not
    affected by roll. For R = Ry(a) @ Rx(b) @ Rz(c) they equal (a, b), the Y-X-Z Euler angles. Roll is
    the Y-X-Z Euler roll folded into (-90, 90]; its sign depends on the axis convention, which is fine
    because roll is diagnostic only (never used to classify).
    """
    yaw, pitch = direction_angles(r[:, 2])
    roll = math.degrees(math.atan2(r[1, 0], r[1, 1]))
    if roll > 90:
        roll -= 180
    elif roll <= -90:
        roll += 180
    return yaw, pitch, roll


def landmark_frame_angles(p: np.ndarray) -> tuple[float, float]:
    """Face forward direction from 3D landmarks in isotropic units (x*W, y*H, z*W)."""
    right = p[CHEEK_B] - p[CHEEK_A]
    up = p[FOREHEAD] - p[CHIN]
    rn = np.linalg.norm(right)
    if rn < 1e-9:
        return 0.0, 0.0
    right = right / rn
    up = up - np.dot(up, right) * right
    un = np.linalg.norm(up)
    if un < 1e-9:
        return 0.0, 0.0
    up = up / un
    fwd = np.cross(right, up)
    return direction_angles(fwd)


def _position_along(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    ab = b - a
    denom = float(np.dot(ab, ab))
    return float(np.dot(p - a, ab) / denom) if denom > 1e-12 else 0.0


def landmark_2d_ratios(p: np.ndarray) -> tuple[float, float]:
    """(nose position between the cheeks, nose position between nasion and chin), both centred on 0."""
    xy = p[:, :2]
    h = _position_along(xy[NOSE_TIP], xy[CHEEK_A], xy[CHEEK_B]) - 0.5
    v = _position_along(xy[NOSE_TIP], xy[NASION], xy[CHIN]) - 0.5
    return h, v


def landmarks_to_isotropic(norm_xyz: np.ndarray, width: int, height: int) -> np.ndarray:
    """MediaPipe normalized landmarks (x/W, y/H, z roughly on the x scale) -> pixel-like units."""
    out = np.asarray(norm_xyz, dtype=float).copy()
    out[:, 0] *= width
    out[:, 1] *= height
    out[:, 2] *= width
    return out


def head_pose(matrix: np.ndarray | None, norm_landmarks: np.ndarray, width: int, height: int) -> HeadPose:
    p = landmarks_to_isotropic(norm_landmarks, width, height)
    lm_yaw, lm_pitch = landmark_frame_angles(p)
    h2, v2 = landmark_2d_ratios(p)
    if matrix is not None:
        raw = np.asarray(matrix, dtype=float).reshape(4, 4)
        r, t, layout = rigid_parts(raw)
        yaw, pitch, roll = matrix_angles(r)
        flat = tuple(float(x) for x in raw.ravel())
    else:
        yaw = pitch = roll = float("nan")
        t = np.full(3, float("nan"))
        layout, flat = "unknown", ()
    return HeadPose(
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        lm_yaw=lm_yaw,
        lm_pitch=lm_pitch,
        lm2d_yaw=h2,
        lm2d_pitch=v2,
        tx=float(t[0]),
        ty=float(t[1]),
        tz=float(t[2]),
        layout=layout,
        matrix=flat,
    )
