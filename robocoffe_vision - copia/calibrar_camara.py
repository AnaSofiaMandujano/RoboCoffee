"""Script 2 - Calibración intrínseca de la cámara con tablero ChArUco.

Obtiene la matriz K y los coeficientes de distorsión del lente. Solo se hace
una vez por cámara (y de nuevo si cambian resolución, foco o zoom).

Modos:
  python calibrar_camara.py capturar     vista en vivo; captura fotos del tablero
  python calibrar_camara.py calibrar     calcula K y dist con las fotos -> datos/camara.yaml
  python calibrar_camara.py verificar    vista en vivo original vs. corregida
"""
import argparse
import math
import time
from pathlib import Path

import cv2
import numpy as np

from common import (ROOT, load_config, charuco_board, open_camera, camera_report,
                    save_yaml, load_camera_calibration)

IMG_DIR = ROOT / "datos" / "calib_imgs"
MIN_CORNERS = 12
RMS_TARGET = 0.5
OUTLIER_FACTOR = 2.0


def make_detector(cfg):
    board = charuco_board(cfg)
    params = cv2.aruco.CharucoParameters()
    det_params = cv2.aruco.DetectorParameters()
    det_params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    return board, cv2.aruco.CharucoDetector(board, params, det_params)


def detect(detector, gray):
    ch_corners, ch_ids, _, _ = detector.detectBoard(gray)
    n = 0 if ch_ids is None else len(ch_ids)
    return ch_corners, ch_ids, n


def capture(cfg):
    board, detector = make_detector(cfg)
    cap = open_camera(cfg)
    print("Ajustes reportados por la cámara:", camera_report(cap))
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    count = len(list(IMG_DIR.glob("*.png")))
    coverage = None
    last_auto, auto = 0.0, False
    print("[ESPACIO] capturar  [A] auto-captura on/off  [Q] salir")

    while True:
        ok, frame = cap.read()
        if not ok:
            print("No llegan imágenes de la cámara")
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if coverage is None:
            coverage = np.zeros(gray.shape, np.uint8)
        corners, ids, n = detect(detector, gray)
        view = frame.copy()
        if n:
            cv2.aruco.drawDetectedCornersCharuco(view, corners, ids, (0, 0, 255))
        green = np.zeros_like(view)
        green[:, :, 1] = coverage
        view = cv2.addWeighted(view, 1.0, green, 0.35, 0)
        sharp = cv2.Laplacian(gray, cv2.CV_64F).var()
        txt = f"fotos:{count}  esquinas:{n}  nitidez:{sharp:.0f}  auto:{'ON' if auto else 'off'}"
        cv2.putText(view, txt, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        cv2.imshow("calibracion", cv2.resize(view, None, fx=0.5, fy=0.5))

        key = cv2.waitKey(1) & 0xFF
        want = key == ord(" ") or (auto and time.time() - last_auto > 1.5)
        if want and n >= MIN_CORNERS:
            cv2.imwrite(str(IMG_DIR / f"calib_{count:03d}.png"), frame)
            hull = cv2.convexHull(corners.reshape(-1, 2).astype(np.int32))
            cv2.fillConvexPoly(coverage, hull, 255)
            count += 1
            last_auto = time.time()
            print(f"  foto {count} guardada ({n} esquinas)")
        if key == ord("a"):
            auto = not auto
        if key == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows()
    frac = 0 if coverage is None else (coverage > 0).mean()
    print(f"{count} fotos. Cobertura de la imagen: {frac:.0%} (objetivo > 80 %)")


def collect_points(cfg, folder):
    board, detector = make_detector(cfg)
    obj_pts, img_pts, names, size = [], [], [], None
    for p in sorted(Path(folder).glob("*.png")) + sorted(Path(folder).glob("*.jpg")):
        gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        size = gray.shape[::-1]
        corners, ids, n = detect(detector, gray)
        if n < MIN_CORNERS:
            print(f"  descartada {p.name}: {n} esquinas")
            continue
        op, ip = board.matchImagePoints(corners, ids)
        obj_pts.append(op.astype(np.float32))
        img_pts.append(ip.astype(np.float32))
        names.append(p.name)
    return obj_pts, img_pts, names, size


def per_view_errors(obj_pts, img_pts, K, dist, rvecs, tvecs):
    errs = []
    for op, ip, r, t in zip(obj_pts, img_pts, rvecs, tvecs):
        proj, _ = cv2.projectPoints(op, r, t, K, dist)
        errs.append(float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - ip.reshape(-1, 2)) ** 2, 1)))))
    return np.array(errs)


def run_calibration(obj_pts, img_pts, size):
    flags = 0
    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(obj_pts, img_pts, size, None, None, flags=flags)
    return rms, K, dist, rvecs, tvecs, flags


def calibrate(cfg, folder=IMG_DIR, out=None):
    obj_pts, img_pts, names, size = collect_points(cfg, folder)
    if len(obj_pts) < 10:
        raise SystemExit(f"Solo {len(obj_pts)} fotos válidas; se necesitan al menos 10 (ideal 25-40).")

    rms, K, dist, rvecs, tvecs, flags = run_calibration(obj_pts, img_pts, size)
    errs = per_view_errors(obj_pts, img_pts, K, dist, rvecs, tvecs)
    keep = errs <= max(OUTLIER_FACTOR * np.median(errs), RMS_TARGET)
    if (~keep).any():
        print("  vistas atípicas eliminadas:", [n for n, k in zip(names, keep) if not k])
        obj_pts = [o for o, k in zip(obj_pts, keep) if k]
        img_pts = [i for i, k in zip(img_pts, keep) if k]
        names = [n for n, k in zip(names, keep) if k]
        rms, K, dist, rvecs, tvecs, flags = run_calibration(obj_pts, img_pts, size)
        errs = per_view_errors(obj_pts, img_pts, K, dist, rvecs, tvecs)

    w, h = size
    fx, fy = K[0, 0], K[1, 1]
    fov_h = math.degrees(2 * math.atan(w / (2 * fx)))
    fov_v = math.degrees(2 * math.atan(h / (2 * fy)))
    H = cfg["camara"]["altura_lente_mm"]
    foot_w, foot_h = w * H / fx, h * H / fy
    h_rob = cfg["robot"]["altura_marcador_mm"]["sin_vaso"]
    data = {
        "fecha": time.strftime("%Y-%m-%d %H:%M"),
        "resolucion": [int(w), int(h)],
        "n_vistas": len(obj_pts),
        "rms_px": round(float(rms), 4),
        "error_medio_px": round(float(errs.mean()), 4),
        "error_max_px": round(float(errs.max()), 4),
        "modelo": "racional (8 coef.)" if flags else "estándar (5 coef.)",
        "K": K.round(4).tolist(),
        "dist": dist.ravel().round(6).tolist(),
        "fov_horizontal_deg": round(fov_h, 2),
        "fov_vertical_deg": round(fov_v, 2),
        "huella_piso_mm_a_altura_config": [int(round(foot_w)), int(round(foot_h))],
        "mm_por_px_piso": round(float(H / fx), 3),
        "mm_por_px_marcador_robot": round(float((H - h_rob) / fx), 3),
        "vistas": dict(zip(names, errs.round(4).tolist())),
    }
    out = Path(out) if out else ROOT / cfg["camara"]["archivo_calibracion"]
    save_yaml(data, out)

    a = cfg["area"]
    fits = (max(foot_w, foot_h) >= max(a["largo_x_mm"], a["ancho_y_mm"]) and
            min(foot_w, foot_h) >= min(a["largo_x_mm"], a["ancho_y_mm"]))
    print(f"\nRMS = {rms:.3f} px  ({'OK' if rms <= RMS_TARGET else 'ALTO: repetir capturas'}; objetivo <= {RMS_TARGET})")
    print(f"Vistas usadas: {len(obj_pts)}   error por vista: media {errs.mean():.3f}, máx {errs.max():.3f} px")
    print(f"FOV: {fov_h:.1f}° x {fov_v:.1f}°")
    print(f"Huella en el piso a {H} mm: {foot_w:.0f} x {foot_h:.0f} mm  "
          f"-> área {a['largo_x_mm']}x{a['ancho_y_mm']}: {'CABE' if fits else 'NO CABE (subir cámara o reubicar)'}")
    print(f"Resolución: {H / fx:.2f} mm/px en el piso, {(H - h_rob) / fx:.2f} mm/px en el marcador del robot")
    print(f"Guardado en {out}")
    return data


def verify(cfg):
    K, dist, _ = load_camera_calibration(cfg)
    cap = open_camera(cfg)
    ok, frame = cap.read()
    h, w = frame.shape[:2]
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), 0)
    mx, my = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_16SC2)
    print("Las líneas de la loseta deben verse RECTAS en la imagen corregida. [Q] salir")
    while ok:
        und = cv2.remap(frame, mx, my, cv2.INTER_LINEAR)
        both = np.hstack([cv2.resize(frame, None, fx=0.4, fy=0.4), cv2.resize(und, None, fx=0.4, fy=0.4)])
        cv2.imshow("original | corregida", both)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
        ok, frame = cap.read()
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("modo", nargs="?", default="capturar", choices=["capturar", "calibrar", "verificar"])
    ap.add_argument("--carpeta", default=str(IMG_DIR), help="carpeta de fotos para 'calibrar'")
    ap.add_argument("--salida", default=None, help="ruta del yaml de salida")
    args = ap.parse_args()
    cfg = load_config()
    {"capturar": lambda: capture(cfg),
     "calibrar": lambda: calibrate(cfg, args.carpeta, args.salida),
     "verificar": lambda: verify(cfg)}[args.modo]()