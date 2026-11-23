"""Adaptador de Webots con la MISMA interfaz que RobotOAK9 (robot real) y RobotSimulado (pruebas).

Pose estimada como en el robot real: encoders para la distancia y giroscopio integrado para el
rumbo (odometria de o1, el centro entre ruedas). La pose real del Supervisor solo se registra,
nunca se usa para controlar. El movimiento lo hace NavegadorPC (robocoffe_fsm/movimiento.py),
el mismo lazo cerrado que corre en el OAK9.
"""
import math

from robocoffe_fsm.movimiento import EN_CURSO, TERMINADO

RHO = 0.034            # [m] radio de rueda (o_1.STL)


class RobotWebots:
    def __init__(self, robot, nav, aceleracion=10.0, zona_muerta_deg=0.0):
        self.robot, self.nav = robot, nav
        self.zona_muerta = zona_muerta_deg          # [deg/s de rueda] emula la del OAK9 (0 = sin zona muerta)
        dt_ms = int(robot.getBasicTimeStep())
        self.m_der = robot.getDevice("right_wheel_joint")
        self.m_izq = robot.getDevice("left_wheel_joint")
        for m in (self.m_der, self.m_izq):
            m.setPosition(float("inf"))
            m.setVelocity(0.0)
            m.setAcceleration(aceleracion)         # [rad/s^2] el real esta <POR MEDIR>
        self.s_der = robot.getDevice("right_wheel_joint_sensor")
        self.s_izq = robot.getDevice("left_wheel_joint_sensor")
        self.gyro = robot.getDevice("gyro")
        for d in (self.s_der, self.s_izq, self.gyro):
            d.enable(dt_ms)
        self.nodo = robot.getSelf()
        self.base_real = None                       # pose real en la que se puso la base
        self.x = self.y = self.th = 0.0             # odometria (th en rad, continua)
        self.phi = None
        self.n_inicios = 0

    # ------------------------------------------------------------ odometria (cada paso basico)
    def integrar(self, dt):
        pd, pi = self.s_der.getValue(), self.s_izq.getValue()
        if self.phi is None or math.isnan(pd):
            self.phi = (pd, pi)
            return
        dd, di = pd - self.phi[0], pi - self.phi[1]
        self.phi = (pd, pi)
        th_nuevo = self.th + self.gyro.getValues()[2] * dt
        ds, th_med = RHO / 2.0 * (dd + di), 0.5 * (self.th + th_nuevo)
        self.x += ds * math.cos(th_med)
        self.y += ds * math.sin(th_med)
        self.th = th_nuevo

    def pose(self):
        return self.x, self.y, math.degrees(self.th)

    def pose_real(self):
        """Pose real de o1 (Supervisor) en el marco de la base."""
        p, R = self.nodo.getPosition(), self.nodo.getOrientation()
        return p[0], p[1], math.degrees(math.atan2(R[3], R[0]))

    def _ruedas(self, izq_deg, der_deg):
        def rad(w):
            return 0.0 if abs(w) < self.zona_muerta else math.radians(w)
        self.m_izq.setVelocity(rad(izq_deg))
        self.m_der.setVelocity(rad(der_deg))

    # ------------------------------------------------------------ interfaz de la maquina
    def verificar_inicio(self, t=0.0):
        """Como encoder.reset + pose.set(0,0,0). Despues de un RESET, 'una persona' pone el robot en la base."""
        self.n_inicios += 1
        if self.n_inicios > 1:
            self.nodo.getField("translation").setSFVec3f([0.0, 0.0, 0.0])
            self.nodo.getField("rotation").setSFRotation([0.0, 0.0, 1.0, 0.0])
            self.nodo.resetPhysics()
        self._ruedas(0.0, 0.0)
        self.nav.detener()
        self.x = self.y = self.th = 0.0
        self.phi = None
        return True, "ok_webots"

    def leer(self, t):
        p = self.pose()
        if self.nav.activo:
            izq, der, _ = self.nav.paso(p, t)
            self._ruedas(izq, der)
        estado = EN_CURSO if self.nav.activo else TERMINADO
        return {"pose": p, "nav": estado, "dist_frontal": None, "bateria": None}

    def iniciar_tramo(self, tramo, t):
        self.nav.iniciar(tramo, self.pose(), t)
        return True

    def detener(self, t):
        self.nav.detener()
        self._ruedas(0.0, 0.0)

    def indicar(self, texto, t=0.0):
        pass
