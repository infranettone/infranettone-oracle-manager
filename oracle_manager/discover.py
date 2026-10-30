"""Descubre por SSH las instancias Oracle de una máquina y lista sus bases de datos y usuarios/esquemas.

No hace falta conocer los SID ni las contraseñas: en la máquina remota se buscan los procesos `ora_pmon_<SID>`, se
leen ORACLE_SID y ORACLE_HOME de su entorno (/proc/<pid>/environ) y se entra con `sqlplus / as sysdba` como el
dueño del proceso. Si la instancia vive en un contenedor (k3s, docker…) se entra en sus espacios de nombres con
`nsenter`, así que no importa qué orquestador haya. Necesita root (o sudo sin contraseña) en la máquina remota.
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# Va por stdin a `bash -s`: nada de esto pasa por el shell local. Cada línea útil empieza por una etiqueta
# (INST, DB, CON, USR) y los campos van separados por tabuladores; el resto (errores ORA-/SP2-) se guarda aparte.
# El script se comparte con la app de Windows (windows/Core.cs).
REMOTE_SCRIPT = (Path(__file__).parent / "remote" / "discover.sh").read_text(encoding="utf-8")


@dataclass
class User:
    name: str
    status: str
    tablespace: str
    created: str
    oracle_maintained: bool  # usuarios que crea Oracle (SYS, SYSTEM, XDB…), no esquemas de aplicación
    objects: int
    size_mb: int
    # Actividad (ver usage.py). Fechas como «YYYY-MM-DD HH:MM» o vacías.
    last_login: str = ""
    sessions: int = 0  # abiertas ahora
    active_sessions: int = 0  # de ellas, ejecutando algo
    sql_execs: int = 0  # ejecuciones de SQL lanzado por este usuario (lo que queda en la shared pool)
    sql_last: str = ""
    logical_reads: int = 0  # en los objetos del esquema, los use quien los use, desde el arranque
    physical_reads: int = 0
    block_changes: int = 0
    dml: int = 0  # filas insertadas/cambiadas/borradas desde las últimas estadísticas
    dml_last: str = ""
    ddl_last: str = ""
    clients: list["Client"] = field(default_factory=list)
    # Calculado en usage.classify
    last_use: str = ""
    idle_days: int | None = None
    level: str = ""


@dataclass
class Client:
    """Sesiones abiertas ahora, agrupadas por máquina, programa y usuario del sistema operativo."""
    machine: str
    program: str
    osuser: str
    count: int
    since: str


@dataclass
class Container:
    con_id: int
    name: str
    open_mode: str
    users: list[User] = field(default_factory=list)


@dataclass
class Instance:
    sid: str
    oracle_home: str
    container: str  # pod/contenedor donde corre; vacío si corre directamente en la máquina
    db_name: str = ""
    unique_name: str = ""
    cdb: bool = False
    open_mode: str = ""
    log_mode: str = ""
    version: str = ""
    host_name: str = ""
    started: str = ""
    now: str = ""  # hora de la base al consultar: las antigüedades se miden con ella
    containers: list[Container] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    command: str = ""  # para entrar a mano en sqlplus como root en la máquina (cambia si se reinicia el contenedor)


def ssh_command(host: str, user: str = "root", port: int | None = None, identity: str | None = None,
                password: bool = False, tty: bool = True, certificate: str | None = None,
                passphrase: bool = False, remote: str = "bash -s") -> list[str]:
    cmd = ["ssh", "-o", "ConnectTimeout=10"]
    if not tty:
        # Desde la interfaz no hay terminal donde preguntar: una huella nueva se acepta (y se avisa si cambia).
        cmd += ["-o", "StrictHostKeyChecking=accept-new"]
    if password:
        # Solo contraseña: ni agente ni claves, y un único intento para no bloquear la cuenta con reintentos.
        cmd += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "NumberOfPasswordPrompts=1"]
    elif not tty and not passphrase:
        cmd += ["-o", "BatchMode=yes"]  # sin terminal, ssh no puede pedir nada: que falle en vez de colgarse
    if port:
        cmd += ["-p", str(port)]
    if identity:
        # Solo esa clave (y su certificado), sin caer en la contraseña si el servidor la rechaza.
        cmd += ["-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey",
                "-i", str(Path(identity).expanduser())]
    if certificate:
        cmd += ["-o", f"CertificateFile={Path(certificate).expanduser()}"]
    # Sin root hace falta sudo sin contraseña: nsenter, su y leer /proc de otro usuario lo exigen.
    return [*cmd, f"{user}@{host}" if user else host, remote if user == "root" else f"sudo -n {remote}"]


@dataclass
class Ssh:
    """Cómo entrar en una máquina; `password`/`passphrase` solo viven en memoria mientras dura la operación."""
    host: str
    user: str = "root"
    port: int | None = None
    identity: str | None = None
    password: str | None = None
    tty: bool = True
    certificate: str | None = None
    passphrase: str | None = None

    def command(self, remote: str = "bash -s") -> list[str]:
        return ssh_command(self.host, self.user, self.port, self.identity, self.password is not None, self.tty,
                           self.certificate, self.passphrase is not None, remote)

    def env(self) -> dict[str, str] | None:
        secret = self.password if self.password is not None else self.passphrase
        if secret is None:
            return None
        # La contraseña o la frase de paso llegan a ssh por SSH_ASKPASS. Sin agente: solo vale lo que se ha pedido.
        env = {k: v for k, v in os.environ.items() if k != "SSH_AUTH_SOCK"}
        return env | {"SSH_ASKPASS": _askpass(), "SSH_ASKPASS_REQUIRE": "force", "ORACLE_MANAGER_SSH_PASSWORD": secret}

    def error(self, stderr: str, code: int) -> str:
        # Los avisos de OpenSSH (p. ej. el de intercambio de claves post-cuántico) empiezan por «**».
        return "\n".join(l for l in stderr.splitlines() if not l.startswith("**")).strip() or \
            f"ssh terminó con código {code}"


def parse(output: str) -> list[Instance]:
    instances: list[Instance] = []
    for line in output.splitlines():
        tag, _, rest = line.partition("\t")
        f = rest.split("\t")
        if tag == "INST" and len(f) == 3:
            instances.append(Instance(*f))
        elif not instances:
            continue
        elif tag == "CMD" and rest:
            instances[-1].command = rest
        elif tag == "DB" and len(f) == 9:
            i = instances[-1]
            i.db_name, i.unique_name, cdb, i.open_mode, i.log_mode, i.version, i.host_name, i.started, i.now = f
            i.cdb = cdb == "YES"
        elif tag in ("ACT", "SES") and (u := _user(instances[-1], f)):
            if tag == "ACT" and len(f) == 13:
                (u.last_login, sessions, active, execs, u.sql_last, lr, pr, bc, dml, u.dml_last, u.ddl_last) = f[2:]
                u.sessions, u.active_sessions, u.sql_execs = int(sessions), int(active), int(execs)
                u.logical_reads, u.physical_reads, u.block_changes, u.dml = int(lr), int(pr), int(bc), int(dml)
            elif tag == "SES" and len(f) == 7:
                u.clients.append(Client(f[2], f[3], f[4], int(f[5]), f[6]))
        elif tag == "CON" and len(f) == 3:
            instances[-1].containers.append(Container(int(f[0]), f[1], f[2]))
        elif tag == "USR" and len(f) == 8:
            i = instances[-1]
            con = next((c for c in i.containers if c.con_id == int(f[0])), None)
            if not con:  # por si v$containers falla: todo va a la propia base
                con = Container(int(f[0]), i.db_name, i.open_mode)
                i.containers.append(con)
            con.users.append(User(f[1], f[2], f[3], f[4], f[5] == "Y", int(f[6]), int(f[7])))
        elif line.startswith(("ORA-", "SP2-", "ERROR")):
            instances[-1].errors.append(line.strip())
    from .usage import classify  # aquí para evitar la importación circular (usage usa estas clases)

    for i in instances:
        for c in i.containers:
            # oracle_maintained también queda en «Y» si se crea un usuario con «_ORACLE_SCRIPT»=true (truco habitual
            # para no usar el prefijo C## en una CDB). Los de verdad nacen con la base, el mismo día que SYS.
            sys_created = next((u.created for u in c.users if u.name == "SYS"), "")
            for u in c.users:
                if u.oracle_maintained and sys_created and u.created != sys_created:
                    u.oracle_maintained = False
        classify(i)
    return instances


def _user(i: Instance, f: list[str]) -> User | None:
    if len(f) < 2 or not f[0].isdigit():
        return None
    con = next((c for c in i.containers if c.con_id == int(f[0])), None)
    return next((u for u in con.users if u.name == f[1]), None) if con else None


def _askpass() -> str:
    """Programa para SSH_ASKPASS: devuelve la contraseña que le llega por el entorno. Vive en XDG_RUNTIME_DIR
    (tmpfs, solo del usuario) y no contiene la contraseña: esa solo está en la memoria del proceso ssh."""
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "infranettone-oracle-askpass"
    if not path.exists():
        path.write_text('#!/bin/sh\nprintf \'%s\\n\' "$ORACLE_MANAGER_SSH_PASSWORD"\n')
        path.chmod(0o700)
    return str(path)


def key_needs_passphrase(path: str) -> bool:
    """¿La clave privada está cifrada? ssh-keygen solo puede sacar la pública sin frase si no lo está."""
    r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(Path(path).expanduser())],
                       capture_output=True, text=True, timeout=10)
    return r.returncode != 0 and "passphrase" in r.stderr.lower()


def discover(host: str, user: str = "root", port: int | None = None, identity: str | None = None,
             password: str | None = None, tty: bool = True, certificate: str | None = None,
             passphrase: str | None = None) -> list[Instance]:
    """Sin `password`, ssh usa el agente y las claves de ~/.ssh. Con ella, solo autenticación por contraseña.
    Con `identity` (y `certificate`), solo esa clave; `passphrase` es su frase de paso si está cifrada."""
    ssh = Ssh(host, user, port, identity, password, tty, certificate, passphrase)
    try:
        r = subprocess.run(ssh.command(), input=REMOTE_SCRIPT, capture_output=True, text=True, timeout=300,
                           env=ssh.env())
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"{host} no ha terminado en {e.timeout:.0f} s") from e
    instances = parse(r.stdout)
    if r.returncode and not instances:
        raise RuntimeError(ssh.error(r.stderr, r.returncode))
    return instances
