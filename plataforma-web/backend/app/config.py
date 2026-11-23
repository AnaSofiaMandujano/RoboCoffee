"""Settings read from environment variables (.env)."""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "0.5.4-phase5.4"
    database_url: str = "postgresql+psycopg://robocoffee:robocoffee@localhost:5432/robocoffee"

    # Products, tables and provisional order statuses (no migration needed to change them)
    order_config_file: str = str(Path(__file__).with_name("order_config.json"))

    # Robot subsystem: main_real.py (or servidor_simulado.py) listening on TCP
    robot_host: str = "host.docker.internal"
    robot_port: int = 9000
    robot_enabled: bool = True
    # Folder that contains the robot team's robocoffe_cliente.py (mounted read-only from robot/cliente)
    robot_client_dir: str = "/code/robot_cliente"
    # Ask ESTADO every N s while connected, for a live pose (LATIDO has no pose). 0 = off
    robot_poll_s: float = 2.0

    # Scale live view (5.1c): no new reading for this long -> "no_data" (adapter sends every ~0.6 s)
    scale_stale_s: float = 3.0

    # AdapterFSM (5.2). "shadow": decide and record, send NOTHING to the robot. "off": disabled.
    # "on" (sending RETIRADO) is deliberately not accepted yet.
    adapter_mode: Literal["off", "shadow"] = "shadow"
    # Readings older/newer than this (backlog, clock problems) are stored but not fed to the FSM
    adapter_fresh_max_age_s: float = 2.0

    # Exports (Pedidos.csv) use the computer's local time, as the dashboard requires
    export_tz: str = "America/Mexico_City"


settings = Settings()
