"""
Calcoli contabili:
- fissa mese = minimale INPS / 12
- per fattura: acconto (var × % acconto) + saldo (extra sopra F(Y−1))
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

CERTAIN_RESERVE_MONTHS = 12


@dataclass(frozen=True)
class Quotas:
    imponibile_rate: float  # % di fatturato che è imponibile (es. 0.62)
    inps_discount_rate: float
    inps_rate: float
    income_tax_rate: float
    inps_advance_rate: float
    income_tax_advance_rate: float
    inps_min_base: Optional[float] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Quotas":
        if row.get("imponibile_rate") is not None:
            imponibile = float(row.get("imponibile_rate") or 0)
        else:
            # Retrocompatibilità: vecchio tax_exempt_rate → imponibile = 1 − esente
            imponibile = 1.0 - float(row.get("tax_exempt_rate") or 0)
        min_base = row.get("inps_min_base")
        return cls(
            imponibile_rate=imponibile,
            inps_discount_rate=float(row.get("inps_discount_rate") or 0),
            inps_rate=float(row.get("inps_rate") or 0),
            income_tax_rate=float(row.get("income_tax_rate") or 0),
            inps_advance_rate=float(row.get("inps_advance_rate") or 0),
            income_tax_advance_rate=float(row.get("income_tax_advance_rate") or 0),
            inps_min_base=float(min_base) if min_base is not None else None,
        )


@dataclass(frozen=True)
class GrossBreakdown:
    """Breakdown 'pieno' (sopra soglia): usato per saldo/acconto annuali."""

    lordo: float
    esente: float
    imponibile: float
    inps_dovuto: float
    imponibile_reddito: float
    imposta_reddito: float

    @property
    def imponibile_inps(self) -> float:
        return self.imponibile


@dataclass(frozen=True)
class AnnualTax:
    year: int
    fatturato: float
    fatturato_prev: float
    surplus: float
    inps_saldo: float
    inps_acconto: float
    inps_totale: float
    ir_saldo: float
    ir_acconto: float
    ir_totale: float


@dataclass(frozen=True)
class InvoiceReserve:
    """
    Accantonamento su una fattura incassata.

    - var (INPS/IR) = componente acconto (× % acconto)
    - extra (INPS/IR) = componente saldo sul surplus vs F(Y−1)
    """

    amount: float
    f_ytd_before: float
    fatturato_prev_year: float
    threshold_fatturato: float
    amount_below_threshold: float
    amount_above_threshold: float
    amount_surplus: float
    inps_var: float
    ir_var: float
    inps_extra: float
    ir_extra: float
    totale: float  # var + extra (senza enasarco)


def attribution_year(
    issue_date: Optional[date],
    payment_date: Optional[date],
) -> Optional[int]:
    """Anno di attribuzione fiscale: solo se c’è data di pagamento (incasso)."""
    if payment_date is not None:
        return int(payment_date.year)
    return None


def attribution_year_month(
    issue_date: Optional[date],
    payment_date: Optional[date],
) -> Optional[tuple[int, int]]:
    """Mese di incasso: solo fatture con payment_date."""
    if payment_date is None:
        return None
    return int(payment_date.year), int(payment_date.month)


def attribution_sort_date(
    issue_date: Optional[date],
    payment_date: Optional[date],
) -> date:
    if payment_date is not None:
        return payment_date
    if issue_date is not None:
        return issue_date
    return date.min


def fatturato_threshold(quotas: Quotas) -> float:
    """
    Fatturato oltre il quale il reddito imponibile supera il minimale INPS.
    Se minimale assente → 0 (tutto trattato come 'sopra soglia').
    """
    if quotas.inps_min_base is None or quotas.inps_min_base <= 0:
        return 0.0
    if quotas.imponibile_rate <= 0:
        return 0.0
    return float(quotas.inps_min_base) / float(quotas.imponibile_rate)


def breakdown_from_lordo(lordo: float, quotas: Quotas) -> GrossBreakdown:
    """Tributi 'pieni' su un lordo (come sopra soglia minimale)."""
    lordo = float(lordo)
    imponibile = lordo * quotas.imponibile_rate
    esente = lordo - imponibile
    inps_dovuto = (
        imponibile * quotas.inps_rate * (1.0 - quotas.inps_discount_rate)
    )
    imponibile_reddito = imponibile - inps_dovuto
    imposta_reddito = imponibile_reddito * quotas.income_tax_rate
    return GrossBreakdown(
        lordo=lordo,
        esente=esente,
        imponibile=imponibile,
        inps_dovuto=inps_dovuto,
        imponibile_reddito=imponibile_reddito,
        imposta_reddito=imposta_reddito,
    )


def tax_inps_on_lordo(lordo: float, quotas: Quotas) -> float:
    """INPS pieno su un pezzo di fatturato."""
    lordo = float(lordo)
    if lordo <= 0:
        return 0.0
    return (
        lordo
        * quotas.imponibile_rate
        * quotas.inps_rate
        * (1.0 - quotas.inps_discount_rate)
    )


def tax_ir_on_lordo(lordo: float, inps: float, quotas: Quotas) -> float:
    """IR su un pezzo di fatturato, dopo deduzione INPS."""
    lordo = float(lordo)
    if lordo <= 0:
        return 0.0
    imponibile = lordo * quotas.imponibile_rate
    return max(0.0, imponibile - float(inps)) * quotas.income_tax_rate


def annual_taxes(
    year: int,
    fatturato: float,
    fatturato_prev: float,
    quotas: Quotas,
) -> AnnualTax:
    """
    Saldo sul surplus (max 0) rispetto all'anno precedente;
    acconto = tributo calcolato su (fatturato_anno × % acconto).
    """
    fatturato = float(fatturato)
    fatturato_prev = float(fatturato_prev)
    surplus = max(0.0, fatturato - fatturato_prev)

    on_surplus = breakdown_from_lordo(surplus, quotas)
    on_acconto_inps = breakdown_from_lordo(
        fatturato * quotas.inps_advance_rate, quotas
    )
    on_acconto_ir = breakdown_from_lordo(
        fatturato * quotas.income_tax_advance_rate, quotas
    )

    inps_saldo = on_surplus.inps_dovuto
    inps_acconto = on_acconto_inps.inps_dovuto
    ir_saldo = on_surplus.imposta_reddito
    ir_acconto = on_acconto_ir.imposta_reddito

    return AnnualTax(
        year=year,
        fatturato=fatturato,
        fatturato_prev=fatturato_prev,
        surplus=surplus,
        inps_saldo=inps_saldo,
        inps_acconto=inps_acconto,
        inps_totale=inps_saldo + inps_acconto,
        ir_saldo=ir_saldo,
        ir_acconto=ir_acconto,
        ir_totale=ir_saldo + ir_acconto,
    )


def inps_minimale_annuo(quotas: Quotas) -> float:
    """INPS annuale sul minimale: min_base × aliquota × (1 − sconto)."""
    if quotas.inps_min_base is None or quotas.inps_min_base <= 0:
        return 0.0
    return (
        float(quotas.inps_min_base)
        * float(quotas.inps_rate)
        * (1.0 - float(quotas.inps_discount_rate))
    )


def fixed_inps_month(quotas: Quotas) -> float:
    """Quota fissa INPS mensile = minimale annuale / 12."""
    return inps_minimale_annuo(quotas) / float(CERTAIN_RESERVE_MONTHS)


def as_of_month(today: Optional[date] = None) -> tuple[int, int]:
    """Mese corrente (anno, mese), limite superiore per la fissa."""
    d = today if today is not None else date.today()
    return int(d.year), int(d.month)


def iter_year_months(
    start: tuple[int, int],
    end: tuple[int, int],
):
    """Genera (anno, mese) da start a end inclusi."""
    y, m = int(start[0]), int(start[1])
    y_end, m_end = int(end[0]), int(end[1])
    while (y, m) <= (y_end, m_end):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def applies_fixed_inps(
    year: int,
    month: int,
    activity_start: Optional[tuple[int, int]],
    as_of: Optional[tuple[int, int]] = None,
) -> bool:
    """
    INPS fissa: dal primo mese di attività fino al mese corrente (incluso).
    Inclusi i mesi magri; esclusi mesi prima dell'inizio e mesi futuri.
    """
    if activity_start is None:
        return False
    if as_of is None:
        as_of = as_of_month()
    key = (int(year), int(month))
    return activity_start <= key <= as_of


def fixed_inps_months_in_year(
    year: int,
    activity_start: Optional[tuple[int, int]],
    as_of: Optional[tuple[int, int]] = None,
) -> int:
    """Quanti mesi dell'anno y rientrano nella finestra fissa."""
    return sum(
        1
        for m in range(1, 13)
        if applies_fixed_inps(year, m, activity_start, as_of)
    )


def invoice_reserve(
    amount: float,
    f_ytd_before: float,
    fatturato_prev_year: float,
    quotas: Quotas,
) -> InvoiceReserve:
    """
    Accantonamento su un incasso.

    INPS/IR var (acconto):
      - INPS solo sulla parte sopra soglia minimale, × % acconto INPS
      - IR su tutta la fattura (imponibile − INPS pieno sopra soglia) × % acconto IR
    INPS/IR extra (saldo):
      - solo sulla parte che, unita allo YTD, supera F(Y−1), aliquota piena
    """
    amount = float(amount)
    f_ytd_before = float(f_ytd_before)
    fatturato_prev_year = float(fatturato_prev_year)
    thr = fatturato_threshold(quotas)

    below_thr_room = max(0.0, thr - f_ytd_before)
    amount_below = min(max(0.0, amount), below_thr_room)
    amount_above = max(0.0, amount - amount_below)

    below_prev_room = max(0.0, fatturato_prev_year - f_ytd_before)
    amount_surplus = max(0.0, amount - below_prev_room)

    inps_above_full = tax_inps_on_lordo(amount_above, quotas)
    inps_var = inps_above_full * quotas.inps_advance_rate
    ir_full = tax_ir_on_lordo(amount, inps_above_full, quotas)
    ir_var = ir_full * quotas.income_tax_advance_rate

    inps_extra = tax_inps_on_lordo(amount_surplus, quotas)
    ir_extra = tax_ir_on_lordo(amount_surplus, inps_extra, quotas)

    totale = inps_var + ir_var + inps_extra + ir_extra
    return InvoiceReserve(
        amount=amount,
        f_ytd_before=f_ytd_before,
        fatturato_prev_year=fatturato_prev_year,
        threshold_fatturato=thr,
        amount_below_threshold=amount_below,
        amount_above_threshold=amount_above,
        amount_surplus=amount_surplus,
        inps_var=inps_var,
        ir_var=ir_var,
        inps_extra=inps_extra,
        ir_extra=ir_extra,
        totale=totale,
    )
