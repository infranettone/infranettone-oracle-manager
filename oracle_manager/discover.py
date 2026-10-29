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
REMOTE_SCRIPT = r"""
set -u
mntns=$(readlink /proc/1/ns/mnt)
SQL=$(cat <<'SQL'
set pagesize 0 linesize 32767 trimspool on trimout on feedback off heading off verify off echo off tab off
select 'DB'||chr(9)||d.name||chr(9)||d.db_unique_name||chr(9)||d.cdb||chr(9)||d.open_mode||chr(9)||d.log_mode
       ||chr(9)||i.version||chr(9)||i.host_name||chr(9)||to_char(i.startup_time, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||to_char(sysdate, 'YYYY-MM-DD HH24:MI')
  from v$database d, v$instance i;
select 'CON'||chr(9)||con_id||chr(9)||name||chr(9)||open_mode from v$containers order by con_id;
select 'USR'||chr(9)||u.con_id||chr(9)||u.username||chr(9)||u.account_status||chr(9)||u.default_tablespace
       ||chr(9)||to_char(u.created, 'YYYY-MM-DD')||chr(9)||u.oracle_maintained||chr(9)||nvl(o.n, 0)||chr(9)||nvl(s.mb, 0)
  from cdb_users u
  left join (select con_id, owner, count(*) n from cdb_objects group by con_id, owner) o
         on o.con_id = u.con_id and o.owner = u.username
  left join (select con_id, owner, round(sum(bytes) / 1048576) mb from cdb_segments group by con_id, owner) s
         on s.con_id = u.con_id and s.owner = u.username
 order by u.con_id, u.username;
-- Actividad, solo con vistas sin licencia extra (nada de AWR/ASH, que exigen el Diagnostics Pack) y sin escribir
-- nada. v$sql y v$segment_statistics cuentan desde el arranque (v$sql, solo lo que siga en la shared pool);
-- dba_tab_modifications, desde las últimas estadísticas de cada tabla.
select 'ACT'||chr(9)||u.con_id||chr(9)||u.username||chr(9)||to_char(u.last_login, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||nvl(s.n, 0)||chr(9)||nvl(s.act, 0)||chr(9)||nvl(q.ex, 0)||chr(9)||to_char(q.last, 'YYYY-MM-DD HH24:MI')
       ||chr(9)||nvl(g.lr, 0)||chr(9)||nvl(g.pr, 0)||chr(9)||nvl(g.bc, 0)
       ||chr(9)||nvl(m.dml, 0)||chr(9)||to_char(m.last, 'YYYY-MM-DD HH24:MI')||chr(9)||to_char(o.ddl, 'YYYY-MM-DD HH24:MI')
  from cdb_users u
  left join (select con_id, username, count(*) n, sum(decode(status, 'ACTIVE', 1, 0)) act
               from v$session where username is not null and type = 'USER' group by con_id, username) s
         on s.con_id = u.con_id and s.username = u.username
  left join (select con_id, parsing_schema_name owner, sum(executions) ex, max(last_active_time) last
               from v$sql group by con_id, parsing_schema_name) q
         on q.con_id = u.con_id and q.owner = u.username
  left join (select con_id, owner, sum(decode(statistic_name, 'logical reads', value)) lr,
                    sum(decode(statistic_name, 'physical reads', value)) pr,
                    sum(decode(statistic_name, 'db block changes', value)) bc
               from v$segment_statistics
              where statistic_name in ('logical reads', 'physical reads', 'db block changes')
              group by con_id, owner) g
         on g.con_id = u.con_id and g.owner = u.username
  left join (select con_id, table_owner owner, sum(inserts + updates + deletes) dml, max(timestamp) last
               from cdb_tab_modifications group by con_id, table_owner) m
         on m.con_id = u.con_id and m.owner = u.username
  left join (select con_id, owner, max(last_ddl_time) ddl from cdb_objects group by con_id, owner) o
         on o.con_id = u.con_id and o.owner = u.username;
-- Quién está conectado ahora (no hay histórico de conexiones sin auditoría).
select 'SES'||chr(9)||con_id||chr(9)||username||chr(9)||replace(machine, chr(9), ' ')||chr(9)||replace(program, chr(9), ' ')
       ||chr(9)||replace(osuser, chr(9), ' ')||chr(9)||count(*)||chr(9)||to_char(min(logon_time), 'YYYY-MM-DD HH24:MI')
  from v$session where username is not null and type = 'USER'
 group by con_id, username, machine, program, osuser;
exit
SQL
)
# El PMON de cada instancia se llama <prefijo>_pmon_<SID> y el prefijo depende de la edición: ora_ (normal),
# xe_ (XE), db_ (23ai Free), mdb_ (base de gestión de Grid)… No se fía del prefijo: vale cualquiera, siempre que el
# ejecutable sea el binario «oracle». Las ASM (asm_, apx_) no son bases de datos y se saltan.
for d in /proc/[0-9]*; do
    pid=${d#/proc/}
    args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    [[ $args =~ ^([a-z0-9]+)_pmon_([^ ]+) ]] || continue
    [[ ${BASH_REMATCH[1]} == asm || ${BASH_REMATCH[1]} == apx ]] && continue
    [[ $(readlink "$d/exe" 2>/dev/null) == */bin/oracle* ]] || continue
    envof() { tr '\0' '\n' < "$d/environ" 2>/dev/null | sed -n "s/^$1=//p" | head -n1; }
    sid=$(envof ORACLE_SID); home=$(envof ORACLE_HOME)
    [ -n "$sid" ] || sid=${BASH_REMATCH[2]}
    [ -n "$home" ] || home=$(readlink "$d/exe" | sed 's|/bin/oracle.*||')
    where=""
    if [ "$(readlink "$d/ns/mnt")" != "$mntns" ]; then
        where=$(envof HOSTNAME); [ -n "$where" ] || where=contenedor
    fi
    printf 'INST\t%s\t%s\t%s\n' "$sid" "$home" "$where"
    # «/ as sysdba» exige los mismos grupos que el PMON (dba, oinstall…). nsenter -S/-G los quitaría, así que se
    # entra en los espacios de nombres sin el de montaje y chroot (del anfitrión) pone UID, GID y grupos y salta a
    # la raíz del proceso (/proc/<pid>/root): vale igual en la máquina o en un contenedor, sin su ni nada dentro.
    ids=$(awk '/^Uid:/ {u=$2} /^Gid:/ {g=$2} /^Groups:/ {$1=""; gs=$0} END {gsub(/^ +| +$/, "", gs); gsub(/ +/, ",", gs); print u":"g" "gs}' "$d/status")
    groups=${ids#* }
    cid=$(grep -oE '[0-9a-f]{64}' "$d/cgroup" | tail -n1)
    # Comando para entrar a mano (como root en la máquina) en sqlplus de esta instancia.
    run="env ORACLE_SID=$sid ORACLE_HOME=$home $home/bin/sqlplus / as sysdba"
    if [ -z "$where" ]; then
        cmd="runuser -u $(stat -c %U "$d") -- $run"
    elif [ -n "$cid" ] && command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
        cmd="crictl exec -it $cid $run"
    else
        cmd="nsenter -t $pid -u -i -n -p -- chroot --userspec=${ids%% *} ${groups:+--groups=$groups} $d/root $run"
        for rt in docker podman; do
            command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 && { cmd="$rt exec -it -u ${ids%% *} $cid $run"; break; }
        done
    fi
    printf 'CMD\t%s\n' "$cmd"
    out=$(ORACLE_SID=$sid ORACLE_HOME=$home NLS_LANG=AMERICAN_AMERICA.AL32UTF8 \
        nsenter -t "$pid" -u -i -n -p -- \
        chroot --userspec="${ids%% *}" ${groups:+--groups="$groups"} "$d/root" \
        "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
    # Algunas (p. ej. XE 18c en k3s) rechazan con ORA-12547 una conexión que no nace dentro del contenedor: entonces
    # se usa el exec del propio motor de contenedores, como hace `kubectl exec`.
    if ! grep -q $'^DB\t' <<<"$out" && [ -n "$where" ] && [ -n "$cid" ]; then
        envs=(ORACLE_SID="$sid" ORACLE_HOME="$home" NLS_LANG=AMERICAN_AMERICA.AL32UTF8)
        if command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
            # crictl exec no deja elegir usuario: entra con el de la imagen (en las de Oracle, «oracle»).
            out=$(crictl exec -i "$cid" env "${envs[@]}" "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
        else
            for rt in docker podman; do
                command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 || continue
                out=$($rt exec -i -u "${ids%% *}" "${envs[@]/#/--env=}" "$cid" \
                      "$home/bin/sqlplus" -S -L / as sysdba <<<"$SQL" 2>&1)
                break
            done
        fi
    fi
    printf '%s\n' "$out"
done
"""


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
