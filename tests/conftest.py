"""conftest.py — hace importable el código de `src/` desde los tests.

Tras la reorganización a layout `src/`, los módulos (`botata`, `db`, `tools`, ...)
viven en `src/`. Esto los pone en `sys.path` una sola vez para toda la suite, así
los tests siguen haciendo `import botata as b` sin editar cada archivo. (El editable
install también los expone; esto es el cinturón-y-tiradores para correr sin instalar.)
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
