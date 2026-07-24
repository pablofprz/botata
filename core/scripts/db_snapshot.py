"""Copia CONSISTENTE de una SQLite en modo WAL, vía la backup API de sqlite3.

Una copia cruda del archivo .db mientras el bot corre puede agarrar un estado
torn (el WAL no está fusionado). `sqlite3.Connection.backup` produce un snapshot
transaccionalmente consistente aunque haya escrituras concurrentes.

Uso: python db_snapshot.py <origen.db> <destino.db>
"""
import sqlite3
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("uso: python db_snapshot.py <origen.db> <destino.db>", file=sys.stderr)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    con = sqlite3.connect(src)
    try:
        bck = sqlite3.connect(dst)
        try:
            con.backup(bck)
        finally:
            bck.close()
    finally:
        con.close()
    print(f"snapshot OK: {src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
