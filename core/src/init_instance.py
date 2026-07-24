"""init_instance.py — crea una instancia nueva de Botata (T28c). Stdlib puro.

Copia a la carpeta destino:
  - instance_template/ completo (settings.json neutro, SOUL.md neutra, .env.example,
    prompts/, moods/, skills/ y config/ genéricos — TODA la identidad default vive ahí)
  - esqueleto de datos: context/feeds/, posted/, scrape/pictures/manual/

No importa botata (la instancia todavía no tiene settings válidos). Idempotente
suave: se niega a escribir sobre una carpeta que ya tiene config/settings.json.

Uso:  python src/init_instance.py <carpeta-destino>
      python -m botata --init <nombre>     (crea bots/<nombre> y abre la UI de config)
"""
from __future__ import annotations

import shutil
import subprocess
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


def resolve_init_target(name: str) -> Path:
    """`--init <nombre>` → `<workspace>/bots/<nombre>`; un path (con separador o
    absoluto) se respeta tal cual. El workspace es el padre del motor (core/)."""
    if any(sep in name for sep in ("/", "\\")) or Path(name).is_absolute():
        return Path(name).expanduser().resolve()
    return (REPO_DIR.parent / "bots" / name).resolve()


def run_init(argv: list[str]) -> None:
    """Pipeline `botata --init <nombre>`: crea la instancia (si no existe) y abre
    la UI de configuración apuntada a ella. Bloquea hasta Ctrl+C en la UI."""
    try:
        name = argv[argv.index("--init") + 1]
    except (ValueError, IndexError):
        raise SystemExit("uso: python -m botata --init <nombre>  (crea bots/<nombre> y abre la UI)")
    if name.startswith("-"):
        raise SystemExit(f"--init espera un nombre de instancia, no '{name}'")

    target = resolve_init_target(name)
    if (target / "config" / "settings.json").exists():
        print(f"La instancia ya existe en {target} — abro la UI de configuración.")
    else:
        init_instance(target)

    print("\nAbriendo la UI de configuración (Ctrl+C para cerrar)...")
    print("Orden sugerido: 1) canal y credenciales → 2) admin y grupos → 3) el resto.")
    cmd = [sys.executable, str(REPO_DIR / "src" / "config_ui.py"), "--instance", str(target)]
    for passthrough in ("--no-browser",):
        if passthrough in argv:
            cmd.append(passthrough)
    if "--port" in argv:
        cmd += ["--port", argv[argv.index("--port") + 1]]
    subprocess.run(cmd, check=False)
    print(f"\nCuando termines de configurar, arrancá el bot con:")
    print(f"  python -m botata --instance {target}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) == 2 else 1)
    init_instance(Path(sys.argv[1]).expanduser().resolve())
