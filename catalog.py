"""catalog.py — sincronizador del catálogo de imágenes de botata.

Dos comandos:
  python catalog.py sync           # procesa archivos nuevos sin catalogar
  python catalog.py sync --dry-run # muestra qué haría, sin tocar
  python catalog.py stats          # resumen del catálogo

El sync es idempotente: escanea scrape/pictures/<platform>/ y para cada
archivo sin fila en image_catalog genera una descripción con IMAGE_MODEL
(gemini-2.5-flash vía OpenRouter) y la inserta.

Desacoplado a propósito del scrapeo: scrape_ig.py solo descarga y guarda
en scraped_items; catalog.py corre después y describe las imágenes.

Carpeta de input MANUAL (T12): dejá imágenes a mano en scrape/pictures/manual/
y corré 'python catalog.py sync' — se catalogan igual que las scrapeadas
(platform='manual'). El bot solo postea imágenes catalogadas (con descripción y
categoría válidas); las que no entiende no las usa.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

import db as dbmod

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("botata.catalog")

# ─── Config ──────────────────────────────────────────────────────────────────
_settings = json.loads((BASE_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
IMAGE_MODEL = _settings.get("IMAGE_MODEL", "google/gemini-2.5-flash")
OPENAI_ENDPOINT = _settings.get("OPENAI_ENDPOINT", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = __import__("os").environ["OPENROUTER_API_KEY"]

DESCRIBE_SYSTEM = (
    "Sos un asistente que analiza imágenes para un bot de una comunidad argentina. "
    "Tu tarea es describir la imagen en una oración en español rioplatense, "
    "clasificarla en una categoría, y extraer etiquetas y texto visible.\n\n"
    "Respondé SOLO con JSON válido. Campos:\n"
    "- category: 'meme' | 'foto' | 'arte' | 'captura' | 'otro'\n"
    "- description: una oración describiendo qué hay en la imagen (máx 100 chars)\n"
    "- tags: array de 3-8 strings con tags relevantes (minúsculas, sin hashtags)\n"
    "- ocr_text: texto visible en la imagen (si no hay, string vacío)"
)


# ─── Descripción de imágenes ─────────────────────────────────────────────────
def describe_image(image_path: Path, llm: OpenAI) -> dict:
    """Usa IMAGE_MODEL para describir una imagen.

    Devuelve {"category", "description", "tags", "ocr_text"}.
    Si el modelo falla, devuelve un dict con error.
    """
    ext = image_path.suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    data = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    data_url = f"data:{mime};base64,{data}"

    try:
        response = llm.chat.completions.create(
            model=IMAGE_MODEL,
            messages=[
                {"role": "system", "content": DESCRIBE_SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "Describí esta imagen."},
                ]},
            ],
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        raw = response.choices[0].message.content
        if not raw:
            raise ValueError("respuesta vacía del modelo")
        return json.loads(raw)
    except Exception as e:
        log.error("describe_image falló para %s: %s", image_path.name, e)
        raise


# ─── Comandos ────────────────────────────────────────────────────────────────
MANUAL_DIR = BASE_DIR / "scrape" / "pictures" / "manual"  # input manual formalizado (T12)


def cmd_sync(dry_run: bool = False) -> None:
    """Sincroniza el catálogo: para cada imagen sin catalogar, genera descripción y la inserta."""
    conn = dbmod.init_db()
    llm = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENAI_ENDPOINT)
    # Asegura la carpeta de input manual (T12): lugar conocido para tirar imágenes a mano.
    MANUAL_DIR.mkdir(parents=True, exist_ok=True)

    try:
        uncataloged = dbmod.list_uncataloged_files(conn, BASE_DIR)
        if not uncataloged:
            log.info("Nada para catalogar — todo sincronizado.")
            return

        log.info("Archivos sin catalogar: %d", len(uncataloged))

        processed = 0
        skipped = 0
        errors = 0

        for platform, external_id, file_path in uncataloged:
            img_path = BASE_DIR / file_path
            if not img_path.exists():
                log.warning("  [skip] %s: archivo no encontrado", file_path)
                skipped += 1
                continue

            if dry_run:
                log.info("  [dry-run] %s | platform=%s id=%s", file_path, platform, external_id)
                processed += 1
                continue

            # Buscar metadata en scraped_items para enriquecer la fila
            meta = dbmod.get_scraped_meta(conn, platform, external_id)
            source_name = meta["source_name"] if meta else "manual"
            source_url = meta["url"] if meta else None
            posted_at = meta["posted_at"] if meta else None

            try:
                desc = describe_image(img_path, llm)
                category = desc.get("category", "otro")
                description = desc.get("description", "")
                tags = desc.get("tags", [])
                ocr_text = desc.get("ocr_text", "")

                if not description:
                    log.warning("  [skip] %s: descripción vacía", file_path)
                    skipped += 1
                    continue

                fid = dbmod.upsert_image_catalog(
                    conn,
                    platform=platform,
                    external_id=external_id,
                    source_name=source_name,
                    file_path=file_path,
                    description=description,
                    category=category,
                    tags=tags,
                    ocr_text=ocr_text or None,
                    source_url=source_url,
                    posted_at=posted_at,
                )
                log.info("  [%s] #%d %s → %s (%s)",
                         category, fid, file_path,
                         description[:60], ", ".join(tags[:3]))
                processed += 1
            except Exception as e:
                log.error("  [error] %s: %s", file_path, e)
                errors += 1

        log.info("Sync completo: %d procesados, %d skipped, %d errores",
                 processed, skipped, errors)
    finally:
        conn.close()


def cmd_stats() -> None:
    """Muestra estadísticas del catálogo de imágenes."""
    conn = dbmod.init_db()
    try:
        stats = dbmod.get_image_catalog_stats(conn)
        print(f"Total imágenes catalogadas: {stats['total']}")
        if stats["total"] == 0:
            print("  (vacío — corré 'python catalog.py sync')")
            return
        print("\nPor categoría y fuente:")
        for entry in stats["by_category"]:
            print(f"  [{entry['category']}] {entry['platform']}/{entry['source_name']}: {entry['count']}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Catálogo de imágenes de botata")
    parser.add_argument(
        "cmd", choices=["sync", "stats"],
        help="sync: procesa archivos nuevos sin catalogar | stats: resumen del catálogo",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostrar qué se haría sin tocar nada (solo con sync)",
    )
    args = parser.parse_args()

    if args.cmd == "sync":
        cmd_sync(dry_run=args.dry_run)
    elif args.cmd == "stats":
        cmd_stats()


if __name__ == "__main__":
    sys.exit(main())