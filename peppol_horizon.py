"""
peppol_horizon.py
=================
A dependency-free, deterministic builder for Peppol BIS Billing 3.0 (UBL 2.1)
invoices, tuned to be accepted by Visma Horizon (horizon.lv).

Design principle
----------------
This module NEVER guesses or parses. It turns *already-structured* data
(dataclasses) into exactly-correct XML, and then validates the math. Keep the
unreliable part (reading a PDF) completely separate from this part (the format
and the arithmetic, which must be exact).

What it handles
---------------
* Multiple VAT categories on one invoice (standard / reverse-charge / zero /
  exempt), grouped into TaxSubtotals correctly.
* "Running-balance" line rounding so the rounded line amounts sum *exactly* to
  the rounded document total — reproducing how most accounting systems (and the
  source invoices we tested) compute totals. Avoids the classic few-cent drift.
* Horizon's recipient-lookup convention: EndpointID uses schemeID "9939" with
  the LV-prefixed VAT number. (Determined empirically against Horizon; it is
  parameterisable in case other targets differ.)

What it does NOT do
-------------------
* Full Peppol schematron validation. The `validate()` here checks the EN16931
  calculation rules (the ones that actually cause wrong numbers) plus
  well-formedness. Before production, also run the official validator once, e.g.
  https://ecosio.com/en/peppol-and-xml-document-validator/ or the EU tool at
  https://www.itb.ec.europa.eu/ . Those require Java/network and are out of
  scope for a pure-stdlib module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _d(x) -> Decimal:
    """Coerce to Decimal via str (avoids binary-float artefacts)."""
    return x if isinstance(x, Decimal) else Decimal(str(x))


def r2(x: Decimal) -> Decimal:
    """Round to 2 decimals, half-up (the rounding used on monetary amounts)."""
    return _d(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Data models
# --------------------------------------------------------------------------- #

@dataclass
class Party:
    """A supplier or customer.

    `reg_number` is the company registration number (no prefix).
    `vat_number` is the VAT payer number (with country prefix, e.g. LV4120...).
    For Horizon, the recipient is matched on the endpoint, which defaults to the
    LV-prefixed VAT number under scheme 9939.
    """
    name: str
    reg_number: str
    vat_number: Optional[str] = None
    street: str = ""
    city: str = ""
    postal_zone: str = ""
    country_code: str = "LV"
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    # Endpoint identification. If left as None, derived from vat_number + 9939.
    endpoint_scheme: Optional[str] = None
    endpoint_id: Optional[str] = None

    def resolved_endpoint(self, default_scheme: str = "9939") -> tuple[str, str]:
        scheme = self.endpoint_scheme or default_scheme
        eid = self.endpoint_id or self.vat_number or self.reg_number
        return scheme, eid

    def address_line(self) -> str:
        parts = [p for p in (self.street, self.city, self.postal_zone,
                             "Latvija" if self.country_code == "LV" else self.country_code)
                 if p]
        return ", ".join(parts)


@dataclass
class Payment:
    iban: str
    bic: Optional[str] = None
    bank_name: Optional[str] = None
    # UBL payment-means code. 30 = credit transfer, 96 = "other".
    means_code: str = "30"
    payment_id: Optional[str] = None  # often the invoice number
    terms_note: Optional[str] = None


@dataclass
class LineItem:
    """A single invoice line.

    `net_unit_price` is the unit price AFTER any line discount, exclusive of VAT
    (this is the "Pārd.c. bez PVN ar atl." column on the LV invoices we handled).
    Quantity and price keep full precision; only the line *amount* is rounded.
    """
    description: str
    quantity: Decimal
    net_unit_price: Decimal
    unit_code: str = "C62"            # C62 = "one/piece"; MTR = metre; etc.
    seller_item_id: Optional[str] = None
    vat_category: str = "S"           # S=standard, AE=reverse charge, Z=zero, E=exempt
    vat_percent: Decimal = Decimal("21")

    def true_net(self) -> Decimal:
        """Unrounded line net = qty * unit price."""
        return _d(self.quantity) * _d(self.net_unit_price)


@dataclass
class Invoice:
    invoice_id: str
    issue_date: str                   # ISO yyyy-mm-dd
    supplier: Party
    customer: Party
    lines: list[LineItem]
    due_date: Optional[str] = None
    currency: str = "EUR"
    buyer_reference: Optional[str] = None   # Horizon uses recipient reg number here
    contract_reference: Optional[str] = None
    note: Optional[str] = None
    payment: Optional[Payment] = None
    delivery_date: Optional[str] = None
    delivery_address: Optional[Party] = None  # reuse Party for an address only
    invoice_type_code: str = "380"    # 380 = commercial invoice
    endpoint_scheme: str = "9939"     # the scheme Horizon expects for LV parties


# --------------------------------------------------------------------------- #
# Rounding: distribute so rounded lines sum to the rounded group total
# --------------------------------------------------------------------------- #

def running_balance_round(true_amounts: list[Decimal]) -> list[Decimal]:
    """Given unrounded line amounts, return 2dp amounts whose sum equals
    r2(sum(true_amounts)). Each result is within 0.01 of its natural rounding.
    """
    out, cum, prev = [], Decimal("0"), Decimal("0")
    for amt in true_amounts:
        cum += amt
        cur = r2(cum)
        out.append(cur - prev)
        prev = cur
    return out


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

NS_DEFAULT = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

_VAT_SCHEME = "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>"


def _esc(v) -> str:
    return escape("" if v is None else str(v))


def _f(x: Decimal) -> str:
    """Format a 2dp monetary amount as a plain string, e.g. '314.01'."""
    return f"{r2(x):.2f}"


def _party_xml(p: Party, tag: str, endpoint_scheme: str) -> str:
    scheme, eid = p.resolved_endpoint(endpoint_scheme)
    contact = ""
    if p.contact_name or p.contact_phone or p.contact_email:
        bits = []
        if p.contact_name:
            bits.append(f"<cbc:Name>{_esc(p.contact_name)}</cbc:Name>")
        if p.contact_phone:
            bits.append(f"<cbc:Telephone>{_esc(p.contact_phone)}</cbc:Telephone>")
        if p.contact_email:
            bits.append(f"<cbc:ElectronicMail>{_esc(p.contact_email)}</cbc:ElectronicMail>")
        contact = f"\n      <cac:Contact>{''.join(bits)}</cac:Contact>"
    tax_scheme = ""
    if p.vat_number:
        tax_scheme = (f"\n      <cac:PartyTaxScheme>"
                      f"<cbc:CompanyID>{_esc(p.vat_number)}</cbc:CompanyID>"
                      f"{_VAT_SCHEME}</cac:PartyTaxScheme>")
    return f"""  <cac:{tag}>
    <cac:Party>
      <cbc:EndpointID schemeID="{_esc(scheme)}">{_esc(eid)}</cbc:EndpointID>
      <cac:PartyName><cbc:Name>{_esc(p.name)}</cbc:Name></cac:PartyName>
      <cac:PostalAddress>
        <cbc:StreetName>{_esc(p.street)}</cbc:StreetName>
        <cbc:CityName>{_esc(p.city)}</cbc:CityName>
        <cbc:PostalZone>{_esc(p.postal_zone)}</cbc:PostalZone>
        <cac:AddressLine><cbc:Line>{_esc(p.address_line())}</cbc:Line></cac:AddressLine>
        <cac:Country><cbc:IdentificationCode>{_esc(p.country_code)}</cbc:IdentificationCode></cac:Country>
      </cac:PostalAddress>{tax_scheme}
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{_esc(p.name)}</cbc:RegistrationName>
        <cbc:CompanyID>{_esc(p.reg_number)}</cbc:CompanyID>
      </cac:PartyLegalEntity>{contact}
    </cac:Party>
  </cac:{tag}>"""


def _exemption_reason(category: str) -> str:
    return {"AE": "Reverse charge", "Z": "Zero rated", "E": "Exempt"}.get(category, "")


def build_invoice_xml(inv: Invoice) -> str:
    """Return a complete UBL Invoice XML string."""
    cur = inv.currency

    # 1. Group lines by (category, percent) and round within each group so the
    #    group total reconciles exactly.
    groups: dict[tuple[str, Decimal], list[int]] = {}
    for i, ln in enumerate(inv.lines):
        groups.setdefault((ln.vat_category, _d(ln.vat_percent)), []).append(i)

    line_net = [Decimal("0")] * len(inv.lines)
    subtotals = []  # (category, percent, taxable, tax)
    for (cat, pct), idxs in groups.items():
        rounded = running_balance_round([inv.lines[i].true_net() for i in idxs])
        for j, i in enumerate(idxs):
            line_net[i] = rounded[j]
        taxable = sum(rounded, Decimal("0"))
        tax = r2(taxable * pct / Decimal("100"))
        subtotals.append((cat, pct, taxable, tax))

    line_extension = sum(line_net, Decimal("0"))
    total_tax = sum(s[3] for s in subtotals)
    tax_exclusive = line_extension
    tax_inclusive = tax_exclusive + total_tax

    # 2. Header references
    head = [f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="{NS_DEFAULT}"
         xmlns:cac="{NS_CAC}"
         xmlns:cbc="{NS_CBC}">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0</cbc:CustomizationID>
  <cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
  <cbc:ID>{_esc(inv.invoice_id)}</cbc:ID>
  <cbc:IssueDate>{_esc(inv.issue_date)}</cbc:IssueDate>"""]
    if inv.due_date:
        head.append(f"  <cbc:DueDate>{_esc(inv.due_date)}</cbc:DueDate>")
    head.append(f"  <cbc:InvoiceTypeCode>{_esc(inv.invoice_type_code)}</cbc:InvoiceTypeCode>")
    if inv.note:
        head.append(f"  <cbc:Note>{_esc(inv.note)}</cbc:Note>")
    head.append(f"  <cbc:DocumentCurrencyCode>{_esc(cur)}</cbc:DocumentCurrencyCode>")
    if inv.buyer_reference:
        head.append(f"  <cbc:BuyerReference>{_esc(inv.buyer_reference)}</cbc:BuyerReference>")
    if inv.contract_reference:
        head.append(f"  <cac:ContractDocumentReference><cbc:ID>{_esc(inv.contract_reference)}</cbc:ID></cac:ContractDocumentReference>")

    parts = ["\n".join(head)]
    parts.append(_party_xml(inv.supplier, "AccountingSupplierParty", inv.endpoint_scheme))
    parts.append(_party_xml(inv.customer, "AccountingCustomerParty", inv.endpoint_scheme))

    # 3. Delivery
    if inv.delivery_date or inv.delivery_address:
        d = ["  <cac:Delivery>"]
        if inv.delivery_date:
            d.append(f"    <cbc:ActualDeliveryDate>{_esc(inv.delivery_date)}</cbc:ActualDeliveryDate>")
        if inv.delivery_address:
            a = inv.delivery_address
            d.append(f"""    <cac:DeliveryLocation>
      <cac:Address>
        <cbc:StreetName>{_esc(a.street)}</cbc:StreetName>
        <cbc:CityName>{_esc(a.city)}</cbc:CityName>
        <cbc:PostalZone>{_esc(a.postal_zone)}</cbc:PostalZone>
        <cac:AddressLine><cbc:Line>{_esc(a.address_line())}</cbc:Line></cac:AddressLine>
        <cac:Country><cbc:IdentificationCode>{_esc(a.country_code)}</cbc:IdentificationCode></cac:Country>
      </cac:Address>
    </cac:DeliveryLocation>""")
        d.append("  </cac:Delivery>")
        parts.append("\n".join(d))

    # 4. Payment
    if inv.payment:
        pm = inv.payment
        pid = f"\n    <cbc:PaymentID>{_esc(pm.payment_id)}</cbc:PaymentID>" if pm.payment_id else ""
        name = f"<cbc:Name>{_esc(pm.bank_name)}</cbc:Name>" if pm.bank_name else ""
        branch = (f"<cac:FinancialInstitutionBranch><cbc:ID>{_esc(pm.bic)}</cbc:ID></cac:FinancialInstitutionBranch>"
                  if pm.bic else "")
        parts.append(f"""  <cac:PaymentMeans>
    <cbc:PaymentMeansCode>{_esc(pm.means_code)}</cbc:PaymentMeansCode>{pid}
    <cac:PayeeFinancialAccount>
      <cbc:ID>{_esc(pm.iban)}</cbc:ID>{name}
      {branch}
    </cac:PayeeFinancialAccount>
  </cac:PaymentMeans>""")
        if pm.terms_note:
            parts.append(f"  <cac:PaymentTerms><cbc:Note>{_esc(pm.terms_note)}</cbc:Note></cac:PaymentTerms>")

    # 5. TaxTotal
    tt = [f'  <cac:TaxTotal>\n    <cbc:TaxAmount currencyID="{cur}">{_f(total_tax)}</cbc:TaxAmount>']
    for cat, pct, taxable, tax in subtotals:
        reason = _exemption_reason(cat)
        reason_xml = f"\n        <cbc:TaxExemptionReason>{_esc(reason)}</cbc:TaxExemptionReason>" if reason else ""
        tt.append(f"""    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="{cur}">{_f(taxable)}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="{cur}">{_f(tax)}</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>{_esc(cat)}</cbc:ID>
        <cbc:Percent>{pct.normalize() if pct == pct.to_integral() else pct}</cbc:Percent>{reason_xml}
        {_VAT_SCHEME}
      </cac:TaxCategory>
    </cac:TaxSubtotal>""")
    tt.append("  </cac:TaxTotal>")
    parts.append("\n".join(tt))

    # 6. LegalMonetaryTotal
    parts.append(f"""  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="{cur}">{_f(line_extension)}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="{cur}">{_f(tax_exclusive)}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="{cur}">{_f(tax_inclusive)}</cbc:TaxInclusiveAmount>
    <cbc:AllowanceTotalAmount currencyID="{cur}">0.00</cbc:AllowanceTotalAmount>
    <cbc:ChargeTotalAmount currencyID="{cur}">0.00</cbc:ChargeTotalAmount>
    <cbc:PrepaidAmount currencyID="{cur}">0.00</cbc:PrepaidAmount>
    <cbc:PayableAmount currencyID="{cur}">{_f(tax_inclusive)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>""")

    # 7. Lines
    for i, ln in enumerate(inv.lines):
        pct = _d(ln.vat_percent)
        sid = (f"\n      <cac:SellersItemIdentification><cbc:ID>{_esc(ln.seller_item_id)}</cbc:ID></cac:SellersItemIdentification>"
               if ln.seller_item_id else "")
        parts.append(f"""  <cac:InvoiceLine>
    <cbc:ID>{i + 1}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="{_esc(ln.unit_code)}">{_d(ln.quantity)}</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="{cur}">{_f(line_net[i])}</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>{_esc(ln.description)}</cbc:Name>{sid}
      <cac:ClassifiedTaxCategory>
        <cbc:ID>{_esc(ln.vat_category)}</cbc:ID>
        <cbc:Percent>{pct.normalize() if pct == pct.to_integral() else pct}</cbc:Percent>
        {_VAT_SCHEME}
      </cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price>
      <cbc:PriceAmount currencyID="{cur}">{_d(ln.net_unit_price)}</cbc:PriceAmount>
      <cbc:BaseQuantity unitCode="{_esc(ln.unit_code)}">1</cbc:BaseQuantity>
    </cac:Price>
  </cac:InvoiceLine>""")

    parts.append("</Invoice>\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Validation (EN16931 calculation rules + well-formedness)
# --------------------------------------------------------------------------- #

@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def validate(xml_str: str) -> ValidationResult:
    """Check well-formedness and the monetary calculation rules that matter.

    This is NOT a substitute for the official Peppol schematron, but it catches
    the errors that produce *wrong numbers* (the ones that matter for booking).
    """
    errors: list[str] = []
    warnings: list[str] = []
    ns = {"cac": NS_CAC, "cbc": NS_CBC}

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        return ValidationResult(False, [f"XML not well-formed: {e}"])

    def dtext(node, path):
        el = node.find(path, ns)
        return Decimal(el.text) if el is not None and el.text else None

    lines = root.findall("cac:InvoiceLine", ns)
    if not lines:
        errors.append("No invoice lines found (BR-16 requires at least one).")

    # BR-CO-10: sum of line nets == LineExtensionAmount
    line_sum = sum((Decimal(l.find("cbc:LineExtensionAmount", ns).text)
                    for l in lines), Decimal("0"))
    lea = dtext(root, "cac:LegalMonetaryTotal/cbc:LineExtensionAmount")
    if lea is not None and lea != line_sum:
        errors.append(f"BR-CO-10: line total {line_sum} != LineExtensionAmount {lea}")

    tex = dtext(root, "cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount")
    tin = dtext(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount")
    pay = dtext(root, "cac:LegalMonetaryTotal/cbc:PayableAmount")
    prepaid = dtext(root, "cac:LegalMonetaryTotal/cbc:PrepaidAmount") or Decimal("0")
    tax_total = dtext(root, "cac:TaxTotal/cbc:TaxAmount")

    if tex is not None and lea is not None and tex != lea:
        errors.append(f"BR-CO-13: TaxExclusiveAmount {tex} != LineExtensionAmount {lea}")
    if tin is not None and tex is not None and tax_total is not None and tin != tex + tax_total:
        errors.append(f"BR-CO-15: TaxInclusive {tin} != TaxExclusive {tex} + Tax {tax_total}")
    if pay is not None and tin is not None and pay != tin - prepaid:
        errors.append(f"BR-CO-16: PayableAmount {pay} != TaxInclusive {tin} - Prepaid {prepaid}")

    # Per-subtotal: TaxAmount == round(Taxable * percent/100); and sum reconciles
    subtotal_tax_sum = Decimal("0")
    for st in root.findall("cac:TaxTotal/cac:TaxSubtotal", ns):
        taxable = Decimal(st.find("cbc:TaxableAmount", ns).text)
        tax = Decimal(st.find("cbc:TaxAmount", ns).text)
        pct = Decimal(st.find("cac:TaxCategory/cbc:Percent", ns).text)
        subtotal_tax_sum += tax
        expected = r2(taxable * pct / Decimal("100"))
        if tax != expected:
            errors.append(f"BR-CO-17: TaxAmount {tax} != round({taxable}*{pct}%)={expected}")
    if tax_total is not None and subtotal_tax_sum != tax_total:
        errors.append(f"TaxTotal {tax_total} != sum of subtotal taxes {subtotal_tax_sum}")

    # Recipient endpoint present (the field Horizon matches on)
    ep = root.find("cac:AccountingCustomerParty/cac:Party/cbc:EndpointID", ns)
    if ep is None or not (ep.text or "").strip():
        errors.append("Recipient EndpointID missing — Horizon will fail to find the company.")
    elif ep.get("schemeID") != "9939":
        warnings.append(f"Recipient EndpointID schemeID is '{ep.get('schemeID')}', "
                        f"not '9939' — Horizon expected 9999/9939-style LV VAT lookup.")

    return ValidationResult(not errors, errors, warnings)
