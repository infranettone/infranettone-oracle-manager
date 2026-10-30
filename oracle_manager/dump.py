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
# El script se comparte con la app de Windows (windows/Core.cs).
DUMP_SCRIPT = (Path(__file__).parent / "remote" / "dump.sh").read_text(encoding="utf-8")

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
