"""OAK9 + RoboCoffe en Webots: entregas a 3 mesas en fila (1, 2 y 3 m) con la maquina de estados.

Es la MISMA logica que el robot real:
    robocoffe_fsm/fsm.py         maquina de estados (9 estados, T1-T17)
    robocoffe_fsm/movimiento.py  navegador de lazo cerrado (girar y avanzar con la pose medida)
    robocoffe_fsm/rutas.py       rutas: Mesa n = IR(n, 0); regreso por los mismos puntos
Solo cambia el adaptador (robot_webots.py): motores, encoders y giroscopio de Webots.

Ordenes (tres formas, las tres llegan a la misma maquina):
  1. Teclado, con la vista 3D seleccionada (haz clic en ella):
        1 / 2 / 3   ORDEN a la mesa 1, 2 o 3
        R           el cliente retiro el vaso (adelanta el regreso)
        P           pausa / reanudar        C  cancelar la orden en curso
        X           RESET (despues de un ERROR)        E  ESTADO
  2. Terminal (igual que con el robot real):  python enviar_orden.py MESA2
     (la simulacion escucha en el puerto 9000 de esta computadora)
  3. Demostracion: controllerArgs "modo=demo" -> MESA1, MESA2 y MESA3 seguidas.

controllerArgs (en el .wbt):
  modo=interactivo|demo   (interactivo)
  puerto=9000             (0 = sin TCP)
  reversa=0|1             regresar de espaldas, sin los dos giros de 180 grados (0)
  entrega=10              [s] espera en la mesa si nadie retira el vaso
  zona_muerta=0           [deg/s de rueda] emula la zona muerta del OAK9 (prueba con 25-30)
  v=0.10                  [m/s] velocidad de avance
  quit                    cerrar Webots al terminar la demostracion
Registros: log_entregas.csv (cada 40 ms: estado, pose estimada y real) y transiciones_webots.csv.
"""
import csv
import math
import os
import sys
import time

from controller import Supervisor

from robocoffe_fsm.canales import CanalTCP
from robocoffe_fsm.config import Config
from robocoffe_fsm.ejecutor import Ejecutor
from robocoffe_fsm.fsm import EN_MESA, ERROR, ESPERANDO_ORDEN, DETENIDO, RECORRIDOS, MaquinaEntrega
from robocoffe_fsm.movimiento import NavegadorPC
from robocoffe_fsm.rutas import D_MESAS
from robot_webots import RobotWebots

args = dict(a.split("=", 1) if "=" in a else (a, "1") for a in sys.argv[1:])
MODO = args.get("modo", "interactivo")
PUERTO = int(args.get("puerto", 9000))
QUIT = "quit" in args
AQUI = os.path.dirname(os.path.abspath(__file__))

robot = Supervisor()
BASICO = int(robot.getBasicTimeStep())
N = max(1, round(40 / BASICO))                     # ciclo de la maquina: ~40 ms
cfg = Config(periodo=N * BASICO / 1000.0,
             regreso_en_reversa=args.get("reversa", "0") == "1",
             t_entrega=float(args.get("entrega", 10.0)),
             v_avance=float(args.get("v", 0.10)))
nav = NavegadorPC(v_avance=cfg.v_avance)
oak9 = RobotWebots(robot, nav, zona_muerta_deg=float(args.get("zona_muerta", 0.0)))

canal = None
if PUERTO:
    try:
        canal = CanalTCP(PUERTO)
    except OSError as ex:
        print(f"[RoboCoffe] no pude abrir el puerto {PUERTO} ({ex}); sigo solo con teclado")

teclado = robot.getKeyboard()
teclado.enable(BASICO)
estado_vaso = {"retirado": False}


def prefijo(linea):
    print(f"[{robot.getTime():7.2f} s] {linea}")


ej = Ejecutor(MaquinaEntrega(cfg), oak9, canal, csv_transiciones=os.path.join(AQUI, "transiciones_webots.csv"),
              log=prefijo, reloj=robot.getTime, dormir=None, fuente_vaso=lambda: estado_vaso["retirado"])
fsm = ej.fsm

_ultimo_id = [0]


def nuevo_id():
    """Mismo esquema que enviar_orden.py: crece con el reloj, asi teclado y terminal no chocan."""
    _ultimo_id[0] = max(_ultimo_id[0] + 1, int(time.time() * 10))
    return _ultimo_id[0]


def teclas():
    lineas = []
    k = teclado.getKey()
    while k != -1:
        c = chr(k & 0xFF).upper() if 0 < (k & 0xFF) < 128 else ""
        if c in "123":
            lineas.append(f"ORDEN id={nuevo_id()} destino=MESA{c}")
        elif c == "R" and fsm.estado == EN_MESA:
            estado_vaso["retirado"] = True
            prefijo("vaso retirado (tecla R)")
        elif c == "P":
            cmd = "REANUDAR" if fsm.estado == DETENIDO else "PAUSA"
            lineas.append(f"{cmd} id={nuevo_id()}")
        elif c == "C":
            lineas.append(f"CANCELAR id={nuevo_id()} orden={fsm.orden_id}")
        elif c == "X":
            lineas.append(f"RESET id={nuevo_id()}")
        elif c == "E":
            lineas.append(f"ESTADO id={nuevo_id()}")
        k = teclado.getKey()
    return lineas


f_log = open(os.path.join(AQUI, "log_entregas.csv"), "w", newline="")
w = csv.writer(f_log)
w.writerow(["t", "estado", "orden", "x_est", "y_est", "th_est", "x_real", "y_real", "th_real"])

print(f"[RoboCoffe] modo={MODO}  mesas en x={list(D_MESAS.values())} m  v={cfg.v_avance} m/s  "
      f"regreso={'reversa' if cfg.regreso_en_reversa else 'girando 180'}  "
      f"TCP={'puerto ' + str(PUERTO) if canal else 'no'}")
if MODO != "demo":
    print("[RoboCoffe] Haz clic en la vista 3D y presiona 1, 2 o 3 (o usa enviar_orden.py MESA1 en una terminal)")

demo = ["MESA1", "MESA2", "MESA3"] if MODO == "demo" else []
t_libre = None
resultados = []
paso = 0
estado_prev = None
pendientes = []
while robot.step(BASICO) != -1:
    oak9.integrar(BASICO / 1000.0)
    pendientes += teclas()                           # el teclado se lee en cada paso basico
    paso += 1
    if paso % N:
        continue
    # demostracion: la siguiente orden 1 s despues de quedar libre
    if demo and fsm.estado == ESPERANDO_ORDEN:
        t_libre = t_libre if t_libre is not None else robot.getTime()
        if robot.getTime() - t_libre >= 1.0:
            pendientes.append(f"ORDEN id={nuevo_id()} destino={demo.pop(0)}")
            t_libre = None
    ej.ciclo(entradas=pendientes)
    pendientes = []

    # registro y resumen en cada llegada
    xe, ye, the = oak9.pose()
    xr, yr, thr = oak9.pose_real()
    w.writerow([f"{robot.getTime():.3f}", fsm.estado, fsm.orden_id if fsm.orden_id is not None else "",
                f"{xe:.4f}", f"{ye:.4f}", f"{the:.2f}", f"{xr:.4f}", f"{yr:.4f}", f"{thr:.2f}"])
    if fsm.estado != estado_prev:
        if fsm.estado == EN_MESA or (fsm.estado == ESPERANDO_ORDEN and estado_prev not in (None, "INICIALIZANDO")):
            obj = (D_MESAS[fsm.mesa], 0.0) if fsm.estado == EN_MESA else (0.0, 0.0)
            donde = f"MESA{fsm.mesa}" if fsm.estado == EN_MESA else "BASE"
            ex, ey = (xr - obj[0]) * 1000, (yr - obj[1]) * 1000
            resultados.append((donde, robot.getTime(), ex, ey, thr))
            prefijo(f"*** {donde}: real ({xr:+.3f}, {yr:+.3f}) m, rumbo {thr:+.1f} grados | error real "
                    f"({ex:+.0f}, {ey:+.0f}) mm | odometria vs real {math.hypot(xe - xr, ye - yr) * 1000:.1f} mm")
        if fsm.estado in RECORRIDOS:
            estado_vaso["retirado"] = False
        estado_prev = fsm.estado
    if fsm.estado == ERROR and MODO == "demo":
        prefijo("ERROR en la demostracion; revisa transiciones_webots.csv")
        break
    if MODO == "demo" and not demo and fsm.estado == ESPERANDO_ORDEN and len(resultados) >= 6:
        prefijo("--- demostracion terminada ---")
        for donde, t, ex, ey, thr in resultados:
            print(f"    {donde:6s} t={t:7.2f} s  error real ({ex:+5.0f}, {ey:+5.0f}) mm  rumbo {thr:+6.1f} grados")
        break

f_log.close()
oak9.detener(robot.getTime())
if QUIT:
    robot.simulationQuit(0)
while robot.step(BASICO) != -1:
    pass
