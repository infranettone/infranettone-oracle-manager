"""Exporta un esquema con Data Pump (expdp) en la máquina remota y descarga el .dmp (y su .log) a una carpeta local.

El volcado se deja en el DATA_PUMP_DIR de la base (o de la PDB), se trae por ssh y se borra de la máquina al
terminar, haya ido bien o no la descarga, para no llenarle el disco.
"""

import re
import shlex
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .discover import Ssh

# Se antepone al script con los valores ya validados (sin comillas ni nada que el shell interprete).
DUMP_SCRIPT = r"""
set -u
fail() { printf 'ERR\t%s\n' "$*"; exit 1; }
pid=""
for d in /proc/[0-9]*; do
    args=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    [[ $args =~ ^([a-z0-9]+)_pmon_([^ ]+) ]] || continue
    [[ ${BASH_REMATCH[1]} == asm || ${BASH_REMATCH[1]} == apx ]] && continue
    [[ $(readlink "$d/exe" 2>/dev/null) == */bin/oracle* ]] || continue
    envof() { tr '\0' '\n' < "$d/environ" 2>/dev/null | sed -n "s/^$1=//p" | head -n1; }
    sid=$(envof ORACLE_SID); [ -n "$sid" ] || sid=${BASH_REMATCH[2]}
    [ "$sid" = "$SID" ] || continue
    home=$(envof ORACLE_HOME); [ -n "$home" ] || home=$(readlink "$d/exe" | sed 's|/bin/oracle.*||')
    pid=${d#/proc/}
    break
done
[ -n "$pid" ] || fail "La instancia $SID ya no está en marcha"
ids=$(awk '/^Uid:/ {u=$2} /^Gid:/ {g=$2} /^Groups:/ {$1=""; gs=$0} END {gsub(/^ +| +$/, "", gs); gsub(/ +/, ",", gs); print u":"g" "gs}' "$d/status")
groups=${ids#* }
cid=$(grep -oE '[0-9a-f]{64}' "$d/cgroup" | tail -n1)
envs=(ORACLE_SID="$sid" ORACLE_HOME="$home" NLS_LANG=AMERICAN_AMERICA.AL32UTF8)
# Con una PDB, el disparador DBMS_SET_PDB (18c en adelante) cambia a ella al entrar como sysdba.
[ -n "$PDB" ] && envs+=(ORACLE_PDB_SID="$PDB")
# Igual que al consultar: chroot con los grupos del PMON y, si la base lo rechaza, el exec del motor de contenedores.
ora() {
    local prog=$home/bin/$1; shift
    case $mode in
        chroot) env "${envs[@]}" nsenter -t "$pid" -u -i -n -p -- \
                    chroot --userspec="${ids%% *}" ${groups:+--groups="$groups"} "$d/root" "$prog" "$@" ;;
        crictl) crictl exec -i "$cid" env "${envs[@]}" "$prog" "$@" ;;
        *) $mode exec -i -u "${ids%% *}" "${envs[@]/#/--env=}" "$cid" "$prog" "$@" ;;
    esac
}
Q="set pagesize 0 linesize 32767 trimspool on feedback off heading off verify off
select 'DIR'||chr(9)||directory_path from dba_directories where directory_name = 'DATA_PUMP_DIR';
select 'OWN'||chr(9)||count(*) from dba_users where username = '$SCHEMA';
select 'CON'||chr(9)||sys_context('USERENV', 'CON_NAME') from dual;
exit"
mode=chroot
out=$(ora sqlplus -S -L / as sysdba <<<"$Q" 2>&1)
if ! grep -q $'^DIR\t' <<<"$out" && [ -n "$cid" ]; then
    if command -v crictl >/dev/null && crictl inspect "$cid" >/dev/null 2>&1; then
        mode=crictl
    else
        for rt in docker podman; do
            command -v $rt >/dev/null && $rt inspect "$cid" >/dev/null 2>&1 && { mode=$rt; break; }
        done
    fi
    [ "$mode" = chroot ] || out=$(ora sqlplus -S -L / as sysdba <<<"$Q" 2>&1)
fi
dir=$(sed -n 's/^DIR\t//p' <<<"$out")
[ -n "$dir" ] || fail "No se puede usar el directorio DATA_PUMP_DIR: $(grep -m1 -E '^(ORA|SP2)-' <<<"$out")"
[ -z "$PDB" ] || grep -qx $'CON\t'"$PDB" <<<"$out" || fail "No se pudo entrar en la PDB $PDB"
grep -qx $'OWN\t1' <<<"$out" || fail "No existe el usuario $SCHEMA"
host_dir=$d/root${dir%/}
# DATA_PUMP_DIR apunta a una carpeta que muchas imágenes no crean (ORA-29283): se crea con el dueño del PMON.
if [ ! -d "$host_dir" ]; then
    printf 'STEP\tCreando %s…\n' "$dir"
    uidgid=${ids%% *}
    mkdir -p "$host_dir" && chown "$uidgid" "$host_dir" && chmod 750 "$host_dir" \
        || fail "No se pudo crear $dir en el contenedor"
fi
printf 'STEP\tExportando %s con expdp…\n' "$SCHEMA"
# flashback_time: copia coherente de todas las tablas en el mismo instante. Sin compresión (exige licencia).
ora expdp '"/ as sysdba"' schemas="$SCHEMA" directory=DATA_PUMP_DIR dumpfile="$NAME.dmp" logfile="$NAME.log" \
    reuse_dumpfiles=y flashback_time=systimestamp </dev/null 2>&1
rc=$?
printf 'RC\t%s\n' "$rc"
[ -f "$host_dir/$NAME.dmp" ] || fail "expdp no ha generado el volcado (código $rc)"
printf 'FILE\t%s\t%s\n' "$host_dir/$NAME.dmp" "$(stat -c %s "$host_dir/$NAME.dmp")"
printf 'LOG\t%s\n' "$host_dir/$NAME.log"
"""

_IDENT = re.compile(r"^[A-Z][A-Z0-9_$#]*$")  # nombres de Oracle sin comillas: los que expdp acepta tal cual
_SID = re.compile(r"^[A-Za-z0-9_$#]+$")


def default_folder() -> str:
    from gi.repository import GLib

    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home() / "Descargas")


def export_schema(ssh: Ssh, sid: str, pdb: str, schema: str, folder: str, db: str = "",
                  progress: Callable[[str, float | None], None] = lambda text, fraction: None) -> tuple[Path, str]:
    """Devuelve la ruta local del .dmp y un aviso si expdp terminó con avisos. `progress` recibe el texto del paso
    y, en la descarga, la fracción descargada."""
    if not _IDENT.match(schema):
        raise RuntimeError(f"No se puede exportar «{schema}»: el nombre lleva caracteres que expdp no acepta sin "
                           "comillas")
    if not _SID.match(sid) or (pdb and not _IDENT.match(pdb)):
        raise RuntimeError("SID o PDB no válidos")
    dest = Path(folder).expanduser()
    if not dest.is_dir():
        raise RuntimeError(f"No existe la carpeta {dest}")
    name = "_".join(x for x in (schema, pdb or db, datetime.now().strftime("%Y%m%d-%H%M%S")) if x)
    header = f"SID='{sid}'\nPDB='{pdb}'\nSCHEMA='{schema}'\nNAME='{name}'\n"

    progress(f"Conectando con {ssh.host}…", None)
    proc = subprocess.Popen(ssh.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=ssh.env())
    proc.stdin.write(header + DUMP_SCRIPT)
    proc.stdin.close()
    tags: dict[str, list[str]] = {}
    tail: list[str] = []  # últimas líneas de expdp, para explicar un fallo
    for line in proc.stdout:
        tag, _, rest = line.rstrip("\n").partition("\t")
        if tag in ("STEP", "ERR", "RC", "FILE", "LOG"):
            tags[tag] = rest.split("\t")
            if tag == "STEP":
                progress(rest, None)
        elif line.strip():
            tail = [*tail[-7:], line.strip()]
            progress(line.strip(), None)
    stderr = proc.stderr.read()
    proc.wait()
    if "ERR" in tags:
        raise RuntimeError("\n".join([tags["ERR"][0], *tail]))
    if "FILE" not in tags:
        raise RuntimeError(ssh.error(stderr, proc.returncode) if proc.returncode else "\n".join(tail))
    remote, size = tags["FILE"][0], int(tags["FILE"][1])
    remote_log = tags.get("LOG", [""])[0]
    # expdp sale con 5 si terminó con avisos (p. ej. un objeto que no pudo exportar): el volcado sirve, pero se avisa.
    warning = "" if tags.get("RC") == ["0"] else "expdp terminó con avisos: revisa el .log"

    local = dest / f"{name}.dmp"
    try:
        _download(ssh, remote, local, size, progress)
        if remote_log:
            r = subprocess.run(ssh.command(f"cat -- {shlex.quote(remote_log)}"), capture_output=True,
                               env=ssh.env(), timeout=60)
            if r.returncode == 0:
                local.with_suffix(".log").write_bytes(r.stdout)
    finally:
        progress("Borrando el volcado de la máquina…", None)
        subprocess.run(ssh.command("rm -f -- " + " ".join(shlex.quote(p) for p in (remote, remote_log) if p)),
                       capture_output=True, env=ssh.env(), timeout=60)
    return local, warning


def _download(ssh: Ssh, remote: str, local: Path, size: int, progress: Callable[[str, float | None], None]) -> None:
    part = local.with_name(local.name + ".part")  # a medias no se confunde con un volcado bueno
    # Compression=yes: un .dmp sin comprimir (la compresión de expdp exige licencia) se reduce mucho por el camino.
    cmd = ssh.command(f"cat -- {shlex.quote(remote)}")
    cmd[1:1] = ["-o", "Compression=yes"]
    done = 0
    try:
        with open(part, "wb") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ssh.env())
            while chunk := proc.stdout.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                progress(f"Descargando {local.name}: {done / 1048576:.0f} de {size / 1048576:.0f} MB",
                         done / size if size else None)
            stderr = proc.stderr.read().decode(errors="replace")
            proc.wait()
        if proc.returncode or done != size:
            raise RuntimeError(f"La descarga falló ({done} de {size} bytes): {ssh.error(stderr, proc.returncode)}")
        part.rename(local)
    finally:
        part.unlink(missing_ok=True)
