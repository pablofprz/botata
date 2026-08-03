"""scheduler.py — registro de tareas periódicas del loop principal (T27).

Reemplaza los pases hardcodeados de `run()`: cada tarea se declara UNA vez
(`PeriodicTask`) y el loop solo llama `run_due()`. Agregar una tarea nueva no
toca el loop.

Dos modos de cadencia:
- `interval_hours = 0` (default): corre en CADA iteración — para pases que ya
  se auto-gatean por dentro (feed por-feed, news por-fuente, poll de menciones).
- `interval_hours > 0`: gate a nivel scheduler vía cursor `task:{name}` en
  `feed_cursors` (helpers get/save inyectados para no acoplar a botata).

Aislamiento de errores POR TAREA: una tarea que explota no frena a las demás.
KeyboardInterrupt es BaseException → atraviesa y corta el loop, como siempre.

Config declarativa: settings.json → sección TASKS (`{name: {enabled,
interval_hours}}`, patrón TOOLS) vía `apply_tasks_config`.

Infra genérica: no conoce botata ni la DB (recibe callables).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger("botata.scheduler")


@dataclass
class PeriodicTask:
    name: str
    fn: Callable[[], None]
    interval_hours: float = 0.0   # 0 = cada iteración (la tarea se auto-gatea)
    enabled: bool = True


def apply_tasks_config(tasks: list[PeriodicTask], config: dict | None) -> None:
    """Overrides de settings.json → TASKS. Nombre desconocido → warning, no falla."""
    if not config:
        return
    by_name = {t.name: t for t in tasks}
    for name, cfg in config.items():
        task = by_name.get(name)
        if task is None:
            log.warning("TASKS config: tarea desconocida '%s' — ignorada", name)
            continue
        if "enabled" in cfg:
            task.enabled = bool(cfg["enabled"])
        if "interval_hours" in cfg:
            task.interval_hours = float(cfg["interval_hours"])


def _is_due(task: PeriodicTask, get_last: Callable[[str], str | None]) -> bool:
    if task.interval_hours <= 0:
        return True
    last = get_last(f"task:{task.name}")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # cursor corrupto → correr y regrabarlo
    elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    return elapsed_h >= task.interval_hours


def _default_describe(e: Exception) -> str:
    """Detalle de un error para el log. Muchas excepciones de cliente traen
    `str(e)` vacío, así que el TIPO siempre va: sin él el log no dice nada."""
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def run_due(tasks: list[PeriodicTask], *,
            get_last: Callable[[str], str | None],
            save_last: Callable[[str], None],
            network_errors: tuple[type[Exception], ...] = (),
            describe_error: Callable[[Exception], str] | None = None) -> None:
    """Corre las tareas habilitadas que estén al día. Nunca lanza (salvo BaseException).

    `describe_error` traduce una excepción a texto para el log (el default
    alcanza; el canal inyecta uno que sabe leer sus errores de red)."""
    describe = describe_error or _default_describe
    for task in tasks:
        try:
            # _is_due lee la DB (get_last): dentro del aislamiento por tarea,
            # porque un `database is locked` acá afuera escapaba run_due entero
            # — y en el loop principal eso era el proceso muerto en silencio.
            if not task.enabled or not _is_due(task, get_last):
                continue
        except Exception:
            log.error("tarea '%s': no pude evaluar si está al día", task.name,
                      exc_info=True)
            continue
        transient = False
        try:
            task.fn()
        except network_errors as e:
            transient = True
            log.warning("tarea '%s': error de red (%s) — reintenta el próximo ciclo",
                        task.name, describe(e))
        except Exception:
            log.error("tarea '%s' falló", task.name, exc_info=True)
        # Tras un error de RED el cursor no se toca: si no, una tarea con
        # interval_hours > 0 (heartbeat, reflection...) se saltea el intervalo
        # ENTERO por un blip — lo contrario de lo que promete el mensaje de
        # arriba. Un fallo inesperado sí lo consume, para no quedar en un loop
        # caliente reintentando algo roto de verdad cada iteración.
        if task.interval_hours > 0 and not transient:
            try:
                save_last(f"task:{task.name}")
            except Exception:
                log.error("tarea '%s': no pude guardar el cursor", task.name, exc_info=True)
