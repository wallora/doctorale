"""
Doctorale — gestione anagrafica medici (uso personale).
Login con credenziali hashate (PBKDF2) + CRUD su Supabase (PostgreSQL).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator, Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st
import altair as alt

from sales_parser import (
    MESI_LABEL,
    infer_period_from_filename,
    infer_period_from_workbook,
    list_sheet_names,
    parse_sales_workbook,
    suggest_sheets,
)
from finance_pages import (
    page_elenco_fatture,
    page_elenco_pagamenti,
    page_elenco_prelievi,
    page_quote_annuali,
    page_contabilita_mensile,
)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------

# Credenziali login: preferisci i Secrets (locale o Streamlit Cloud).
# Fallback hardcoded solo per sviluppo locale senza secrets.
# Per generare un nuovo hash:
#   python -c "import hashlib,base64; print(base64.b64encode(hashlib.pbkdf2_hmac('sha256', b'NUOVA_PASSWORD', b'doctorale_salt_v1', 260000)).decode())"
DEFAULT_AUTH_USERNAME = "admin"
AUTH_PASSWORD_SALT = b"doctorale_salt_v1"
DEFAULT_AUTH_PASSWORD_HASH = "TNBQkbqxlofbt8MPQi+UZBitspP8OiI1m+0e0L7xxXA="
AUTH_PBKDF2_ITERATIONS = 260_000

TABLE_NAME = "doctors"
SALES_TABLE = "sales_data"

MICROAREE = ("Monza Brianza 08", "Milano 09")
GIORNI = ("lunedi", "martedi", "mercoledi", "giovedi", "venerdi")
GIORNI_LABEL = {
    "lunedi": "Lunedì",
    "martedi": "Martedì",
    "mercoledi": "Mercoledì",
    "giovedi": "Giovedì",
    "venerdi": "Venerdì",
}
CANALI_CONTATTO = (
    "Telefono fisso",
    "Cellulare",
    "Email",
    "WhatsApp",
    "Altro",
)
SPECIALIZZAZIONI = (
    "Gastroenterogia",
    "Geriatria",
    "MMG",
    "Neurologia",
    "Proctologia",
    "Psichiatria",
    "Urologia",
    "Vascolare",
)

COLUMNS = [
    "id",
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

DISPLAY_LABELS = {
    "id": "ID",
    "nome": "Nome",
    "cognome": "Cognome",
    "specializzazione": "Specializzazione",
    "citta": "Città",
    "indirizzo": "Indirizzo",
    "microarea": "Microarea",
    "telefono_fisso": "Telefono fisso",
    "telefono_cellulare": "Cellulare",
    "email": "Email",
    "orario_lunedi": "Orario lunedì",
    "orario_martedi": "Orario martedì",
    "orario_mercoledi": "Orario mercoledì",
    "orario_giovedi": "Orario giovedì",
    "orario_venerdi": "Orario venerdì",
    "data_ultima_visita": "Ultima visita",
    "data_prossimo_appuntamento": "Prossimo appuntamento",
    "note_prossimo_appuntamento": "Note prossimo app.",
    "note_generali": "Note generali",
    "canale_contatto_preferito": "Canale preferito",
}

# Campi importabili (senza id) e alias per suggerire il mapping automatico
IMPORT_FIELDS = [c for c in COLUMNS if c != "id"]
IMPORT_REQUIRED = ("cognome",)
NONE_OPTION = "— non importare —"

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "cognome": (
        "cognome",
        "account cognome",
        "account: cognome",
        "surname",
        "last name",
        "lastname",
    ),
    "nome": (
        "nome",
        "account nome",
        "account: nome",
        "name",
        "first name",
        "firstname",
    ),
    "specializzazione": (
        "specializzazione",
        "specialita",
        "specialità",
        "1a specializzazione",
        "account: 1a specializzazione",
        "argomento di visita",
        "specialty",
    ),
    "citta": ("citta", "città", "city", "comune", "localita", "località"),
    "indirizzo": (
        "indirizzo",
        "indirizzo preferito",
        "address",
        "via",
        "sede",
    ),
    "microarea": ("microarea", "area", "zona", "territorio"),
    "telefono_fisso": ("telefono fisso", "tel fisso", "fisso", "phone", "telefono"),
    "telefono_cellulare": (
        "telefono cellulare",
        "cellulare",
        "cell",
        "mobile",
        "account: telefono",
    ),
    "email": ("email", "e-mail", "mail"),
    "orario_lunedi": ("lunedi", "lunedì", "lun", "orario lunedi", "orario lunedì"),
    "orario_martedi": ("martedi", "martedì", "mart", "orario martedi", "orario martedì"),
    "orario_mercoledi": (
        "mercoledi",
        "mercoledì",
        "merc",
        "orario mercoledi",
        "orario mercoledì",
    ),
    "orario_giovedi": ("giovedi", "giovedì", "giova", "orario giovedi", "orario giovedì"),
    "orario_venerdi": ("venerdi", "venerdì", "ven", "orario venerdi", "orario venerdì"),
    "data_ultima_visita": (
        "data ultima visita",
        "ultima visita",
        "last visit",
    ),
    "data_prossimo_appuntamento": (
        "data prossimo appuntamento",
        "prossimo appuntamento",
        "prox appuntamento",
        "next appointment",
    ),
    "note_prossimo_appuntamento": (
        "note prossimo appuntamento",
        "note prossimo",
        "note prox",
    ),
    "note_generali": ("note", "note generali", "notes", "commenti"),
    "canale_contatto_preferito": (
        "canale",
        "canale preferito",
        "canale di contatto preferito",
        "contatto preferito",
    ),
}


# ---------------------------------------------------------------------------
# Autenticazione
# ---------------------------------------------------------------------------

def get_auth_username() -> str:
    try:
        username = st.secrets.get("AUTH_USERNAME")
        if username:
            return str(username)
    except Exception:
        pass
    return DEFAULT_AUTH_USERNAME


def get_auth_password_hash() -> str:
    try:
        password_hash = st.secrets.get("AUTH_PASSWORD_HASH")
        if password_hash:
            return str(password_hash)
    except Exception:
        pass
    return DEFAULT_AUTH_PASSWORD_HASH


def verify_password(password: str) -> bool:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        AUTH_PASSWORD_SALT,
        AUTH_PBKDF2_ITERATIONS,
    )
    return base64.b64encode(dk).decode("ascii") == get_auth_password_hash()


def check_login(username: str, password: str) -> bool:
    return username == get_auth_username() and verify_password(password)


# ---------------------------------------------------------------------------
# Database (Supabase / PostgreSQL)
# ---------------------------------------------------------------------------

def get_database_url() -> str:
    # Locale: .streamlit/secrets.toml  |  Cloud: App settings → Secrets
    try:
        url = st.secrets.get("DATABASE_URL")
        if url:
            return str(url)
    except Exception:
        pass
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL mancante. Configurala in .streamlit/secrets.toml "
            "o nei Secrets di Streamlit Cloud."
        )
    return url


@contextmanager
def get_connection() -> Iterator[psycopg2.extensions.connection]:
    conn = psycopg2.connect(get_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Verifica doctors e crea sales_data se manca."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (TABLE_NAME,),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Tabella '{TABLE_NAME}' non trovata su Supabase. "
                    "Creala dall'SQL Editor prima di usare l'app."
                )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {SALES_TABLE} (
                    id BIGSERIAL PRIMARY KEY,
                    anno INTEGER NOT NULL,
                    mese INTEGER NOT NULL,
                    metrica TEXT NOT NULL,
                    livello TEXT NOT NULL,
                    informatore TEXT NOT NULL DEFAULT '',
                    microarea TEXT NOT NULL DEFAULT '',
                    prodotto TEXT NOT NULL,
                    vendita_ap DOUBLE PRECISION,
                    vendita DOUBLE PRECISION,
                    pct_italia DOUBLE PRECISION,
                    crescita_pct DOUBLE PRECISION,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            # Chiave primaria logica: microarea (+ periodo/metrica/prodotto).
            # L'informatore è uno snapshot storico del periodo (può cambiare nel tempo).
            cur.execute(f"DROP INDEX IF EXISTS {SALES_TABLE}_uniq")
            cur.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {SALES_TABLE}_microarea_uniq
                ON {SALES_TABLE} (anno, mese, metrica, microarea, prodotto)
                WHERE livello = 'microarea'
                """
            )
            cur.execute(
                f"""
                CREATE UNIQUE INDEX IF NOT EXISTS {SALES_TABLE}_aggregati_uniq
                ON {SALES_TABLE} (anno, mese, metrica, livello, informatore, prodotto)
                WHERE livello <> 'microarea'
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {SALES_TABLE}_periodo_idx
                ON {SALES_TABLE} (anno, mese)
                """
            )
            cur.execute(f"ALTER TABLE {SALES_TABLE} ENABLE ROW LEVEL SECURITY")

            # Ferrari Raffaella è l'AM, non un informatore
            cur.execute(
                f"""
                DELETE FROM {SALES_TABLE}
                WHERE livello IN ('informatore', 'microarea')
                  AND (
                    informatore ILIKE 'ferrari raffaella%'
                    OR LOWER(TRIM(informatore)) = 'ferrari'
                  )
                """
            )
            cur.execute(
                f"""
                UPDATE {SALES_TABLE}
                SET informatore = ''
                WHERE livello IN ('am', 'italia')
                  AND informatore <> ''
                """
            )


def _date_to_str(value: Optional[date]) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _str_to_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def search_medici(
    query: str = "",
    microarea: Optional[str] = None,
    specializzazione: Optional[str] = None,
) -> pd.DataFrame:
    sql = f"SELECT * FROM {TABLE_NAME} WHERE 1=1"
    params: list[Any] = []

    if query.strip():
        like = f"%{query.strip()}%"
        sql += """
            AND (
                nome ILIKE %s
                OR cognome ILIKE %s
                OR citta ILIKE %s
            )
        """
        params.extend([like, like, like])

    if microarea and microarea != "Tutte":
        sql += " AND microarea = %s"
        params.append(microarea)

    if specializzazione and specializzazione != "Tutte":
        sql += " AND specializzazione = %s"
        params.append(specializzazione)

    sql += " ORDER BY cognome ASC, nome ASC"

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def get_medico(medico_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM {TABLE_NAME} WHERE id = %s",
                (medico_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def insert_medico(data: dict[str, Any]) -> int:
    fields = [c for c in COLUMNS if c != "id"]
    placeholders = ", ".join("%s" for _ in fields)
    cols = ", ".join(fields)
    values = [data.get(f) for f in fields]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE_NAME} ({cols})
                VALUES ({placeholders})
                RETURNING id
                """,
                values,
            )
            new_id = cur.fetchone()[0]
    return int(new_id)


def update_medico(medico_id: int, data: dict[str, Any]) -> None:
    fields = [c for c in COLUMNS if c != "id"]
    assignments = ", ".join(f"{f} = %s" for f in fields)
    values = [data.get(f) for f in fields] + [medico_id]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_NAME} SET {assignments} WHERE id = %s",
                values,
            )


def delete_medico(medico_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {TABLE_NAME} WHERE id = %s",
                (medico_id,),
            )


def count_medici() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            return int(cur.fetchone()[0])


def reset_all_medici() -> int:
    """Elimina tutti i record e resetta la sequenza id. Ritorna quanti erano."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            total = int(cur.fetchone()[0])
            cur.execute(f"TRUNCATE TABLE {TABLE_NAME} RESTART IDENTITY")
    return total


def count_sales_for_period(anno: int, mese: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM {SALES_TABLE} WHERE anno = %s AND mese = %s",
                (anno, mese),
            )
            return int(cur.fetchone()[0])


def delete_sales_period(anno: int, mese: int) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {SALES_TABLE} WHERE anno = %s AND mese = %s",
                (anno, mese),
            )
            return int(cur.rowcount)


def insert_sales_records(anno: int, mese: int, records: list[Any]) -> int:
    if not records:
        return 0

    # Deduplica per chiave logica: per le microaree l'informatore non fa parte della chiave
    deduped: dict[tuple, Any] = {}
    for r in records:
        if r.livello == "microarea":
            if not (r.microarea or "").strip():
                continue
            key = (r.metrica, "microarea", r.microarea.strip().upper(), r.prodotto)
            # normalizza microarea in maiuscolo
            r.microarea = r.microarea.strip().upper()
        else:
            key = (
                r.metrica,
                r.livello,
                (r.informatore or "").strip(),
                r.prodotto,
            )
        deduped[key] = r

    values = [
        (
            anno,
            mese,
            r.metrica,
            r.livello,
            r.informatore or "",
            r.microarea or "",
            r.prodotto,
            r.vendita_ap,
            r.vendita,
            r.pct_italia,
            r.crescita_pct,
        )
        for r in deduped.values()
    ]
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                f"""
                INSERT INTO {SALES_TABLE} (
                    anno, mese, metrica, livello, informatore, microarea, prodotto,
                    vendita_ap, vendita, pct_italia, crescita_pct
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                values,
                page_size=200,
            )
    return len(values)


def search_sales(
    anno: Optional[int] = None,
    mese: Optional[int] = None,
    metrica: Optional[str] = None,
    livello: Optional[str] = None,
    prodotto: Optional[str] = None,
    informatore: Optional[str] = None,
    microarea: Optional[str] = None,
) -> pd.DataFrame:
    sql = f"SELECT * FROM {SALES_TABLE} WHERE 1=1"
    params: list[Any] = []
    if anno:
        sql += " AND anno = %s"
        params.append(anno)
    if mese:
        sql += " AND mese = %s"
        params.append(mese)
    if metrica and metrica != "Tutte":
        sql += " AND metrica = %s"
        params.append(metrica)
    if livello and livello != "Tutti":
        sql += " AND livello = %s"
        params.append(livello)
    if prodotto and prodotto != "Tutti":
        sql += " AND prodotto = %s"
        params.append(prodotto)
    if informatore and informatore != "Tutti":
        sql += " AND informatore ILIKE %s"
        params.append(informatore)
    if microarea and microarea != "Tutte":
        sql += " AND microarea = %s"
        params.append(microarea)
    sql += " ORDER BY anno DESC, mese DESC, microarea, prodotto, metrica, livello"

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def list_sales_filter_values() -> dict[str, list[Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT anno FROM {SALES_TABLE} ORDER BY anno DESC")
            anni = [r[0] for r in cur.fetchall()]
            cur.execute(f"SELECT DISTINCT mese FROM {SALES_TABLE} ORDER BY mese")
            mesi = [r[0] for r in cur.fetchall()]
            cur.execute(
                f"SELECT DISTINCT prodotto FROM {SALES_TABLE} ORDER BY prodotto"
            )
            prodotti = [r[0] for r in cur.fetchall()]
            cur.execute(
                f"""
                SELECT DISTINCT informatore FROM {SALES_TABLE}
                WHERE informatore <> ''
                  AND livello IN ('informatore', 'microarea')
                  AND informatore NOT ILIKE 'ferrari raffaella%'
                  AND LOWER(informatore) <> 'ferrari'
                ORDER BY informatore
                """
            )
            informatori = [r[0] for r in cur.fetchall()]
            cur.execute(
                f"""
                SELECT DISTINCT microarea FROM {SALES_TABLE}
                WHERE microarea <> '' ORDER BY microarea
                """
            )
            microaree = [r[0] for r in cur.fetchall()]
            cur.execute(
                f"""
                SELECT DISTINCT anno, mese FROM {SALES_TABLE}
                ORDER BY anno ASC, mese ASC
                """
            )
            periodi = [(int(r[0]), int(r[1])) for r in cur.fetchall()]
    return {
        "anni": anni,
        "mesi": mesi,
        "prodotti": prodotti,
        "informatori": informatori,
        "microaree": microaree,
        "periodi": periodi,
    }


def _entity_norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("`", "").strip()
    return re.sub(r"\s+", " ", text)


def _match_name_to_existing(value: str, existing: list[str]) -> tuple[str, str]:
    """Ritorna (stato, valore_db).

    stato: 'già presente' | 'associato' | 'nuovo'
    """
    raw = (value or "").strip()
    if not raw:
        return "nuovo", ""
    key = _entity_norm(raw)
    existing_norm = {_entity_norm(e): e for e in existing if e}

    if key in existing_norm:
        return "già presente", existing_norm[key]

    # Match per prefisso / cognome (utile per informatori)
    candidates: list[str] = []
    for e_key, e_val in existing_norm.items():
        if key.startswith(e_key + " ") or e_key.startswith(key + " "):
            candidates.append(e_val)
            continue
        if key.split()[0] == e_key.split()[0] and (
            " " in key or " " in e_key
        ):
            candidates.append(e_val)

    # Unico candidato chiaro
    uniq = sorted(set(candidates), key=lambda x: (-len(x), x))
    if len(uniq) == 1:
        return "associato", uniq[0]
    if len(uniq) > 1:
        # preferisci quello con più token in comune
        best = max(
            uniq,
            key=lambda x: len(set(_entity_norm(x).split()) & set(key.split())),
        )
        return "associato", best

    return "nuovo", ""


def build_sales_match_report(records: list[Any]) -> dict[str, pd.DataFrame]:
    """Confronta entità del file con quelle già presenti a DB."""
    existing = list_sales_filter_values()
    file_inf = sorted(
        {
            (r.informatore or "").strip()
            for r in records
            if (r.informatore or "").strip()
        }
    )
    file_prod = sorted(
        {(r.prodotto or "").strip() for r in records if (r.prodotto or "").strip()}
    )
    file_micro = sorted(
        {
            (r.microarea or "").strip().upper()
            for r in records
            if (r.livello == "microarea" and (r.microarea or "").strip())
        }
    )

    def rows_for(values: list[str], existing_values: list[str]) -> list[dict[str, str]]:
        out = []
        for value in values:
            stato, matched = _match_name_to_existing(value, existing_values)
            out.append(
                {
                    "Nel file": value,
                    "Stato": stato,
                    "Associato a DB": matched if matched else "—",
                }
            )
        return out

    return {
        "informatori": pd.DataFrame(
            rows_for(file_inf, existing["informatori"])
        ),
        "prodotti": pd.DataFrame(rows_for(file_prod, existing["prodotti"])),
        "microaree": pd.DataFrame(rows_for(file_micro, existing["microaree"])),
    }


def apply_sales_entity_mapping(records: list[Any]) -> list[Any]:
    """Allinea i nomi del file a quelli già presenti a DB quando c'è match."""
    existing = list_sales_filter_values()
    inf_map: dict[str, str] = {}
    prod_map: dict[str, str] = {}
    micro_map: dict[str, str] = {}

    for r in records:
        inf = (r.informatore or "").strip()
        if inf and inf not in inf_map:
            stato, matched = _match_name_to_existing(inf, existing["informatori"])
            inf_map[inf] = matched if stato != "nuovo" and matched else inf

        prod = (r.prodotto or "").strip()
        if prod and prod not in prod_map:
            stato, matched = _match_name_to_existing(prod, existing["prodotti"])
            prod_map[prod] = matched if stato != "nuovo" and matched else prod

        if r.livello == "microarea":
            micro = (r.microarea or "").strip().upper()
            if micro and micro not in micro_map:
                stato, matched = _match_name_to_existing(micro, existing["microaree"])
                micro_map[micro] = (
                    matched if stato != "nuovo" and matched else micro
                )

    for r in records:
        if r.informatore:
            r.informatore = inf_map.get(r.informatore.strip(), r.informatore)
        if r.prodotto:
            r.prodotto = prod_map.get(r.prodotto.strip(), r.prodotto)
        if r.livello == "microarea" and r.microarea:
            key = r.microarea.strip().upper()
            r.microarea = micro_map.get(key, key)

    return records


def _period_label(period: tuple[int, int]) -> str:
    anno, mese = period
    return f"{MESI_LABEL.get(mese, mese)} {anno}"


def _period_sort_key(period: tuple[int, int]) -> int:
    anno, mese = period
    return anno * 12 + mese


def _informatore_cognome(name: str) -> str:
    """Estrae il cognome per etichette legenda compatte."""
    parts = [p for p in str(name or "").strip().split() if p]
    if not parts:
        return str(name or "")
    particles = {"di", "de", "del", "della", "dei", "degli", "dal", "dalla", "da"}
    first = parts[0].lower().rstrip("'")
    if len(parts) >= 2 and (first in particles or parts[0].lower().startswith("d'")):
        return f"{parts[0]} {parts[1]}"
    return parts[0]


def fetch_sales_trend(
    metrica: str,
    valore: str,
    dimensione: str,
    entities: list[str],
    prodotti: list[str],
    period_from: tuple[int, int],
    period_to: tuple[int, int],
    include_italia: bool = False,
    include_am: bool = False,
) -> pd.DataFrame:
    """Serie temporali per grafico andamento.

    valore: 'vendita' | 'crescita_pct'
    """
    if (not entities and not include_italia and not include_am) or not prodotti:
        return pd.DataFrame()

    start_key = _period_sort_key(period_from)
    end_key = _period_sort_key(period_to)
    if start_key > end_key:
        period_from, period_to = period_to, period_from
        start_key, end_key = end_key, start_key

    if valore == "crescita_pct":
        value_expr = "AVG(crescita_pct)"
    else:
        value_expr = "SUM(vendita)"

    frames: list[pd.DataFrame] = []

    def _load(
        sql: str,
        params: list[Any],
        serie_builder,
    ) -> None:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        if not rows:
            return
        part = pd.DataFrame([dict(r) for r in rows])
        part["periodo"] = pd.to_datetime(
            dict(year=part["anno"], month=part["mese"], day=1)
        )
        labels = serie_builder(part)
        part["serie"] = labels["short"]
        part["serie_full"] = labels["full"]
        frames.append(part)

    if entities:
        if dimensione == "Informatore":
            livello = "informatore"
            entity_col = "informatore"
        else:
            livello = "microarea"
            entity_col = "microarea"

        sql = f"""
            SELECT anno, mese, {entity_col} AS entita, prodotto,
                   {value_expr} AS valore
            FROM {SALES_TABLE}
            WHERE metrica = %s
              AND livello = %s
              AND {entity_col} = ANY(%s)
              AND prodotto = ANY(%s)
              AND (anno * 12 + mese) BETWEEN %s AND %s
            GROUP BY anno, mese, {entity_col}, prodotto
            ORDER BY anno, mese, {entity_col}, prodotto
        """
        params = [metrica, livello, entities, prodotti, start_key, end_key]

        def _serie_entities(df: pd.DataFrame) -> dict[str, pd.Series]:
            full = df["entita"].astype(str)
            if dimensione == "Informatore":
                short = full.map(_informatore_cognome)
            else:
                short = full
            if len(prodotti) > 1:
                prod = df["prodotto"].astype(str)
                return {
                    "short": short + " — " + prod,
                    "full": full + " — " + prod,
                }
            return {"short": short, "full": full}

        _load(sql, params, _serie_entities)

    for enabled, livello, label in (
        (include_italia, "italia", "Totale Italia"),
        (include_am, "am", "Totale AM"),
    ):
        if not enabled:
            continue
        sql = f"""
            SELECT anno, mese, prodotto, {value_expr} AS valore
            FROM {SALES_TABLE}
            WHERE metrica = %s
              AND livello = %s
              AND prodotto = ANY(%s)
              AND (anno * 12 + mese) BETWEEN %s AND %s
            GROUP BY anno, mese, prodotto
            ORDER BY anno, mese, prodotto
        """
        params = [metrica, livello, prodotti, start_key, end_key]

        def _serie_agg(df: pd.DataFrame, lbl: str = label) -> dict[str, pd.Series]:
            if len(prodotti) == 1:
                series = pd.Series([lbl] * len(df), index=df.index)
            else:
                series = pd.Series(
                    [f"{lbl} — {p}" for p in df["prodotto"].astype(str)],
                    index=df.index,
                )
            return {"short": series, "full": series}

        _load(sql, params, _serie_agg)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def find_duplicate_id(
    cognome: str,
    nome: str,
    citta: str,
    indirizzo: str,
) -> Optional[int]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id FROM {TABLE_NAME}
                WHERE UPPER(cognome) = UPPER(%s)
                  AND UPPER(nome) = UPPER(%s)
                  AND UPPER(COALESCE(citta, '')) = UPPER(%s)
                  AND UPPER(COALESCE(indirizzo, '')) = UPPER(%s)
                LIMIT 1
                """,
                (cognome, nome, citta or "", indirizzo or ""),
            )
            row = cur.fetchone()
    return int(row[0]) if row else None


def insert_medici_batch(records: list[dict[str, Any]]) -> int:
    if not records:
        return 0
    fields = IMPORT_FIELDS
    placeholders = ", ".join("%s" for _ in fields)
    cols = ", ".join(fields)
    values = [[rec.get(f) for f in fields] for rec in records]
    with get_connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                f"INSERT INTO {TABLE_NAME} ({cols}) VALUES ({placeholders})",
                values,
                page_size=100,
            )
    return len(records)


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _normalize_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "nat"}:
        return ""
    return text


def normalize_microarea(value: str) -> str:
    key = _normalize_key(value)
    if "monza" in key and "08" in key:
        return "Monza Brianza 08"
    if "milano" in key and "09" in key:
        return "Milano 09"
    for micro in MICROAREE:
        if _normalize_key(micro) == key:
            return micro
    return ""


def normalize_specializzazione(value: str) -> str:
    key = _normalize_key(value)
    for spec in SPECIALIZZAZIONI:
        if _normalize_key(spec) == key:
            return spec
    # piccoli alias comuni
    aliases = {
        "mmg": "MMG",
        "medicina generale": "MMG",
        "medicina generica": "MMG",
        "gastroenterologia": "Gastroenterogia",
        "chirurgia vascolare": "Vascolare",
    }
    return aliases.get(key, "")


def parse_import_date(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean_cell(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    # pandas può lasciare timestamp stringhe lunghe
    try:
        return pd.to_datetime(text, dayfirst=True).date().isoformat()
    except Exception:
        return None


def suggest_column_mapping(source_columns: list[str]) -> dict[str, str]:
    normalized = {_normalize_key(col): col for col in source_columns}
    mapping: dict[str, str] = {field: NONE_OPTION for field in IMPORT_FIELDS}

    used: set[str] = set()
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            source = normalized.get(_normalize_key(alias))
            if source and source not in used:
                mapping[field] = source
                used.add(source)
                break
    return mapping


def load_uploaded_dataframe(
    uploaded_file: Any,
    sheet_name: Optional[str] = None,
) -> pd.DataFrame:
    name = uploaded_file.name.lower()
    raw = uploaded_file.getvalue()
    if name.endswith(".csv"):
        # prova encoding comuni
        for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=encoding, dtype=str)
            except Exception:
                continue
        return pd.read_csv(io.BytesIO(raw), dtype=str)
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(
            io.BytesIO(raw),
            sheet_name=sheet_name if sheet_name is not None else 0,
            dtype=str,
            engine="openpyxl",
        )
    raise ValueError("Formato non supportato. Usa .xlsx o .csv (esporta da Numbers).")


def list_excel_sheets(uploaded_file: Any) -> list[str]:
    name = uploaded_file.name.lower()
    if not name.endswith((".xlsx", ".xls")):
        return []
    xl = pd.ExcelFile(io.BytesIO(uploaded_file.getvalue()), engine="openpyxl")
    return list(xl.sheet_names)


def apply_mapping_to_row(
    row: pd.Series,
    mapping: dict[str, str],
    defaults: dict[str, str],
) -> dict[str, Any]:
    data: dict[str, Any] = {field: "" for field in IMPORT_FIELDS}

    for field, source_col in mapping.items():
        if source_col == NONE_OPTION or source_col not in row.index:
            continue
        raw = row[source_col]
        if field.startswith("data_"):
            data[field] = parse_import_date(raw)
        elif field == "microarea":
            data[field] = normalize_microarea(clean_cell(raw))
        elif field == "specializzazione":
            data[field] = normalize_specializzazione(clean_cell(raw))
        else:
            data[field] = clean_cell(raw)

    # default solo se campo vuoto
    for field, value in defaults.items():
        if not data.get(field):
            data[field] = value

    # date: stringa vuota -> None
    for field in ("data_ultima_visita", "data_prossimo_appuntamento"):
        if not data.get(field):
            data[field] = None

    return data


def prepare_import_preview(
    df: pd.DataFrame,
    mapping: dict[str, str],
    defaults: dict[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Ritorna (validi, scartati, duplicati)."""
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []

    for idx, row in df.iterrows():
        data = apply_mapping_to_row(row, mapping, defaults)
        row_no = int(idx) + 2  # + header

        if not data["cognome"]:
            rejected.append(
                {
                    "riga": row_no,
                    "motivo": "Manca il cognome",
                    "cognome": data.get("cognome", ""),
                    "nome": data.get("nome", ""),
                }
            )
            continue

        dup_id = find_duplicate_id(
            data["cognome"],
            data["nome"],
            data.get("citta", "") or "",
            data.get("indirizzo", "") or "",
        )
        if dup_id:
            duplicates.append(
                {
                    "riga": row_no,
                    "id_esistente": dup_id,
                    "cognome": data["cognome"],
                    "nome": data["nome"],
                    "citta": data.get("citta", ""),
                }
            )
            continue

        valid.append(data)

    return valid, rejected, duplicates


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------

def collect_form_data(
    prefix: str,
    defaults: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    d = defaults or {}

    c1, c2 = st.columns(2)
    with c1:
        cognome = st.text_input(
            "Cognome *", value=d.get("cognome", ""), key=f"{prefix}_cognome"
        )
        spec_default = d.get("specializzazione") or "MMG"
        spec_idx = (
            SPECIALIZZAZIONI.index(spec_default)
            if spec_default in SPECIALIZZAZIONI
            else SPECIALIZZAZIONI.index("MMG")
        )
        specializzazione = st.selectbox(
            "Specializzazione *",
            options=SPECIALIZZAZIONI,
            index=spec_idx,
            key=f"{prefix}_spec",
        )
        telefono_fisso = st.text_input(
            "Telefono fisso",
            value=d.get("telefono_fisso", "") or "",
            key=f"{prefix}_fisso",
        )
        email = st.text_input(
            "Email",
            value=d.get("email", "") or "",
            key=f"{prefix}_email",
        )
    with c2:
        nome = st.text_input("Nome *", value=d.get("nome", ""), key=f"{prefix}_nome")
        citta = st.text_input(
            "Città *", value=d.get("citta", ""), key=f"{prefix}_citta"
        )
        telefono_cellulare = st.text_input(
            "Telefono cellulare",
            value=d.get("telefono_cellulare", "") or "",
            key=f"{prefix}_cell",
        )
        micro_default = d.get("microarea") or MICROAREE[0]
        micro_idx = (
            MICROAREE.index(micro_default) if micro_default in MICROAREE else 0
        )
        microarea = st.selectbox(
            "Microarea *",
            options=MICROAREE,
            index=micro_idx,
            key=f"{prefix}_micro",
        )

    indirizzo = st.text_input(
        "Indirizzo",
        value=d.get("indirizzo", "") or "",
        placeholder="Via, numero civico…",
        key=f"{prefix}_indirizzo",
    )

    canale_default = d.get("canale_contatto_preferito") or CANALI_CONTATTO[0]
    canale_idx = (
        CANALI_CONTATTO.index(canale_default)
        if canale_default in CANALI_CONTATTO
        else 0
    )
    canale = st.selectbox(
        "Canale di contatto preferito",
        options=CANALI_CONTATTO,
        index=canale_idx,
        key=f"{prefix}_canale",
    )

    st.markdown("##### Orari di visita (lun–ven)")
    orari: dict[str, str] = {}
    cols = st.columns(5)
    for i, giorno in enumerate(GIORNI):
        with cols[i]:
            orari[f"orario_{giorno}"] = st.text_input(
                GIORNI_LABEL[giorno],
                value=d.get(f"orario_{giorno}", "") or "",
                placeholder="es. 9-13",
                key=f"{prefix}_orario_{giorno}",
            )

    d1, d2 = st.columns(2)
    with d1:
        has_ultima = st.checkbox(
            "Imposta data ultima visita",
            value=bool(d.get("data_ultima_visita")),
            key=f"{prefix}_has_ultima",
        )
        data_ultima: Optional[date] = None
        if has_ultima:
            data_ultima = st.date_input(
                "Data ultima visita",
                value=_str_to_date(d.get("data_ultima_visita")) or date.today(),
                format="DD/MM/YYYY",
                key=f"{prefix}_ultima",
            )
    with d2:
        has_prossimo = st.checkbox(
            "Imposta data prossimo appuntamento",
            value=bool(d.get("data_prossimo_appuntamento")),
            key=f"{prefix}_has_prossimo",
        )
        data_prossimo: Optional[date] = None
        if has_prossimo:
            data_prossimo = st.date_input(
                "Data prossimo appuntamento",
                value=_str_to_date(d.get("data_prossimo_appuntamento"))
                or date.today(),
                format="DD/MM/YYYY",
                key=f"{prefix}_prossimo",
            )

    note_prossimo = st.text_area(
        "Note per il prossimo appuntamento",
        value=d.get("note_prossimo_appuntamento", "") or "",
        key=f"{prefix}_note_prossimo",
        height=80,
    )
    note_generali = st.text_area(
        "Note generali",
        value=d.get("note_generali", "") or "",
        key=f"{prefix}_note_gen",
        height=100,
    )

    return {
        "nome": nome.strip(),
        "cognome": cognome.strip(),
        "specializzazione": specializzazione.strip(),
        "citta": citta.strip(),
        "indirizzo": indirizzo.strip(),
        "microarea": microarea,
        "telefono_fisso": telefono_fisso.strip(),
        "telefono_cellulare": telefono_cellulare.strip(),
        "email": email.strip(),
        **orari,
        "data_ultima_visita": _date_to_str(data_ultima),
        "data_prossimo_appuntamento": _date_to_str(data_prossimo),
        "note_prossimo_appuntamento": note_prossimo.strip(),
        "note_generali": note_generali.strip(),
        "canale_contatto_preferito": canale,
    }


def validate_medico(data: dict[str, Any]) -> Optional[str]:
    if not data["nome"]:
        return "Il nome è obbligatorio."
    if not data["cognome"]:
        return "Il cognome è obbligatorio."
    if not data["specializzazione"]:
        return "La specializzazione è obbligatoria."
    if not data["citta"]:
        return "La città è obbligatoria."
    if data["email"] and "@" not in data["email"]:
        return "L'indirizzo email non sembra valido."
    return None


# ---------------------------------------------------------------------------
# Pagine UI
# ---------------------------------------------------------------------------

def page_login() -> None:
    st.markdown("## Doctorale")
    st.caption("Accesso riservato — gestione anagrafica medici")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Accedi", width="stretch")

        if submitted:
            if check_login(username.strip(), password):
                st.session_state.authenticated = True
                st.session_state.username = username.strip()
                st.rerun()
            else:
                st.error("Credenziali non valide.")


def page_elenco() -> None:
    st.subheader("Elenco medici")

    f1, f2, f3 = st.columns([3, 1.2, 1.2])
    with f1:
        query = st.text_input(
            "Ricerca",
            placeholder="Nome, cognome o città…",
            key="search_query",
        )
    with f2:
        spec_filter = st.selectbox(
            "Specializzazione",
            options=["Tutte", *SPECIALIZZAZIONI],
            key="search_spec",
        )
    with f3:
        micro_filter = st.selectbox(
            "Microarea",
            options=["Tutte", *MICROAREE],
            key="search_micro",
        )

    df = search_medici(
        query=query,
        microarea=micro_filter,
        specializzazione=spec_filter,
    )

    st.caption(f"{len(df)} medic{'o' if len(df) == 1 else 'i'} trovati")

    if df.empty:
        st.info("Nessun medico corrisponde ai criteri di ricerca.")
        return

    show_cols = [
        "id",
        "cognome",
        "nome",
        "specializzazione",
        "citta",
        "microarea",
        "telefono_cellulare",
        "email",
        "data_prossimo_appuntamento",
        "canale_contatto_preferito",
    ]
    display = df[show_cols].rename(columns=DISPLAY_LABELS)
    event = st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="elenco_table",
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if not selected_rows:
        st.info("Seleziona una riga nella tabella per vedere i dettagli.")
        return

    medico_id = int(df.iloc[selected_rows[0]]["id"])
    medico = get_medico(medico_id)
    if not medico:
        st.warning("Record non trovato.")
        return

    st.markdown("---")
    st.markdown(
        f"##### Dettaglio — {medico['cognome']} {medico['nome']}"
    )

    with st.expander("Scheda completa", expanded=True):
        # Prefisso legato all'id così i campi si aggiornano al cambio riga
        data = collect_form_data(f"edit_{medico_id}", defaults=medico)

        b1, b2, b3 = st.columns([1, 1, 2])
        with b1:
            if st.button("Salva modifiche", type="primary", width="stretch"):
                err = validate_medico(data)
                if err:
                    st.error(err)
                else:
                    update_medico(medico_id, data)
                    st.success("Medico aggiornato.")
                    st.rerun()
        with b2:
            confirm = st.checkbox(
                "Conferma eliminazione", key=f"confirm_delete_{medico_id}"
            )
            if st.button(
                "Elimina",
                type="secondary",
                width="stretch",
                disabled=not confirm,
            ):
                delete_medico(medico_id)
                st.success("Medico eliminato.")
                st.rerun()


def page_nuovo() -> None:
    st.subheader("Nuovo medico")
    st.caption("I campi contrassegnati con * sono obbligatori.")

    data = collect_form_data("new")

    if st.button("Inserisci", type="primary"):
        err = validate_medico(data)
        if err:
            st.error(err)
        else:
            new_id = insert_medico(data)
            st.success(f"Medico inserito (ID {new_id}).")
            st.balloons()


def page_importa() -> None:
    st.subheader("Importa medici")
    st.caption(
        "Carica un file **Excel (.xlsx)** o **CSV** esportato da Numbers. "
        "Mappa le colonne del file sui campi del database, anteprima e conferma."
    )

    uploaded = st.file_uploader(
        "File da importare",
        type=["xlsx", "csv"],
        help="Da Numbers: File → Esporta in Excel o CSV",
        key="import_uploader",
    )
    if not uploaded:
        st.info("Seleziona un file per iniziare.")
        return

    sheets = list_excel_sheets(uploaded)
    sheet_name: Optional[str] = None
    if sheets:
        sheet_name = st.selectbox("Foglio Excel", options=sheets, key="import_sheet")

    try:
        df_source = load_uploaded_dataframe(uploaded, sheet_name=sheet_name)
    except Exception as exc:
        st.error(f"Impossibile leggere il file: {exc}")
        return

    if df_source.empty:
        st.warning("Il foglio selezionato è vuoto.")
        return

    # Pulisci nomi colonne
    df_source.columns = [str(c).strip() for c in df_source.columns]
    source_cols = list(df_source.columns)

    st.markdown("##### Anteprima file")
    st.caption(f"{len(df_source)} righe · {len(source_cols)} colonne")
    st.dataframe(df_source.head(8), width="stretch", hide_index=True)

    st.markdown("---")
    st.markdown("##### Mapping colonne")

    # Template JSON
    t1, t2 = st.columns(2)
    with t1:
        template_file = st.file_uploader(
            "Carica template mapping (.json)",
            type=["json"],
            key="import_template_upload",
        )
    with t2:
        st.caption("Puoi salvare il mapping corrente come template JSON e riusarlo.")

    file_token = f"{uploaded.name}_{sheet_name or 'csv'}"
    if "import_mapping" not in st.session_state or st.session_state.get(
        "import_mapping_file"
    ) != file_token:
        st.session_state.import_mapping = suggest_column_mapping(source_cols)
        st.session_state.import_mapping_file = file_token
        st.session_state.import_ready = False
        # reset widget keys del mapping precedente
        for field in IMPORT_FIELDS:
            st.session_state.pop(f"map_{file_token}_{field}", None)

    if template_file is not None:
        try:
            loaded = json.loads(template_file.getvalue().decode("utf-8"))
            if isinstance(loaded, dict):
                for field in IMPORT_FIELDS:
                    value = loaded.get(field, NONE_OPTION)
                    if value in source_cols or value == NONE_OPTION:
                        st.session_state.import_mapping[field] = value
                        st.session_state[f"map_{file_token}_{field}"] = value
                st.success("Template mapping applicato.")
        except Exception as exc:
            st.error(f"Template non valido: {exc}")

    mapping: dict[str, str] = {}
    options = [NONE_OPTION, *source_cols]
    cols_ui = st.columns(2)
    for i, field in enumerate(IMPORT_FIELDS):
        with cols_ui[i % 2]:
            current = st.session_state.import_mapping.get(field, NONE_OPTION)
            if current not in options:
                current = NONE_OPTION
            widget_key = f"map_{file_token}_{field}"
            if widget_key not in st.session_state:
                st.session_state[widget_key] = current
            label = DISPLAY_LABELS.get(field, field)
            if field in IMPORT_REQUIRED:
                label = f"{label} *"
            mapping[field] = st.selectbox(
                label,
                options=options,
                key=widget_key,
            )
    st.session_state.import_mapping = mapping

    st.download_button(
        "Scarica template mapping",
        data=json.dumps(mapping, ensure_ascii=False, indent=2).encode("utf-8"),
        file_name="mapping_import_doctorale.json",
        mime="application/json",
    )

    st.markdown("##### Valori di default (se colonna assente o vuota)")
    d1, d2, d3 = st.columns(3)
    with d1:
        default_spec = st.selectbox(
            "Specializzazione di default",
            options=["(nessuna)", *SPECIALIZZAZIONI],
            index=SPECIALIZZAZIONI.index("MMG") + 1,
            key="import_default_spec",
        )
    with d2:
        default_micro = st.selectbox(
            "Microarea di default",
            options=["(nessuna)", *MICROAREE],
            key="import_default_micro",
        )
    with d3:
        default_canale = st.selectbox(
            "Canale preferito di default",
            options=["(nessuna)", *CANALI_CONTATTO],
            key="import_default_canale",
        )

    defaults: dict[str, str] = {}
    if default_spec != "(nessuna)":
        defaults["specializzazione"] = default_spec
    if default_micro != "(nessuna)":
        defaults["microarea"] = default_micro
    if default_canale != "(nessuna)":
        defaults["canale_contatto_preferito"] = default_canale

    if mapping.get("cognome") == NONE_OPTION:
        st.warning("Devi mappare almeno **Cognome**.")
        return

    st.markdown("---")
    if st.button("Analizza file (dry-run)", type="primary", key="import_dry_run"):
        with st.spinner("Analisi in corso…"):
            valid, rejected, duplicates = prepare_import_preview(
                df_source, mapping, defaults
            )
        st.session_state.import_valid = valid
        st.session_state.import_rejected = rejected
        st.session_state.import_duplicates = duplicates
        st.session_state.import_ready = True

    if not st.session_state.get("import_ready"):
        return

    valid = st.session_state.get("import_valid", [])
    rejected = st.session_state.get("import_rejected", [])
    duplicates = st.session_state.get("import_duplicates", [])

    m1, m2, m3 = st.columns(3)
    m1.metric("Da inserire", len(valid))
    m2.metric("Scartati", len(rejected))
    m3.metric("Duplicati (saltati)", len(duplicates))

    if valid:
        st.markdown("###### Anteprima record da inserire")
        preview_cols = [
            "cognome",
            "nome",
            "specializzazione",
            "citta",
            "indirizzo",
            "microarea",
        ]
        st.dataframe(
            pd.DataFrame(valid)[preview_cols].head(20),
            width="stretch",
            hide_index=True,
        )
    if rejected:
        with st.expander(f"Righe scartate ({len(rejected)})"):
            st.dataframe(pd.DataFrame(rejected), width="stretch", hide_index=True)
    if duplicates:
        with st.expander(f"Duplicati già in anagrafica ({len(duplicates)})"):
            st.dataframe(pd.DataFrame(duplicates), width="stretch", hide_index=True)

    if not valid:
        st.info("Nessun record nuovo da inserire.")
        return

    if st.button(
        f"Conferma import di {len(valid)} medici",
        type="primary",
        key="import_confirm",
    ):
        with st.spinner("Import in corso…"):
            count = insert_medici_batch(valid)
        st.success(f"Import completato: {count} medici inseriti.")
        st.balloons()
        st.session_state.import_ready = False
        st.session_state.pop("import_valid", None)
        st.session_state.pop("import_rejected", None)
        st.session_state.pop("import_duplicates", None)


def page_dati_importa() -> None:
    st.subheader("Importa dati di vendita")
    st.caption(
        "Carica l’Excel mensile (fogli unità e fatturato). "
        "La **microarea** è la chiave dei dati: l’informatore viene salvato come "
        "assegnazione storica del periodo (può cambiare nel tempo). "
        "I prodotti possono comparire o sparire di mese in mese."
    )

    uploaded = st.file_uploader(
        "File Excel vendite",
        type=["xlsx"],
        key="sales_uploader",
    )
    if not uploaded:
        st.info("Seleziona un file .xlsx per iniziare.")
        return

    raw = uploaded.getvalue()
    try:
        sheets = list_sheet_names(raw)
    except Exception as exc:
        st.error(f"Impossibile leggere il file: {exc}")
        return

    suggested_unita, suggested_valori = suggest_sheets(sheets)
    anno_file, mese_file = infer_period_from_filename(uploaded.name)
    anno_wb, mese_wb = infer_period_from_workbook(raw)
    anno_default = anno_file or anno_wb or date.today().year
    mese_default = mese_file or mese_wb or date.today().month

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        anno = st.number_input(
            "Anno",
            min_value=2000,
            max_value=2100,
            value=int(anno_default),
            step=1,
            key="sales_anno",
        )
    with c2:
        mese = st.selectbox(
            "Mese",
            options=list(range(1, 13)),
            index=int(mese_default) - 1,
            format_func=lambda m: MESI_LABEL[m],
            key="sales_mese",
        )
    with c3:
        sheet_unita = st.selectbox(
            "Foglio unità",
            options=sheets,
            index=sheets.index(suggested_unita) if suggested_unita in sheets else 0,
            key="sales_sheet_unita",
        )
    with c4:
        sheet_valori = st.selectbox(
            "Foglio fatturato",
            options=sheets,
            index=sheets.index(suggested_valori) if suggested_valori in sheets else 0,
            key="sales_sheet_valori",
        )

    existing = count_sales_for_period(int(anno), int(mese))
    if existing:
        st.warning(
            f"Esistono già **{existing}** record per {MESI_LABEL[int(mese)]} {int(anno)}. "
            "Confermando l’import verranno sostituiti."
        )

    if st.button("Analizza file", type="primary", key="sales_dry_run"):
        try:
            records = parse_sales_workbook(raw, sheet_unita, sheet_valori)
            match_report = build_sales_match_report(records)
            records = apply_sales_entity_mapping(records)
        except Exception as exc:
            st.error(f"Errore parsing: {exc}")
            return
        st.session_state.sales_records = records
        st.session_state.sales_match_report = match_report
        st.session_state.sales_period = (int(anno), int(mese))
        st.session_state.sales_ready = True

    if not st.session_state.get("sales_ready"):
        return

    records = st.session_state.get("sales_records", [])
    period = st.session_state.get("sales_period")
    match_report = st.session_state.get("sales_match_report")
    if not records or not period:
        return

    p_anno, p_mese = period
    st.markdown(
        f"##### Anteprima — {MESI_LABEL[p_mese]} {p_anno} · {len(records)} record"
    )

    preview_df = pd.DataFrame(
        [
            {
                "metrica": r.metrica,
                "livello": r.livello,
                "informatore": r.informatore,
                "microarea": r.microarea,
                "prodotto": r.prodotto,
                "vendita_ap": r.vendita_ap,
                "vendita": r.vendita,
                "pct_italia": r.pct_italia,
                "crescita_pct": r.crescita_pct,
            }
            for r in records
        ]
    )
    g1, g2, g3 = st.columns(3)
    g1.metric("Unità", int((preview_df["metrica"] == "unita").sum()))
    g2.metric("Fatturato", int((preview_df["metrica"] == "fatturato").sum()))
    g3.metric("Microaree (righe)", int((preview_df["livello"] == "microarea").sum()))

    st.markdown("##### Associazione con anagrafica già in DB")
    st.caption(
        "**già presente** = corrispondenza esatta · "
        "**associato** = abbinato a una voce simile già nota · "
        "**nuovo** = non trovato, verrà creato al prossimo import"
    )

    if match_report is None:
        st.info("Rilancia Analizza file per vedere il report di associazione.")
    else:
        tab_inf, tab_prod, tab_micro = st.tabs(
            ["Informatori", "Prodotti", "Microaree"]
        )
        with tab_inf:
            df_inf = match_report["informatori"]
            if df_inf.empty:
                st.info("Nessun informatore nel file.")
            else:
                c_ok = int((df_inf["Stato"] == "già presente").sum())
                c_as = int((df_inf["Stato"] == "associato").sum())
                c_new = int((df_inf["Stato"] == "nuovo").sum())
                st.caption(
                    f"{len(df_inf)} trovati · {c_ok} già presenti · "
                    f"{c_as} associati · {c_new} nuovi"
                )
                st.dataframe(df_inf, width="stretch", hide_index=True)
        with tab_prod:
            df_prod = match_report["prodotti"]
            if df_prod.empty:
                st.info("Nessun prodotto nel file.")
            else:
                c_ok = int((df_prod["Stato"] == "già presente").sum())
                c_as = int((df_prod["Stato"] == "associato").sum())
                c_new = int((df_prod["Stato"] == "nuovo").sum())
                st.caption(
                    f"{len(df_prod)} trovati · {c_ok} già presenti · "
                    f"{c_as} associati · {c_new} nuovi"
                )
                st.dataframe(df_prod, width="stretch", hide_index=True)
        with tab_micro:
            df_micro = match_report["microaree"]
            if df_micro.empty:
                st.info("Nessuna microarea nel file.")
            else:
                c_ok = int((df_micro["Stato"] == "già presente").sum())
                c_as = int((df_micro["Stato"] == "associato").sum())
                c_new = int((df_micro["Stato"] == "nuovo").sum())
                st.caption(
                    f"{len(df_micro)} trovate · {c_ok} già presenti · "
                    f"{c_as} associate · {c_new} nuove"
                )
                st.dataframe(df_micro, width="stretch", hide_index=True)

    with st.expander("Anteprima prime 30 righe dati"):
        st.dataframe(preview_df.head(30), width="stretch", hide_index=True)

    if st.button(
        f"Conferma import ({len(records)} record)",
        type="primary",
        key="sales_confirm",
    ):
        with st.spinner("Import in corso…"):
            # Riapplica mapping al momento del confirm (DB potrebbe essere cambiato)
            mapped = apply_sales_entity_mapping(records)
            delete_sales_period(p_anno, p_mese)
            inserted = insert_sales_records(p_anno, p_mese, mapped)
        st.success(
            f"Import completato: {inserted} record per {MESI_LABEL[p_mese]} {p_anno}."
        )
        st.balloons()
        st.session_state.sales_ready = False
        st.session_state.pop("sales_records", None)
        st.session_state.pop("sales_match_report", None)


def page_dati_consulta() -> None:
    st.subheader("Consulta dati di vendita")
    st.caption(
        "I dati sono organizzati per **microarea**. L’informatore indicato è quello "
        "assegnato in quel mese (storico)."
    )

    filters = list_sales_filter_values()
    if not filters["anni"] or not filters["periodi"]:
        st.info("Nessun dato di vendita presente. Usa Importa per caricare un Excel.")
        return

    periodi = filters["periodi"]
    period_labels = {_period_label(p): p for p in periodi}
    label_list = list(period_labels.keys())

    # ------------------------------------------------------------------
    # Grafico andamento
    # ------------------------------------------------------------------
    st.markdown("##### Andamento nel tempo")

    g1, g2, g3, g4 = st.columns([1.1, 1.3, 1.3, 1.3])
    with g1:
        chart_base = st.selectbox(
            "Base",
            options=["unita", "fatturato"],
            format_func=lambda m: "Unità" if m == "unita" else "Fatturato",
            key="chart_metrica",
        )
    with g2:
        chart_valore = st.selectbox(
            "Valore asse Y",
            options=["vendita", "crescita_pct"],
            format_func=lambda v: (
                "Vendita"
                if v == "vendita"
                else "Crescita % su anno precedente"
            ),
            key="chart_valore",
        )
    with g3:
        periodo_da = st.selectbox(
            "Dal",
            options=label_list,
            index=0,
            key="chart_periodo_da",
        )
    with g4:
        periodo_a = st.selectbox(
            "Al",
            options=label_list,
            index=len(label_list) - 1,
            key="chart_periodo_a",
        )

    g5, g6 = st.columns([1, 2])
    with g5:
        chart_dim = st.radio(
            "Confronta per",
            options=["Informatore", "Microarea"],
            horizontal=True,
            key="chart_dimensione",
        )
    with g6:
        if chart_dim == "Informatore":
            default_inf = (
                ["Stanzione Alessandra"]
                if "Stanzione Alessandra" in filters["informatori"]
                else filters["informatori"][:1]
            )
            chart_entities = st.multiselect(
                "Informatori",
                options=filters["informatori"],
                default=default_inf,
                key="chart_informatori",
            )
        else:
            preferred_micro = ["MILANO 09", "MONZA BRIANZA 08"]
            default_micro = [m for m in preferred_micro if m in filters["microaree"]]
            if not default_micro:
                default_micro = filters["microaree"][:1] if filters["microaree"] else []
            chart_entities = st.multiselect(
                "Microaree",
                options=filters["microaree"],
                default=default_micro,
                key="chart_microaree",
            )

    default_prodotti = (
        ["Totale selezione"]
        if "Totale selezione" in filters["prodotti"]
        else filters["prodotti"][:1]
    )
    chart_prodotti = st.multiselect(
        "Prodotti",
        options=filters["prodotti"],
        default=default_prodotti,
        key="chart_prodotti",
    )

    chart_area = st.empty()

    c_it, c_am, _ = st.columns([1, 1, 2])
    with c_it:
        show_italia = st.checkbox("Mostra Totale Italia", key="chart_show_italia")
    with c_am:
        show_am = st.checkbox("Mostra Totale AM", key="chart_show_am")

    if not chart_entities and not show_italia and not show_am:
        chart_area.info(
            "Seleziona almeno un informatore/microarea, oppure abilita Totale Italia / Totale AM."
        )
    elif not chart_prodotti:
        chart_area.info("Seleziona almeno un prodotto per il grafico.")
    else:
        trend_df = fetch_sales_trend(
            metrica=chart_base,
            valore=chart_valore,
            dimensione=chart_dim,
            entities=chart_entities,
            prodotti=chart_prodotti,
            period_from=period_labels[periodo_da],
            period_to=period_labels[periodo_a],
            include_italia=show_italia,
            include_am=show_am,
        )
        if trend_df.empty:
            chart_area.warning("Nessun dato disponibile per i filtri del grafico.")
        else:
            if chart_valore == "crescita_pct":
                y_title = (
                    "Crescita % unità"
                    if chart_base == "unita"
                    else "Crescita % fatturato"
                )
                y_format = ",.2f"
            else:
                y_title = "Unità" if chart_base == "unita" else "Fatturato (€)"
                y_format = ",.2f"
            chart = (
                alt.Chart(trend_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X(
                        "periodo:T",
                        title="Periodo",
                        axis=alt.Axis(format="%b %Y"),
                    ),
                    y=alt.Y("valore:Q", title=y_title),
                    color=alt.Color(
                        "serie:N",
                        title=None,
                        legend=alt.Legend(
                            orient="right",
                            labelLimit=220,
                            symbolLimit=40,
                            columns=1,
                            labelFontSize=12,
                            symbolSize=80,
                            padding=12,
                            offset=16,
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("periodo:T", title="Periodo", format="%b %Y"),
                        alt.Tooltip("serie_full:N", title="Serie"),
                        alt.Tooltip("valore:Q", title=y_title, format=y_format),
                    ],
                )
                .properties(height=420)
                .configure_view(strokeWidth=0)
                .configure_legend(title=None)
                .interactive()
            )
            chart_area.altair_chart(chart, use_container_width=True)

    st.markdown("---")
    st.markdown("##### Dettaglio tabellare")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        anno = st.selectbox("Anno", options=filters["anni"], key="q_sales_anno")
    with c2:
        mesi_opts = filters["mesi"] or list(range(1, 13))
        mese = st.selectbox(
            "Mese",
            options=mesi_opts,
            format_func=lambda m: MESI_LABEL.get(m, str(m)),
            key="q_sales_mese",
        )
    with c3:
        metrica = st.selectbox(
            "Metrica tabella",
            options=["Tutte", "unita", "fatturato"],
            key="q_sales_metrica",
        )
    with c4:
        livello_opts = ["microarea", "informatore", "am", "italia", "Tutti"]
        livello = st.selectbox(
            "Livello",
            options=livello_opts,
            key="q_sales_livello",
        )

    c5, c6, c7 = st.columns(3)
    with c5:
        microarea = st.selectbox(
            "Microarea",
            options=["Tutte", *filters["microaree"]],
            key="q_sales_micro",
        )
    with c6:
        prodotto = st.selectbox(
            "Prodotto",
            options=["Tutti", *filters["prodotti"]],
            key="q_sales_prodotto",
        )
    with c7:
        informatore = st.selectbox(
            "Informatore (nel periodo)",
            options=["Tutti", *filters["informatori"]],
            key="q_sales_inf",
        )

    df = search_sales(
        anno=int(anno),
        mese=int(mese),
        metrica=metrica,
        livello=livello,
        prodotto=prodotto,
        informatore=None if informatore == "Tutti" else informatore,
        microarea=microarea,
    )

    st.caption(f"{len(df)} record trovati")
    if df.empty:
        st.info("Nessun risultato con i filtri selezionati.")
        return

    show = df[
        [
            "microarea",
            "informatore",
            "prodotto",
            "metrica",
            "livello",
            "vendita_ap",
            "vendita",
            "pct_italia",
            "crescita_pct",
        ]
    ].rename(
        columns={
            "microarea": "Microarea",
            "informatore": "Informatore (periodo)",
            "prodotto": "Prodotto",
            "metrica": "Metrica",
            "livello": "Livello",
            "vendita_ap": "Vendita AP",
            "vendita": "Vendita",
            "pct_italia": "% Italia",
            "crescita_pct": "Crescita %",
        }
    )

    def _style_crescita(value: Any) -> str:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return ""
        if num > 0:
            return "color: #1b7f3a; font-weight: 600"
        if num < 0:
            return "color: #c62828; font-weight: 600"
        return ""

    styled = show.style.map(_style_crescita, subset=["Crescita %"])
    st.dataframe(styled, width="stretch", hide_index=True)

    csv_bytes = show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Scarica CSV",
        data=csv_bytes,
        file_name=f"vendite_{anno}_{int(mese):02d}.csv",
        mime="text/csv",
    )


def page_impostazioni() -> None:
    st.subheader("Impostazioni")

    total = count_medici()
    st.caption(f"Record attualmente in anagrafica: **{total}**")

    st.markdown("---")
    st.markdown("##### Zona pericolosa")
    st.warning(
        "Questa operazione elimina **tutti** i medici dal database in modo permanente. "
        "Non è recuperabile."
    )

    with st.form("reset_db_form"):
        confirm_text = st.text_input(
            'Per confermare digita esattamente: ELIMINA TUTTO',
            key="reset_confirm_text",
        )
        password = st.text_input(
            "Password di login",
            type="password",
            key="reset_password",
        )
        submitted = st.form_submit_button(
            "Svuota database",
            type="primary",
            width="stretch",
        )

        if submitted:
            if confirm_text.strip() != "ELIMINA TUTTO":
                st.error('Testo di conferma non corretto. Digita esattamente: ELIMINA TUTTO')
            elif not verify_password(password):
                st.error("Password di login non valida.")
            else:
                deleted = reset_all_medici()
                st.success(f"Database svuotato. Eliminati {deleted} record.")
                st.session_state.import_ready = False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="Doctorale",
        page_icon="🩺",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    try:
        init_db()
    except Exception as exc:
        st.error(f"Errore di connessione al database: {exc}")
        st.stop()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        # Layout centrato per il login
        _, mid, _ = st.columns([1, 1.2, 1])
        with mid:
            page_login()
        return

    with st.sidebar:
        st.markdown("### Doctorale")
        st.caption(f"Utente: **{st.session_state.get('username', '')}**")

        sezione = st.radio(
            "Sezione",
            options=["Medici", "Dati", "Contabilità", "Impostazioni"],
            key="nav_sezione",
        )

        pagina_medici: Optional[str] = None
        pagina_dati: Optional[str] = None
        pagina_contab: Optional[str] = None
        if sezione == "Medici":
            pagina_medici = st.radio(
                "Funzioni medici",
                options=["Elenco e ricerca", "Nuovo medico", "Importa"],
                key="nav_medici",
            )
        elif sezione == "Dati":
            pagina_dati = st.radio(
                "Funzioni dati",
                options=["Consulta", "Importa"],
                key="nav_dati",
            )
        elif sezione == "Contabilità":
            pagina_contab = st.radio(
                "Funzioni contabilità",
                options=[
                    "Contabilità mensile",
                    "Elenco Fatture",
                    "Elenco pagamenti",
                    "Elenco prelievi",
                    "Aliquote",
                ],
                key="nav_contab",
            )

        st.markdown("---")
        if st.button("Esci", width="stretch"):
            st.session_state.authenticated = False
            st.session_state.pop("username", None)
            st.rerun()

    if sezione == "Impostazioni":
        page_impostazioni()
    elif sezione == "Contabilità":
        if pagina_contab == "Elenco pagamenti":
            page_elenco_pagamenti()
        elif pagina_contab == "Elenco prelievi":
            page_elenco_prelievi()
        elif pagina_contab == "Aliquote":
            page_quote_annuali()
        elif pagina_contab == "Elenco Fatture":
            page_elenco_fatture()
        else:
            page_contabilita_mensile()
    elif sezione == "Dati":
        if pagina_dati == "Importa":
            page_dati_importa()
        else:
            page_dati_consulta()
    elif pagina_medici == "Nuovo medico":
        page_nuovo()
    elif pagina_medici == "Importa":
        page_importa()
    else:
        page_elenco()


if __name__ == "__main__":
    main()
