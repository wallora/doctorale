"""
Sezione Contabilità: fatture, pagamenti tasse, prelievi, quote annuali.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

import pandas as pd
import psycopg2.extras
import streamlit as st

from sales_parser import MESI_LABEL

INVOICE_KIND_LABELS = {
    "advance": "Acconto",
    "balance": "Saldo",
}
INVOICE_KIND_OPTIONS = list(INVOICE_KIND_LABELS.keys())

LINE_TYPE_LABELS = {
    "commissions": "Provvigioni",
    "direct_sale": "Vendita diretta",
    "bonuses": "Premi",
    "enasarco": "Enasarco",
    "advance_deduction": "Deduzione acconto",
    "other": "Altro",
}
LINE_TYPE_OPTIONS = list(LINE_TYPE_LABELS.keys())

TAX_TYPE_LABELS = {
    "inps": "INPS",
    "bankAccount": "Conto corrente",
    "bolloFatture": "Bollo fatture",
    "cameraCommercio": "Camera di commercio",
    "incomeTax": "Imposte sul reddito",
}
TAX_TYPE_OPTIONS = list(TAX_TYPE_LABELS.keys())

QUOTA_FIELDS = [
    ("tax_exempt_rate", "Aliquota esente", "rate"),
    ("inps_discount_rate", "Sconto INPS", "rate"),
    ("inps_rate", "Aliquota INPS", "rate"),
    ("enasarco_rate", "Aliquota Enasarco", "rate"),
    ("enasarco_max", "Massimale Enasarco (€)", "money"),
    ("inps_min_base", "Minimale INPS (€)", "money"),
    ("income_tax_rate", "Aliquota imposte reddito", "rate"),
    ("inps_advance_rate", "Acconto INPS", "rate"),
    ("income_tax_advance_rate", "Acconto imposte reddito", "rate"),
]


def _conn():
    from app import get_connection

    return get_connection()


def _read_df(sql: str, params: Optional[list[Any]] = None) -> pd.DataFrame:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])


def _money(value: Any) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    return float(Decimal(str(value)))


def _fmt_euro(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"€ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{n * 100:.2f} %"


def _month_label(month: int) -> str:
    return MESI_LABEL.get(int(month), str(month))


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def list_distinct_years() -> list[int]:
    years: set[int] = set()
    with _conn() as conn:
        with conn.cursor() as cur:
            for table, col in (
                ("invoices", "reference_year"),
                ("tax_payments", "reference_year"),
                ("withdrawals", "reference_year"),
                ("year_quotas", "year"),
            ):
                cur.execute(f"SELECT DISTINCT {col} FROM {table} ORDER BY 1 DESC")
                years.update(int(r[0]) for r in cur.fetchall())
    return sorted(years, reverse=True)


def list_invoice_year_options() -> dict[str, list[int]]:
    """Anni disponibili per i filtri fatture."""
    out: dict[str, list[int]] = {
        "reference": [],
        "issue": [],
        "payment": [],
    }
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT reference_year
                FROM invoices
                WHERE reference_year IS NOT NULL
                ORDER BY 1 DESC
                """
            )
            out["reference"] = [int(r[0]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT DISTINCT EXTRACT(YEAR FROM issue_date)::INTEGER
                FROM invoices
                WHERE issue_date IS NOT NULL
                ORDER BY 1 DESC
                """
            )
            out["issue"] = [int(r[0]) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT DISTINCT EXTRACT(YEAR FROM payment_date)::INTEGER
                FROM invoices
                WHERE payment_date IS NOT NULL
                ORDER BY 1 DESC
                """
            )
            out["payment"] = [int(r[0]) for r in cur.fetchall()]
    return out


def invoice_lines_total_bounds() -> tuple[float, float]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(MIN(t.total), 0),
                    COALESCE(MAX(t.total), 0)
                FROM (
                    SELECT COALESCE(SUM(l.amount), 0) AS total
                    FROM invoices i
                    LEFT JOIN invoice_lines l ON l.invoice_id = i.id
                    GROUP BY i.id
                ) t
                """
            )
            row = cur.fetchone()
            return float(row[0]), float(row[1])


def list_invoices(
    reference_year: Optional[int] = None,
    issue_year: Optional[int] = None,
    payment_year: Optional[int] = None,
    kind: Optional[str] = None,
    lines_total_min: Optional[float] = None,
    lines_total_max: Optional[float] = None,
) -> pd.DataFrame:
    sql = """
        SELECT
            i.id,
            i.number,
            i.kind,
            i.issue_date,
            i.payment_date,
            i.series_year,
            i.reference_year,
            i.reference_month,
            i.notes,
            i.legacy_amount,
            COALESCE(SUM(l.amount), 0) AS lines_total,
            COUNT(l.id) AS lines_count
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
    """
    where: list[str] = []
    params: list[Any] = []
    if reference_year is not None:
        where.append("i.reference_year = %s")
        params.append(reference_year)
    if issue_year is not None:
        where.append("EXTRACT(YEAR FROM i.issue_date) = %s")
        params.append(issue_year)
    if payment_year is not None:
        where.append("EXTRACT(YEAR FROM i.payment_date) = %s")
        params.append(payment_year)
    if kind is not None:
        where.append("i.kind = %s")
        params.append(kind)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY i.id"

    having: list[str] = []
    if lines_total_min is not None:
        having.append("COALESCE(SUM(l.amount), 0) >= %s")
        params.append(lines_total_min)
    if lines_total_max is not None:
        having.append("COALESCE(SUM(l.amount), 0) <= %s")
        params.append(lines_total_max)
    if having:
        sql += " HAVING " + " AND ".join(having)

    sql += """
        ORDER BY i.reference_year DESC, i.reference_month DESC, i.number ASC
    """
    return _read_df(sql, params or None)



def get_invoice(invoice_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM invoices WHERE id = %s", (invoice_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def list_invoice_lines(invoice_id: int) -> pd.DataFrame:
    sql = """
        SELECT id, line_type, description, amount, sort_order
        FROM invoice_lines
        WHERE invoice_id = %s
        ORDER BY sort_order ASC, id ASC
    """
    return _read_df(sql, [invoice_id])


def update_invoice(invoice_id: int, data: dict[str, Any]) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE invoices SET
                    number = %(number)s,
                    kind = %(kind)s,
                    issue_date = %(issue_date)s,
                    payment_date = %(payment_date)s,
                    series_year = %(series_year)s,
                    reference_year = %(reference_year)s,
                    reference_month = %(reference_month)s,
                    notes = %(notes)s,
                    updated_at = NOW()
                WHERE id = %(id)s
                """,
                {**data, "id": invoice_id},
            )


def replace_invoice_lines(invoice_id: int, lines: list[dict[str, Any]]) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM invoice_lines WHERE invoice_id = %s", (invoice_id,)
            )
            for i, line in enumerate(lines):
                cur.execute(
                    """
                    INSERT INTO invoice_lines (
                        invoice_id, line_type, description, amount, sort_order
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        invoice_id,
                        line["line_type"],
                        line.get("description") or "",
                        line["amount"],
                        int(line.get("sort_order", i)),
                    ),
                )


def list_tax_payments(year: Optional[int] = None) -> pd.DataFrame:
    sql = """
        SELECT id, type, description, payment_date, reference_year, amount, paid
        FROM tax_payments
    """
    params: list[Any] = []
    if year is not None:
        sql += " WHERE reference_year = %s"
        params.append(year)
    sql += " ORDER BY reference_year DESC, payment_date NULLS LAST, id"
    return _read_df(sql, params or None)


def get_tax_payment(payment_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tax_payments WHERE id = %s", (payment_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_tax_payment(data: dict[str, Any], payment_id: Optional[int] = None) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            if payment_id is None:
                cur.execute(
                    """
                    INSERT INTO tax_payments (
                        type, description, payment_date, reference_year,
                        amount, paid, updated_at
                    ) VALUES (
                        %(type)s, %(description)s, %(payment_date)s, %(reference_year)s,
                        %(amount)s, %(paid)s, NOW()
                    )
                    RETURNING id
                    """,
                    data,
                )
                return int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE tax_payments SET
                    type = %(type)s,
                    description = %(description)s,
                    payment_date = %(payment_date)s,
                    reference_year = %(reference_year)s,
                    amount = %(amount)s,
                    paid = %(paid)s,
                    updated_at = NOW()
                WHERE id = %(id)s
                """,
                {**data, "id": payment_id},
            )
            return payment_id


def delete_tax_payment(payment_id: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tax_payments WHERE id = %s", (payment_id,))


def list_withdrawals(year: Optional[int] = None) -> pd.DataFrame:
    sql = """
        SELECT id, withdrawal_date, reference_year, reference_month,
               amount, description
        FROM withdrawals
    """
    params: list[Any] = []
    if year is not None:
        sql += " WHERE reference_year = %s"
        params.append(year)
    sql += " ORDER BY reference_year DESC, reference_month DESC, id"
    return _read_df(sql, params or None)


def get_withdrawal(withdrawal_id: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM withdrawals WHERE id = %s", (withdrawal_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_withdrawal(
    data: dict[str, Any], withdrawal_id: Optional[int] = None
) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            if withdrawal_id is None:
                cur.execute(
                    """
                    INSERT INTO withdrawals (
                        withdrawal_date, reference_year, reference_month,
                        amount, description, updated_at
                    ) VALUES (
                        %(withdrawal_date)s, %(reference_year)s, %(reference_month)s,
                        %(amount)s, %(description)s, NOW()
                    )
                    RETURNING id
                    """,
                    data,
                )
                return int(cur.fetchone()[0])
            cur.execute(
                """
                UPDATE withdrawals SET
                    withdrawal_date = %(withdrawal_date)s,
                    reference_year = %(reference_year)s,
                    reference_month = %(reference_month)s,
                    amount = %(amount)s,
                    description = %(description)s,
                    updated_at = NOW()
                WHERE id = %(id)s
                """,
                {**data, "id": withdrawal_id},
            )
            return withdrawal_id


def delete_withdrawal(withdrawal_id: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM withdrawals WHERE id = %s", (withdrawal_id,))


def list_year_quotas() -> pd.DataFrame:
    sql = """
        SELECT year, tax_exempt_rate, inps_discount_rate, inps_rate,
               enasarco_rate, enasarco_max, inps_min_base, income_tax_rate,
               inps_advance_rate, income_tax_advance_rate
        FROM year_quotas
        ORDER BY year DESC
    """
    return _read_df(sql)


def get_year_quota(year: int) -> Optional[dict[str, Any]]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM year_quotas WHERE year = %s", (year,))
            row = cur.fetchone()
            return dict(row) if row else None


def upsert_year_quota(year: int, data: dict[str, Any]) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO year_quotas (
                    year, tax_exempt_rate, inps_discount_rate, inps_rate,
                    enasarco_rate, enasarco_max, inps_min_base, income_tax_rate,
                    inps_advance_rate, income_tax_advance_rate, updated_at
                ) VALUES (
                    %(year)s, %(tax_exempt_rate)s, %(inps_discount_rate)s, %(inps_rate)s,
                    %(enasarco_rate)s, %(enasarco_max)s, %(inps_min_base)s, %(income_tax_rate)s,
                    %(inps_advance_rate)s, %(income_tax_advance_rate)s, NOW()
                )
                ON CONFLICT (year) DO UPDATE SET
                    tax_exempt_rate = EXCLUDED.tax_exempt_rate,
                    inps_discount_rate = EXCLUDED.inps_discount_rate,
                    inps_rate = EXCLUDED.inps_rate,
                    enasarco_rate = EXCLUDED.enasarco_rate,
                    enasarco_max = EXCLUDED.enasarco_max,
                    inps_min_base = EXCLUDED.inps_min_base,
                    income_tax_rate = EXCLUDED.income_tax_rate,
                    inps_advance_rate = EXCLUDED.inps_advance_rate,
                    income_tax_advance_rate = EXCLUDED.income_tax_advance_rate,
                    updated_at = NOW()
                """,
                {**data, "year": year},
            )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _year_filter(key: str, include_all: bool = True) -> Optional[int]:
    years = list_distinct_years()
    if not years:
        years = [date.today().year]
    options = (["Tutti"] + [str(y) for y in years]) if include_all else [str(y) for y in years]
    choice = st.selectbox("Anno", options=options, key=key)
    if choice == "Tutti":
        return None
    return int(choice)


def _optional_year_select(
    label: str, years: list[int], key: str
) -> Optional[int]:
    options = ["Tutti"] + [str(y) for y in years] if years else ["Tutti"]
    choice = st.selectbox(label, options=options, key=key)
    if choice == "Tutti":
        return None
    return int(choice)


def page_elenco_fatture() -> None:
    st.subheader("Elenco fatture")

    year_opts = list_invoice_year_options()
    total_lo, total_hi = invoice_lines_total_bounds()
    # Arrotonda i bound del range per lo slider
    slider_min = float(int(total_lo) - (0 if total_lo >= 0 else 1))
    slider_max = float(int(total_hi) + 1)
    if slider_max <= slider_min:
        slider_max = slider_min + 1.0

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        reference_year = _optional_year_select(
            "Anno competenza", year_opts["reference"], "fin_inv_ref_year"
        )
    with f2:
        issue_year = _optional_year_select(
            "Anno emissione", year_opts["issue"], "fin_inv_issue_year"
        )
    with f3:
        payment_year = _optional_year_select(
            "Anno pagamento", year_opts["payment"], "fin_inv_pay_year"
        )
    with f4:
        kind_label = st.selectbox(
            "Tipo",
            options=["Tutti", "Acconto", "Saldo"],
            key="fin_inv_kind",
        )
    kind = None
    if kind_label == "Acconto":
        kind = "advance"
    elif kind_label == "Saldo":
        kind = "balance"

    amount_range = st.slider(
        "Totale righe (€)",
        min_value=slider_min,
        max_value=slider_max,
        value=(slider_min, slider_max),
        step=1.0,
        key="fin_inv_amount_range",
    )
    lines_min, lines_max = float(amount_range[0]), float(amount_range[1])
    # Se lo slider è al massimo range, non filtrare (evita tagli per arrotondamenti)
    use_min = lines_min > slider_min + 1e-9
    use_max = lines_max < slider_max - 1e-9

    df = list_invoices(
        reference_year=reference_year,
        issue_year=issue_year,
        payment_year=payment_year,
        kind=kind,
        lines_total_min=lines_min if use_min else None,
        lines_total_max=lines_max if use_max else None,
    )
    st.caption(f"{len(df)} fattur{'a' if len(df) == 1 else 'e'}")

    if df.empty:
        st.info("Nessuna fattura corrisponde ai filtri.")
        return

    display = pd.DataFrame(
        {
            "N.": df.apply(
                lambda r: f"{int(r['series_year'])}-{int(r['number']):02d}"
                if "series_year" in df.columns and pd.notna(r.get("series_year"))
                else f"{int(r['number']):02d}",
                axis=1,
            ),
            "Tipo": df["kind"].map(INVOICE_KIND_LABELS),
            "Competenza": df.apply(
                lambda r: date(
                    int(r["reference_year"]), int(r["reference_month"]), 1
                ),
                axis=1,
            ),
            "Emissione": df["issue_date"],
            "Pagamento": df["payment_date"],
            "Legacy": pd.to_numeric(df["legacy_amount"], errors="coerce"),
            "Totale righe": pd.to_numeric(df["lines_total"], errors="coerce"),
            "Righe": df["lines_count"].astype(int),
        }
    )
    event = st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Competenza": st.column_config.DateColumn(
                "Competenza",
                format="MMM YYYY",
            ),
            "Emissione": st.column_config.DateColumn("Emissione"),
            "Pagamento": st.column_config.DateColumn("Pagamento"),
            "Legacy": st.column_config.NumberColumn(
                "Legacy",
                format="€ %.2f",
            ),
            "Totale righe": st.column_config.NumberColumn(
                "Totale righe",
                format="€ %.2f",
            ),
        },
        key="fin_inv_table",
    )

    selected = event.selection.rows if event and event.selection else []
    if not selected:
        st.info("Seleziona una fattura per vedere il dettaglio e le voci.")
        return

    invoice_id = int(df.iloc[selected[0]]["id"])
    inv = get_invoice(invoice_id)
    if not inv:
        st.warning("Fattura non trovata.")
        return

    st.markdown("---")
    st.markdown(
        f"##### Fattura {inv.get('series_year', inv['reference_year'])}-{int(inv['number']):02d} · "
        f"{INVOICE_KIND_LABELS.get(inv['kind'], inv['kind'])} · "
        f"{_month_label(inv['reference_month'])} {inv['reference_year']}"
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        series_year = st.number_input(
            "Anno numerazione",
            min_value=2000,
            max_value=2100,
            step=1,
            value=int(inv.get("series_year") or inv["reference_year"]),
            key=f"inv_series_{invoice_id}",
        )
        number = st.number_input(
            "Numero",
            min_value=1,
            step=1,
            value=int(inv["number"]),
            key=f"inv_num_{invoice_id}",
        )
    with c2:
        kind = st.selectbox(
            "Tipo",
            options=INVOICE_KIND_OPTIONS,
            format_func=lambda k: INVOICE_KIND_LABELS[k],
            index=INVOICE_KIND_OPTIONS.index(inv["kind"])
            if inv["kind"] in INVOICE_KIND_OPTIONS
            else 0,
            key=f"inv_kind_{invoice_id}",
        )
    with c3:
        ref_year = st.number_input(
            "Anno competenza",
            min_value=2000,
            max_value=2100,
            step=1,
            value=int(inv["reference_year"]),
            key=f"inv_ry_{invoice_id}",
        )
    with c4:
        ref_month = st.selectbox(
            "Mese competenza",
            options=list(range(1, 13)),
            format_func=_month_label,
            index=int(inv["reference_month"]) - 1,
            key=f"inv_rm_{invoice_id}",
        )

    d1, d2 = st.columns(2)
    with d1:
        issue_date = st.date_input(
            "Data emissione",
            value=inv["issue_date"] or date.today(),
            key=f"inv_issue_{invoice_id}",
        )
    with d2:
        payment_date = st.date_input(
            "Data pagamento",
            value=inv["payment_date"] or date.today(),
            key=f"inv_pay_{invoice_id}",
        )

    notes = st.text_area(
        "Note",
        value=inv.get("notes") or "",
        key=f"inv_notes_{invoice_id}",
    )

    legacy = inv.get("legacy_amount")
    if legacy is not None:
        st.caption(f"Importo legacy (Firebase, lordo enasarco): **{_fmt_euro(legacy)}**")

    if st.button("Salva testata", type="primary", key=f"inv_save_h_{invoice_id}"):
        update_invoice(
            invoice_id,
            {
                "number": int(number),
                "kind": kind,
                "issue_date": issue_date,
                "payment_date": payment_date,
                "series_year": int(series_year),
                "reference_year": int(ref_year),
                "reference_month": int(ref_month),
                "notes": notes or "",
            },
        )
        st.success("Testata aggiornata.")
        st.rerun()

    st.markdown("##### Voci di dettaglio")
    st.caption(
        "Ricavi positivi (provvigioni, vendita diretta, premi); "
        "enasarco e deduzione acconto di solito negativi."
    )

    lines_df = list_invoice_lines(invoice_id)
    edit_df = lines_df.copy()
    if edit_df.empty:
        edit_df = pd.DataFrame(
            {
                "line_type": pd.Series(dtype="object"),
                "description": pd.Series(dtype="object"),
                "amount": pd.Series(dtype="float"),
            }
        )
    else:
        edit_df = edit_df[["line_type", "description", "amount"]]

    edited = st.data_editor(
        edit_df,
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        column_config={
            "line_type": st.column_config.SelectboxColumn(
                "Tipo voce",
                options=LINE_TYPE_OPTIONS,
                required=True,
                format_func=lambda k: LINE_TYPE_LABELS.get(k, k),
            ),
            "description": st.column_config.TextColumn("Descrizione"),
            "amount": st.column_config.NumberColumn(
                "Importo (€)",
                format="%.2f",
                required=True,
            ),
        },
        key=f"inv_lines_editor_{invoice_id}",
    )

    if not edited.empty and "amount" in edited.columns:
        total = float(pd.to_numeric(edited["amount"], errors="coerce").fillna(0).sum())
        st.caption(f"Totale voci: **{_fmt_euro(total)}**")

    if st.button("Salva voci", type="primary", key=f"inv_save_l_{invoice_id}"):
        lines: list[dict[str, Any]] = []
        for i, row in edited.iterrows():
            lt = row.get("line_type")
            if not lt or (isinstance(lt, float) and pd.isna(lt)):
                continue
            if lt not in LINE_TYPE_LABELS:
                st.error(f"Tipo voce non valido: {lt}")
                return
            amt = row.get("amount")
            if amt is None or (isinstance(amt, float) and pd.isna(amt)):
                st.error("Ogni voce deve avere un importo.")
                return
            desc = row.get("description")
            if desc is None or (isinstance(desc, float) and pd.isna(desc)):
                desc = ""
            lines.append(
                {
                    "line_type": str(lt),
                    "description": str(desc),
                    "amount": _money(amt),
                    "sort_order": int(i) if isinstance(i, int) else len(lines),
                }
            )
        replace_invoice_lines(invoice_id, lines)
        st.success(f"Salvate {len(lines)} voci.")
        st.rerun()


def page_elenco_pagamenti() -> None:
    st.subheader("Elenco pagamenti")

    year = _year_filter("fin_tax_year")
    df = list_tax_payments(year)
    st.caption(f"{len(df)} pagament{'o' if len(df) == 1 else 'i'}")

    if not df.empty:
        display = pd.DataFrame(
            {
                "Tipo": df["type"].map(TAX_TYPE_LABELS),
                "Descrizione": df["description"],
                "Anno": df["reference_year"].astype(int),
                "Data": df["payment_date"],
                "Importo": df["amount"].map(_fmt_euro),
                "Pagato": df["paid"].map(lambda x: "Sì" if x else "No"),
            }
        )
        event = st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="fin_tax_table",
        )
        selected = event.selection.rows if event and event.selection else []
    else:
        st.info("Nessun pagamento trovato.")
        selected = []

    st.markdown("---")
    payment: Optional[dict[str, Any]] = None
    payment_id: Optional[int] = None
    if selected:
        payment_id = int(df.iloc[selected[0]]["id"])
        payment = get_tax_payment(payment_id)
        st.markdown("##### Modifica pagamento")
    else:
        st.markdown("##### Nuovo pagamento")

    defaults = payment or {
        "type": "inps",
        "description": "",
        "payment_date": date.today(),
        "reference_year": year or date.today().year,
        "amount": 0.0,
        "paid": False,
    }

    c1, c2, c3 = st.columns(3)
    with c1:
        tax_type = st.selectbox(
            "Tipo",
            options=TAX_TYPE_OPTIONS,
            format_func=lambda k: TAX_TYPE_LABELS[k],
            index=TAX_TYPE_OPTIONS.index(defaults["type"])
            if defaults.get("type") in TAX_TYPE_OPTIONS
            else 0,
            key=f"tax_type_{payment_id or 'new'}",
        )
    with c2:
        ref_year = st.number_input(
            "Anno competenza",
            min_value=2000,
            max_value=2100,
            value=int(defaults["reference_year"]),
            key=f"tax_year_{payment_id or 'new'}",
        )
    with c3:
        payment_date = st.date_input(
            "Data pagamento",
            value=defaults.get("payment_date") or date.today(),
            key=f"tax_date_{payment_id or 'new'}",
        )

    description = st.text_input(
        "Descrizione",
        value=defaults.get("description") or "",
        key=f"tax_desc_{payment_id or 'new'}",
    )
    a1, a2 = st.columns(2)
    with a1:
        amount = st.number_input(
            "Importo (€)",
            value=_money(defaults.get("amount")),
            step=0.01,
            format="%.2f",
            key=f"tax_amt_{payment_id or 'new'}",
        )
    with a2:
        paid = st.checkbox(
            "Pagato",
            value=bool(defaults.get("paid")),
            key=f"tax_paid_{payment_id or 'new'}",
        )

    b1, b2, _ = st.columns([1, 1, 2])
    with b1:
        if st.button("Salva", type="primary", key=f"tax_save_{payment_id or 'new'}"):
            upsert_tax_payment(
                {
                    "type": tax_type,
                    "description": description or "",
                    "payment_date": payment_date,
                    "reference_year": int(ref_year),
                    "amount": float(amount),
                    "paid": bool(paid),
                },
                payment_id=payment_id,
            )
            st.success("Pagamento salvato.")
            st.rerun()
    with b2:
        if payment_id is not None:
            confirm = st.checkbox(
                "Conferma eliminazione", key=f"tax_del_confirm_{payment_id}"
            )
            if st.button(
                "Elimina",
                disabled=not confirm,
                key=f"tax_del_{payment_id}",
            ):
                delete_tax_payment(payment_id)
                st.success("Pagamento eliminato.")
                st.rerun()


def page_elenco_prelievi() -> None:
    st.subheader("Elenco prelievi")

    year = _year_filter("fin_wd_year")
    df = list_withdrawals(year)
    st.caption(f"{len(df)} preliev{'o' if len(df) == 1 else 'i'}")

    if not df.empty:
        display = pd.DataFrame(
            {
                "Anno": df["reference_year"].astype(int),
                "Mese": df["reference_month"].map(_month_label),
                "Data": df["withdrawal_date"],
                "Importo": df["amount"].map(_fmt_euro),
                "Descrizione": df["description"],
            }
        )
        event = st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="fin_wd_table",
        )
        selected = event.selection.rows if event and event.selection else []
    else:
        st.info("Nessun prelievo trovato.")
        selected = []

    st.markdown("---")
    withdrawal: Optional[dict[str, Any]] = None
    withdrawal_id: Optional[int] = None
    if selected:
        withdrawal_id = int(df.iloc[selected[0]]["id"])
        withdrawal = get_withdrawal(withdrawal_id)
        st.markdown("##### Modifica prelievo")
    else:
        st.markdown("##### Nuovo prelievo")

    defaults = withdrawal or {
        "withdrawal_date": date.today(),
        "reference_year": year or date.today().year,
        "reference_month": date.today().month,
        "amount": 0.0,
        "description": "",
    }

    c1, c2, c3 = st.columns(3)
    with c1:
        ref_year = st.number_input(
            "Anno",
            min_value=2000,
            max_value=2100,
            value=int(defaults["reference_year"]),
            key=f"wd_year_{withdrawal_id or 'new'}",
        )
    with c2:
        ref_month = st.selectbox(
            "Mese",
            options=list(range(1, 13)),
            format_func=_month_label,
            index=int(defaults["reference_month"]) - 1,
            key=f"wd_month_{withdrawal_id or 'new'}",
        )
    with c3:
        withdrawal_date = st.date_input(
            "Data prelievo",
            value=defaults.get("withdrawal_date") or date.today(),
            key=f"wd_date_{withdrawal_id or 'new'}",
        )

    amount = st.number_input(
        "Importo (€)",
        value=_money(defaults.get("amount")),
        step=0.01,
        format="%.2f",
        key=f"wd_amt_{withdrawal_id or 'new'}",
    )
    description = st.text_input(
        "Descrizione",
        value=defaults.get("description") or "",
        key=f"wd_desc_{withdrawal_id or 'new'}",
    )

    b1, b2, _ = st.columns([1, 1, 2])
    with b1:
        if st.button("Salva", type="primary", key=f"wd_save_{withdrawal_id or 'new'}"):
            upsert_withdrawal(
                {
                    "withdrawal_date": withdrawal_date,
                    "reference_year": int(ref_year),
                    "reference_month": int(ref_month),
                    "amount": float(amount),
                    "description": description or "",
                },
                withdrawal_id=withdrawal_id,
            )
            st.success("Prelievo salvato.")
            st.rerun()
    with b2:
        if withdrawal_id is not None:
            confirm = st.checkbox(
                "Conferma eliminazione", key=f"wd_del_confirm_{withdrawal_id}"
            )
            if st.button(
                "Elimina",
                disabled=not confirm,
                key=f"wd_del_{withdrawal_id}",
            ):
                delete_withdrawal(withdrawal_id)
                st.success("Prelievo eliminato.")
                st.rerun()


def page_quote_annuali() -> None:
    st.subheader("Quote annuali")

    df = list_year_quotas()
    if df.empty:
        st.info("Nessuna quota annuale. Creane una nuova qui sotto.")
        selected_year = None
    else:
        display = pd.DataFrame(
            {
                "Anno": df["year"].astype(int),
                "Esente": df["tax_exempt_rate"].map(_fmt_pct),
                "INPS": df["inps_rate"].map(_fmt_pct),
                "Sconto INPS": df["inps_discount_rate"].map(_fmt_pct),
                "Enasarco": df["enasarco_rate"].map(_fmt_pct),
                "Max Enasarco": df["enasarco_max"].map(_fmt_euro),
                "Imposte": df["income_tax_rate"].map(_fmt_pct),
            }
        )
        event = st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="fin_quota_table",
        )
        selected = event.selection.rows if event and event.selection else []
        selected_year = int(df.iloc[selected[0]]["year"]) if selected else None

    st.markdown("---")
    if selected_year is not None:
        st.markdown(f"##### Modifica quote {selected_year}")
        quota = get_year_quota(selected_year) or {}
        year_value = selected_year
        year_locked = True
    else:
        st.markdown("##### Nuova / aggiorna quota annuale")
        quota = {}
        year_value = date.today().year
        year_locked = False

    if year_locked:
        st.caption(f"Anno: **{year_value}**")
        year_input = year_value
    else:
        year_input = st.number_input(
            "Anno",
            min_value=2000,
            max_value=2100,
            value=year_value,
            key="quota_year_new",
        )

    values: dict[str, Any] = {}
    cols = st.columns(2)
    for i, (field, label, kind) in enumerate(QUOTA_FIELDS):
        with cols[i % 2]:
            raw = quota.get(field)
            if kind == "rate":
                # UI in percentuale per comodità
                pct_default = float(raw) * 100 if raw is not None else 0.0
                pct = st.number_input(
                    label + " (%)",
                    value=pct_default,
                    step=0.01,
                    format="%.4f",
                    key=f"quota_{year_input}_{field}",
                )
                values[field] = float(pct) / 100.0
            else:
                values[field] = st.number_input(
                    label,
                    value=_money(raw) if raw is not None else 0.0,
                    step=0.01,
                    format="%.2f",
                    key=f"quota_{year_input}_{field}",
                )

    if st.button("Salva quote", type="primary", key=f"quota_save_{year_input}"):
        upsert_year_quota(int(year_input), values)
        st.success(f"Quote {int(year_input)} salvate.")
        st.rerun()
