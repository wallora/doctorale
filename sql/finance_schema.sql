-- Doctorale · schema sezione Fatture & Tasse
-- Esegui su Supabase SQL Editor (o via migrate_finance_from_firestore.py --apply-schema)
-- Non elimina dati esistenti di medici/vendite.

-- ---------------------------------------------------------------------------
-- Tipi chiusi (come CHECK; semplici da evolvere)
-- invoice.kind: advance | balance
-- invoice_lines.line_type:
--   commissions | direct_sale | bonuses | enasarco | advance_deduction | other
-- tax_payments.type:
--   inps | bankAccount | bolloFatture | cameraCommercio | incomeTax | strumenti
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS year_quotas (
    year INTEGER PRIMARY KEY,
    -- Coefficiente di imponibilità sul fatturato (es. 0.62 forfettario)
    imponibile_rate NUMERIC(12, 8) NOT NULL DEFAULT 0.62,
    inps_discount_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    inps_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    enasarco_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    enasarco_max NUMERIC(14, 2),
    inps_min_base NUMERIC(14, 2),
    income_tax_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    inps_advance_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    income_tax_advance_rate NUMERIC(12, 8) NOT NULL DEFAULT 0,
    firebase_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS invoices (
    id BIGSERIAL PRIMARY KEY,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('advance', 'balance')),
    issue_date DATE,
    payment_date DATE,
    -- Anno della numerazione (es. fattura 2026/2), distinto dalla competenza
    series_year INTEGER NOT NULL,
    reference_year INTEGER NOT NULL,
    reference_month INTEGER NOT NULL CHECK (reference_month BETWEEN 1 AND 12),
    notes TEXT NOT NULL DEFAULT '',
    -- Importo grezzo da Firebase (lordo enasarco); utile in migrazione / confronto
    legacy_amount NUMERIC(14, 2),
    firebase_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (series_year, number)
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
    line_type TEXT NOT NULL CHECK (
        line_type IN (
            'commissions',
            'direct_sale',
            'bonuses',
            'enasarco',
            'advance_deduction',
            'other'
        )
    ),
    description TEXT NOT NULL DEFAULT '',
    -- Importo con segno: ricavi > 0, detrazioni (enasarco, deduzione acconto) < 0
    amount NUMERIC(14, 2) NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS invoice_lines_invoice_id_idx
    ON invoice_lines (invoice_id);

CREATE TABLE IF NOT EXISTS tax_payments (
    id BIGSERIAL PRIMARY KEY,
    type TEXT NOT NULL CHECK (
        type IN (
            'inps',
            'bankAccount',
            'bolloFatture',
            'cameraCommercio',
            'incomeTax',
            'strumenti'
        )
    ),
    description TEXT NOT NULL DEFAULT '',
    payment_date DATE,
    reference_year INTEGER NOT NULL,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    paid BOOLEAN NOT NULL DEFAULT FALSE,
    firebase_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tax_payments_year_idx
    ON tax_payments (reference_year);

-- Promemoria email già inviati (anti-doppio per soglia 15g / 7g)
CREATE TABLE IF NOT EXISTS payment_reminders_sent (
    payment_id BIGINT NOT NULL REFERENCES tax_payments(id) ON DELETE CASCADE,
    days_before INTEGER NOT NULL CHECK (days_before > 0),
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (payment_id, days_before)
);

-- Promemoria fissi annuali (1 giu Camera Commercio, 10 gen rate INPS)
CREATE TABLE IF NOT EXISTS calendar_reminders_sent (
    reminder_key TEXT NOT NULL,
    year INTEGER NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (reminder_key, year)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id BIGSERIAL PRIMARY KEY,
    withdrawal_date DATE,
    reference_year INTEGER NOT NULL,
    reference_month INTEGER NOT NULL CHECK (reference_month BETWEEN 1 AND 12),
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    firebase_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS withdrawals_period_idx
    ON withdrawals (reference_year, reference_month);

-- Vista di comodo: totale fattura dalle righe (+ legacy se senza righe)
CREATE OR REPLACE VIEW invoice_totals AS
SELECT
    i.id,
    i.number,
    i.kind,
    i.reference_year,
    i.reference_month,
    i.legacy_amount,
    COALESCE(SUM(l.amount), 0) AS lines_total,
    CASE
        WHEN COUNT(l.id) = 0 THEN i.legacy_amount
        ELSE COALESCE(SUM(l.amount), 0)
    END AS effective_total
FROM invoices i
LEFT JOIN invoice_lines l ON l.invoice_id = i.id
GROUP BY i.id;

-- RLS: allineato al resto dell'app (accesso via connection string postgres)
ALTER TABLE year_quotas ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
ALTER TABLE invoice_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE tax_payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE payment_reminders_sent ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_reminders_sent ENABLE ROW LEVEL SECURITY;
ALTER TABLE withdrawals ENABLE ROW LEVEL SECURITY;
