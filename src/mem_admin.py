"""mem_admin.py — CLI de administración de la memoria del bot.

Permite revisar, editar, borrar y re-embeber aprendizajes a mano. Pensado para
correr desde una terminal local (no es parte del runtime del bot):

    python mem_admin.py list --handle ppolci.com
    python mem_admin.py list --lessons
    python mem_admin.py search "que le gusta a ppolci" --handle ppolci.com
    python mem_admin.py add --handle ppolci.com --text "Le gustan los gatos"
    python mem_admin.py edit 42 --text "Vive en Rosario, no en Córdoba"
    python mem_admin.py delete 42
    python mem_admin.py reembed --handle ppolci.com
    python mem_admin.py reembed --lessons

`edit` y `delete` aceptan IDs de user_facts o de lessons (se detecta solo por
la tabla en la que existe el ID). `reembed` regenera embeddings cuando editaste
texto a mano y el vec0 quedó desactualizado.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import numpy as np

import db

log = logging.getLogger("botata.mem_admin")


# ─── helpers de localización de ID ───────────────────────────────────────────
def _which_table(conn: sqlite3.Connection, fact_id: int) -> str | None:
    """Devuelve 'user_facts', 'lessons' o None según en qué tabla existe el ID."""
    if conn.execute("SELECT 1 FROM user_facts WHERE id = ?", (fact_id,)).fetchone():
        return "user_facts"
    if conn.execute("SELECT 1 FROM lessons WHERE id = ?", (fact_id,)).fetchone():
        return "lessons"
    return None


def _vec_table(table: str) -> str:
    return "user_facts_vec" if table == "user_facts" else "lessons_vec"


def _text_col(table: str) -> str:
    return "fact_text" if table == "user_facts" else "lesson_text"


# ─── operaciones ─────────────────────────────────────────────────────────────
def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.lessons:
        rows = conn.execute(
            "SELECT id, scope, lesson_text FROM lessons ORDER BY id"
        ).fetchall()
        print(f"== lessons ({len(rows)}) ==")
        for r in rows:
            print(f"  [{r['id']}] ({r['scope']}) {r['lesson_text']}")
        return

    if not args.handle:
        # resumen por usuario
        rows = conn.execute(
            "SELECT handle, COUNT(*) AS c FROM user_facts GROUP BY handle ORDER BY c DESC"
        ).fetchall()
        print(f"== usuarios con facts ({len(rows)}) ==")
        for r in rows:
            print(f"  @{r['handle']}: {r['c']}")
        return

    rows = conn.execute(
        "SELECT id, fact_text, source_uri FROM user_facts WHERE handle = ? ORDER BY id",
        (args.handle,),
    ).fetchall()
    print(f"== user_facts @{args.handle} ({len(rows)}) ==")
    for r in rows:
        src = f" <{r['source_uri']}>" if r["source_uri"] else ""
        print(f"  [{r['id']}] {r['fact_text']}{src}")


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.handle:
        res = db.hybrid_search_user_facts(conn, args.handle, args.query, k=args.k)
        print(f"== hybrid_search_user_facts @{args.handle} ==")
    else:
        res = db.hybrid_search_lessons(conn, args.query, k=args.k)
        print("== hybrid_search_lessons ==")
    for i, t in res:
        print(f"  [{i}] {t}")


def cmd_add(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.handle:
        # asegurar la fila del usuario (FK user_facts.handle → users.handle)
        conn.execute(
            "INSERT INTO users(handle) VALUES (?) ON CONFLICT(handle) DO NOTHING",
            (args.handle,),
        )
        new_id = db.upsert_user_fact(conn, args.handle, args.text, source_uri="manual")
        print(f"user_facts: {'insertado id=' + str(new_id) if new_id else 'dedup skip (ya existe)'}")
    else:
        new_id = db.upsert_lesson(conn, args.text, scope=args.scope or "community")
        print(f"lessons: {'insertado id=' + str(new_id) if new_id else 'dedup skip (ya existe)'}")


def cmd_edit(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    table = _which_table(conn, args.id)
    if table is None:
        print(f"ERROR: id {args.id} no existe en user_facts ni lessons", file=sys.stderr)
        sys.exit(1)
    col = _text_col(table)
    conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (args.text, args.id))
    conn.execute(f"DELETE FROM {_vec_table(table)} WHERE rowid = ?", (args.id,))
    emb = db.embed(args.text)
    if table == "user_facts":
        handle = conn.execute(
            "SELECT handle FROM user_facts WHERE id = ?", (args.id,)
        ).fetchone()["handle"]
        conn.execute(
            f"INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
            (args.id, emb, handle),
        )
    else:
        conn.execute(
            "INSERT INTO lessons_vec(rowid, embedding) VALUES (?, ?)", (args.id, emb)
        )
    conn.commit()
    print(f"{table}[{args.id}] editado y re-embebedo")


def cmd_delete(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    table = _which_table(conn, args.id)
    if table is None:
        print(f"ERROR: id {args.id} no existe", file=sys.stderr)
        sys.exit(1)
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (args.id,))
    conn.execute(f"DELETE FROM {_vec_table(table)} WHERE rowid = ?", (args.id,))
    conn.commit()
    print(f"{table}[{args.id}] borrado (texto + embedding)")


def cmd_reembed(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Regenera embeddings. Útil si el texto fue editado a mano fuera de este CLI."""
    if args.lessons:
        rows = conn.execute("SELECT id, lesson_text FROM lessons").fetchall()
        for r in rows:
            emb = db.embed(r["lesson_text"])
            conn.execute(
                "DELETE FROM lessons_vec WHERE rowid = ?", (r["id"],)
            )
            conn.execute(
                "INSERT INTO lessons_vec(rowid, embedding) VALUES (?, ?)", (r["id"], emb)
            )
            print(f"  lessons[{r['id']}] re-embebedo")
        conn.commit()
        print(f"reembed lessons: {len(rows)} registro(s)")
        return

    if args.handle:
        rows = conn.execute(
            "SELECT id, fact_text FROM user_facts WHERE handle = ?", (args.handle,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, fact_text FROM user_facts").fetchall()
        print("reembed ALL user_facts (sin --handle)...")
    for r in rows:
        emb = db.embed(r["fact_text"])
        conn.execute("DELETE FROM user_facts_vec WHERE rowid = ?", (r["id"],))
        conn.execute(
            "INSERT INTO user_facts_vec(rowid, embedding, partition_key) VALUES (?, ?, ?)",
            (r["id"], emb, args.handle or _lookup_handle(conn, r["id"])),
        )
        print(f"  user_facts[{r['id']}] re-embebedo")
    conn.commit()
    print(f"reembed user_facts: {len(rows)} registro(s)")


def _lookup_handle(conn: sqlite3.Connection, fact_id: int) -> str:
    return conn.execute(
        "SELECT handle FROM user_facts WHERE id = ?", (fact_id,)
    ).fetchone()["handle"]


# ─── argparse ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Administración de memoria de botata.")
    p.add_argument("--instance", help="Directorio de la instancia (default: raíz del repo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="Listar facts de un usuario, lecciones, o resumen.")
    pl.add_argument("--handle", help="Handle (sin @). Si se omite y no --lessons, resumen.")
    pl.add_argument("--lessons", action="store_true", help="Listar lecciones en vez de facts.")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("search", help="Búsqueda híbrida sobre facts (con --handle) o lecciones.")
    ps.add_argument("query")
    ps.add_argument("--handle", help="Si se setea, busca facts del usuario; si no, lecciones.")
    ps.add_argument("--k", type=int, default=5)
    ps.set_defaults(func=cmd_search)

    pa = sub.add_parser("add", help="Agregar un fact (con --handle) o lección.")
    pa.add_argument("--handle", help="Si se omite, agrega una lección.")
    pa.add_argument("--text", required=True)
    pa.add_argument("--scope", help="Solo para lecciones (default: community).")
    pa.set_defaults(func=cmd_add)

    pe = sub.add_parser("edit", help="Editar texto de un fact/lección por ID y re-embeber.")
    pe.add_argument("id", type=int)
    pe.add_argument("--text", required=True)
    pe.set_defaults(func=cmd_edit)

    pd = sub.add_parser("delete", help="Borrar un fact/lección por ID (texto + embedding).")
    pd.add_argument("id", type=int)
    pd.set_defaults(func=cmd_delete)

    pr = sub.add_parser("reembed", help="Regenerar embeddings (tras edición manual).")
    pr.add_argument("--handle", help="Solo facts de este usuario. Si se omite, todos los facts.")
    pr.add_argument("--lessons", action="store_true", help="Re-embeber lecciones en vez de facts.")
    pr.set_defaults(func=cmd_reembed)

    return p


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()
    conn = db.init_db()
    args.func(conn, args)
    conn.close()


if __name__ == "__main__":
    main()
