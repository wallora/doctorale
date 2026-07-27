"""
Migra i record da medici.db (SQLite locale) alla tabella doctors su Supabase.

Uso:
  1. Assicurati che .streamlit/secrets.toml contenga DATABASE_URL
  2. source .venv/bin/activate
  3. python migrate_to_supabase.py
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import psycopg2

ROOT = Path(__file__).parent
SQLITE_PATH = ROOT / "medici.db"
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"

FIELDS = [
    "nome",
    "cognome",
    "specializzazione",
    "citta",
    "indirizzo",
    "microarea",
    "telefono_fisso",
    "telefono_cellulare",
    "email",
    "orario_lunedi",
    "orario_martedi",
    "orario_mercoledi",
    "orario_giovedi",
    "orario_venerdi",
    "data_ultima_visita",
    "data_prossimo_appuntamento",
    "note_prossimo_appuntamento",
    "note_generali",
    "canale_contatto_preferito",
]


def read_database_url() -> str:
    if not SECRETS_PATH.exists():
        raise SystemExit(
            "Manca .streamlit/secrets.toml con DATABASE_URL. "
            "Crealo prima di migrare."
        )
    for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL"):
            # DATABASE_URL = "..."
            _, _, value = line.partition("=")
            return value.strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL non trovata in .streamlit/secrets.toml")


def main() -> None:
    if not SQLITE_PATH.exists():
        raise SystemExit(f"File SQLite non trovato: {SQLITE_PATH}")

    db_url = read_database_url()

    sq = sqlite3.connect(SQLITE_PATH)
    sq.row_factory = sqlite3.Row
    rows = sq.execute("SELECT * FROM medici").fetchall()
    sq.close()

    pg = psycopg2.connect(db_url)
    cur = pg.cursor()

    cur.execute("SELECT COUNT(*) FROM doctors")
    before = cur.fetchone()[0]
    print(f"Record già presenti su Supabase: {before}")
    print(f"Record da migrare da SQLite: {len(rows)}")

    inserted = 0
    skipped_dup = 0

    for r in rows:
        cur.execute(
            """
            SELECT 1 FROM doctors
            WHERE UPPER(cognome) = UPPER(%s)
              AND UPPER(nome) = UPPER(%s)
              AND UPPER(COALESCE(citta, '')) = UPPER(%s)
              AND UPPER(COALESCE(indirizzo, '')) = UPPER(%s)
            LIMIT 1
            """,
            (
                r["cognome"],
                r["nome"],
                r["citta"] or "",
                r["indirizzo"] or "",
            ),
        )
        if cur.fetchone():
            skipped_dup += 1
            continue

        values = []
        for f in FIELDS:
            v = r[f]
            if f.startswith("data_"):
                values.append(v if v else None)
            else:
                values.append(v if v is not None else "")

        placeholders = ", ".join(["%s"] * len(FIELDS))
        cur.execute(
            f"INSERT INTO doctors ({', '.join(FIELDS)}) VALUES ({placeholders})",
            values,
        )
        inserted += 1

    pg.commit()
    cur.execute("SELECT COUNT(*) FROM doctors")
    total = cur.fetchone()[0]
    cur.close()
    pg.close()

    print(
        f"OK — inseriti: {inserted}, duplicati saltati: {skipped_dup}, "
        f"totale su Supabase: {total}"
    )


if __name__ == "__main__":
    main()
