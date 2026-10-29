import argparse
import getpass
import json
import sys
from dataclasses import asdict

from .discover import Instance, discover
from .usage import idle_text
from .version import version


def _table(rows: list[list[str]], indent: str) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print(indent + "  ".join(c.rjust(w) if i >= 4 and c.isdigit() else c.ljust(w) for i, (c, w) in enumerate(zip(r, widths))))


def _print(instances: list[Instance], show_all: bool) -> None:
    for i in instances:
        where = f" en {i.container}" if i.container else ""
        print(f"\n■ {i.sid}{where} — {i.db_name or '?'} {i.version} · {i.open_mode} · {i.log_mode} · "
              f"{'CDB' if i.cdb else 'no CDB'} · arrancada {i.started}")
        for e in i.errors:
            print(f"  ⚠️ {e}")
        for c in i.containers:
            users = [u for u in c.users if show_all or not u.oracle_maintained]
            hidden = len(c.users) - len(users)
            print(f"  {c.name} [{c.open_mode}] — {len(users)} usuarios"
                  + (f" (+{hidden} de Oracle, --all para verlos)" if hidden else ""))
            if users:
                _table([["USUARIO", "USO", "ÚLTIMO USO", "ESTADO", "SESIONES", "SQL", "LECTURAS", "CAMBIOS",
                         "OBJETOS", "MB"]]
                       + [[u.name + (" *" if u.oracle_maintained else ""), u.level, idle_text(u), u.status,
                           str(u.sessions), str(u.sql_execs), str(u.logical_reads), str(u.block_changes),
                           str(u.objects), str(u.size_mb)] for u in users], "    ")


def main() -> int:
    parser = argparse.ArgumentParser(prog="infranettone-oracle",
                                     description="Lista por SSH las bases de datos Oracle y sus usuarios/esquemas")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version()}")
    parser.add_argument("host", nargs="?", help="máquina con Oracle (IP o nombre); sin ella, abre la interfaz")
    parser.add_argument("-u", "--user", default="root", help="usuario SSH (root, o uno con sudo sin contraseña)")
    parser.add_argument("-p", "--port", type=int, help="puerto SSH")
    parser.add_argument("-i", "--identity", help="clave SSH (por defecto, la del agente o ~/.ssh)")
    parser.add_argument("-c", "--cert", help="certificado SSH firmado por la CA (con -i para la clave privada)")
    parser.add_argument("-P", "--password", action="store_true",
                        help="entra con contraseña (la pide) en vez de con el agente o las claves")
    parser.add_argument("-a", "--all", action="store_true", help="incluye los usuarios que crea Oracle (SYS, XDB…)")
    parser.add_argument("--json", action="store_true", help="salida en JSON")
    args = parser.parse_args()
    if not args.host:
        from .ui import App
        return App().run([sys.argv[0]])

    try:
        password = getpass.getpass(f"Contraseña de {args.user}@{args.host}: ") if args.password else None
        if args.cert and not args.identity:
            parser.error("--cert necesita -i con la clave privada")
        # Con terminal, ssh pide él mismo la frase de paso de la clave si hace falta.
        instances = discover(args.host, args.user, args.port, args.identity, password, certificate=args.cert)
    except (RuntimeError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.json:
        json.dump([asdict(i) for i in instances], sys.stdout, indent=2, ensure_ascii=False)
        print()
    elif not instances:
        print(f"No hay instancias Oracle en marcha en {args.host}")
    else:
        _print(instances, args.all)
    return 1 if any(i.errors and not i.containers for i in instances) else 0


if __name__ == "__main__":
    sys.exit(main())
