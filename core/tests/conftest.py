"""conftest.py — hace importable `src/` y provee una instancia de test.

Tras la reorganización a layout `src/`, los módulos (`botata`, `db`, `tools`, ...)
viven en `src/`. Esto los pone en `sys.path` una sola vez para toda la suite.

Desde la reorg 2026-07-24 el repo es motor puro: no trae config/, prompts/ ni
.env propios. Como varios módulos resuelven sus paths en import-time contra la
instancia (instance.instance_dir()), acá se crea UNA instancia efímera desde
instance_template/ y se apunta BOTATA_INSTANCE a ella ANTES de que cualquier
test importe botata/db/catalog. Las credenciales dummy van por env vars (los
módulos hacen os.environ["..."] en import-time).
"""
import os
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if "BOTATA_INSTANCE" not in os.environ:
    from init_instance import init_instance

    _inst = Path(tempfile.mkdtemp(prefix="botata-test-instance-"))
    init_instance(_inst)
    os.environ["BOTATA_INSTANCE"] = str(_inst)

os.environ.setdefault("BSKY_PASSWORD", "dummy")
os.environ.setdefault("OPENROUTER_API_KEY", "dummy")
