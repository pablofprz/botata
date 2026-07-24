"""parse_memory.py — vuelca la memoria del bot a archivos legibles.

Lee botata.db (SQLite) y escribe la memoria semántica como markdown en una
carpeta (por default `parsed_memory/`), para revisarla a ojo o versionarla. NO
toca embeddings ni el runtime: es solo lectura + dump de texto.

    python parse_memory.py                 # vuelca a parsed_memory/
    python parse_memory.py --out dump      # otra carpeta de salida
    python parse_memory.py --db posted/botata.db

Estructura de salida:
    parsed_memory/
        README.md            resumen (conteos + fecha del dump)
        users/<handle>.md    perfil (did/bio) + facts autorrevelados
        lessons.md           lecciones conductuales (cross-user)
        events.md            calendario (eventos de usuario + comunidad)
        relationships.md     grafo de relaciones (si hay)
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

import db


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_name(handle: str) -> str:
    """Handle → nombre de archivo seguro (los handles pueden traer '.' y '/')."""
    return handle.replace("/", "_") or "sin_handle"


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """La DB puede ser vieja (tabla aún no creada por init_db): dump tolerante."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def dump_users(conn: sqlite3.Connection, out: Path) -> int:
    users_dir = out / "users"
    users_dir.mkdir(parents=True, exist_ok=True)
    users = conn.execute(
        "SELECT handle, did, display_name, bio_raw, bio_interp, created_at, updated_at "
        "FROM users ORDER BY handle"
    ).fetchall()
    for u in users:
        facts = conn.execute(
            "SELECT id, fact_text, source_uri, created_at FROM user_facts "
            "WHERE handle = ? ORDER BY id",
            (u["handle"],),
        ).fetchall()
        lines = [f"# @{u['handle']}", ""]
        if u["display_name"]:
            lines.append(f"- **Nombre:** {u['display_name']}")
        if u["did"]:
            lines.append(f"- **DID:** {u['did']}")
        lines.append(f"- **Creado:** {u['created_at']}  ·  **Actualizado:** {u['updated_at']}")
        lines.append("")
        if u["bio_raw"]:
            lines += ["## Bio (literal)", "", u["bio_raw"], ""]
        if u["bio_interp"]:
            lines += ["## Bio (interpretada)", "", u["bio_interp"], ""]
        lines += [f"## Hechos ({len(facts)})", ""]
        for f in facts:
            src = f"  ·  fuente: {f['source_uri']}" if f["source_uri"] else ""
            lines.append(f"- [{f['id']}] {f['fact_text']}{src}")
        lines.append("")
        inters = conn.execute(
            "SELECT summary, created_at FROM interactions WHERE handle = ? ORDER BY id DESC",
            (u["handle"],),
        ).fetchall() if _has_table(conn, "interactions") else []
        if inters:
            lines += [f"## Interacciones ({len(inters)})", ""]
            for i in inters:
                lines.append(f"- [{i['created_at'][:16]}] {i['summary']}")
            lines.append("")
        (users_dir / f"{_safe_name(u['handle'])}.md").write_text(
            "\n".join(lines), encoding="utf-8"
        )
    return len(users)


def dump_lessons(conn: sqlite3.Connection, out: Path) -> int:
    rows = conn.execute(
        "SELECT id, lesson_text, scope, created_at FROM lessons ORDER BY id"
    ).fetchall()
    lines = [f"# Lecciones ({len(rows)})", ""]
    for r in rows:
        lines.append(f"- [{r['id']}] ({r['scope']}) {r['lesson_text']}  ·  {r['created_at']}")
    lines.append("")
    (out / "lessons.md").write_text("\n".join(lines), encoding="utf-8")
    return len(rows)


def dump_events(conn: sqlite3.Connection, out: Path) -> int:
    rows = conn.execute(
        "SELECT id, handle, title, description, event_at, kind, source, created_at "
        "FROM events ORDER BY event_at"
    ).fetchall()
    lines = [f"# Eventos / calendario ({len(rows)})", ""]
    for r in rows:
        owner = f"@{r['handle']}" if r["handle"] else "comunidad"
        lines.append(f"## [{r['id']}] {r['title']}")
        lines.append(f"- **Cuándo:** {r['event_at']}  ·  **Tipo:** {r['kind']}  ·  **Dueño:** {owner}")
        if r["source"]:
            lines.append(f"- **Fuente:** {r['source']}")
        if r["description"]:
            lines += ["", r["description"]]
        lines.append("")
    (out / "events.md").write_text("\n".join(lines), encoding="utf-8")
    return len(rows)


def dump_relationships(conn: sqlite3.Connection, out: Path) -> int:
    rows = conn.execute(
        "SELECT handle_a, handle_b, kind, weight, last_at FROM relationships "
        "ORDER BY weight DESC"
    ).fetchall()
    lines = [f"# Relaciones ({len(rows)})", ""]
    for r in rows:
        lines.append(
            f"- @{r['handle_a']} — @{r['handle_b']}  ·  {r['kind']}  ·  "
            f"peso {r['weight']:.2f}  ·  {r['last_at']}"
        )
    lines.append("")
    (out / "relationships.md").write_text("\n".join(lines), encoding="utf-8")
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="Vuelca la memoria del bot a archivos legibles.")
    p.add_argument("--instance", help="Directorio de la instancia (default: raíz del repo)")
    p.add_argument("--db", default=str(db.DB_PATH), help="Ruta a botata.db.")
    p.add_argument("--out", default="parsed_memory", help="Carpeta de salida.")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = _connect(args.db)

    n_users = dump_users(conn, out)
    n_lessons = dump_lessons(conn, out)
    n_events = dump_events(conn, out)
    n_rels = dump_relationships(conn, out)
    n_facts = conn.execute("SELECT COUNT(*) FROM user_facts").fetchone()[0]
    n_inters = (conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
                if _has_table(conn, "interactions") else 0)
    conn.close()

    readme = [
        "# Memoria del bot (dump)",
        "",
        f"- Generado: {datetime.now().isoformat(timespec='seconds')}",
        f"- Origen: `{args.db}`",
        "",
        "| Tabla | Registros |",
        "| --- | --- |",
        f"| users | {n_users} |",
        f"| user_facts | {n_facts} |",
        f"| interactions | {n_inters} |",
        f"| lessons | {n_lessons} |",
        f"| events | {n_events} |",
        f"| relationships | {n_rels} |",
        "",
        "Solo lectura: este dump no incluye embeddings y no modifica la DB.",
        "",
    ]
    (out / "README.md").write_text("\n".join(readme), encoding="utf-8")

    print(f"OK → {out}/  ({n_users} users, {n_facts} facts, {n_lessons} lessons, "
          f"{n_events} events, {n_rels} rels)")


if __name__ == "__main__":
    main()
