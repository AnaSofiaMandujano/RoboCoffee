"""Script 4 - Localización del robot en tiempo real y prueba de repetibilidad A <-> B.

Soporta cambio automático de altura del marcador ID 40 en vivo:
  * Viaje A -> B con vaso (ej. 360 mm) y regreso B -> A sin vaso (ej. 180 mm).
  * Elige automáticamente en cada cuadro la altura de config.yaml cuyo lado
    reconstruido se acerca más al tamaño nominal (60.0 mm).

Teclas: ESPACIO medición manual | R reiniciar ciclos (nueva sesión)
        E calibrar excentricidad (girar el robot sobre su eje; E otra vez para terminar)
        Q salir

Uso:
  python medir_robot.py --grabar
  python medir_robot.py --condicion sin_vaso   # forzar una altura fija
  python medir_robot.py --condicion con_vaso   # forzar una altura fija
"""
import argparse
import csv
import time
from collections import Counter, deque
from pathlib import Path

import cv2
import numpy as np

from common import ROOT, load_config, aruco_detector, load_camera_calibration
from geometria import (FrameSource, PlanoPiso, marker_pose_on_plane, wrap_deg,
                       circular_mean_deg, circular_std_deg, rot2)

RES_CSV = ROOT / "datos" / "resultados_repetibilidad.csv"
COLUMNS = ["sesion", "ciclo", "tipo", "destino", "fecha_hora", "t_s",
           "x_obj_mm", "y_obj_mm", "th_obj_deg",
           "x_mm", "y_mm", "theta_deg", "dx_mm", "dy_mm", "dth_deg",
           "e_long_mm", "e_lat_mm", "error_mm", "cumple",
           "t_tramo_s", "rumbo_mov_deg",
           "x_o1_mm", "y_o1_mm", "x_o5_mm", "y_o5_mm",
           "sd_x_mm", "sd_y_mm", "sd_th_deg", "n_frames", "lado_mm", "h_est_mm",
           "punto_evaluado", "condicion"]


# --------------------------------------------------------------------------
class Robot:
    def __init__(self, cfg, plano, condicion="auto"):
        r = cfg["robot"]
        self.id = int(r["id_marcador"])
        self.side = float(r["lado_marcador_mm"])
        alturas = r["altura_marcador_mm"]

        if condicion == "auto":
            self.heights = {k: float(v) for k, v in alturas.items() if v is not None}
            if not self.heights:
                raise SystemExit("No hay alturas definidas en robot.altura_marcador_mm dentro de config.yaml.")
        else:
            h = alturas.get(condicion)
            if h is None:
                raise SystemExit(f"robot.altura_marcador_mm.{condicion} no está definida en config.yaml.")
            self.heights = {condicion: float(h)}

        self.condicion_modo = condicion
        self.offsets = {
            "default": np.array(r.get("offset_marcador_mm", [0.0, 0.0]), np.float64),
            "con_vaso": np.array(r.get("offset_marcador_con_vaso_mm", r.get("offset_marcador_mm", [0.0, 0.0])), np.float64),
        }
        self.off_thetas = {
            "default": float(r.get("offset_theta_deg", 0.0)),
            "con_vaso": float(r.get("offset_theta_con_vaso_deg", r.get("offset_theta_deg", 0.0))),
        }
        self.d5 = float(r.get("adelanto_o5_mm", 100.0))
        self.eval_point = r.get("punto_evaluado", "o1")
        self.plano = plano

    def pose(self, corners_px):
        """Evalúa las alturas configuradas y elige la que reconstruye el lado más cercano al nominal."""
        best = None
        for cond, h in self.heights.items():
            cm, thm, side = marker_pose_on_plane(self.plano, corners_px, h)
            err_side = abs(side - self.side)
            if best is None or err_side < best[0]:
                best = (err_side, cond, h, cm, thm, side)

        _, cond, h, cm, thm, side = best
        offset = self.offsets.get(cond, self.offsets["default"])
        off_th = self.off_thetas.get(cond, self.off_thetas["default"])

        o1 = cm + rot2(thm) @ offset
        th = float(wrap_deg(thm + off_th))
        o5 = o1 + self.d5 * np.array([np.cos(np.radians(th)), np.sin(np.radians(th))])
        D_used = self.plano.altura_camara - h
        h_est = self.plano.altura_camara - D_used * side / self.side
        return {"cm": cm, "thm": thm, "o1": o1, "o5": o5, "th": th,
                "side": side, "h_est": h_est, "h": h, "cond": cond}

    def eval_xy(self, p):
        return p["o5"] if self.eval_point == "o5" else p["o1"]


class QuietDetector:
    def __init__(self, umbral_mm, umbral_deg, ventana_s):
        self.umbral_mm, self.umbral_deg, self.ventana = umbral_mm, umbral_deg, ventana_s
        self.h = deque()

    def reset(self):
        self.h.clear()

    def update(self, t, xy, th, pose=None):
        self.h.append((t, xy[0], xy[1], th, pose))
        while self.h and t - self.h[0][0] > self.ventana:
            self.h.popleft()
        if len(self.h) < 5 or t - self.h[0][0] < 0.9 * self.ventana:
            return False
        a = np.array([e[:4] for e in self.h])
        dev = np.hypot(a[:, 1] - a[:, 1].mean(), a[:, 2] - a[:, 2].mean()).max()
        dth = np.abs(wrap_deg(a[:, 3] - circular_mean_deg(a[:, 3]))).max()
        return dev <= self.umbral_mm and dth <= self.umbral_deg

    def poses(self):
        return [e[4] for e in self.h if e[4] is not None]


def summarize(samples):
    """samples: list of pose dicts -> averaged pose + dispersion."""
    o1 = np.array([s["o1"] for s in samples])
    o5 = np.array([s["o5"] for s in samples])
    th = np.array([s["th"] for s in samples])
    cond = Counter(s["cond"] for s in samples).most_common(1)[0][0]
    return {"o1": o1.mean(0), "o5": o5.mean(0), "th": circular_mean_deg(th),
            "sd": o1.std(0, ddof=1) if len(o1) > 1 else np.zeros(2),
            "sd_th": circular_std_deg(th), "n": len(samples),
            "side": float(np.mean([s["side"] for s in samples])),
            "h_est": float(np.mean([s["h_est"] for s in samples])),
            "cond": cond}


def solve_eccentricity(samples):
    """In-place rotation: marker centre p_i, marker angle t_i.
    Model  p_i + R(t_i) o = c  ->  linear least squares in (c, o)."""
    A, b = [], []
    for s in samples:
        R = rot2(s["thm"])
        A.append(np.hstack([np.eye(2), -R]))
        b.append(s["cm"])
    A = np.vstack(A)
    b = np.concatenate(b)
    x, *_ = np.linalg.lstsq(A, b, rcond=None)
    res = b - A @ x
    span = np.ptp(np.unwrap(np.radians([s["thm"] for s in samples])))
    cond = Counter(s["cond"] for s in samples).most_common(1)[0][0]
    return x[:2], x[2:], float(np.sqrt(np.mean(res ** 2))), float(np.degrees(span)), cond


# --------------------------------------------------------------------------
class Session:
    def __init__(self, cfg, robot, t0_wall):
        p = cfg["prueba"]
        self.targets = {"A": np.array(p["punto_A"], np.float64), "B": np.array(p["punto_B"], np.float64)}
        self.tol = float(p["tolerancia_mm"])
        self.capture = float(p.get("radio_captura_mm", 300.0))
        self.robot = robot
        self.t0_wall = t0_wall
        self.new()

    def new(self):
        self.id = time.strftime("%Y%m%d_%H%M%S")
        self.expected = None
        self.home = None
        self.cycle = 0
        self.last = None
        self.t_depart = None
        self.depart_xy = None
        self.n_ok = self.n_tot = 0
        self.errors = {"A": [], "B": []}

    def nearest(self, xy):
        return min(self.targets, key=lambda k: np.hypot(*(xy - self.targets[k][:2])))

    def record(self, summ, t_arrive, kind_hint):
        """Classify an averaged stop and return a CSV row (dict)."""
        xy = self.robot.eval_xy(summ)
        if kind_hint == "manual":
            dest, kind = self.nearest(xy), "manual"
        elif self.expected is None:
            dest, kind = self.nearest(xy), "inicio"
            self.home = dest
            self.expected = "B" if dest == "A" else "A"
        else:
            dest = self.expected
            if np.hypot(*(xy - self.targets[dest][:2])) <= self.capture:
                kind = "auto"
                if dest != self.home:
                    self.cycle += 1
                self.expected = "B" if dest == "A" else "A"
            else:
                prev = "B" if dest == "A" else "A"
                if np.hypot(*(xy - self.targets[prev][:2])) <= self.capture:
                    kind, dest = "en_destino", prev
                else:
                    kind = "intermedia"
        tgt = self.targets[dest]
        d = xy - tgt[:2]
        other = self.targets["A" if dest == "B" else "B"][:2]
        u = tgt[:2] - other
        u /= np.linalg.norm(u)
        n = np.array([-u[1], u[0]])
        err = float(np.hypot(*d))
        rumbo = ""
        if self.depart_xy is not None and np.hypot(*(summ["o1"] - self.depart_xy)) > 100:
            v = summ["o1"] - self.depart_xy
            rumbo = round(float(np.degrees(np.arctan2(v[1], v[0]))), 2)
        t_tramo = round(t_arrive - self.t_depart, 2) if (self.t_depart is not None and kind != "inicio") else ""
        if kind in ("auto", "inicio"):
            self.n_tot += 1
            self.n_ok += err <= self.tol
            self.errors[dest].append(err)
        row = {
            "sesion": self.id, "ciclo": self.cycle, "tipo": kind, "destino": dest,
            "fecha_hora": time.strftime("%Y-%m-%d %H:%M:%S"), "t_s": round(t_arrive, 3),
            "x_obj_mm": tgt[0], "y_obj_mm": tgt[1], "th_obj_deg": tgt[2],
            "x_mm": round(float(xy[0]), 2), "y_mm": round(float(xy[1]), 2), "theta_deg": round(summ["th"], 2),
            "dx_mm": round(float(d[0]), 2), "dy_mm": round(float(d[1]), 2),
            "dth_deg": round(float(wrap_deg(summ["th"] - tgt[2])), 2),
            "e_long_mm": round(float(d @ u), 2), "e_lat_mm": round(float(d @ n), 2),
            "error_mm": round(err, 2), "cumple": int(err <= self.tol),
            "t_tramo_s": t_tramo, "rumbo_mov_deg": rumbo,
            "x_o1_mm": round(float(summ["o1"][0]), 2), "y_o1_mm": round(float(summ["o1"][1]), 2),
            "x_o5_mm": round(float(summ["o5"][0]), 2), "y_o5_mm": round(float(summ["o5"][1]), 2),
            "sd_x_mm": round(float(summ["sd"][0]), 3), "sd_y_mm": round(float(summ["sd"][1]), 3),
            "sd_th_deg": round(summ["sd_th"], 3), "n_frames": summ["n"],
            "lado_mm": round(summ["side"], 2), "h_est_mm": round(summ["h_est"], 1),
            "punto_evaluado": self.robot.eval_point, "condicion": summ["cond"],
        }
        if kind in ("auto", "inicio"):
            self.last = (xy, t_arrive)
        return row


def append_csv(path, row):
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------
def draw(view, cfg, plano, robot, sess, pose, corners, state, info, fps):
    a = cfg["area"]
    L, W = a["largo_x_mm"], a["ancho_y_mm"]
    edge = np.vstack([np.linspace([0, 0], [L, 0], 40), np.linspace([L, 0], [L, W], 40),
                      np.linspace([L, W], [0, W], 40), np.linspace([0, W], [0, 0], 40)])
    cv2.polylines(view, [plano.project(edge).astype(np.int32)], True, (255, 200, 0), 2)
    ang = np.linspace(0, 2 * np.pi, 60)
    h_draw = pose["h"] if pose is not None else next(iter(robot.heights.values()))
    for k, tgt in sess.targets.items():
        col = (0, 255, 255) if k == sess.expected else (200, 200, 200)
        f = plano.project([tgt[:2]])[0]
        cv2.drawMarker(view, tuple(f.astype(int)), col, cv2.MARKER_CROSS, 25, 2)
        circ = tgt[:2] + sess.tol * np.c_[np.cos(ang), np.sin(ang)]
        cv2.polylines(view, [plano.project(circ, h_draw).astype(np.int32)], True, col, 2)
        cv2.putText(view, k, tuple((f + [30, -30]).astype(int)), cv2.FONT_HERSHEY_SIMPLEX, 1.6, col, 3)
    if pose is not None:
        cv2.polylines(view, [corners.astype(np.int32)], True, (0, 255, 0), 2)
        p = robot.eval_xy(pose)
        tip = p + 120 * np.array([np.cos(np.radians(pose["th"])), np.sin(np.radians(pose["th"]))])
        a0, a1 = plano.project([p, tip], pose["h"])
        cv2.arrowedLine(view, tuple(a0.astype(int)), tuple(a1.astype(int)), (0, 0, 255), 3, tipLength=0.25)
        cv2.circle(view, tuple(a0.astype(int)), 6, (0, 0, 255), -1)

    lines = [f"Estado: {state}   sesion {sess.id}   ciclo {sess.cycle}   {fps:4.1f} fps"]
    if pose is not None:
        p = robot.eval_xy(pose)
        lines.append(f"{robot.eval_point}: X={p[0]:7.1f}  Y={p[1]:7.1f} mm   theta={pose['th']:+7.2f} deg")
        if sess.expected:
            dd = np.hypot(*(p - sess.targets[sess.expected][:2]))
            lines.append(f"Destino {sess.expected}: distancia {dd:6.1f} mm")
        lines.append(f"Modo: {pose['cond']} (h={pose['h']:.0f} mm) | lado {pose['side']:5.1f} mm (nom {robot.side:.0f})")
    else:
        lines.append("SIN MARCADOR DEL ROBOT")
    lines.append(f"Llegadas dentro de tolerancia: {sess.n_ok}/{sess.n_tot}")
    lines += info[-3:]
    lines.append("[ESPACIO] manual  [R] reiniciar  [E] excentricidad  [Q] salir")
    y = 45
    cv2.rectangle(view, (10, 10), (1180, 30 + 42 * len(lines)), (0, 0, 0), -1)
    for s in lines:
        cv2.putText(view, s, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        y += 42


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--condicion", default="auto", help="auto (default), sin_vaso o con_vaso")
    ap.add_argument("--entrada", default=None, help="video para re-procesar (en vez de la cámara)")
    ap.add_argument("--grabar", action="store_true", help="guardar el video crudo de la cámara")
    ap.add_argument("--sin_ventana", action="store_true")
    ap.add_argument("--salida", default=str(RES_CSV))
    args = ap.parse_args()

    cfg = load_config()
    K, dist, _ = load_camera_calibration(cfg)
    plano, pdata = PlanoPiso.load(K, dist)
    robot = Robot(cfg, plano, args.condicion)
    p = cfg["prueba"]
    quiet = QuietDetector(float(p["quieto_umbral_mm"]), float(p.get("quieto_umbral_deg", 1.0)),
                          float(p["quieto_tiempo_s"]))
    n_avg = int(p["frames_promedio"])
    leave_mm = float(p.get("salida_mm", 20.0))
    res_path = Path(args.salida)

    det = aruco_detector(cfg)
    src = FrameSource(cfg, video=args.entrada)
    sess = Session(cfg, robot, time.time())

    def open_traj():
        f = open(ROOT / "datos" / f"trayectoria_{sess.id}.csv", "w", newline="", encoding="utf-8")
        w = csv.writer(f)
        w.writerow(["t_s", "estado", "condicion", "x_o1_mm", "y_o1_mm", "theta_deg", "x_o5_mm", "y_o5_mm", "lado_mm"])
        return f, w

    traj_f, traj_w = open_traj()
    writer = None
    if args.grabar and src.kind == "camera":
        writer = cv2.VideoWriter(str(ROOT / "datos" / f"video_{sess.id}.avi"),
                                 cv2.VideoWriter_fourcc(*"MJPG"), src.fps, (cfg["camara"]["ancho"], cfg["camara"]["alto"]))

    alt_str = ", ".join(f"{k}={v:.0f}mm" for k, v in robot.heights.items())
    print(f"Plano: RMS {pdata['rms_plano_mm']} mm, altura cámara {pdata['altura_camara_mm']:.0f} mm.  "
          f"Alturas marcador ({args.condicion}): {alt_str}.  Punto evaluado: {robot.eval_point}.")
    print(f"Resultados -> {res_path}")

    state, buf, anchor, manual_buf = "MOVIENDO", [], None, None
    anchor_is_arrival, t_at_anchor = False, None
    ecc_buf = None
    info = []
    t_last_seen, t_quiet_start = -1e9, None
    fps, t_prev = 0.0, None

    def log(msg):
        print(msg)
        info.append(msg)

    while True:
        ok, frame, t = src.read()
        if not ok:
            break
        if writer is not None:
            writer.write(frame)
        if t_prev is not None and t > t_prev:
            fps = 0.9 * fps + 0.1 / (t - t_prev)
        t_prev = t

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = det.detectMarkers(gray)
        pose, c4 = None, None
        if ids is not None:
            for c, i in zip(corners, ids.ravel()):
                if int(i) == robot.id:
                    c4 = c.reshape(4, 2)
                    pose = robot.pose(c4)
        if pose is not None:
            t_last_seen = t
            traj_w.writerow([round(t, 3), state, pose["cond"], *np.round(pose["o1"], 2), round(pose["th"], 3),
                             *np.round(pose["o5"], 2), round(pose["side"], 2)])

        # ---- eccentricity capture ------------------------------------------
        if ecc_buf is not None and pose is not None:
            ecc_buf.append(pose)

        # ---- manual measurement ------------------------------------------
        if manual_buf is not None and pose is not None:
            manual_buf.append(pose)
            if len(manual_buf) >= n_avg:
                row = sess.record(summarize(manual_buf), t, "manual")
                append_csv(res_path, row)
                log(f"[manual][{row['condicion']}] {row['destino']}: error {row['error_mm']:.1f} mm "
                    f"(dx {row['dx_mm']:+.1f}, dy {row['dy_mm']:+.1f}), theta {row['theta_deg']:+.2f}")
                manual_buf = None

        # ---- automatic stop detection ------------------------------------
        if pose is None:
            if t - t_last_seen > 1.0 and state == "MIDIENDO":
                state, buf = "MOVIENDO", []
                quiet.reset()
        else:
            xy = pose["o1"]
            is_quiet = quiet.update(t, xy, pose["th"], pose)
            if state == "MOVIENDO" and is_quiet:
                state, buf = "MIDIENDO", quiet.poses()[-n_avg:]
                t_quiet_start = quiet.h[0][0]
            elif state == "MIDIENDO":
                if is_quiet:
                    buf.append(pose)
                else:
                    state, buf = "MOVIENDO", []
            if state == "MIDIENDO" and len(buf) >= n_avg:
                summ = summarize(buf[-n_avg:])
                row = sess.record(summ, t_quiet_start, "auto")
                if row["tipo"] == "en_destino":
                    log(f"Re-estabilizado en {row['destino']} [{row['condicion']}]: no se duplica registro.")
                else:
                    append_csv(res_path, row)
                    ok_txt = "OK" if row["cumple"] else "FUERA"
                    log(f"[{row['tipo']}][{row['condicion']}] ciclo {row['ciclo']} {row['destino']}: "
                        f"error {row['error_mm']:.1f} mm {ok_txt} "
                        f"(long {row['e_long_mm']:+.1f}, lat {row['e_lat_mm']:+.1f}), theta {row['theta_deg']:+.2f}"
                        + (f", tramo {row['t_tramo_s']} s" if row["t_tramo_s"] != "" else ""))
                state, anchor = "DETENIDO", summ["o1"]
                anchor_is_arrival = anchor_is_arrival or (row["tipo"] in ("auto", "inicio"))
                t_at_anchor = t
            elif state == "DETENIDO" and anchor is not None:
                dist_anchor = np.hypot(*(xy - anchor))
                if dist_anchor <= 0.5 * leave_mm:
                    t_at_anchor = t
                if dist_anchor > leave_mm:
                    state = "MOVIENDO"
                    quiet.reset()
                    if anchor_is_arrival:
                        sess.t_depart, sess.depart_xy = t_at_anchor, anchor.copy()
                        anchor_is_arrival = False

        # ---- display / keys ----------------------------------------------
        if not args.sin_ventana:
            v = frame.copy()
            draw(v, cfg, plano, robot, sess, pose, c4, state + (" | EXCENTRICIDAD" if ecc_buf is not None else ""), info, fps)
            cv2.imshow("medir_robot", cv2.resize(v, None, fx=0.5, fy=0.5))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord(" "):
                manual_buf = []
                log("Medición manual: promediando...")
            if key == ord("r"):
                traj_f.close()
                sess.new()
                state, buf, anchor, anchor_is_arrival = "MOVIENDO", [], None, False
                quiet.reset()
                traj_f, traj_w = open_traj()
                log(f"Ciclos reiniciados. Nueva sesión {sess.id}")
            if key == ord("e"):
                if ecc_buf is None:
                    ecc_buf = []
                    log("Excentricidad: gira el robot SOBRE SU EJE (ideal 360°). Pulsa E al terminar.")
                else:
                    if len(ecc_buf) < 30:
                        log("Excentricidad: muy pocos datos.")
                    else:
                        c, o, rms, span, cond = solve_eccentricity(ecc_buf)
                        key_cfg = "offset_marcador_con_vaso_mm" if cond == "con_vaso" else "offset_marcador_mm"
                        log(f"Excentricidad [{cond}]: giro {span:.0f}°, residuo {rms:.2f} mm, eje en ({c[0]:.1f}, {c[1]:.1f})")
                        log(f"  -> config.yaml  robot.{key_cfg}: [{o[0]:.1f}, {o[1]:.1f}]")
                        if span < 180:
                            log("  aviso: giro < 180°, el resultado es poco fiable; repite con una vuelta completa.")
                    ecc_buf = None

    src.release()
    traj_f.close()
    if writer is not None:
        writer.release()
    cv2.destroyAllWindows()
    for k, e in sess.errors.items():
        if e:
            print(f"Destino {k}: n={len(e)}  error medio {np.mean(e):.1f} mm  máx {np.max(e):.1f} mm")
    print(f"Llegadas dentro de tolerancia: {sess.n_ok}/{sess.n_tot}.  CSV: {res_path}")


if __name__ == "__main__":
    main()