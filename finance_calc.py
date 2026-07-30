"""
Calcoli contabili: lordo, esente, INPS, imponibile/imposta reddito,
saldo (surplus) e acconto annuali.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class Quotas:
    tax_exempt_rate: float
    inps_discount_rate: float
    inps_rate: float
    income_tax_rate: float
    inps_advance_rate: float
    income_tax_advance_rate: float

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Quotas":
        return cls(
            tax_exempt_rate=float(row.get("tax_exempt_rate") or 0),
            inps_discount_rate=float(row.get("inps_discount_rate") or 0),
            inps_rate=float(row.get("inps_rate") or 0),
            income_tax_rate=float(row.get("income_tax_rate") or 0),
            inps_advance_rate=float(row.get("inps_advance_rate") or 0),
            income_tax_advance_rate=float(row.get("income_tax_advance_rate") or 0),
        )


@dataclass(frozen=True)
class GrossBreakdown:
    lordo: float
    esente: float
    imponibile_inps: float
    inps_dovuto: float
    imponibile_reddito: float
    imposta_reddito: float

    @property
    def netto_dopo_tributi(self) -> float:
        """Lordo già al netto enasarco in fattura, meno INPS e IR di competenza."""
        return self.lordo - self.inps_dovuto - self.imposta_reddito


@dataclass(frozen=True)
class AnnualTax:
    year: float
    fatturato: float
    fatturato_prev: float
    surplus: float
    inps_saldo: float
    inps_acconto: float
    inps_totale: float
    ir_saldo: float
    ir_acconto: float
    ir_totale: float


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


def breakdown_from_lordo(lordo: float, quotas: Quotas) -> GrossBreakdown:
    lordo = float(lordo)
    esente = lordo * quotas.tax_exempt_rate
    imponibile_inps = lordo * (1.0 - quotas.tax_exempt_rate)
    inps_dovuto = (
        imponibile_inps * quotas.inps_rate * (1.0 - quotas.inps_discount_rate)
    )
    imponibile_reddito = imponibile_inps - inps_dovuto
    imposta_reddito = imponibile_reddito * quotas.income_tax_rate
    return GrossBreakdown(
        lordo=lordo,
        esente=esente,
        imponibile_inps=imponibile_inps,
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
    Le formule sono lineari → equivalente a % × tributo su fatturato pieno.
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
