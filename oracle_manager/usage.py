"""Nivel de uso de cada esquema a partir de lo que guarda Oracle sin auditoría ni licencias extra.

Señales (ver la consulta ACT de discover.py):
- ahora: sesiones abiertas;
- desde el arranque de la instancia: lecturas y cambios en los objetos del esquema (los haga quien los haga, p. ej.
  una cuenta WWW_X que usa las tablas de X) y ejecuciones de SQL lanzado por el usuario;
- con fecha: última conexión (`last_login`), último SQL en memoria y último DML desde las estadísticas.

«Último uso» es la más reciente de esas fechas (ahora, si hay sesiones). El nivel:
- Sin uso: nada desde el arranque y último uso hace más de UNUSED_DAYS días (o nunca);
- Bajo / Medio / Alto: los que sí tienen actividad, repartidos en tercios dentro de la misma instancia según una
  puntuación logarítmica (lecturas, cambios, ejecuciones y sesiones). Un esquema sin actividad desde el arranque
  pero usado en los últimos UNUSED_DAYS días queda en Bajo.
"""

import math
from datetime import datetime

from .discover import Instance, User

UNUSED_DAYS = 90
LEVELS = ["Sin uso", "Bajo", "Medio", "Alto"]
_FMT = "%Y-%m-%d %H:%M"


def _date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, _FMT)
    except ValueError:
        return None


def score(u: User) -> float:
    return (math.log10(1 + u.logical_reads) + math.log10(1 + u.block_changes) + math.log10(1 + u.sql_execs)
            + 2 * math.log10(1 + u.sessions))


def classify(i: Instance) -> None:
    now = _date(i.now) or datetime.now()
    active: list[User] = []
    for c in i.containers:
        for u in c.users:
            dates = [d for d in map(_date, (u.last_login, u.sql_last, u.dml_last)) if d]
            last = now if u.sessions else max(dates, default=None)
            u.last_use = last.strftime(_FMT) if last else ""
            u.idle_days = max(0, (now - last).days) if last else None
            if has_activity(u):
                active.append(u)
            elif u.idle_days is not None and u.idle_days <= UNUSED_DAYS:
                u.level = "Bajo"
            else:
                u.level = "Sin uso"
    # Los de Oracle (SYS, DBSNMP…) siempre tienen mucha actividad: se clasifican aparte para no desplazar al resto.
    for group in ([u for u in active if not u.oracle_maintained], [u for u in active if u.oracle_maintained]):
        ranked = sorted(group, key=score)
        for n, u in enumerate(ranked):
            u.level = LEVELS[1 + min(2, 3 * n // len(ranked))]


def has_activity(u: User) -> bool:
    """Algo desde el arranque, aunque no haya fecha (p. ej. otra cuenta lee sus tablas)."""
    return bool(u.sessions or u.sql_execs or u.logical_reads or u.block_changes)


def idle_sort(u: User) -> int:
    """Para ordenar «Último uso»: ahora < hace N días < solo desde el arranque < nunca."""
    if u.sessions:
        return -1
    if u.idle_days is not None:
        return u.idle_days
    return 10**6 if has_activity(u) else 10**7


def idle_text(u: User) -> str:
    if u.sessions:
        return "ahora"
    if u.idle_days is None:
        return "desde el arranque" if has_activity(u) else "nunca"
    if u.idle_days == 0:
        return "hoy"
    if u.idle_days < 60:
        return f"hace {u.idle_days} d"
    if u.idle_days < 730:
        return f"hace {u.idle_days // 30} meses"
    return f"hace {u.idle_days // 365} años"
