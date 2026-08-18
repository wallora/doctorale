#!/usr/bin/env python3
"""
Promemoria Doctorale:
- tax_payments non pagati (15 e 7 giorni prima)
- 1 giugno: Camera di Commercio
- 10 gennaio: verifica rate INPS nuovo anno

Secret / env richiesti (mai nel codice):
  DATABASE_URL
  SMTP_USER          — Gmail mittente
  SMTP_PASSWORD      — password per le app Gmail
  REMINDER_TO        — destinatari separati da virgola
  SMTP_FROM          — opzionale, default = SMTP_USER
  REMINDER_DAYS      — opzionale, default "15,7"
"""

from __future__ import annotations

import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras

LOCAL_TZ = ZoneInfo("Europe/Rome")

TAX_TYPE_LABELS = {
    "inps": "INPS",
    "bankAccount": "Conto corrente",
    "bolloFatture": "Bollo fatture",
    "cameraCommercio": "Camera di commercio",
    "incomeTax": "Imposte sul reddito",
    "strumenti": "Strumenti",
}

# (month, day, key, subject, body)
CALENDAR_REMINDERS: list[tuple[int, int, str, str, str]] = [
    (
        6,
        1,
        "camera_commercio",
        "[Doctorale] Promemoria: pagamento Camera di Commercio",
        "\n".join(
            [
                "Promemoria Doctorale — 1 giugno.",
                "",
                "Ricordati di pagare il diritto annuale della Camera di Commercio.",
                "Quando l'hai fatto, registra il pagamento in Contabilità → Elenco pagamenti.",
                "",
                "— Messaggio automatico Doctorale",
            ]
        ),
    ),
    (
        1,
        10,
        "inps_rate_verifica",
        "[Doctorale] Promemoria: verifica rate INPS {year}",
        "\n".join(
            [
                "Promemoria Doctorale — 10 gennaio.",
                "",
                "Verifica le rate INPS previste per il {year} e inserisci/aggiorna "
                "le scadenze in Contabilità → Elenco pagamenti.",
                "",
                "— Messaggio automatico Doctorale",
            ]
        ),
    ),
]

ENSURE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS payment_reminders_sent (
    payment_id BIGINT NOT NULL REFERENCES tax_payments(id) ON DELETE CASCADE,
    days_before INTEGER NOT NULL CHECK (days_before > 0),
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (payment_id, days_before)
);

CREATE TABLE IF NOT EXISTS calendar_reminders_sent (
    reminder_key TEXT NOT NULL,
    year INTEGER NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reminder_key, year)
);

ALTER TABLE payment_reminders_sent ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_reminders_sent ENABLE ROW LEVEL SECURITY;
"""

DUE_SQL = """
SELECT
    p.id,
    p.type,
    p.description,
    p.payment_date,
    p.reference_year,
    p.amount
FROM tax_payments p
LEFT JOIN payment_reminders_sent r
    ON r.payment_id = p.id AND r.days_before = %s
WHERE p.paid = FALSE
  AND p.payment_date = %s
  AND r.payment_id IS NULL
ORDER BY p.payment_date, p.id
"""

MARK_SENT_SQL = """
INSERT INTO payment_reminders_sent (payment_id, days_before)
VALUES (%s, %s)
ON CONFLICT DO NOTHING
"""

CALENDAR_ALREADY_SQL = """
SELECT 1
FROM calendar_reminders_sent
WHERE reminder_key = %s AND year = %s
"""

MARK_CALENDAR_SQL = """
INSERT INTO calendar_reminders_sent (reminder_key, year)
VALUES (%s, %s)
ON CONFLICT DO NOTHING
"""


def _require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        print(f"{name} mancante", file=sys.stderr)
        sys.exit(1)
    return value


def _parse_days(raw: str) -> list[int]:
    days: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        n = int(part)
        if n <= 0:
            raise ValueError(f"giorni non validi: {n}")
        if n not in days:
            days.append(n)
    if not days:
        raise ValueError("REMINDER_DAYS vuoto")
    return days


def _parse_recipients(raw: str) -> list[str]:
    recipients = [p.strip() for p in raw.split(",") if p.strip()]
    if not recipients:
        print("REMINDER_TO senza indirizzi", file=sys.stderr)
        sys.exit(1)
    return recipients


def _fmt_euro(amount: Any) -> str:
    return f"€ {float(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _type_label(tax_type: str) -> str:
    return TAX_TYPE_LABELS.get(tax_type, tax_type)


def _build_body(days_before: int, due_on: date, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"Promemoria Doctorale: {len(rows)} pagamento/i in scadenza tra {days_before} giorni "
        f"({_fmt_date(due_on)}).",
        "",
    ]
    for r in rows:
        desc = (r.get("description") or "").strip()
        title = desc or _type_label(str(r["type"]))
        lines.append(
            f"- {title} · {_type_label(str(r['type']))} · "
            f"anno {r['reference_year']} · {_fmt_euro(r['amount'])}"
        )
    lines.extend(
        [
            "",
            "Segna come pagato in Contabilità → Elenco pagamenti quando effettuato.",
            "",
            "— Messaggio automatico Doctorale",
        ]
    )
    return "\n".join(lines)


def _send_email(
    *,
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    recipients: list[str],
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


def _send_due_payment_reminders(
    cur: Any,
    *,
    today: date,
    days_list: list[int],
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    recipients: list[str],
) -> tuple[int, int]:
    sent_mails = 0
    marked = 0
    for days_before in days_list:
        due_on = today + timedelta(days=days_before)
        cur.execute(DUE_SQL, (days_before, due_on))
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            print(f"nessun pagamento a {days_before} giorni ({due_on})")
            continue

        subject = (
            f"[Doctorale] Scadenza tra {days_before} giorni "
            f"({_fmt_date(due_on)}) — {len(rows)} pagamento/i"
        )
        body = _build_body(days_before, due_on, rows)
        _send_email(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            mail_from=mail_from,
            recipients=recipients,
            subject=subject,
            body=body,
        )
        sent_mails += 1

        for r in rows:
            cur.execute(MARK_SENT_SQL, (int(r["id"]), days_before))
            marked += 1

        print(
            f"inviata mail {days_before}g: {len(rows)} pagamento/i "
            f"scadenza {due_on}"
        )
    return sent_mails, marked


def _send_calendar_reminders(
    cur: Any,
    *,
    today: date,
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    recipients: list[str],
) -> int:
    sent = 0
    year = today.year
    for month, day, key, subject_tpl, body_tpl in CALENDAR_REMINDERS:
        if today.month != month or today.day != day:
            print(f"calendario {key}: non oggi ({month:02d}-{day:02d})")
            continue

        cur.execute(CALENDAR_ALREADY_SQL, (key, year))
        if cur.fetchone():
            print(f"calendario {key}: già inviato per {year}")
            continue

        subject = subject_tpl.format(year=year)
        body = body_tpl.format(year=year)
        _send_email(
            smtp_user=smtp_user,
            smtp_password=smtp_password,
            mail_from=mail_from,
            recipients=recipients,
            subject=subject,
            body=body,
        )
        cur.execute(MARK_CALENDAR_SQL, (key, year))
        sent += 1
        print(f"inviata mail calendario {key} anno {year}")
    return sent


def main() -> None:
    database_url = _require_env("DATABASE_URL")
    smtp_user = _require_env("SMTP_USER")
    smtp_password = _require_env("SMTP_PASSWORD")
    recipients = _parse_recipients(_require_env("REMINDER_TO"))
    mail_from = (os.environ.get("SMTP_FROM") or smtp_user).strip()
    days_list = _parse_days(os.environ.get("REMINDER_DAYS") or "15,7")

    now_local = datetime.now(LOCAL_TZ)
    today = now_local.date()
    print(
        f"oggi={today} tz=Europe/Rome "
        f"utc={datetime.now(timezone.utc).isoformat()} "
        f"soglie={days_list}"
    )

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(ENSURE_TABLE_SQL)
                mail_kwargs = dict(
                    smtp_user=smtp_user,
                    smtp_password=smtp_password,
                    mail_from=mail_from,
                    recipients=recipients,
                )
                sent_due, marked = _send_due_payment_reminders(
                    cur, today=today, days_list=days_list, **mail_kwargs
                )
                sent_cal = _send_calendar_reminders(cur, today=today, **mail_kwargs)
    finally:
        conn.close()

    print(
        f"done due_mails={sent_due} due_marked={marked} calendar_mails={sent_cal}"
    )


if __name__ == "__main__":
    main()
