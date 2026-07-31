"""
Sezione Contabilità: fatture, pagamenti tasse, prelievi, quote annuali.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

import altair as alt
import pandas as pd
import psycopg2.extras
import streamlit as st

from sales_parser import MESI_LABEL
from finance_calc import (
    CERTAIN_RESERVE_MONTHS,
    Quotas,
    applies_fixed_inps,
    as_of_month,
    attribution_sort_date,
    attribution_year,
    attribution_year_month,
    fixed_inps_month,
    fixed_inps_months_in_year,
    inps_minimale_annuo,
    invoice_reserve,
    iter_year_months,
)

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
    "strumenti": "Strumenti",
}
TAX_TYPE_OPTIONS = list(TAX_TYPE_LABELS.keys())

QUOTA_FIELDS = [
    ("imponibile_rate", "Imponibile sul fatturato", "rate"),
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


def list_collection_periods() -> list[tuple[int, int]]:
    """Mesi con incassi (payment_date) e/o prelievi di riferimento."""
    sql = """
        SELECT y, m FROM (
            SELECT
                EXTRACT(YEAR FROM payment_date)::INTEGER AS y,
                EXTRACT(MONTH FROM payment_date)::INTEGER AS m
            FROM invoices
            WHERE payment_date IS NOT NULL
            UNION
            SELECT reference_year AS y, reference_month AS m
            FROM withdrawals
            WHERE reference_year IS NOT NULL AND reference_month IS NOT NULL
        ) t
        WHERE y IS NOT NULL AND m IS NOT NULL
        ORDER BY y DESC, m DESC
    """
    df = _read_df(sql)
    if df.empty:
        return []
    return [(int(r.y), int(r.m)) for r in df.itertuples(index=False)]


def list_invoices_for_collection(year: int, month: int) -> pd.DataFrame:
    """Fatture incassate nel mese (payment_date)."""
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
            COALESCE(SUM(l.amount), 0) AS lines_total,
            COALESCE(
                SUM(l.amount) FILTER (
                    WHERE l.line_type IS DISTINCT FROM 'enasarco'
                ),
                0
            ) AS lordo_senza_enasarco
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.payment_date IS NOT NULL
          AND EXTRACT(YEAR FROM i.payment_date) = %s
          AND EXTRACT(MONTH FROM i.payment_date) = %s
        GROUP BY i.id
        ORDER BY i.payment_date ASC, i.number ASC
    """
    return _read_df(sql, [year, month])


def list_invoices_lordo_all() -> pd.DataFrame:
    """Tutte le fatture con lordo (senza enasarco) per YTD / totale prelevabile."""
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
            COALESCE(
                SUM(l.amount) FILTER (
                    WHERE l.line_type IS DISTINCT FROM 'enasarco'
                ),
                0
            ) AS lordo
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        GROUP BY i.id
        ORDER BY
            COALESCE(i.payment_date, i.issue_date) ASC NULLS LAST,
            i.id ASC
    """
    return _read_df(sql)


def enasarco_by_collection_month() -> dict[tuple[int, int], float]:
    df = _read_df(
        """
        SELECT
            EXTRACT(YEAR FROM i.payment_date)::INTEGER AS y,
            EXTRACT(MONTH FROM i.payment_date)::INTEGER AS m,
            COALESCE(SUM(l.amount), 0) AS enasarco
        FROM invoices i
        JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.kind = 'balance'
          AND l.line_type = 'enasarco'
          AND i.payment_date IS NOT NULL
        GROUP BY 1, 2
        """
    )
    if df.empty:
        return {}
    return {
        (int(r.y), int(r.m)): float(r.enasarco)
        for r in df.itertuples(index=False)
        if r.y is not None and r.m is not None
    }


def withdrawals_by_month() -> dict[tuple[int, int], float]:
    """Prelievi aggregati per mese di riferimento (come in Contabilità mensile)."""
    df = _read_df(
        """
        SELECT
            reference_year,
            reference_month,
            COALESCE(SUM(amount), 0) AS prelievi
        FROM withdrawals
        GROUP BY reference_year, reference_month
        """
    )
    if df.empty:
        return {}
    return {
        (int(r.reference_year), int(r.reference_month)): float(r.prelievi)
        for r in df.itertuples(index=False)
    }


def fatturato_by_competence_month() -> dict[tuple[int, int], float]:
    """Lordo senza enasarco per mese di competenza (reference_year/month)."""
    df = _read_df(
        """
        SELECT
            i.reference_year AS y,
            i.reference_month AS m,
            COALESCE(
                SUM(l.amount) FILTER (
                    WHERE l.line_type IS DISTINCT FROM 'enasarco'
                ),
                0
            ) AS fatturato
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.reference_year IS NOT NULL
          AND i.reference_month IS NOT NULL
        GROUP BY 1, 2
        """
    )
    if df.empty:
        return {}
    return {
        (int(r.y), int(r.m)): float(r.fatturato)
        for r in df.itertuples(index=False)
        if r.y is not None and r.m is not None
    }


def fatturato_by_collection_month() -> dict[tuple[int, int], float]:
    """Lordo senza enasarco per mese di incasso (payment_date)."""
    df = _read_df(
        """
        SELECT
            EXTRACT(YEAR FROM i.payment_date)::INTEGER AS y,
            EXTRACT(MONTH FROM i.payment_date)::INTEGER AS m,
            COALESCE(
                SUM(l.amount) FILTER (
                    WHERE l.line_type IS DISTINCT FROM 'enasarco'
                ),
                0
            ) AS fatturato
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.payment_date IS NOT NULL
        GROUP BY 1, 2
        """
    )
    if df.empty:
        return {}
    return {
        (int(r.y), int(r.m)): float(r.fatturato)
        for r in df.itertuples(index=False)
        if r.y is not None and r.m is not None
    }


def list_annual_chart_years() -> list[int]:
    """Anni disponibili per il grafico annuale."""
    years: set[int] = set()
    for mapping in (
        fatturato_by_competence_month(),
        fatturato_by_collection_month(),
        enasarco_by_collection_month(),
        withdrawals_by_month(),
    ):
        for y, _m in mapping:
            years.add(int(y))
    quotas = list_year_quotas()
    if not quotas.empty:
        years.update(int(y) for y in quotas["year"].tolist())
    return sorted(years, reverse=True)


ANNUAL_METRICS = [
    ("fatturato_competenza", "Fatturato (competenza)"),
    ("fatturato_cassa", "Fatturato (cassa)"),
    ("enasarco", "Enasarco"),
    ("netto", "Netto"),
    ("prelievi", "Prelievi"),
    ("accantonamenti", "Accantonamenti"),
]


def _months_vector_from_map(
    month_map: dict[tuple[int, int], float], year: int
) -> list[float]:
    return [float(month_map.get((year, m), 0.0)) for m in range(1, 13)]


@st.cache_data(ttl=120, show_spinner=False)
def _annual_finance_bundle() -> dict[str, Any]:
    quotas_map = _load_quotas_map()
    fatturato_anni = fatturato_by_attribution_year()
    all_inv = list_invoices_lordo_all()
    res_by_id = _invoice_reserves(all_inv, quotas_map, fatturato_anni)

    fat_comp = fatturato_by_competence_month()
    fat_cassa = fatturato_by_collection_month()
    ena_raw = enasarco_by_collection_month()
    ena_cost = {k: -float(v) for k, v in ena_raw.items()}
    prelievi = withdrawals_by_month()

    month_acc_inv: dict[tuple[int, int], float] = {}
    year_inps_acconto: dict[int, float] = {}
    year_inps_saldo: dict[int, float] = {}
    year_ir_acconto: dict[int, float] = {}
    year_ir_saldo: dict[int, float] = {}

    if not all_inv.empty:
        for r in all_inv.itertuples(index=False):
            key = attribution_year_month(r.issue_date, r.payment_date)
            if key is None:
                continue
            res = res_by_id.get(int(r.id))
            if res is None:
                continue
            y, _m = key
            month_acc_inv[key] = month_acc_inv.get(key, 0.0) + res.totale
            year_inps_acconto[y] = year_inps_acconto.get(y, 0.0) + res.inps_var
            year_inps_saldo[y] = year_inps_saldo.get(y, 0.0) + res.inps_extra
            year_ir_acconto[y] = year_ir_acconto.get(y, 0.0) + res.ir_var
            year_ir_saldo[y] = year_ir_saldo.get(y, 0.0) + res.ir_extra

    years: set[int] = set()
    for mapping in (fat_comp, fat_cassa, ena_cost, prelievi, month_acc_inv):
        for y, _m in mapping:
            years.add(int(y))
    years.update(quotas_map.keys())
    years.update(year_inps_acconto)
    years.update(year_inps_saldo)
    years.update(year_ir_acconto)
    years.update(year_ir_saldo)

    activity_start = _finance_activity_start()
    as_of = as_of_month()

    monthly: dict[str, dict[int, list[float]]] = {
        key: {} for key, _ in ANNUAL_METRICS
    }
    yearly: dict[int, dict[str, float]] = {}

    for y in years:
        fissa_mese = fixed_inps_month(quotas_map[y]) if y in quotas_map else 0.0
        fissa_n = fixed_inps_months_in_year(y, activity_start, as_of)
        fissa_anno = fissa_mese * float(fissa_n)
        fat_c = _months_vector_from_map(fat_cassa, y)
        ena = _months_vector_from_map(ena_cost, y)
        acc_inv = _months_vector_from_map(month_acc_inv, y)
        acc = [
            acc_inv[i]
            + (fissa_mese if applies_fixed_inps(y, i + 1, activity_start, as_of) else 0.0)
            for i in range(12)
        ]
        netto_m = [fat_c[i] - ena[i] - acc[i] for i in range(12)]
        prel_m = _months_vector_from_map(prelievi, y)
        fat_comp_m = _months_vector_from_map(fat_comp, y)

        monthly["fatturato_competenza"][y] = fat_comp_m
        monthly["fatturato_cassa"][y] = fat_c
        monthly["enasarco"][y] = ena
        monthly["prelievi"][y] = prel_m
        monthly["accantonamenti"][y] = acc
        monthly["netto"][y] = netto_m

        inps_a = float(year_inps_acconto.get(y, 0.0))
        inps_s = float(year_inps_saldo.get(y, 0.0))
        ir_a = float(year_ir_acconto.get(y, 0.0))
        ir_s = float(year_ir_saldo.get(y, 0.0))
        fat_c_y = sum(fat_c)
        ena_y = sum(ena)
        netto_y = fat_c_y - ena_y - (inps_a + inps_s + ir_a + ir_s + fissa_anno)

        yearly[y] = {
            "fatturato_competenza": sum(fat_comp_m),
            "fatturato_cassa": fat_c_y,
            "enasarco": ena_y,
            "inps_acconto": inps_a,
            "inps_saldo": inps_s,
            "ir_acconto": ir_a,
            "ir_saldo": ir_s,
            "inps_fissa": fissa_anno,
            "netto": netto_y,
            "prelievi": sum(prel_m),
        }

    return {"monthly": monthly, "yearly": yearly}


def annual_metrics_by_year() -> dict[str, dict[int, list[float]]]:
    """
    Metriche mensili (gen–dic) per anno.

    Enasarco / Accantonamenti / Netto = cassa (payment_date).
    Fatturato disponibile in competenza e cassa.
    Netto = fatturato cassa − enasarco − accantonamenti (prima dei prelievi).
    Accantonamenti = riserve su fatture + INPS fissa nei mesi da inizio
    attività al mese corrente (anche mesi magri; non mesi futuri).
    """
    return _annual_finance_bundle()["monthly"]


def annual_totals_by_year() -> dict[int, dict[str, float]]:
    """Totali annuali (cassa dove applicabile) per la sezione dettaglio."""
    return _annual_finance_bundle()["yearly"]


def list_enasarco_lines_for_collection(year: int, month: int) -> pd.DataFrame:
    """Voci enasarco sulle fatture di saldo incassate nel mese."""
    sql = """
        SELECT
            i.id AS invoice_id,
            i.series_year,
            i.number,
            l.description,
            l.amount
        FROM invoices i
        JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.payment_date IS NOT NULL
          AND EXTRACT(YEAR FROM i.payment_date) = %s
          AND EXTRACT(MONTH FROM i.payment_date) = %s
          AND i.kind = 'balance'
          AND l.line_type = 'enasarco'
        ORDER BY i.number, l.sort_order, l.id
    """
    return _read_df(sql, [year, month])


def list_withdrawals_for_month(year: int, month: int) -> pd.DataFrame:
    sql = """
        SELECT id, withdrawal_date, reference_year, reference_month,
               amount, description
        FROM withdrawals
        WHERE reference_year = %s AND reference_month = %s
        ORDER BY withdrawal_date NULLS LAST, id
    """
    return _read_df(sql, [year, month])


def fatturato_by_attribution_year() -> dict[int, float]:
    """
    Fatturato annuale = voci senza enasarco, solo fatture incassate
    (aggregato per anno di payment_date).
    """
    sql = """
        SELECT
            EXTRACT(YEAR FROM i.payment_date)::INTEGER AS attr_year,
            COALESCE(
                SUM(l.amount) FILTER (
                    WHERE l.line_type IS DISTINCT FROM 'enasarco'
                ),
                0
            ) AS fatturato
        FROM invoices i
        LEFT JOIN invoice_lines l ON l.invoice_id = i.id
        WHERE i.payment_date IS NOT NULL
        GROUP BY 1
        ORDER BY 1
    """
    df = _read_df(sql)
    if df.empty:
        return {}
    return {
        int(r.attr_year): float(r.fatturato)
        for r in df.itertuples(index=False)
        if r.attr_year is not None
    }


def list_year_quotas() -> pd.DataFrame:
    sql = """
        SELECT year, imponibile_rate, inps_discount_rate, inps_rate,
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
                    year, imponibile_rate, inps_discount_rate, inps_rate,
                    enasarco_rate, enasarco_max, inps_min_base, income_tax_rate,
                    inps_advance_rate, income_tax_advance_rate, updated_at
                ) VALUES (
                    %(year)s, %(imponibile_rate)s, %(inps_discount_rate)s, %(inps_rate)s,
                    %(enasarco_rate)s, %(enasarco_max)s, %(inps_min_base)s, %(income_tax_rate)s,
                    %(inps_advance_rate)s, %(income_tax_advance_rate)s, NOW()
                )
                ON CONFLICT (year) DO UPDATE SET
                    imponibile_rate = EXCLUDED.imponibile_rate,
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

    with st.form(f"tax_form_{payment_id or 'new'}", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            tax_type = st.selectbox(
                "Tipo",
                options=TAX_TYPE_OPTIONS,
                format_func=lambda k: TAX_TYPE_LABELS[k],
                index=TAX_TYPE_OPTIONS.index(defaults["type"])
                if defaults.get("type") in TAX_TYPE_OPTIONS
                else 0,
            )
        with c2:
            ref_year = st.number_input(
                "Anno competenza",
                min_value=2000,
                max_value=2100,
                value=int(defaults["reference_year"]),
            )
        with c3:
            payment_date = st.date_input(
                "Data pagamento",
                value=defaults.get("payment_date") or date.today(),
            )

        description = st.text_input(
            "Descrizione",
            value=defaults.get("description") or "",
        )
        a1, a2 = st.columns(2)
        with a1:
            amount = st.number_input(
                "Importo (€)",
                value=_money(defaults.get("amount")),
                step=0.01,
                format="%.2f",
            )
        with a2:
            paid = st.checkbox(
                "Pagato",
                value=bool(defaults.get("paid")),
            )

        saved = st.form_submit_button("Salva", type="primary")
        if saved:
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

    with st.form(f"wd_form_{withdrawal_id or 'new'}", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            ref_year = st.number_input(
                "Anno",
                min_value=2000,
                max_value=2100,
                value=int(defaults["reference_year"]),
            )
        with c2:
            ref_month = st.selectbox(
                "Mese",
                options=list(range(1, 13)),
                format_func=_month_label,
                index=int(defaults["reference_month"]) - 1,
            )
        with c3:
            withdrawal_date = st.date_input(
                "Data prelievo",
                value=defaults.get("withdrawal_date") or date.today(),
            )

        amount = st.number_input(
            "Importo (€)",
            value=_money(defaults.get("amount")),
            step=0.01,
            format="%.2f",
        )
        description = st.text_input(
            "Descrizione",
            value=defaults.get("description") or "",
        )

        saved = st.form_submit_button("Salva", type="primary")
        if saved:
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
    st.subheader("Aliquote")

    @st.fragment
    def _aliquote_body() -> None:
        form_key = "fin_quota_form_open"
        if form_key not in st.session_state:
            st.session_state[form_key] = False

        h1, h2 = st.columns([6, 1])
        with h1:
            st.markdown("##### Elenco aliquote")
        with h2:
            if st.button(
                "−" if st.session_state[form_key] else "+",
                key="fin_quota_form_toggle",
                help="Mostra/nascondi nuova aliquota",
            ):
                st.session_state[form_key] = not st.session_state[form_key]
                # Il click sul bottone riesegue già il fragment: niente st.rerun()

        df = list_year_quotas()
        if df.empty:
            st.info("Nessuna aliquota. Premi + per crearne una.")
            selected_year = None
        else:
            display = pd.DataFrame(
                {
                    "Anno": df["year"].astype(int),
                    "Imponibile": df["imponibile_rate"].map(_fmt_pct),
                    "INPS": df["inps_rate"].map(_fmt_pct),
                    "Sconto INPS": df["inps_discount_rate"].map(_fmt_pct),
                    "Enasarco": df["enasarco_rate"].map(_fmt_pct),
                    "Max Enasarco": df["enasarco_max"].map(_fmt_euro),
                    "Min. INPS": df["inps_min_base"].map(_fmt_euro),
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
            selected_year = (
                int(df.iloc[selected[0]]["year"]) if selected else None
            )

        show_form = st.session_state[form_key] or selected_year is not None
        if not show_form:
            return

        st.markdown("---")
        if selected_year is not None:
            st.markdown(f"##### Modifica aliquote {selected_year}")
            quota = get_year_quota(selected_year) or {}
            year_value = selected_year
            year_locked = True
        else:
            st.markdown("##### Nuova aliquota")
            quota = {}
            year_value = date.today().year
            year_locked = False

        key_prefix = (
            f"quota_edit_{year_value}" if year_locked else "quota_new"
        )
        with st.form(f"quota_form_{key_prefix}", clear_on_submit=False):
            if year_locked:
                st.caption(f"Anno: **{year_value}**")
                year_input = year_value
            else:
                year_input = st.number_input(
                    "Anno",
                    min_value=2000,
                    max_value=2100,
                    value=year_value,
                )

            values: dict[str, Any] = {}
            cols = st.columns(2)
            for i, (field, label, kind) in enumerate(QUOTA_FIELDS):
                with cols[i % 2]:
                    raw = quota.get(field)
                    if kind == "rate":
                        pct_default = (
                            float(raw) * 100 if raw is not None else 0.0
                        )
                        pct = st.number_input(
                            label + " (%)",
                            value=float(pct_default),
                            step=0.01,
                            format="%.4f",
                        )
                        values[field] = float(pct) / 100.0
                    else:
                        values[field] = st.number_input(
                            label,
                            value=_money(raw) if raw is not None else 0.0,
                            step=0.01,
                            format="%.2f",
                        )

            submitted = st.form_submit_button("Salva aliquote", type="primary")
            if submitted:
                upsert_year_quota(int(year_input), values)
                _clear_finance_caches()
                st.session_state[form_key] = False
                st.success(f"Aliquote {int(year_input)} salvate.")
                st.rerun()

    _aliquote_body()


def _load_quotas_map() -> dict[int, Quotas]:
    df = list_year_quotas()
    if df.empty:
        return {}
    out: dict[int, Quotas] = {}
    for _, row in df.iterrows():
        out[int(row["year"])] = Quotas.from_row(row)
    return out


def _quotas_or_none(year: Optional[int], quotas_map: Optional[dict[int, Quotas]] = None) -> Optional[Quotas]:
    if year is None:
        return None
    if quotas_map is not None:
        return quotas_map.get(int(year))
    row = get_year_quota(year)
    if not row:
        return None
    return Quotas.from_row(row)


def _colored_amount_box(label: str, value: float) -> None:
    if value > 0:
        color, bg, border = "#0a7a2f", "#e8f5e9", "#a5d6a7"
    elif value < 0:
        color, bg, border = "#c62828", "#ffebee", "#ef9a9a"
    else:
        color, bg, border = "#424242", "#f5f5f5", "#e0e0e0"
    st.markdown(
        f'<div style="padding:1rem 1.25rem;border-radius:0.5rem;'
        f"background:{bg};border:1px solid {border};margin-bottom:0.75rem;\">"
        f'<div style="font-size:0.9rem;opacity:0.85;">{label}</div>'
        f'<div style="font-size:1.75rem;font-weight:700;color:{color};">'
        f"{_fmt_euro(value)}</div></div>",
        unsafe_allow_html=True,
    )


def _colored_metric(label: str, value: float) -> None:
    if value > 0:
        color = "#0a7a2f"
    elif value < 0:
        color = "#c62828"
    else:
        color = "inherit"
    st.markdown(
        f'<p style="margin:0;font-size:0.875rem;opacity:0.7;">{label}</p>'
        f'<p style="margin:0;font-size:1.5rem;font-weight:600;color:{color};">'
        f"{_fmt_euro(value)}</p>",
        unsafe_allow_html=True,
    )



def _invoice_reserves(
    all_inv: pd.DataFrame,
    quotas_map: dict[int, Quotas],
    fatturato_anni: dict[int, float],
) -> dict[int, Any]:
    """
    Per ogni fattura incassata: acconto (var) + saldo (extra) in base allo YTD.
    """
    if all_inv.empty:
        return {}
    rows = []
    for r in all_inv.itertuples(index=False):
        attr = attribution_year(r.issue_date, r.payment_date)
        rows.append(
            {
                "id": int(r.id),
                "attr": attr,
                "lordo": float(r.lordo),
                "sort": attribution_sort_date(r.issue_date, r.payment_date),
            }
        )
    rows.sort(key=lambda x: (x["attr"] or 0, x["sort"], x["id"]))
    ytd: dict[int, float] = {}
    out: dict[int, Any] = {}
    for row in rows:
        attr = row["attr"]
        lordo = row["lordo"]
        if attr is None:
            out[row["id"]] = None
            continue
        quotas = quotas_map.get(int(attr))
        before = ytd.get(int(attr), 0.0)
        if quotas is None:
            out[row["id"]] = None
        else:
            f_prev = float(fatturato_anni.get(int(attr) - 1, 0.0))
            out[row["id"]] = invoice_reserve(lordo, before, f_prev, quotas)
        ytd[int(attr)] = before + lordo
    return out


def compute_month_figures(year: int, month: int) -> dict[str, Any]:
    """
    Mese di incasso: per fattura enasarco + var (acconto) + extra (saldo);
    a livello mese solo INPS fissa (minimale / 12) e prelievi.
    """
    invoices = list_invoices_for_collection(year, month)
    enasarco_df = list_enasarco_lines_for_collection(year, month)
    withdrawals_df = list_withdrawals_for_month(year, month)
    quotas_map = _load_quotas_map()
    fatturato_anni = fatturato_by_attribution_year()
    all_inv = list_invoices_lordo_all()
    res_by_id = _invoice_reserves(all_inv, quotas_map, fatturato_anni)

    enasarco_by_inv: dict[int, float] = {}
    if not enasarco_df.empty:
        for er in enasarco_df.itertuples(index=False):
            iid = int(er.invoice_id)
            enasarco_by_inv[iid] = enasarco_by_inv.get(iid, 0.0) + float(er.amount)

    current_quotas = quotas_map.get(year)
    activity_start = _finance_activity_start()
    as_of = as_of_month()
    inps_fissa = (
        fixed_inps_month(current_quotas)
        if current_quotas and applies_fixed_inps(year, month, activity_start, as_of)
        else 0.0
    )
    inps_min_annuo = (
        inps_minimale_annuo(current_quotas) if current_quotas else 0.0
    )

    total_lordo = 0.0
    total_enasarco_tassa = 0.0
    sum_inps_var = 0.0
    sum_ir_var = 0.0
    sum_inps_extra = 0.0
    sum_ir_extra = 0.0
    sum_totale_fatture = 0.0
    missing_quotas: set[int] = set()
    attr_years_used: set[int] = set()
    inv_rows: list[dict[str, Any]] = []

    for r in invoices.itertuples(index=False):
        issue = r.issue_date
        pay = r.payment_date
        attr = attribution_year(issue, pay)
        lordo = float(r.lordo_senza_enasarco)
        ena_line = float(enasarco_by_inv.get(int(r.id), 0.0))
        ena_tassa = -ena_line
        total_lordo += lordo
        total_enasarco_tassa += ena_tassa
        res = res_by_id.get(int(r.id))
        inps_var = ir_var = inps_extra = ir_extra = 0.0
        ytd_before = soglia = 0.0
        f_prev = 0.0
        if attr is not None:
            attr_years_used.add(int(attr))
        if res is None:
            if attr is not None and attr not in quotas_map:
                missing_quotas.add(int(attr))
        else:
            inps_var = res.inps_var
            ir_var = res.ir_var
            inps_extra = res.inps_extra
            ir_extra = res.ir_extra
            ytd_before = res.f_ytd_before
            soglia = res.threshold_fatturato
            f_prev = res.fatturato_prev_year
            sum_inps_var += inps_var
            sum_ir_var += ir_var
            sum_inps_extra += inps_extra
            sum_ir_extra += ir_extra

        totale_inv = inps_var + ir_var + inps_extra + ir_extra
        sum_totale_fatture += totale_inv
        netto_inv = lordo - ena_tassa - totale_inv
        competenza = (
            f"{_month_label(int(r.reference_month))} {int(r.reference_year)}"
            if r.reference_year is not None and r.reference_month is not None
            else "—"
        )
        inv_rows.append(
            {
                "N.": f"{int(r.series_year)}-{int(r.number):02d}",
                "Tipo": INVOICE_KIND_LABELS.get(r.kind, r.kind),
                "Competenza": competenza,
                "Emissione": issue,
                "Pagamento": pay,
                "YTD prima": ytd_before,
                "Soglia": soglia,
                "Fatt. anno prec.": f_prev,
                "Lordo": lordo,
                "Enasarco": ena_tassa,
                "INPS acconto": inps_var,
                "IR acconto": ir_var,
                "INPS saldo": inps_extra,
                "IR saldo": ir_extra,
                "Da accantonare": totale_inv,
                "Netto": netto_inv,
            }
        )

    wd_total = (
        float(pd.to_numeric(withdrawals_df["amount"], errors="coerce").fillna(0).sum())
        if not withdrawals_df.empty
        else 0.0
    )
    totale_mese = sum_totale_fatture + inps_fissa
    netto = total_lordo - total_enasarco_tassa - totale_mese
    netto_residuo = netto - wd_total

    return {
        "invoices": invoices,
        "inv_rows": inv_rows,
        "enasarco_df": enasarco_df,
        "withdrawals_df": withdrawals_df,
        "lordo": total_lordo,
        "enasarco": -total_enasarco_tassa,
        "enasarco_tassa": total_enasarco_tassa,
        "inps_fissa": inps_fissa,
        "inps_min_annuo": inps_min_annuo,
        "current_quotas": current_quotas,
        "inps_var": sum_inps_var,
        "ir_var": sum_ir_var,
        "inps_extra": sum_inps_extra,
        "ir_extra": sum_ir_extra,
        "totale_fatture": sum_totale_fatture,
        "totale_mese": totale_mese,
        "netto": netto,
        "prelievi": wd_total,
        "netto_residuo": netto_residuo,
        "missing_quotas": missing_quotas,
        "attr_years_used": attr_years_used,
        "fatturato_anni": fatturato_anni,
    }


def _clear_finance_caches() -> None:
    total_ancora_prelevabile.clear()
    _annual_finance_bundle.clear()
    _finance_activity_start.clear()


@st.cache_data(ttl=120, show_spinner=False)
def _finance_activity_start() -> Optional[tuple[int, int]]:
    """Primo mese con incassi, enasarco o prelievi (inizio attività)."""
    keys: set[tuple[int, int]] = set()
    all_inv = list_invoices_lordo_all()
    if not all_inv.empty:
        for r in all_inv.itertuples(index=False):
            key = attribution_year_month(r.issue_date, r.payment_date)
            if key is not None:
                keys.add(key)
    keys.update(enasarco_by_collection_month().keys())
    keys.update(withdrawals_by_month().keys())
    return min(keys) if keys else None


@st.cache_data(ttl=120, show_spinner=False)
def total_ancora_prelevabile() -> float:
    """Somma netti residui per mese: da inizio attività al mese corrente."""
    quotas_map = _load_quotas_map()
    fatturato_anni = fatturato_by_attribution_year()
    all_inv = list_invoices_lordo_all()
    res_by_id = _invoice_reserves(all_inv, quotas_map, fatturato_anni)
    month_ena = enasarco_by_collection_month()
    month_wd = withdrawals_by_month()

    month_lordo: dict[tuple[int, int], float] = {}
    month_acc: dict[tuple[int, int], float] = {}

    if not all_inv.empty:
        for r in all_inv.itertuples(index=False):
            key = attribution_year_month(r.issue_date, r.payment_date)
            if key is None:
                continue
            lordo = float(r.lordo)
            month_lordo[key] = month_lordo.get(key, 0.0) + lordo
            res = res_by_id.get(int(r.id))
            if res is not None:
                month_acc[key] = month_acc.get(key, 0.0) + res.totale

    activity_keys = set(month_lordo) | set(month_ena) | set(month_wd) | set(month_acc)
    activity_start = min(activity_keys) if activity_keys else None
    if activity_start is None:
        return 0.0
    as_of = as_of_month()

    fissa_by_year: dict[int, float] = {}
    total = 0.0
    for key in iter_year_months(activity_start, as_of):
        y, m = key
        if y not in fissa_by_year:
            q = quotas_map.get(y)
            fissa_by_year[y] = fixed_inps_month(q) if q else 0.0
        lordo = month_lordo.get(key, 0.0)
        enasarco_tassa = -month_ena.get(key, 0.0)
        fissa = (
            fissa_by_year[y]
            if applies_fixed_inps(y, m, activity_start, as_of)
            else 0.0
        )
        acc = month_acc.get(key, 0.0) + fissa
        prelievi = month_wd.get(key, 0.0)
        total += lordo - enasarco_tassa - acc - prelievi
    return total


def page_contabilita_mensile() -> None:
    st.subheader("Contabilità mensile")
    st.caption(
        "Mese = data di pagamento (solo fatture incassate). "
        "Per fattura: enasarco + INPS/IR acconto + INPS/IR saldo. "
        f"Per mese: INPS fissa (minimale ÷ {CERTAIN_RESERVE_MONTHS}) "
        "dal primo mese di attività al mese corrente (anche mesi magri)."
    )

    periods = list_collection_periods()
    if not periods:
        st.info("Nessuna fattura incassata.")
        return

    _colored_amount_box(
        "Totale ancora prelevabile (somma netti residui di tutti i mesi)",
        total_ancora_prelevabile(),
    )

    labels = [f"{_month_label(m)} {y}" for y, m in periods]
    choice = st.selectbox("Mese di incasso", options=labels, key="fin_month_period")
    year, month = periods[labels.index(choice)]

    fig = compute_month_figures(year, month)
    missing_quotas: set[int] = fig["missing_quotas"]
    inps_fissa = float(fig["inps_fissa"])
    inps_min_annuo = float(fig["inps_min_annuo"])
    totale_fatture = float(fig["totale_fatture"])
    totale_mese = float(fig["totale_mese"])
    wd_total = float(fig["prelievi"])
    current_quotas = fig["current_quotas"]
    lordo = float(fig["lordo"])
    enasarco_tassa = float(fig["enasarco_tassa"])
    lordo_netto_ena = lordo - enasarco_tassa
    netto_mese = lordo_netto_ena - totale_mese
    netto_residuo = netto_mese - wd_total

    st.markdown("##### Fatture incassate nel mese")
    if not fig["inv_rows"]:
        st.info("Nessuna fattura incassata in questo mese.")
    else:
        money_cols = [
            "YTD prima",
            "Soglia",
            "Fatt. anno prec.",
            "Lordo",
            "Enasarco",
            "INPS acconto",
            "IR acconto",
            "INPS saldo",
            "IR saldo",
            "Da accantonare",
            "Netto",
        ]
        col_config = {
            "Emissione": st.column_config.DateColumn("Emissione"),
            "Pagamento": st.column_config.DateColumn("Pagamento"),
        }
        for c in money_cols:
            col_config[c] = st.column_config.NumberColumn(c, format="€ %.2f")
        st.dataframe(
            pd.DataFrame(fig["inv_rows"]),
            width="stretch",
            hide_index=True,
            column_config=col_config,
            key=f"fin_month_inv_{year}_{month}",
        )
        st.caption(
            f"Somma da accantonare sulle fatture: **{_fmt_euro(totale_fatture)}** "
            f"(acconto + saldo)"
        )
    if missing_quotas:
        st.warning(
            "Mancano le quote annuali per: "
            + ", ".join(str(y) for y in sorted(missing_quotas))
        )

    st.markdown("##### Accantonamento fisso del mese")
    if current_quotas is None:
        det_fissa = f"Quote anno {year} mancanti"
    else:
        fissa_piena = fixed_inps_month(current_quotas)
        if inps_fissa > 0:
            det_fissa = (
                f"Minimale {_fmt_euro(current_quotas.inps_min_base or 0)} × "
                f"INPS {_fmt_pct(current_quotas.inps_rate)} × "
                f"(1 − sconto {_fmt_pct(current_quotas.inps_discount_rate)}) "
                f"= {_fmt_euro(inps_min_annuo)} ÷ {CERTAIN_RESERVE_MONTHS} "
                f"= {_fmt_euro(inps_fissa)}"
            )
        else:
            det_fissa = (
                f"Non applicata (fuori dalla finestra inizio attività → mese corrente). "
                f"Quota piena sarebbe {_fmt_euro(fissa_piena)}."
            )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Voce": "INPS fissa (minimale / 12)",
                    "Importo": inps_fissa,
                    "Dettaglio": det_fissa,
                }
            ]
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "Importo": st.column_config.NumberColumn("Importo", format="€ %.2f"),
            "Dettaglio": st.column_config.TextColumn("Dettaglio", width="large"),
        },
        key=f"fin_month_fissa_{year}_{month}",
    )
    st.caption(
        "Indipendente dal fatturato: dovuta ogni mese anche senza incassi."
    )

    @st.fragment
    def _prelievi_mese() -> None:
        form_key = f"fin_month_wd_form_open_{year}_{month}"
        if form_key not in st.session_state:
            st.session_state[form_key] = False

        h1, h2 = st.columns([6, 1])
        with h1:
            st.markdown("##### Prelievi del mese")
        with h2:
            if st.button(
                "−" if st.session_state[form_key] else "+",
                key=f"fin_month_wd_toggle_{year}_{month}",
                help="Mostra/nascondi nuovo prelievo",
            ):
                st.session_state[form_key] = not st.session_state[form_key]

        wd_df = list_withdrawals_for_month(year, month)
        wd_sum = (
            float(pd.to_numeric(wd_df["amount"], errors="coerce").fillna(0).sum())
            if not wd_df.empty
            else 0.0
        )

        if wd_df.empty:
            st.info("Nessun prelievo in questo mese.")
            selected: list[int] = []
        else:
            display_wd = pd.DataFrame(
                {
                    "Data": wd_df["withdrawal_date"],
                    "Importo": pd.to_numeric(wd_df["amount"], errors="coerce"),
                    "Descrizione": wd_df["description"],
                }
            )
            event = st.dataframe(
                display_wd,
                width="stretch",
                hide_index=True,
                on_select="rerun",
                selection_mode="single-row",
                column_config={
                    "Data": st.column_config.DateColumn("Data"),
                    "Importo": st.column_config.NumberColumn(
                        "Importo", format="€ %.2f"
                    ),
                },
                key=f"fin_month_wd_table_{year}_{month}",
            )
            selected = event.selection.rows if event and event.selection else []
            st.caption(f"Totale prelievi: **{_fmt_euro(wd_sum)}**")

        withdrawal: Optional[dict[str, Any]] = None
        withdrawal_id: Optional[int] = None
        if selected and not wd_df.empty:
            withdrawal_id = int(wd_df.iloc[selected[0]]["id"])
            withdrawal = get_withdrawal(withdrawal_id)

        show_form = st.session_state[form_key] or withdrawal is not None
        if not show_form:
            return

        st.markdown("---")
        title = (
            "##### Modifica prelievo"
            if withdrawal is not None
            else "##### Nuovo prelievo per questo mese"
        )
        st.markdown(title)

        defaults = withdrawal or {
            "withdrawal_date": date(year, month, 1),
            "reference_year": year,
            "reference_month": month,
            "amount": 0.0,
            "description": "",
        }
        form_id = f"mwd_form_{year}_{month}_{withdrawal_id or 'new'}"
        with st.form(form_id, clear_on_submit=False):
            d1, d2 = st.columns(2)
            with d1:
                withdrawal_date = st.date_input(
                    "Data prelievo",
                    value=defaults.get("withdrawal_date") or date(year, month, 1),
                )
            with d2:
                amount = st.number_input(
                    "Importo (€)",
                    value=_money(defaults.get("amount")),
                    step=0.01,
                    format="%.2f",
                )
            description = st.text_input(
                "Descrizione",
                value=defaults.get("description") or "",
            )
            saved = st.form_submit_button("Salva prelievo", type="primary")
            if saved:
                upsert_withdrawal(
                    {
                        "withdrawal_date": withdrawal_date,
                        "reference_year": year,
                        "reference_month": month,
                        "amount": float(amount),
                        "description": description or "",
                    },
                    withdrawal_id=withdrawal_id,
                )
                _clear_finance_caches()
                st.session_state[form_key] = False
                st.success("Prelievo salvato.")
                st.rerun()

        if withdrawal_id is not None:
            confirm = st.checkbox(
                "Conferma eliminazione",
                key=f"mwd_del_confirm_{year}_{month}_{withdrawal_id}",
            )
            if st.button(
                "Elimina",
                disabled=not confirm,
                key=f"mwd_del_{year}_{month}_{withdrawal_id}",
            ):
                delete_withdrawal(withdrawal_id)
                _clear_finance_caches()
                st.success("Prelievo eliminato.")
                st.rerun()

    _prelievi_mese()

    st.markdown("---")
    st.markdown("##### Riepilogo netto mese")
    r1, r2, r3, r4, r5 = st.columns(5)
    r1.metric("Lordo (netto Enasarco)", _fmt_euro(lordo_netto_ena))
    r2.metric("Da accantonare", _fmt_euro(totale_mese))
    r3.metric("Netto", _fmt_euro(netto_mese))
    r4.metric("Prelievi", _fmt_euro(wd_total))
    with r5:
        _colored_metric("Netto residuo", netto_residuo)
    st.caption(
        f"Da accantonare = somma fatture {_fmt_euro(totale_fatture)} "
        f"+ INPS fissa {_fmt_euro(inps_fissa)}. "
        "Lordo (netto Enasarco) = lordo − enasarco · "
        "Netto = lordo netto enasarco − da accantonare · "
        "Netto residuo = netto − prelievi."
    )


def page_contabilita_annuale() -> None:
    st.subheader("Contabilità annuale")
    st.caption(
        "Grafico gen–dic. Fatturato disponibile in competenza o cassa; "
        "Enasarco, Accantonamenti e Netto sono in cassa (data pagamento). "
        "Netto = fatturato cassa − enasarco − accantonamenti (prima dei prelievi)."
    )

    years = list_annual_chart_years()
    if not years:
        st.info("Nessun dato contabile da rappresentare.")
        return

    metrics = annual_metrics_by_year()
    metric_labels = {k: lab for k, lab in ANNUAL_METRICS}

    selected_years = st.multiselect(
        "Anni (grafico)",
        options=years,
        default=[years[0]],
        key="fin_annual_years",
    )

    st.markdown("##### Voci da mostrare nel grafico")
    series_specs: list[tuple[int, str]] = []
    if selected_years:
        cols = st.columns(min(len(selected_years), 4))
        for i, y in enumerate(selected_years):
            with cols[i % len(cols)]:
                st.markdown(f"**{y}**")
                for key, label in ANNUAL_METRICS:
                    if st.checkbox(
                        label,
                        key=f"fin_annual_{y}_{key}",
                        value=(
                            key == "fatturato_cassa" and y == selected_years[0]
                        ),
                    ):
                        series_specs.append((y, key))

    if series_specs:
        month_labels = [_month_label(m) for m in range(1, 13)]
        chart_data: dict[str, list[float]] = {}
        for y, key in series_specs:
            label = f"{y} · {metric_labels[key]}"
            values = metrics.get(key, {}).get(y)
            if values is None:
                values = [0.0] * 12
            chart_data[label] = values

        chart_df = pd.DataFrame(chart_data, index=month_labels)
        long = (
            chart_df.reset_index(names="Mese")
            .melt(id_vars="Mese", var_name="Serie", value_name="Valore")
        )
        chart = (
            alt.Chart(long)
            .mark_line(point=True)
            .encode(
                x=alt.X("Mese:N", sort=month_labels, title=None),
                y=alt.Y("Valore:Q", title="€"),
                color=alt.Color("Serie:N", title=None),
                tooltip=[
                    alt.Tooltip("Mese:N"),
                    alt.Tooltip("Serie:N"),
                    alt.Tooltip("Valore:Q", format=",.2f"),
                ],
            )
            .properties(height=400)
        )
        st.altair_chart(chart, width="stretch")

        with st.expander("Dati tabellari del grafico", expanded=False):
            col_config = {
                c: st.column_config.NumberColumn(c, format="€ %.2f")
                for c in chart_df.columns
            }
            st.dataframe(
                chart_df,
                width="stretch",
                column_config=col_config,
            )
    else:
        st.info("Seleziona anni e voci per il grafico.")

    st.markdown("---")
    st.markdown("##### Dettaglio per anno")
    st.caption(
        "Totali annuali. Enasarco e riserve in cassa. "
        "Netto = fatturato cassa − enasarco − INPS/IR acconto e saldo − INPS fissa "
        "(dal primo mese di attività al mese corrente, anche mesi magri), "
        "prima dei prelievi. "
        "Netto residuo = Netto − Prelievi (stesso concetto del totale ancora prelevabile)."
    )
    totals = annual_totals_by_year()
    detail_rows = []
    residuo_totale = 0.0
    for y in sorted(totals.keys(), reverse=True):
        t = totals[y]
        residuo = float(t["netto"]) - float(t["prelievi"])
        residuo_totale += residuo
        detail_rows.append(
            {
                "Anno": y,
                "Fatturato (competenza)": t["fatturato_competenza"],
                "Fatturato (cassa)": t["fatturato_cassa"],
                "Enasarco": t["enasarco"],
                "INPS acconto": t["inps_acconto"],
                "INPS saldo": t["inps_saldo"],
                "IR acconto": t["ir_acconto"],
                "IR saldo": t["ir_saldo"],
                "INPS fissa": t["inps_fissa"],
                "Netto": t["netto"],
                "Prelievi": t["prelievi"],
                "Netto residuo": residuo,
            }
        )
    _colored_amount_box(
        "Totale ancora prelevabile (somma netti residui annuali)",
        residuo_totale,
    )
    detail_df = pd.DataFrame(detail_rows)
    money_cols = [c for c in detail_df.columns if c != "Anno"]
    st.dataframe(
        detail_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Anno": st.column_config.NumberColumn("Anno", format="%d"),
            **{
                c: st.column_config.NumberColumn(c, format="€ %.2f")
                for c in money_cols
            },
        },
        key="fin_annual_detail",
    )
