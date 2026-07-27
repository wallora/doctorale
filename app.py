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
    """Verifica che la tabella esista (creata da SQL Editor su Supabase)."""
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
) -> pd.DataFrame:
    sql = f"SELECT * FROM {TABLE_NAME} WHERE 1=1"
    params: list[Any] = []

    if query.strip():
        like = f"%{query.strip()}%"
        sql += """
            AND (
                nome ILIKE %s
                OR cognome ILIKE %s
                OR specializzazione ILIKE %s
                OR citta ILIKE %s
            )
        """
        params.extend([like, like, like, like])

    if microarea and microarea != "Tutte":
        sql += " AND microarea = %s"
        params.append(microarea)

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

    f1, f2 = st.columns([3, 1])
    with f1:
        query = st.text_input(
            "Cerca",
            placeholder="Nome, cognome, specializzazione o città…",
            label_visibility="collapsed",
            key="search_query",
        )
    with f2:
        micro_filter = st.selectbox(
            "Microarea",
            options=["Tutte", *MICROAREE],
            label_visibility="collapsed",
            key="search_micro",
        )

    df = search_medici(query=query, microarea=micro_filter)

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
        pagina = st.radio(
            "Menu",
            options=["Elenco e ricerca", "Nuovo medico", "Importa"],
            label_visibility="collapsed",
        )
        st.markdown("---")
        if st.button("Esci", width="stretch"):
            st.session_state.authenticated = False
            st.session_state.pop("username", None)
            st.rerun()

    if pagina == "Elenco e ricerca":
        page_elenco()
    elif pagina == "Nuovo medico":
        page_nuovo()
    else:
        page_importa()


if __name__ == "__main__":
    main()
