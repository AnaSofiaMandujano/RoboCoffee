"""Geometry shared by calibrar_plano.py and medir_robot.py.

Conventions
-----------
* World frame {W}: origin at area corner A, +X along the 1800 mm side,
  +Y along the 1200 mm side, Z = X x Y (right-handed). Depending on how the
  area is seen from the camera, Z may point into the floor or out of it; the
  floor calibration stores which sign is "up" (towards the camera) in
  `signo_arriba`, and every height h is used as z = signo_arriba * h.
* theta is measured from +X towards +Y (in degrees, wrapped to (-180, 180]).
* ArUco corner order (OpenCV): 0 TL, 1 TR, 2 BR, 3 BL; marker +x = TL->TR.
"""
import time
from pathlib import Path

import cv2
import numpy as np
import yaml

from common import ROOT, open_camera


# --------------------------------------------------------------------------
# Frame sources: live camera, recorded video (offline re-processing) or image
# --------------------------------------------------------------------------
class FrameSource:
    """read() -> (ok, frame, t_seconds). Video time comes from the frame index,
    so time-based logic (stop detection, leg times) is identical offline."""

    def __init__(self, cfg, video=None, image=None, repeat=30):
        self.kind = "image" if image else "video" if video else "camera"
        self.n = 0
        if image:
            self.img = cv2.imread(str(image))
            if self.img is None:
                raise SystemExit(f"No se pudo leer la imagen {image}")
            self.repeat = repeat
            self.fps = 30.0
        elif video:
            self.cap = cv2.VideoCapture(str(video))
            if not self.cap.isOpened():
                raise SystemExit(f"No se pudo abrir el video {video}")
            self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        else:
            self.cap = open_camera(cfg)
            self.fps = float(cfg["camara"]["fps"])
            self.t0 = time.monotonic()

    def read(self):
        if self.kind == "image":
            if self.n >= self.repeat:
                return False, None, None
            self.n += 1
            return True, self.img.copy(), self.n / self.fps
        ok, frame = self.cap.read()
        self.n += 1
        if not ok:
            return False, None, None
        t = self.n / self.fps if self.kind == "video" else time.monotonic() - self.t0
        return True, frame, t

    def release(self):
        if self.kind != "image":
            self.cap.release()


# --------------------------------------------------------------------------
# Point helpers
# --------------------------------------------------------------------------
def undistort_px(pts, K, dist):
    """Distorted pixels (N,2) -> ideal (undistorted) pixels with the same K."""
    pts = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    return cv2.undistortPoints(pts, K, dist, P=K).reshape(-1, 2)


def normalized_rays(pts, K, dist):
    """Distorted pixels (N,2) -> camera-frame ray directions (N,3), z = 1."""
    pts = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    n = cv2.undistortPoints(pts, K, dist).reshape(-1, 2)
    return np.hstack([n, np.ones((len(n), 1))])


def diagonal_center(c4):
    """Projective centre of a square from its 4 image corners (TL,TR,BR,BL):
    intersection of the diagonals. Exact under perspective, unlike the mean."""
    p0, p1, p2, p3 = [np.append(c, 1.0) for c in np.asarray(c4, np.float64)]
    x = np.cross(np.cross(p0, p2), np.cross(p1, p3))
    return x[:2] / x[2]


def wrap_deg(a):
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def circular_mean_deg(a):
    r = np.radians(a)
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())))


def circular_std_deg(a):
    r = np.radians(a)
    R = np.hypot(np.sin(r).mean(), np.cos(r).mean())
    return float(np.degrees(np.sqrt(max(-2.0 * np.log(max(R, 1e-12)), 0.0))))


def rot2(theta_deg):
    t = np.radians(theta_deg)
    return np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])


# --------------------------------------------------------------------------
# Floor plane / camera extrinsics
# --------------------------------------------------------------------------
class PlanoPiso:
    """Camera pose w.r.t. the world frame plus ray-plane intersection."""

    def __init__(self, K, dist, rvec, tvec, signo_arriba):
        self.K, self.dist = K, dist
        self.rvec = np.asarray(rvec, np.float64).reshape(3, 1)
        self.tvec = np.asarray(tvec, np.float64).reshape(3, 1)
        self.R, _ = cv2.Rodrigues(self.rvec)
        self.C = (-self.R.T @ self.tvec).ravel()          # camera centre in {W}
        self.up = float(signo_arriba)

    @classmethod
    def load(cls, K, dist, path=ROOT / "datos" / "plano.yaml"):
        path = Path(path)
        if not path.exists():
            raise SystemExit(f"No existe {path}. Corre primero: python calibrar_plano.py")
        d = yaml.safe_load(open(path, encoding="utf-8"))
        return cls(K, dist, d["rvec"], d["tvec"], d["signo_arriba"]), d

    def z_of_height(self, h_mm):
        return self.up * float(h_mm)

    def pixels_to_plane(self, px, h_mm):
        """Intersect the rays of distorted pixels (N,2) with the horizontal
        plane at height h_mm above the floor. Returns world (N,2) in mm."""
        d_cam = normalized_rays(px, self.K, self.dist)        # (N,3)
        d_w = d_cam @ self.R                                   # = (R^T d)^T
        z = self.z_of_height(h_mm)
        s = (z - self.C[2]) / d_w[:, 2]
        P = self.C[None, :] + s[:, None] * d_w
        return P[:, :2]

    def project(self, xy, h_mm=0.0):
        """World (N,2) at height h_mm -> distorted pixels (N,2)."""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        P = np.hstack([xy, np.full((len(xy), 1), self.z_of_height(h_mm))])
        px, _ = cv2.projectPoints(P, self.rvec, self.tvec, self.K, self.dist)
        return px.reshape(-1, 2)

    @property
    def altura_camara(self):
        return abs(self.C[2])

    @property
    def inclinacion_deg(self):
        """Angle between the optical axis and the floor normal."""
        axis_w = self.R.T @ np.array([0.0, 0.0, 1.0])
        return float(np.degrees(np.arccos(min(1.0, abs(axis_w[2])))))


def marker_pose_on_plane(plano, corners_px, h_mm):
    """Marker corners (4,2 distorted px) -> centre (x,y), theta, side (mm)
    by intersecting the 4 corner rays with the plane z = h."""
    W = plano.pixels_to_plane(corners_px, h_mm)            # TL, TR, BR, BL
    centre = W.mean(axis=0)                                  # exact on a metric plane
    xdir = (W[1] + W[2]) / 2 - (W[0] + W[3]) / 2              # marker +x
    theta = float(np.degrees(np.arctan2(xdir[1], xdir[0])))
    side = float(np.mean([np.linalg.norm(W[(i + 1) % 4] - W[i]) for i in range(4)]))
    return centre, theta, side
