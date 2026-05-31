"""
pipeline.py
===========
Glue between the LLM extraction (untrusted) and the deterministic builder.

  map_to_invoice()  : extraction dict  -> Invoice dataclass
  reconcile()       : compares the extracted LINE ITEMS against the extracted
                      PRINTED TOTALS, and flags any line or total that doesn't
                      add up. This is the guardrail that turns "the LLM might
                      misread a digit" into "the suspect line is highlighted in
                      red on the review screen".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from peppol_horizon import (
    Party, Payment, LineItem, Invoice, r2, running_balance_round, _d,
)

# Per-line tolerance: qty*price vs printed line total. A misread digit blows
# way past a cent; legitimate rounding never does.
LINE_TOL = Decimal("0.01")
# Document tolerance: allows for a supplier using per-line rounding instead of
# round-at-the-end. Beyond this, the user should look.
DOC_TOL = Decimal("0.02")


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

def _party(d: Optional[dict]) -> Party:
    d = d or {}
    return Party(
        name=d.get("name") or "",
        reg_number=d.get("reg_number") or "",
        vat_number=d.get("vat_number"),
        street=d.get("street") or "",
        city=d.get("city") or "",
        postal_zone=d.get("postal_zone") or "",
        country_code=d.get("country_code") or "LV",
        contact_name=d.get("contact_name"),
        contact_phone=d.get("contact_phone"),
        contact_email=d.get("contact_email"),
    )


def map_to_invoice(ext: dict[str, Any]) -> Invoice:
    """Convert an extraction dict into an Invoice dataclass."""
    supplier = _party(ext.get("supplier"))
    customer = _party(ext.get("customer"))

    delivery_addr = None
    if ext.get("delivery"):
        dv = ext["delivery"]
        delivery_addr = Party(
            name="", reg_number="",
            street=dv.get("street") or "",
            city=dv.get("city") or "",
            postal_zone=dv.get("postal_zone") or "",
            country_code=dv.get("country_code") or "LV",
        )

    payment = None
    if ext.get("payment") and (ext["payment"].get("iban")):
        pm = ext["payment"]
        payment = Payment(
            iban=pm.get("iban"),
            bic=pm.get("bic"),
            bank_name=pm.get("bank_name"),
            payment_id=pm.get("payment_id"),
            terms_note=pm.get("terms_note"),
        )

    lines = []
    for ln in ext.get("lines", []):
        lines.append(LineItem(
            description=ln.get("description") or "",
            quantity=_d(_dec(ln.get("quantity")) or Decimal("0")),
            net_unit_price=_d(_dec(ln.get("net_unit_price")) or Decimal("0")),
            unit_code=ln.get("unit_code") or "C62",
            seller_item_id=ln.get("seller_item_id"),
            vat_category=ln.get("vat_category") or "S",
            vat_percent=_d(_dec(ln.get("vat_percent")) or Decimal("0")),
        ))

    return Invoice(
        invoice_id=ext.get("invoice_id") or "",
        issue_date=ext.get("issue_date") or "",
        due_date=ext.get("due_date"),
        currency=ext.get("currency") or "EUR",
        buyer_reference=ext.get("buyer_reference") or (customer.reg_number or None),
        contract_reference=ext.get("contract_reference"),
        note=ext.get("note"),
        supplier=supplier,
        customer=customer,
        lines=lines,
        payment=payment,
        delivery_date=ext.get("delivery_date"),
        delivery_address=delivery_addr,
    )


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

@dataclass
class Flag:
    level: str          # "error" | "warning"
    code: str
    message: str
    line_index: Optional[int] = None   # 0-based, if line-specific


@dataclass
class ReconcileResult:
    flags: list[Flag] = field(default_factory=list)
    computed_net: Decimal = Decimal("0")
    computed_vat: Decimal = Decimal("0")
    computed_total: Decimal = Decimal("0")

    @property
    def ok(self) -> bool:
        return not any(f.level == "error" for f in self.flags)


def reconcile(invoice: Invoice, ext: dict[str, Any]) -> ReconcileResult:
    """Cross-check extracted lines against extracted printed totals."""
    flags: list[Flag] = []
    raw_lines = ext.get("lines", [])

    # --- per-line: qty*price vs printed line total ---
    for i, (li, raw) in enumerate(zip(invoice.lines, raw_lines)):
        printed = _dec(raw.get("printed_line_total"))
        if printed is None:
            continue
        calc = r2(li.true_net())
        if abs(calc - r2(printed)) > LINE_TOL:
            flags.append(Flag(
                "error", "LINE_MISMATCH",
                f"Line {i+1} '{li.description[:40]}': qty×price = {calc} but "
                f"invoice shows {r2(printed)}. Check quantity/price digits.",
                line_index=i,
            ))

    # --- document totals: group by VAT, running-balance round (same as builder) ---
    groups: dict[tuple[str, Decimal], list[int]] = {}
    for i, ln in enumerate(invoice.lines):
        groups.setdefault((ln.vat_category, _d(ln.vat_percent)), []).append(i)

    net = Decimal("0")
    vat = Decimal("0")
    for (cat, pct), idxs in groups.items():
        rounded = running_balance_round([invoice.lines[i].true_net() for i in idxs])
        taxable = sum(rounded, Decimal("0"))
        net += taxable
        vat += r2(taxable * pct / Decimal("100"))
    total = net + vat

    pt = ext.get("printed_totals") or {}
    p_net = _dec(pt.get("net_excl_vat"))
    p_vat = _dec(pt.get("total_vat"))
    p_total = _dec(pt.get("grand_total_incl_vat"))

    if p_net is not None and abs(net - r2(p_net)) > DOC_TOL:
        flags.append(Flag("error", "NET_MISMATCH",
                          f"Computed net {net} vs printed net {r2(p_net)} "
                          f"(Δ {abs(net - r2(p_net))}). A line is likely misread."))
    if p_vat is not None and abs(vat - r2(p_vat)) > DOC_TOL:
        flags.append(Flag("warning", "VAT_MISMATCH",
                          f"Computed VAT {vat} vs printed VAT {r2(p_vat)} "
                          f"(Δ {abs(vat - r2(p_vat))})."))
    if p_total is not None and abs(total - r2(p_total)) > DOC_TOL:
        flags.append(Flag("error", "TOTAL_MISMATCH",
                          f"Computed total {total} vs printed total {r2(p_total)} "
                          f"(Δ {abs(total - r2(p_total))})."))

    # Missing essentials that Horizon needs
    if not invoice.customer.vat_number and not invoice.customer.reg_number:
        flags.append(Flag("error", "NO_RECIPIENT_ID",
                          "Recipient has no VAT/registration number — Horizon "
                          "cannot match the company."))
    if not invoice.lines:
        flags.append(Flag("error", "NO_LINES", "No line items were extracted."))

    return ReconcileResult(flags, net, vat, total)
