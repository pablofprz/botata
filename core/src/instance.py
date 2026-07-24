"""instance.py — resolución del directorio de instancia (T28c).

Un despliegue de Botata = motor (este repo, hay UNO) + instancia (carpeta con
la identidad del agente: config/settings.json, .env, context/SOUL.md, prompts/,
skills/, moods/, posted/botata.db — hay N). El mismo código corre cualquier
instancia; la carpeta la decide, en orden de precedencia:

  1. flag CLI  `--instance <dir>`  (cualquier entrypoint: botata, config_ui, mem_admin…)
  2. env var   `BOTATA_INSTANCE`
  3. la raíz del repo, SOLO si es una instancia (tiene config/settings.json) —
     back-compat para deploys viejos que corren el repo-como-instancia. Desde la
     reorg 2026-07-24 el repo es motor puro (sin identidad): sin flag ni env var,
     el proceso corta con instrucciones claras.

El flag se escanea de sys.argv directamente porque los módulos resuelven sus
paths en import-time (antes de que corra cualquier argparse). Los argparse de
los entrypoints declaran --instance solo para que no lo rechacen y salga en -h.

Regla de diseño: canal nuevo (Discord de la misma comunidad) = misma instancia;
comunidad nueva (Mastodon) = instancia nueva con su propia DB e identidad.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent  # src/ -> raíz del repo


def _from_argv(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--instance" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--instance="):
            return arg.split("=", 1)[1]
    return None


def instance_dir() -> Path:
    """El directorio de instancia vigente para este proceso."""
    raw = _from_argv(sys.argv) or os.environ.get("BOTATA_INSTANCE")
    if not raw:
        if (REPO_DIR / "config" / "settings.json").is_file():
            return REPO_DIR  # back-compat: el repo mismo es una instancia
        raise SystemExit(
            "Botata necesita una instancia (identidad + datos del agente).\n"
            "Indicala con --instance <dir> o la env var BOTATA_INSTANCE.\n"
            "Para crear una nueva: python core/src/init_instance.py <dir>"
        )
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise SystemExit(
            f"instancia inexistente: {path}\n"
            "Creala primero con: python src/init_instance.py <dir>"
        )
    return path
