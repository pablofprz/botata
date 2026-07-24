"""budget.py — presupuesto diario de tokens (guard económico del bot).

Portado de maripobot (fetch_credits_data / is_burned / announce_burn_transitions):
mide el gasto del día como `total_usage_ahora - snapshot_al_inicio_del_día`
contra el endpoint /credits de OpenRouter, y "quema" al bot cuando supera el
presupuesto: el loop deja de correr tareas hasta el día siguiente.

Diseño (patrón scheduler.py — infra genérica, no conoce botata):
- El fetcher de uso se inyecta (`fetch`) → testeable sin red y agnóstico de API.
- Estado persistido en la tabla `kv` de la DB (sobrevive reinicios), clave
  'budget_state': {today, snapshot, state, announced_today, wake_announced_today}.
- Día en hora ARGENTINA (UTC-3), consistente con events/heartbeat.
- Fail-open pegajoso: si el endpoint de créditos no responde, se mantiene el
  estado anterior (un hiccup de red no despierta ni duerme al bot).
- `check()` devuelve la transición ('sleep' | 'wake' | None); los ANUNCIOS los
  postea el caller (botata) — este módulo no habla con Bluesky.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

log = logging.getLogger("botata.budget")

_AR = timezone(timedelta(hours=-3))


def fetch_openrouter_usage(base_url: str, api_key: str, timeout: int = 15) -> Optional[float]:
    """Uso acumulado total (USD) según el endpoint /credits de OpenRouter.
    None si la red/auth falla (el caller decide qué hacer — acá no se lanza)."""
    url = f"{base_url.rstrip('/')}/credits"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = (json.load(r).get("data") or {})
        return float(data.get("total_usage", 0) or 0)
    except Exception as e:
        log.warning("credits endpoint falló: %s", e)
        return None


def _today_ar() -> str:
    return datetime.now(_AR).strftime("%Y-%m-%d")


_DEFAULT_STATE = {
    "today": None,               # 'YYYY-MM-DD' (AR) del snapshot vigente
    "snapshot": None,            # total_usage al inicio del día (None = pendiente)
    "state": "active",           # 'active' | 'sleeping'
    "announced_today": False,       # anuncio de siesta ya salió hoy
    "wake_announced_today": False,  # anuncio de despertar ya salió hoy
}


class BudgetGuard:
    """Guard del presupuesto diario. Uso desde el loop:

        guard = BudgetGuard(get=..., set=..., fetch=..., daily_usd=1.0)
        transition = guard.check()   # 'sleep' | 'wake' | None
        if guard.burned: <saltear tareas este ciclo>

    `get`/`set` persisten el estado (string JSON) — botata inyecta db.kv_get/kv_set.
    Con enabled=False, check() es un no-op barato (nunca burned, sin red).
    """

    KEY = "budget_state"

    def __init__(
        self,
        *,
        get: Callable[[str], Optional[str]],
        set: Callable[[str, str], None],
        fetch: Callable[[], Optional[float]],
        daily_usd: float,
        enabled: bool = True,
        check_interval_s: int = 600,
        now_day: Callable[[], str] = _today_ar,
    ):
        self._get, self._set = get, set
        self._fetch = fetch
        self.daily_usd = float(daily_usd)
        self.enabled = bool(enabled)
        self.check_interval_s = check_interval_s
        self._now_day = now_day
        self._cache: tuple[float, Optional[float]] | None = None  # (ts, total_usage)
        self.burned = False  # resultado del último check()

    # ─── estado persistido ──────────────────────────────────────────────────
    def _load(self) -> dict:
        raw = self._get(self.KEY)
        if raw:
            try:
                state = json.loads(raw)
                return {**_DEFAULT_STATE, **state}
            except (ValueError, TypeError):
                log.warning("budget_state corrupto — regenerando")
        return dict(_DEFAULT_STATE)

    def _save(self, state: dict) -> None:
        self._set(self.KEY, json.dumps(state))

    # ─── lecturas ───────────────────────────────────────────────────────────
    def _usage_cached(self) -> Optional[float]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache[0] < self.check_interval_s:
            return self._cache[1]
        total = self._fetch()
        self._cache = (now, total)
        return total

    def usage_today(self, state: dict | None = None) -> Optional[float]:
        """USD gastados hoy, o None si falta snapshot o falla la lectura.
        Clamp a 0: defiende contra correcciones/refunds del provider."""
        state = state or self._load()
        if state["snapshot"] is None:
            return None
        current = self._usage_cached()
        if current is None:
            return None
        return max(0.0, current - state["snapshot"])

    # ─── el pase ────────────────────────────────────────────────────────────
    def check(self) -> Optional[str]:
        """Rota el día si cambió, asegura snapshot, evalúa el burn y devuelve
        la transición a anunciar ('sleep'|'wake') o None. Idempotente por día:
        cada anuncio sale a lo sumo una vez."""
        if not self.enabled:
            self.burned = False
            return None

        state = self._load()
        dirty = False

        today = self._now_day()
        if state["today"] != today:
            state.update(today=today, snapshot=None,
                         announced_today=False, wake_announced_today=False)
            self._cache = None  # lectura fresca para el snapshot del día nuevo
            dirty = True

        # UNA lectura de uso por pase: sirve para el snapshot y para el gasto.
        current = self._usage_cached()

        # Snapshot del inicio del día; si /credits está caído, reintenta cada pase.
        if state["snapshot"] is None and current is not None:
            state["snapshot"] = current
            log.info("budget: snapshot del día %s = $%.4f", today, current)
            dirty = True

        spent = (max(0.0, current - state["snapshot"])
                 if current is not None and state["snapshot"] is not None else None)
        if spent is None:
            self.burned = state["state"] == "sleeping"  # pegajoso ante fallas
            if dirty:
                self._save(state)
            return None
        self.burned = spent >= self.daily_usd

        transition: Optional[str] = None
        if self.burned and state["state"] == "active":
            state["state"] = "sleeping"
            if not state["announced_today"]:
                state["announced_today"] = True
                transition = "sleep"
            log.warning("budget: QUEMADO — $%.4f de $%.2f del día", spent, self.daily_usd)
            dirty = True
        elif not self.burned and state["state"] == "sleeping":
            state["state"] = "active"
            if not state["wake_announced_today"]:
                state["wake_announced_today"] = True
                transition = "wake"
            log.info("budget: despierto — día nuevo, $%.4f gastados", spent)
            dirty = True

        if dirty:
            self._save(state)
        return transition
