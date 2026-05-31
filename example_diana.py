"""
example_diana.py
================
Worked example: reconstruct the Diāna -> 4 PLUS SIA invoice using the
peppol_horizon module, build the XML, validate it, and print the totals.

Run:  python3 example_diana.py
"""

from decimal import Decimal
from peppol_horizon import (
    Party, Payment, LineItem, Invoice, build_invoice_xml, validate,
)

D = Decimal

supplier = Party(
    name="A/S Diāna",
    reg_number="41203000447",
    vat_number="LV41203000447",
    street="Andreja iela 5",
    city="Ventspils",
    country_code="LV",
    contact_name="Māris Šļivka",
    contact_phone="25414718",
)

customer = Party(
    name="4 PLUS SIA",
    reg_number="54103007931",
    vat_number="LV54103007931",
    street="Abula iela 6B",
    city="Valmiera, Valmieras nov.",
    postal_zone="LV-4201",
    country_code="LV",
)

delivery = Party(  # Party reused purely as an address holder
    name="", reg_number="",
    street="Merķeļa iela 20",
    city="Alūksne, Alūksnes nov.",
    postal_zone="LV-4301",
    country_code="LV",
)

payment = Payment(
    iban="LV60UNLA0055002704058",
    bic="UNLALV2X",
    bank_name="SEB Banka AS",
    means_code="30",
    payment_id="DIA 863689",
    terms_note="Apmaksāt līdz 10.04.2026 (10 dienas), pārskaitījums",
)

# (description, qty, net_unit_price_after_discount, unit_code, seller_item_id)
RAW = [
    ("Aerosolkrāsa Maxi Color RAL8019 400ml brūna", 3, "2.75636", "C62", "8711347241859"),
    ("Lāpsta dārza 30x23x120cm tērauda a/k", 1, "8.72000", "C62", "4750959023679"),
    ("Respirators PL FFP1 vārsts 3gb CE", 2, "2.82909", "C62", "5903755460073"),
    ("Marķieris Lyra permanents melns", 1, "1.08364", "C62", "4084900650608"),
    ("Marķieris permanents melns 1gab (50)", 1, "0.50182", "C62", "5901477161568"),
    ("Grīdas aizsargplāksne 396x600mm ZN", 2, "6.53818", "C62", "5906083729348"),
    ("Koka konstr. skrūve TOP 6x120/70 (PZ3+T30) Dz", 100, "0.14050", "C62", "53795"),
    ("Baterija Duracell AA 4gb", 1, "3.62909", "C62", "5000394076952"),
    ("Plāksne plastm. 5x34x100mm zaļa 1gb (1000)", 20, "0.05091", "C62", "56142"),
    ("Plāksne plastm. 8x34x100mm peleka 1gb (1000)", 10, "0.05091", "C62", "56145"),
    ("Ķīlis plastm. 14x29x95mm (500)", 10, "0.06545", "C62", "56080"),
    ("Plāksne plastm. 2x34x100mm zila 1gb (1000)", 20, "0.05091", "C62", "56139"),
    ("Plāksne plastm. 3x34x100mm sarkana 1gb (1000)", 10, "0.05091", "C62", "56140"),
    ("Plāksne plastm. 7x34x100mm bruna 1gb (1000)", 10, "0.05091", "C62", "56144"),
    ("Plāksne plastm. 4x34x100mm dzeltena 1gb (1000)", 10, "0.05091", "C62", "56141"),
    ("Zāģripa D165x20x2mm 40 zobi Makita", 1, "11.48364", "C62", "88381173681"),
    ("Stiprinājums-kurpe WB23 76x122x75x2mm brusu,siju ārējais", 2, "1.22909", "C62", "5907708145239"),
    ("Kokskr.ar sešk.galvu 571-8x100", 4, "0.18182", "C62", "46757"),
    ("Kokskr.ar sešk.galvu 571-8x60", 4, "0.12397", "C62", "46769"),
    ("Enkurskrūve leņķiem 5x40mm TX, pretrūs.pārkl.Ruspert", 10, "0.04959", "C62", "54627"),
    ("Stiprinājums-kurpe WB23 76x122x75x2mm brusu,siju ārējais", 2, "1.22909", "C62", "5907708145239"),
    ("Koka konstr. skrūve TOP plata g. 8x120/54 Dz TX40", 50, "0.27273", "C62", "54613"),
    ("Atkritumu maisi ar auklu 60L 10gb ECO (40)", 4, "1.15636", "C62", "5903355124030"),
    ("Atkritumu maisi īpaši izturīgi 250L 5gb 60mkr (20)", 2, "3.48364", "C62", "4750707008361"),
    ("Asmeņi 18mm FatMax 10gb Stanley", 1, "3.19273", "C62", "3253562117182"),
    ("Respirators Lahti FFP1 vārsts 3gb CE", 2, "3.48364", "C62", "5903755061171"),
    ("Putas īpaši ātra sacietēšana FastFoam 123 870ml", 1, "4.95041", "C62", "4743307118479"),
    ("Silikonespray Glidex 400ml, aerosols", 1, "6.53818", "C62", "5708923907219"),
    ("Elektroinst.caurule gofrēta ar trosi d32 320N", 4, "0.72000", "MTR", "8699430115020"),
    ("Caurule ar uzmavu 50/2000", 1, "2.43802", "C62", "5904215775201"),
    ("Stiprinājums ar gumijām 2 ½ ar skrūv.", 2, "1.08364", "C62", "8027830155535"),
    ("Cementa java ZM 40kg", 1, "4.78512", "C62", "4751006560055"),
    ("Atdure durvīm ar atsperi ST 250x14,0 mm tērauda", 2, "1.37455", "C62", "5907708131867"),
    ("Rokturis skavveida UN 150 mm tērauda misiņa", 2, "0.93818", "C62", "5907708188403"),
    ("Birste M14 75mm koniska", 1, "2.46545", "C62", "5903755325075"),
    ("Birste M14 65mm koniska žņaugveida", 1, "3.04727", "C62", "5903755325761"),
    ("Vēdeklveida slīp. disks 125x22mm A60 Prox Expert", 2, "1.08364", "C62", "4750959122983"),
    ("Tents 1.7x2m 110gr", 2, "1.39669", "C62", "4750959061121"),
    ("Koka konstr. skrūve ar kateri dz.c. 4x45 TX20, 50gb", 2, "1.01091", "C62", "4752099150345"),
    ("Koka konstr. skrūve TOP 4.0x45/30 (PZ2+T20) Dz", 40, "0.02182", "C62", "53780"),
    ("Kelle mūrnieku 14x8cm 2K nerūs.tēr. (6)", 2, "2.82909", "C62", "5905061046491"),
    ("Cementa java ZM 25kg", 4, "3.46281", "C62", "4751006562059"),
    ("Līdz.motora mazgāšanai ar smidzinātāju 1L POLAR", 3, "6.24727", "C62", "6413040000567"),
    ("Cimdi neilona ar mikroputu nitrila pārkl. 9.izm", 1, "1.47934", "PR", "4750959131084"),
    ("Cimdi neilona ar mikroputu nitrila pārkl. 10.izm", 1, "1.47934", "PR", "4750959131091"),
    ("Cementa java ZM 25kg", 4, "3.46281", "C62", "4751006562059"),
    ("Špaktele 2K nerūs. 50mm", 5, "1.08364", "C62", "5903755311726"),
    ("Cementa java ZM 25kg", 3, "3.46281", "C62", "4751006562059"),
    ("Cementa java ZM 25kg", 1, "3.46281", "C62", "4751006562059"),
    ("Vītņu blīvējamais Loctite 50m", 1, "5.08364", "C62", "4100630374703"),
    ("Putas 650ml Filling Foam 212", 1, "3.87603", "C62", "4743307160362"),
    ("Vējs.mazg.šķidrums vasaras BESK! 4l", 1, "1.66545", "C62", "4750959068823"),
    ("Vēdeklveida slīp. disks 125x22mm A36 Prox Expert", 3, "1.08364", "C62", "4750959122969"),
    ("Zāģa asmens Carbon G-Man 300mm 2gb metālam", 1, "1.44727", "C62", "7392746540128"),
    ("Zāģa asmens Carbon G-Man 300mm 2gb metālam", 4, "1.44727", "C62", "7392746540128"),
    ("Ota plakana Nr.30 2.0 koka rokt.(12)", 5, "0.53091", "C62", "5905061034986"),
    ("Slīpdisks Prox Expert 125x22mm tirišanai un krāsu nonemš.", 1, "4.35636", "C62", "4750959123072"),
    ("Koka konstr. Skrūve EASY-FIX 045X050 TORX20 (500)", 1, "11.90083", "C62", "4750874029923"),
    ("Koka konstr. Skrūve EASY-FIX 045X040 TORX20 (500)", 1, "10.65289", "C62", "4750874029893"),
    ("Līmlenta elektroiz. auduma 19mm/15m", 3, "1.66545", "C62", "4750707006770"),
    ("Līmlenta elektroiz. auduma PET Poly 19mm/25m", 2, "2.61091", "C62", "4750707011484"),
    ("Urbis kokam Bosch 8x110mm", 2, "2.17455", "C62", "3165140059114"),
    ("Plāksne plastm. 2x34x100mm zila 1gb (1000)", 20, "0.05091", "C62", "56139"),
    ("Plāksne plastm. 3x34x100mm sarkana 1gb (1000)", 20, "0.05091", "C62", "56140"),
    ("Baterija Duracell AA 4gb", 1, "3.62909", "C62", "5000394076952"),
    ("Koka konstr. skrūve TOP 5x 50/30 (PZ2+T25) Dz", 250, "0.04132", "C62", "53788"),
    ("Zāģripa D200x30mm 40z", 1, "3.46281", "C62", "4750959032442"),
]

lines = [
    LineItem(description=d, quantity=D(str(q)), net_unit_price=D(p),
             unit_code=u, seller_item_id=sid, vat_category="S", vat_percent=D("21"))
    for (d, q, p, u, sid) in RAW
]

note = ("Pavadzīme Nr. DIA 863689. Piegādātāja nodaļa: MD Alūksne pamata. "
        "Līguma nr.: VB 035-001/2023. Kontaktpersona: Māris Šļivka, 25414718. "
        "Pārvadātājs: Pircējs preci aiznesa pats. Darījuma apraksts: preču pārdošana un pakalpojumi. "
        "Summa bez atlaides ar PVN: 399.89 EUR; Atlaides summa: 19.94 EUR. "
        "Dokumentu sagatavoja: Kristīne Botva. Darījums apdrošināts ar Allianz.")

invoice = Invoice(
    invoice_id="DIA 863689",
    issue_date="2026-03-31",
    due_date="2026-04-10",
    supplier=supplier,
    customer=customer,
    lines=lines,
    buyer_reference="54103007931",
    contract_reference="VB 035-001/2023",
    note=note,
    payment=payment,
    delivery_date="2026-03-31",
    delivery_address=delivery,
)

if __name__ == "__main__":
    xml = build_invoice_xml(invoice)
    with open("DIANA_generated.xml", "w", encoding="utf-8") as fh:
        fh.write(xml)

    result = validate(xml)
    print(f"Lines: {len(lines)}")
    print(f"Validation OK: {result.ok}")
    for e in result.errors:
        print("  ERROR:", e)
    for w in result.warnings:
        print("  warn :", w)

    # echo the computed totals
    import xml.etree.ElementTree as ET
    from peppol_horizon import NS_CAC, NS_CBC
    ns = {"cac": NS_CAC, "cbc": NS_CBC}
    root = ET.fromstring(xml)
    g = lambda p: root.find(p, ns).text
    print("Net  (TaxExclusive):", g("cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount"))
    print("VAT  (TaxTotal)    :", g("cac:TaxTotal/cbc:TaxAmount"))
    print("Total (Payable)    :", g("cac:LegalMonetaryTotal/cbc:PayableAmount"))
    print("Expected per PDF   : 314.01 / 65.94 / 379.95")
