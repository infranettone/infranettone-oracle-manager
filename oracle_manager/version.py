"""Versión de la app: la escribe packaging/build-deb.sh en _version.py; desde el repositorio sale de git."""

import subprocess
from pathlib import Path


def version() -> str:
    try:
        from ._version import VERSION  # solo existe en el .deb

        return VERSION
    except ImportError:
        pass
    try:
        r = subprocess.run(["git", "describe", "--tags", "--dirty"], cwd=Path(__file__).parent,
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "desarrollo"
    return f"{r.stdout.strip().removeprefix('v')} (repositorio)" if r.returncode == 0 else "desarrollo"
