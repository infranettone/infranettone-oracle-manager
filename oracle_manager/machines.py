"""Máquinas guardadas en ~/.config/infranettone-oracle/config.json. La contraseña SSH nunca se guarda."""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from gi.repository import GLib

CONFIG_FILE = Path(GLib.get_user_config_dir()) / "infranettone-oracle" / "config.json"
AUTH_MODES = [("agent", "Agente SSH / claves"), ("password", "Contraseña"), ("key", "Clave / certificado")]
# Sin «-» al principio: ssh lo tomaría por una opción (p. ej. -oProxyCommand=…).
_NAME = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.@-]*$")
_HOST = re.compile(r"^[A-Za-z0-9_.:\[\]][A-Za-z0-9_.:\[\]-]*$")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Machine:
    name: str
    host: str
    user: str = "root"
    port: int = 22
    auth: str = "agent"
    id: str = field(default_factory=new_id)
    # Solo con auth «key»: rutas de la clave privada y, si se usa, del certificado firmado por la CA (*-cert.pub).
    key_file: str = ""
    cert_file: str = ""

    @property
    def label(self) -> str:
        return self.name or self.host

    def validate(self) -> None:
        if not _HOST.match(self.host):
            raise ValueError("Host no válido (IP o nombre, sin espacios)")
        if not _NAME.match(self.user):
            raise ValueError("Usuario SSH no válido")
        if not 0 < self.port < 65536:
            raise ValueError("Puerto no válido")
        if self.auth not in dict(AUTH_MODES):
            raise ValueError("Autenticación no válida")
        if self.auth == "key":
            if not self.key_file or not Path(self.key_file).expanduser().is_file():
                raise ValueError("No encuentro la clave privada")
            if self.cert_file and not Path(self.cert_file).expanduser().is_file():
                raise ValueError("No encuentro el certificado")


@dataclass
class Config:
    machines: list[Machine] = field(default_factory=list)
    show_oracle: bool = False
    dump_folder: str = ""  # carpeta local de los volcados; vacía = Descargas

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(Machine)}
        machines = [Machine(**{k: v for k, v in m.items() if k in known}) for m in data.get("machines", [])]
        if not machines and data.get("host"):  # versión anterior: una sola máquina
            machines = [Machine(data["host"], data["host"], data.get("user", "root"), auth=data.get("auth", "agent"))]
        return cls(machines, bool(data.get("show_oracle")), str(data.get("dump_folder") or ""))

    def save(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
