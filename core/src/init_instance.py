"""init_instance.py — crea una instancia nueva de Botata (T28c). Stdlib puro.

Copia a la carpeta destino:
  - instance_template/ completo (settings.json neutro, SOUL.md neutra, .env.example,
    prompts/, moods/, skills/ y config/ genéricos — TODA la identidad default vive ahí)
  - esqueleto de datos: context/feeds/, posted/, scrape/pictures/manual/

No importa botata (la instancia todavía no tiene settings válidos). Idempotente
suave: se niega a escribir sobre una carpeta que ya tiene config/settings.json.

Uso:  python src/init_instance.py <carpeta-destino>
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_DIR / "instance_template"


def init_instance(target: Path) -> None:
    if (target / "config" / "settings.json").exists():
        raise SystemExit(f"{target} ya es una instancia (tiene config/settings.json) — no piso nada.")
    target.mkdir(parents=True, exist_ok=True)

    # 1. Plantilla completa (settings, SOUL, .env.example, prompts, moods, skills, config).
    for src in TEMPLATE.rglob("*"):
        if src.is_dir():
            continue
        dst = target / src.relative_to(TEMPLATE)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # 2. Esqueleto de datos.
    for rel in ("skills", "context/feeds", "posted", "scrape/pictures/manual"):
        (target / rel).mkdir(parents=True, exist_ok=True)

    print(f"Instancia creada en {target}")
    print("Siguientes pasos:")
    print(f"  1. Completá {target / '.env'} (plantilla en .env.example)")
    print(f"  2. Editá {target / 'context' / 'SOUL.md'} y {target / 'config' / 'settings.json'}")
    print(f"  3. Arrancá: python -m botata --instance {target}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 else 1)
    init_instance(Path(sys.argv[1]).expanduser().resolve())
