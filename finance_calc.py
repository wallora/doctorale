"""
Calcoli contabili: riserva certa (da anno precedente / 12),
riserva variabile (soglia minimale INPS), saldo/acconto annuali.
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

    # Alias per compatibilità con codice che usava imponibile_inps
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
class CertainReserve:
    """
    Riserva certa mensile = obblighi annuali / 12.

    INPS: max(obbligo da F(Y−1), minimale INPS anno corrente)
    IR: solo obbligo da F(Y−1)
    """

    reference_year: int  # anno in cui si accantona (anno corrente)
    source_year: int  # anno fatturato che determina l'obbligo (Y-1)
    inps_from_prev: float  # saldo+acconto INPS su F(Y-1)
    inps_minimale: float  # inps_min_base × rate × (1−sconto), quote anno corrente
    inps_annual: float  # max(from_prev, minimale) — base usata per /12
    ir_annual: float
    inps_month: float
    ir_month: float
    annual: AnnualTax
    used_minimale: bool


@dataclass(frozen=True)
class VariableReserve:
    """Accantonamento variabile su un importo di fatturato, con split soglia."""

    amount: float
    f_ytd_before: float
    threshold_fatturato: float
    amount_below: float
    amount_above: float
    inps: float
    ir: float
    pct_effective: float


def attribution_year(
    issue_date: Optional[date],
    payment_date: Optional[date],
) -> Optional[int]:
    """Anno quote/aggregati: pagamento se presente, altrimenti emissione."""
    if payment_date is not None:
        return int(payment_date.year)
    if issue_date is not None:
        return int(issue_date.year)
    return None


def attribution_year_month(
    issue_date: Optional[date],
    payment_date: Optional[date],
) -> Optional[tuple[int, int]]:
    """Mese di incasso: pagamento se presente, altrimenti emissione."""
    d = payment_date if payment_date is not None else issue_date
    if d is None:
        return None
    return int(d.year), int(d.month)


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


def certain_reserve(
    current_year: int,
    fatturato_by_year: Mapping[int, float],
    quotas_for_source_year: Quotas,
    quotas_for_current_year: Optional[Quotas] = None,
) -> CertainReserve:
    """
    Riserva certa per l'anno corrente, divisa per 12.

    - INPS: max(obbligo da F(Y−1), minimale INPS con quote dell'anno corrente)
    - IR: solo obbligo da F(Y−1) (saldo surplus + acconto)
    """
    quotas_cur = quotas_for_current_year or quotas_for_source_year
    source_year = current_year - 1
    f_src = float(fatturato_by_year.get(source_year, 0.0))
    f_prev = float(fatturato_by_year.get(source_year - 1, 0.0))
    ann = annual_taxes(source_year, f_src, f_prev, quotas_for_source_year)
    inps_from_prev = float(ann.inps_totale)
    inps_min = inps_minimale_annuo(quotas_cur)
    inps_annual = max(inps_from_prev, inps_min)
    ir_annual = float(ann.ir_totale)
    n = float(CERTAIN_RESERVE_MONTHS)
    return CertainReserve(
        reference_year=current_year,
        source_year=source_year,
        inps_from_prev=inps_from_prev,
        inps_minimale=inps_min,
        inps_annual=inps_annual,
        ir_annual=ir_annual,
        inps_month=inps_annual / n,
        ir_month=ir_annual / n,
        annual=ann,
        used_minimale=inps_annual > inps_from_prev + 1e-9,
    )


def _variable_on_slice(amount: float, above_threshold: bool, quotas: Quotas) -> tuple[float, float]:
    """Restituisce (inps, ir) su un pezzo di fatturato."""
    amount = float(amount)
    if amount <= 0:
        return 0.0, 0.0
    imponibile = amount * quotas.imponibile_rate
    if not above_threshold:
        # Sotto soglia: solo imposta sul reddito (INPS già coperto dal minimale)
        return 0.0, imponibile * quotas.income_tax_rate
    inps = imponibile * quotas.inps_rate * (1.0 - quotas.inps_discount_rate)
    ir = (imponibile - inps) * quotas.income_tax_rate
    return inps, ir


def variable_reserve(
    amount: float,
    f_ytd_before: float,
    quotas: Quotas,
) -> VariableReserve:
    """
    Riserva variabile su un incasso, con split se attraversa la soglia
    (reddito imponibile = fatturato × imponibile_rate vs inps_min_base).
    """
    amount = float(amount)
    f_ytd_before = float(f_ytd_before)
    thr = fatturato_threshold(quotas)
    below_room = max(0.0, thr - f_ytd_before)
    amount_below = min(max(0.0, amount), below_room)
    amount_above = max(0.0, amount - amount_below)
    inps_b, ir_b = _variable_on_slice(amount_below, False, quotas)
    inps_a, ir_a = _variable_on_slice(amount_above, True, quotas)
    inps = inps_b + inps_a
    ir = ir_b + ir_a
    pct = (inps + ir) / amount if amount > 0 else 0.0
    return VariableReserve(
        amount=amount,
        f_ytd_before=f_ytd_before,
        threshold_fatturato=thr,
        amount_below=amount_below,
        amount_above=amount_above,
        inps=inps,
        ir=ir,
        pct_effective=pct,
    )
