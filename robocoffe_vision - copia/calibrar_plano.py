"""Script 3 - Calibración extrínseca del piso (pose cámara <-> área de prueba).

Con el área DESPEJADA (sin robot ni personas encima de los marcadores):
  1. Captura N cuadros y detecta los marcadores del piso (IDs de config.yaml)
     con refinamiento subpíxel. Las esquinas se toman como la MEDIANA de
     todos los cuadros (reduce ruido de detección).
  2. Homografía con los CENTROS de los marcadores -> verifica que cada
     marcador esté orientado con +X (detecta hojas giradas 90/180°).
  3. solvePnP (IPPE + refinamiento LM) con las 4 esquinas de los marcadores
     bien orientados, a z = 0, con K y dist de datos/camara.yaml.
  4. Reporta el error en mm de cada marcador (centro detectado vs. posición
     en config.yaml), el lado medido (verifica la escala de impresión),
     la altura e inclinación estimadas de la cámara, y el RMS del plano.
  5. Guarda datos/plano.yaml y datos/plano_verificacion.png.

Las esquinas se detectan en la imagen ORIGINAL y la distorsión se corrige
analíticamente con dist (más exacto que detectar sobre la imagen remapeada).

Uso:
  python calibrar_plano.py                 # cámara en vivo, 30 cuadros
  python calibrar_plano.py --frames 60
  python calibrar_plano.py --imagen foto.png   # procesar una foto guardada
  python calibrar_plano.py --sin_ventana       # sin ventana (solo consola)
"""
import argparse
import time

import cv2
import numpy as np

from common import ROOT, load_config, aruco_detector, load_camera_calibration, save_yaml
from geometria import FrameSource, PlanoPiso, undistort_px, diagonal_center

OUT_YAML = ROOT / "datos" / "plano.yaml"
OUT_PNG = ROOT / "datos" / "plano_verificacion.png"
ORIENT_TOL_DEG = 15.0     # a marker rotated more than this is not used for its corners
RMS_OK_MM = 5.0
RMS_WARN_MM = 10.0
# Corner offsets (TL, TR, BR, BL) in marker units; Y sign decided from data
CORNER_UNIT = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], np.float64) / 2.0


def to_py(o):
    """numpy scalars/arrays -> plain Python (PyYAML safe_dump)."""
    if isinstance(o, dict):
        return {(int(k) if isinstance(k, np.integer) else k): to_py(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_py(v) for v in o]
    if isinstance(o, np.ndarray):
        return to_py(o.tolist())
    if isinstance(o, np.generic):
        return o.item()
    return o


def capture_corners(cfg, src, n_frames, ids_wanted, show):
    det = aruco_detector(cfg)
    samples = {i: [] for i in ids_wanted}
    last = None
    got = 0
    # Let auto-adjust / buffers settle on a live camera
    if src.kind == "camera":
        for _ in range(10):
            src.read()
    while got < n_frames:
        ok, frame, _ = src.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = det.detectMarkers(gray)
        last = frame
        if ids is None:
            continue
        for c, i in zip(corners, ids.ravel()):
            if int(i) in samples:
                samples[int(i)].append(c.reshape(4, 2))
        got += 1
        if show:
            v = frame.copy()
            cv2.aruco.drawDetectedMarkers(v, corners, ids)
            cv2.putText(v, f"capturando {got}/{n_frames}", (30, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 255), 3)
            cv2.imshow("calibrar_plano", cv2.resize(v, None, fx=0.5, fy=0.5))
            cv2.waitKey(1)
    med = {i: np.median(np.stack(s), axis=0) for i, s in samples.items() if s}
    counts = {i: len(s) for i, s in samples.items()}
    return med, counts, last, got


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--imagen", default=None, help="procesar una imagen en lugar de la cámara")
    ap.add_argument("--sin_ventana", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    K, dist, _ = load_camera_calibration(cfg)
    side = float(cfg["marcadores_piso"]["lado_mm"])
    nominal = {int(k): np.array(v, np.float64) for k, v in cfg["marcadores_piso"]["posiciones"].items()}
    show = not args.sin_ventana

    src = FrameSource(cfg, image=args.imagen, repeat=args.frames)
    print(f"Capturando {args.frames} cuadros (área despejada)...")
    corners, counts, frame, got = capture_corners(cfg, src, args.frames, list(nominal), show)
    src.release()

    found = sorted(corners)
    missing = [i for i in nominal if i not in corners]
    print(f"Cuadros con detecciones: {got}. Marcadores detectados: {found}"
          + (f"   NO detectados: {missing}" if missing else ""))
    for i in found:
        if counts[i] < 0.8 * got:
            print(f"  aviso: ID {i} solo apareció en {counts[i]}/{got} cuadros (¿sombra, reflejo, borde?)")
    if len(found) < 4:
        raise SystemExit("Se necesitan al menos 4 marcadores del piso detectados. Revisa iluminación/encuadre.")

    # ---- 1) Centres-only homography (rotation-invariant) ------------------
    und = {i: undistort_px(corners[i], K, dist) for i in found}           # ideal pixels
    ctr_px = np.array([diagonal_center(und[i]) for i in found])
    ctr_w = np.array([nominal[i] for i in found])
    Hc, _ = cv2.findHomography(ctr_w, ctr_px, 0)                          # world -> px
    Hc_inv = np.linalg.inv(Hc)

    # ---- 2) Orientation of every marker ---------------------------------
    orient, ysigns = {}, {}
    for i in found:
        w = cv2.perspectiveTransform(und[i].reshape(-1, 1, 2), Hc_inv).reshape(4, 2)
        ex = (w[1] + w[2]) / 2 - (w[0] + w[3]) / 2
        ey = (w[3] + w[2]) / 2 - (w[0] + w[1]) / 2
        orient[i] = float(np.degrees(np.arctan2(ex[1], ex[0])))
        ysigns[i] = 1.0 if (ex[0] * ey[1] - ex[1] * ey[0]) > 0 else -1.0
    ysign = 1.0 if sum(ysigns.values()) >= 0 else -1.0
    good = [i for i in found if abs(orient[i]) <= ORIENT_TOL_DEG and ysigns[i] == ysign]
    bad = [i for i in found if i not in good]
    for i in bad:
        print(f"  AVISO: ID {i} está girado {orient[i]:+.0f}° respecto a +X "
              f"-> no se usan sus esquinas (solo su centro). Gíralo y repite.")
    if len(good) < 2:
        raise SystemExit("Menos de 2 marcadores bien orientados; revisa que la flecha +X de cada hoja apunte igual.")

    # ---- 3) solvePnP with all corners of well-oriented markers -----------
    obj, img = [], []
    for i in good:
        cw = nominal[i] + side * CORNER_UNIT * np.array([1.0, ysign])
        obj.append(np.hstack([cw, np.zeros((4, 1))]))
        img.append(corners[i])
    obj = np.vstack(obj).astype(np.float64)
    img = np.vstack(img).astype(np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, dist, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        raise SystemExit("solvePnP falló")
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, K, dist, rvec, tvec)
    proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
    reproj = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - img) ** 2, axis=1))))

    R, _ = cv2.Rodrigues(rvec)
    C = (-R.T @ tvec).ravel()
    up = 1.0 if C[2] > 0 else -1.0
    plano = PlanoPiso(K, dist, rvec, tvec, up)

    # ---- 4) Per-marker errors in mm -------------------------------------
    print("\n ID | nominal (x, y) mm | medido (x, y) mm    |  dx    dy  | error | lado  | giro")
    print("----+-------------------+---------------------+-------------+-------+-------+------")
    rows, errs = {}, []
    for i in found:
        # centre from the 4 corner rays on z = 0 (exact metric mean)
        W = plano.pixels_to_plane(corners[i], 0.0)
        c = W.mean(axis=0)
        lado = float(np.mean([np.linalg.norm(W[(k + 1) % 4] - W[k]) for k in range(4)]))
        d = c - nominal[i]
        e = float(np.hypot(*d))
        if i in good:
            errs.append(e)
        flag = "" if i in good else "  <- no usado"
        print(f" {i:2d} | ({nominal[i][0]:6.0f},{nominal[i][1]:6.0f}) | ({c[0]:8.1f},{c[1]:8.1f}) |"
              f" {d[0]:+5.1f} {d[1]:+5.1f} | {e:5.1f} | {lado:5.1f} | {orient[i]:+4.0f}°{flag}")
        rows[i] = {"nominal": nominal[i].tolist(), "medido": [round(float(c[0]), 2), round(float(c[1]), 2)],
                   "error_mm": round(e, 2), "lado_mm": round(lado, 2), "giro_deg": round(orient[i], 1),
                   "detecciones": counts[i], "usado": i in good}
    rms = float(np.sqrt(np.mean(np.square(errs))))
    H_mm_a_px = K @ np.column_stack([R[:, 0], R[:, 1], tvec.ravel()])
    H_mm_a_px /= H_mm_a_px[2, 2]
    H_px_a_mm = np.linalg.inv(H_mm_a_px)
    H_px_a_mm /= H_px_a_mm[2, 2]

    alt_cfg = cfg["camara"].get("altura_lente_mm")
    print(f"\nRMS del plano (marcadores usados): {rms:.2f} mm   "
          f"[{'OK' if rms <= RMS_OK_MM else 'ACEPTABLE' if rms <= RMS_WARN_MM else 'ALTO'}; objetivo <= {RMS_OK_MM} mm]")
    print(f"Error de reproyección: {reproj:.3f} px")
    print(f"Altura estimada de la cámara: {plano.altura_camara:.0f} mm"
          + (f"  (cinta: {alt_cfg} mm, diferencia {plano.altura_camara - alt_cfg:+.0f} mm)" if alt_cfg else ""))
    print(f"Inclinación del eje óptico respecto a la vertical: {plano.inclinacion_deg:.2f}°")
    print(f"Centro óptico sobre el piso en (x, y) = ({C[0]:.0f}, {C[1]:.0f}) mm")
    if rms > RMS_WARN_MM:
        print("  -> Error alto: re-mide con cinta los centros de los marcadores y corrige config.yaml,"
              " o revisa que estén planos (sin ondulaciones).")
    lados = [rows[i]["lado_mm"] for i in good]
    if abs(np.mean(lados) - side) > 2.0:
        print(f"  -> El lado medido promedio ({np.mean(lados):.1f} mm) difiere de {side} mm: "
              "verifica la escala de impresión con regla y corrige marcadores_piso.lado_mm.")

    data = {
        "fecha": time.strftime("%Y-%m-%d %H:%M"),
        "n_cuadros": int(got),
        "marcadores_usados": [int(i) for i in good],
        "signo_y_marcador": float(ysign),
        "signo_arriba": up,
        "rvec": rvec.ravel().round(8).tolist(),
        "tvec": tvec.ravel().round(4).tolist(),
        "R": R.round(8).tolist(),
        "centro_camara_mm": C.round(2).tolist(),
        "altura_camara_mm": round(plano.altura_camara, 2),
        "inclinacion_deg": round(plano.inclinacion_deg, 3),
        "rms_plano_mm": round(rms, 3),
        "reproyeccion_px": round(reproj, 4),
        "nota_H": "H_px_a_mm: píxel SIN distorsión (undistortPoints con P=K) -> (x,y) mm en z=0",
        "H_px_a_mm": H_px_a_mm.round(10).tolist(),
        "H_mm_a_px": H_mm_a_px.round(8).tolist(),
        "marcadores": rows,
    }
    save_yaml(to_py(data), OUT_YAML)
    print(f"\nGuardado: {OUT_YAML.relative_to(ROOT)}")

    # ---- 5) Visual check --------------------------------------------------
    v = frame.copy()
    a = cfg["area"]
    edge = np.vstack([np.linspace([0, 0], [a["largo_x_mm"], 0], 50),
                      np.linspace([a["largo_x_mm"], 0], [a["largo_x_mm"], a["ancho_y_mm"]], 50),
                      np.linspace([a["largo_x_mm"], a["ancho_y_mm"]], [0, a["ancho_y_mm"]], 50),
                      np.linspace([0, a["ancho_y_mm"]], [0, 0], 50)])
    cv2.polylines(v, [plano.project(edge).astype(np.int32)], True, (255, 200, 0), 3)
    o, x, y = plano.project([[0, 0], [300, 0], [0, 300]])
    cv2.arrowedLine(v, tuple(o.astype(int)), tuple(x.astype(int)), (0, 0, 255), 5, tipLength=0.15)
    cv2.arrowedLine(v, tuple(o.astype(int)), tuple(y.astype(int)), (0, 255, 0), 5, tipLength=0.15)
    cv2.putText(v, "+X", tuple((x + [10, 0]).astype(int)), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 255), 3)
    cv2.putText(v, "+Y", tuple((y + [10, 0]).astype(int)), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 3)
    for i in found:
        col = (0, 200, 0) if i in good else (0, 0, 255)
        cv2.polylines(v, [corners[i].astype(np.int32)], True, col, 3)
        n = plano.project([nominal[i]])[0]
        cv2.drawMarker(v, tuple(n.astype(int)), (255, 0, 255), cv2.MARKER_CROSS, 30, 2)
        cv2.putText(v, f"ID {i}  {rows[i]['error_mm']:.1f} mm", tuple((corners[i][0] + [0, -15]).astype(int)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, col, 3)
    cv2.putText(v, f"RMS plano {rms:.2f} mm | altura {plano.altura_camara:.0f} mm | "
                   f"inclinacion {plano.inclinacion_deg:.2f} deg", (30, v.shape[0] - 30),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3)
    cv2.imwrite(str(OUT_PNG), v)
    print(f"Imagen de verificación: {OUT_PNG.relative_to(ROOT)}")
    if show:
        print("Cruz magenta = posición de config.yaml; contorno = detección. Cualquier tecla para cerrar.")
        cv2.imshow("calibrar_plano", cv2.resize(v, None, fx=0.5, fy=0.5))
        cv2.waitKey(0)
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
