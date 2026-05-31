"""End-to-end test with a MOCK extractor (no API key / network needed).
Proves: mapping, reconciliation (clean + catching a misread), build, validate,
and the FastAPI endpoints via TestClient."""
from decimal import Decimal
from example_diana import RAW          # reuse the 67 real lines
import app as appmod
from fastapi.testclient import TestClient

D = Decimal
def q4(x): return str(D(x).quantize(D("0.0001")))

def build_extraction(corrupt=False):
    lines = []
    for idx, (desc, qty, price, unit, sid) in enumerate(RAW):
        p = price
        if corrupt and idx == 6:        # corrupt line 7: misread price digit
            p = "0.94050"               # was 0.14050
        plt = q4(D(str(qty)) * D(p))
        lines.append({
            "description": desc, "seller_item_id": sid,
            "quantity": str(qty), "unit_code": unit,
            "net_unit_price": p, "vat_category": "S", "vat_percent": "21",
            "printed_line_total": q4(D(str(qty)) * D(price)),  # printed = CORRECT value
        })
        if corrupt and idx == 6:
            # keep printed_line_total at the correct printed figure so the
            # mismatch between (misread price * qty) and printed is detectable
            pass
    return {
        "invoice_id": "DIA 863689", "issue_date": "2026-03-31",
        "due_date": "2026-04-10", "currency": "EUR",
        "buyer_reference": "54103007931", "contract_reference": "VB 035-001/2023",
        "supplier": {"name": "A/S Diāna", "reg_number": "41203000447",
                     "vat_number": "LV41203000447", "street": "Andreja iela 5",
                     "city": "Ventspils", "country_code": "LV",
                     "contact_name": "Māris Šļivka", "contact_phone": "25414718"},
        "customer": {"name": "4 PLUS SIA", "reg_number": "54103007931",
                     "vat_number": "LV54103007931", "street": "Abula iela 6B",
                     "city": "Valmiera, Valmieras nov.", "postal_zone": "LV-4201",
                     "country_code": "LV"},
        "delivery": {"street": "Merķeļa iela 20", "city": "Alūksne, Alūksnes nov.",
                     "postal_zone": "LV-4301", "country_code": "LV"},
        "delivery_date": "2026-03-31",
        "payment": {"iban": "LV60UNLA0055002704058", "bic": "UNLALV2X",
                    "bank_name": "SEB Banka AS", "payment_id": "DIA 863689",
                    "terms_note": "Apmaksāt līdz 10.04.2026"},
        "lines": lines,
        "printed_totals": {"net_excl_vat": "314.01", "total_vat": "65.94",
                           "grand_total_incl_vat": "379.95"},
    }

# ---- 1. clean extraction: expect no flags, valid XML, correct totals ----
appmod.app.state.extractor = lambda pdf: build_extraction(corrupt=False)
client = TestClient(appmod.app)

r = client.post("/extract", files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")})
data = r.json()
print("== CLEAN ==")
print(" status:", r.status_code, "| ok:", data["ok"], "| flags:", len(data["flags"]))
print(" computed totals:", data["computed_totals"])

g = client.post("/generate", json=data["extraction"])
print(" /generate status:", g.status_code, "| content-type:", g.headers["content-type"])
print(" xml starts:", g.text[:38])

# ---- 2. corrupted extraction: expect the cross-check to catch line 7 ----
appmod.app.state.extractor = lambda pdf: build_extraction(corrupt=True)
r2 = client.post("/extract", files={"file": ("x.pdf", b"%PDF-1.4 fake", "application/pdf")})
d2 = r2.json()
print("== CORRUPTED (line 7 price misread) ==")
print(" ok:", d2["ok"])
for f in d2["flags"]:
    print("  flag:", f["level"], f["code"], "->", f["message"][:75])

g2 = client.post("/generate", json=d2["extraction"])
print(" /generate status:", g2.status_code, "(422 = correctly blocked)")