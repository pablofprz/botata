"""instance.py — resolución del directorio de instancia (T28c).

Un despliegue de Botata = motor (este repo, hay UNO) + instancia (carpeta con
la identidad del agente: config/settings.json, .env, context/SOUL.md, prompts/,
skills/, moods/, posted/botata.db — hay N). El mismo código corre cualquier
instancia; la carpeta la decide, en orden de precedencia:

  1. flag CLI  `--instance <dir>`  (cualquier entrypoint: botata, config_ui, mem_admin…)
  2. env var   `BOTATA_INSTANCE`
  3. la raíz del repo (back-compat: el layout actual sigue funcionando igual)

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
        return REPO_DIR
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise SystemExit(
            f"instancia inexistente: {path}\n"
            "Creala primero con: python src/init_instance.py <dir>"
        )
    return path
