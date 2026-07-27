"""
Doctorale — gestione anagrafica medici (uso personale).
Login con credenziali hashate (PBKDF2) + CRUD su Supabase (PostgreSQL).
"""

from __future__ import annotations

import base64
import hashlib
import os
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

# Credenziali: username in chiaro, password hashata con PBKDF2-HMAC-SHA256.
# Hash generato con: salt = b"doctorale_salt_v1", iterations = 260000
# Password di default: admin123  →  cambia HASH e USERNAME prima di un uso reale.
AUTH_USERNAME = "admin"
AUTH_PASSWORD_SALT = b"doctorale_salt_v1"
AUTH_PASSWORD_HASH = "TNBQkbqxlofbt8MPQi+UZBitspP8OiI1m+0e0L7xxXA="
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


# ---------------------------------------------------------------------------
# Autenticazione
# ---------------------------------------------------------------------------

def verify_password(password: str) -> bool:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        AUTH_PASSWORD_SALT,
        AUTH_PBKDF2_ITERATIONS,
    )
    return base64.b64encode(dk).decode("ascii") == AUTH_PASSWORD_HASH


def check_login(username: str, password: str) -> bool:
    return username == AUTH_USERNAME and verify_password(password)


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
            options=["Elenco e ricerca", "Nuovo medico"],
            label_visibility="collapsed",
        )
        st.markdown("---")
        if st.button("Esci", width="stretch"):
            st.session_state.authenticated = False
            st.session_state.pop("username", None)
            st.rerun()

    if pagina == "Elenco e ricerca":
        page_elenco()
    else:
        page_nuovo()


if __name__ == "__main__":
    main()
