"""Shared helpers for the RoboCoffe vision measurement system."""
from pathlib import Path
import cv2
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent


def load_config(path=ROOT / "config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def aruco_dictionary(cfg):
    name = cfg["aruco"]["diccionario"]
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


def aruco_detector(cfg):
    params = cv2.aruco.DetectorParameters()
    # Sub-pixel corner refinement: key for mm-level accuracy
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.cornerRefinementWinSize = 5
    params.cornerRefinementMaxIterations = 50
    params.cornerRefinementMinAccuracy = 0.01
    return cv2.aruco.ArucoDetector(aruco_dictionary(cfg), params)


def charuco_board(cfg):
    c = cfg["charuco"]
    return cv2.aruco.CharucoBoard(
        (c["cuadros_x"], c["cuadros_y"]),
        c["lado_cuadro_mm"], c["lado_marcador_mm"],
        aruco_dictionary(cfg),
    )


def open_camera(cfg):
    """Open the USB camera at full resolution and try to lock auto-adjustments.

    Not every webcam honours these properties; the calibration script reports
    which ones actually took effect.
    """
    c = cfg["camara"]
    cap = cv2.VideoCapture(c["indice"], cv2.CAP_DSHOW) if _is_windows() else cv2.VideoCapture(c["indice"])
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # needed for 1080p@30 on most USB cams
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, c["ancho"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c["alto"])
    cap.set(cv2.CAP_PROP_FPS, c["fps"])
    if c.get("bloquear_autoajustes", True):
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
        cap.set(cv2.CAP_PROP_FOCUS, c.get("foco_manual", 0))
    if not cap.isOpened():
        raise RuntimeError(f"No se pudo abrir la cámara índice {c['indice']}")
    return cap


def camera_report(cap):
    keys = {
        "ancho": cv2.CAP_PROP_FRAME_WIDTH, "alto": cv2.CAP_PROP_FRAME_HEIGHT,
        "fps": cv2.CAP_PROP_FPS, "autofoco": cv2.CAP_PROP_AUTOFOCUS,
        "foco": cv2.CAP_PROP_FOCUS, "auto_exposicion": cv2.CAP_PROP_AUTO_EXPOSURE,
    }
    return {k: cap.get(v) for k, v in keys.items()}


def _is_windows():
    import platform
    return platform.system() == "Windows"


def save_yaml(data, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_camera_calibration(cfg):
    path = ROOT / cfg["camara"]["archivo_calibracion"]
    d = yaml.safe_load(open(path, encoding="utf-8"))
    K = np.array(d["K"], dtype=np.float64)
    dist = np.array(d["dist"], dtype=np.float64)
    return K, dist, d
