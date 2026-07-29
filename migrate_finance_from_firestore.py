"""
Migra Firestore (invoices, tax_payments, withdrawals, year_quotas) → Supabase.

Uso:
  1. Schema SQL pronto in sql/finance_schema.sql (NON applicato finché non lo chiedi)
  2. Chiave Firebase in .secrets/firebase-sa.json
  3. DATABASE_URL in .streamlit/secrets.toml

  Dry-run (default, non scrive su Supabase):
    python migrate_finance_from_firestore.py

  Applica solo lo schema:
    python migrate_finance_from_firestore.py --apply-schema

  Migrazione dati (dopo schema):
    python migrate_finance_from_firestore.py --apply

  Schema + dati:
    python migrate_finance_from_firestore.py --apply-schema --apply

Note:
  - Le fatture arrivano senza righe (invoice_lines vuote): le compilerai a mano in app.
  - legacy_amount conserva l'importo Firebase (lordo enasarco).
  - I prelievi legacy (1 doc/mese) diventano 1 riga con description vuota.
  - Upsert su firebase_id (re-run sicuro).
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import firebase_admin
import psycopg2
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1._helpers import DatetimeWithNanoseconds

ROOT = Path(__file__).parent
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"
DEFAULT_CREDENTIALS = ROOT / ".secrets" / "firebase-sa.json"
SCHEMA_PATH = ROOT / "sql" / "finance_schema.sql"
ROME = ZoneInfo("Europe/Rome")

TAX_TYPES = {
    "inps",
    "bankAccount",
    "bolloFatture",
    "cameraCommercio",
    "incomeTax",
}

YEAR_ID_RE = re.compile(r"^(\d{4})$")
YEAR_MONTH_ID_RE = re.compile(r"^(\d{4})-(\d{2})$")


def read_database_url() -> str:
    if not SECRETS_PATH.exists():
        raise SystemExit(
            "Manca .streamlit/secrets.toml con DATABASE_URL. "
            "Crealo prima di migrare."
        )
    for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DATABASE_URL"):
            _, _, value = line.partition("=")
            return value.strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL non trovata in .streamlit/secrets.toml")


def init_firestore(credentials_path: Path):
    if not credentials_path.exists():
        raise SystemExit(
            f"Chiave non trovata: {credentials_path}\n"
            "Salva il JSON della service account in .secrets/firebase-sa.json"
        )
    if not firebase_admin._apps:
        cred = credentials.Certificate(str(credentials_path))
        firebase_admin.initialize_app(cred)
    return firestore.client()


def to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, DatetimeWithNanoseconds):
        return value.replace(tzinfo=value.tzinfo or ZoneInfo("UTC"))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo("UTC"))
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=ROME)
    if isinstance(value, str):
        # ISO-ish
        text = value.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt
    return None


def to_date(value: Any) -> date | None:
    dt = to_datetime(value)
    if dt is None:
        return None
    return dt.astimezone(ROME).date()


def year_month_from_ts(value: Any) -> tuple[int, int] | None:
    dt = to_datetime(value)
    if dt is None:
        return None
    local = dt.astimezone(ROME)
    return local.year, local.month


def year_from_doc(doc_id: str, year_field: Any) -> int:
    m = YEAR_ID_RE.match(doc_id)
    if m:
        return int(m.group(1))
    ym = year_month_from_ts(year_field)
    if ym:
        return ym[0]
    raise ValueError(f"Impossibile determinare year per doc {doc_id!r}")


def year_month_from_period_id(doc_id: str, ref_field: Any) -> tuple[int, int]:
    """Per withdrawals: id tipicamente YYYY-MM."""
    m = YEAR_MONTH_ID_RE.match(doc_id)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return y, mo
    ym = year_month_from_ts(ref_field)
    if ym:
        return ym
    raise ValueError(f"Impossibile determinare anno/mese per doc {doc_id!r}")


def invoice_period(ref_field: Any) -> tuple[int, int]:
    """Anno/mese di competenza: solo da referenceMonth (timestamp)."""
    ym = year_month_from_ts(ref_field)
    if not ym:
        raise ValueError("Impossibile determinare mese di competenza da referenceMonth")
    return ym


def invoice_series_year(doc_id: str, issue_date_field: Any) -> int:
    """
    Anno della numerazione fattura (id Firebase YYYY-NN oppure anno emissione).
    Può differire dall'anno di competenza (es. saldo nov 2025 emesso a gen 2026).
    """
    m = re.match(r"^(\d{4})-", doc_id)
    if m:
        return int(m.group(1))
    d = to_date(issue_date_field)
    if d:
        return d.year
    raise ValueError(f"Impossibile determinare series_year per doc {doc_id!r}")


def money(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    d = Decimal(str(value))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def rate(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def fetch_collection(db, name: str) -> list[tuple[str, dict[str, Any]]]:
    docs = []
    for snap in db.collection(name).stream():
        docs.append((snap.id, snap.to_dict() or {}))
    return docs


def apply_schema(conn) -> None:
    if not SCHEMA_PATH.exists():
        raise SystemExit(f"Schema non trovato: {SCHEMA_PATH}")
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"Schema applicato da {SCHEMA_PATH}")


def migrate_year_quotas(cur, rows: list[tuple[str, dict]], dry_run: bool) -> int:
    n = 0
    for doc_id, data in rows:
        year = year_from_doc(doc_id, data.get("year"))
        payload = {
            "year": year,
            "tax_exempt_rate": rate(data.get("taxExemptRate")),
            "inps_discount_rate": rate(data.get("inpsDiscountRate")),
            "inps_rate": rate(data.get("inpsRate")),
            "enasarco_rate": rate(data.get("enasarcoRate")),
            "enasarco_max": money(data.get("enasarcoMax")) if data.get("enasarcoMax") is not None else None,
            "inps_min_base": money(data.get("inpsMinBase")) if data.get("inpsMinBase") is not None else None,
            "income_tax_rate": rate(data.get("incomeTaxRate")),
            "inps_advance_rate": rate(data.get("inpsAdvanceRate")),
            "income_tax_advance_rate": rate(data.get("incomeTaxAdvanceRate")),
            "firebase_id": doc_id,
        }
        print(
            f"  year_quotas {doc_id} → year={year} "
            f"enasarco={payload['enasarco_rate']} max={payload['enasarco_max']}"
        )
        if not dry_run:
            cur.execute(
                """
                INSERT INTO year_quotas (
                    year, tax_exempt_rate, inps_discount_rate, inps_rate,
                    enasarco_rate, enasarco_max, inps_min_base, income_tax_rate,
                    inps_advance_rate, income_tax_advance_rate, firebase_id,
                    updated_at
                ) VALUES (
                    %(year)s, %(tax_exempt_rate)s, %(inps_discount_rate)s, %(inps_rate)s,
                    %(enasarco_rate)s, %(enasarco_max)s, %(inps_min_base)s, %(income_tax_rate)s,
                    %(inps_advance_rate)s, %(income_tax_advance_rate)s, %(firebase_id)s,
                    NOW()
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
                    firebase_id = EXCLUDED.firebase_id,
                    updated_at = NOW()
                """,
                payload,
            )
        n += 1
    return n


def migrate_invoices(cur, rows: list[tuple[str, dict]], dry_run: bool) -> int:
    n = 0
    for doc_id, data in rows:
        ref_y, ref_m = invoice_period(data.get("referenceMonth"))
        series_year = invoice_series_year(doc_id, data.get("issueDate"))
        is_advance = bool(data.get("isAdvance"))
        kind = "advance" if is_advance else "balance"
        payload = {
            "number": int(data.get("number") or 0),
            "kind": kind,
            "issue_date": to_date(data.get("issueDate")),
            "payment_date": to_date(data.get("paymentDate")),
            "series_year": series_year,
            "reference_year": ref_y,
            "reference_month": ref_m,
            "notes": "",
            "legacy_amount": money(data.get("amount")),
            "firebase_id": doc_id,
        }
        print(
            f"  invoice {doc_id} → n={payload['number']} {kind} "
            f"serie={series_year} competenza={ref_y}-{ref_m:02d} "
            f"legacy={payload['legacy_amount']}"
        )
        if not dry_run:
            cur.execute(
                """
                INSERT INTO invoices (
                    number, kind, issue_date, payment_date, series_year,
                    reference_year, reference_month, notes, legacy_amount,
                    firebase_id, updated_at
                ) VALUES (
                    %(number)s, %(kind)s, %(issue_date)s, %(payment_date)s, %(series_year)s,
                    %(reference_year)s, %(reference_month)s, %(notes)s, %(legacy_amount)s,
                    %(firebase_id)s, NOW()
                )
                ON CONFLICT (firebase_id) DO UPDATE SET
                    number = EXCLUDED.number,
                    kind = EXCLUDED.kind,
                    issue_date = EXCLUDED.issue_date,
                    payment_date = EXCLUDED.payment_date,
                    series_year = EXCLUDED.series_year,
                    reference_year = EXCLUDED.reference_year,
                    reference_month = EXCLUDED.reference_month,
                    legacy_amount = EXCLUDED.legacy_amount,
                    updated_at = NOW()
                """,
                payload,
            )
        n += 1
    return n


def migrate_tax_payments(cur, rows: list[tuple[str, dict]], dry_run: bool) -> int:
    n = 0
    for doc_id, data in rows:
        tax_type = data.get("type") or ""
        if tax_type not in TAX_TYPES:
            print(f"  WARN tax_payments {doc_id}: type sconosciuto {tax_type!r} — skip")
            continue
        # id tipicamente 2024-inps-2025.02.16 → anno di competenza dal prefisso
        prefix = doc_id.split("-", 1)[0]
        if YEAR_ID_RE.match(prefix):
            year = int(prefix)
        else:
            year = year_from_doc(doc_id, data.get("referenceYear"))

        paid = data.get("paid")
        if paid is None:
            paid = data.get("payed", False)

        payload = {
            "type": tax_type,
            "description": (data.get("description") or "") or "",
            "payment_date": to_date(data.get("date")),
            "reference_year": year,
            "amount": money(data.get("amount")),
            "paid": bool(paid),
            "firebase_id": doc_id,
        }
        print(
            f"  tax {doc_id} → {tax_type} year={year} "
            f"amount={payload['amount']} paid={payload['paid']}"
        )
        if not dry_run:
            cur.execute(
                """
                INSERT INTO tax_payments (
                    type, description, payment_date, reference_year,
                    amount, paid, firebase_id, updated_at
                ) VALUES (
                    %(type)s, %(description)s, %(payment_date)s, %(reference_year)s,
                    %(amount)s, %(paid)s, %(firebase_id)s, NOW()
                )
                ON CONFLICT (firebase_id) DO UPDATE SET
                    type = EXCLUDED.type,
                    description = EXCLUDED.description,
                    payment_date = EXCLUDED.payment_date,
                    reference_year = EXCLUDED.reference_year,
                    amount = EXCLUDED.amount,
                    paid = EXCLUDED.paid,
                    updated_at = NOW()
                """,
                payload,
            )
        n += 1
    return n


def migrate_withdrawals(cur, rows: list[tuple[str, dict]], dry_run: bool) -> int:
    n = 0
    for doc_id, data in rows:
        ref_y, ref_m = year_month_from_period_id(doc_id, data.get("referenceMonth"))
        # Data prelievo: 1° del mese di competenza (legacy non aveva giorno)
        withdrawal_date = date(ref_y, ref_m, 1)
        payload = {
            "withdrawal_date": withdrawal_date,
            "reference_year": ref_y,
            "reference_month": ref_m,
            "amount": money(data.get("amount")),
            "description": "",
            "firebase_id": doc_id,
        }
        print(
            f"  withdrawal {doc_id} → {ref_y}-{ref_m:02d} "
            f"amount={payload['amount']}"
        )
        if not dry_run:
            cur.execute(
                """
                INSERT INTO withdrawals (
                    withdrawal_date, reference_year, reference_month,
                    amount, description, firebase_id, updated_at
                ) VALUES (
                    %(withdrawal_date)s, %(reference_year)s, %(reference_month)s,
                    %(amount)s, %(description)s, %(firebase_id)s, NOW()
                )
                ON CONFLICT (firebase_id) DO UPDATE SET
                    withdrawal_date = EXCLUDED.withdrawal_date,
                    reference_year = EXCLUDED.reference_year,
                    reference_month = EXCLUDED.reference_month,
                    amount = EXCLUDED.amount,
                    description = EXCLUDED.description,
                    updated_at = NOW()
                """,
                payload,
            )
        n += 1
    return n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migra fatture/tasse/prelievi da Firestore a Supabase"
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help="Path service account Firebase",
    )
    parser.add_argument(
        "--apply-schema",
        action="store_true",
        help="Esegue sql/finance_schema.sql su Supabase",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Scrive i dati su Supabase (senza flag = solo dry-run)",
    )
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== migrate_finance_from_firestore · {mode} ===\n")

    db = init_firestore(args.credentials)
    year_quotas = fetch_collection(db, "year_quotas")
    invoices = fetch_collection(db, "invoices")
    tax_payments = fetch_collection(db, "tax_payments")
    withdrawals = fetch_collection(db, "withdrawals")

    print(
        f"Firestore: year_quotas={len(year_quotas)} invoices={len(invoices)} "
        f"tax_payments={len(tax_payments)} withdrawals={len(withdrawals)}\n"
    )

    conn = None
    cur = None
    if args.apply_schema or args.apply:
        db_url = read_database_url()
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        if args.apply_schema:
            apply_schema(conn)
            cur = conn.cursor()

    print("--- year_quotas ---")
    n_q = migrate_year_quotas(cur, year_quotas, dry_run)
    print("--- invoices ---")
    n_i = migrate_invoices(cur, invoices, dry_run)
    print("--- tax_payments ---")
    n_t = migrate_tax_payments(cur, tax_payments, dry_run)
    print("--- withdrawals ---")
    n_w = migrate_withdrawals(cur, withdrawals, dry_run)

    if conn is not None and args.apply:
        conn.commit()
        print("\nCommit OK.")
    if conn is not None:
        conn.close()

    print(
        f"\nRiepilogo ({mode}): "
        f"year_quotas={n_q} invoices={n_i} tax_payments={n_t} withdrawals={n_w}"
    )
    if dry_run:
        print(
            "\nNessuna scrittura effettuata. "
            "Per applicare: --apply-schema (se serve) e --apply"
        )
    else:
        print(
            "\nFatture migrate senza righe (invoice_lines). "
            "Compila i dettagli a mano in app."
        )


if __name__ == "__main__":
    main()
