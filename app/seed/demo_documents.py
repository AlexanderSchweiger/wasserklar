"""Beispiel-Belege für den Demo-Datensatz: PDF-Rechnungen und Lieferanten-E-Rechnungen.

Erzeugt Dateien so, wie sie ein Mandant hochladen würde — ohne WeasyPrint (läuft
auch im Windows-Dev-Setup und in den Tests):

* ``simple_pdf`` schreibt ein einseitiges PDF mit echter Textebene (Helvetica,
  WinAnsi). ``pypdf`` liest den Text, die Belegerkennung (``documents.extract``)
  findet darin Nummer, Datum, Betrag und USt-IdNr. wie bei einem echten Scan
  mit Textebene.
* ``supplier_einvoice`` baut eine Lieferanten-E-Rechnung über das eigene
  EN-16931-Modell und den CII-Serializer (``app.einvoice``) — XRechnung oder
  ZUGFeRD-Profil. Der Eingangs-Parser liest sie wie jede fremde Datei.
* ``zugferd_pdf`` hängt das XML als ``factur-x.xml`` an ein PDF (Hybrid).

Alles deterministisch (keine Zeitstempel/Zufalls-IDs im PDF), damit ein erneuter
Seed dieselben Dateien — und damit dieselben Prüfsummen — erzeugt.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# PDF mit Textebene
# ---------------------------------------------------------------------------

def _pdf_text(text):
    raw = str(text).encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def simple_pdf(rows, *, title="Beleg"):
    """Einseitiges A4-PDF. ``rows``: Liste von Zeilen; jede Zeile ist ein String, ``None``
    (Leerzeile), ``"---"`` (Trennlinie) oder eine Liste ``[(x, Text, Größe, fett), …]``
    für Spalten auf derselben Grundlinie."""
    ops = [b"0.2 0.2 0.2 rg"]
    y = 790.0
    for row in rows:
        if row is None:
            y -= 8
            continue
        if row == "---":
            ops.append(f"0.6 w 50 {y + 6:.1f} m 545 {y + 6:.1f} l S".encode())
            y -= 8
            continue
        cells = [(50, row, 10, False)] if isinstance(row, str) else row
        size = max(cell[2] for cell in cells)
        for x, text, sz, bold in cells:
            font = b"/F2" if bold else b"/F1"
            ops.append(b"BT " + font + f" {sz} Tf 1 0 0 1 {x} {y:.1f} Tm (".encode()
                       + _pdf_text(text) + b") Tj ET")
        y -= size * 1.5
    content = b"\n".join(ops)

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Title (" + _pdf_text(title) + b") /Producer (quellstube Demo-Datensatz) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def de_amount(value):
    """1234.5 -> ``1.234,50``"""
    q = Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    whole, frac = f"{abs(q):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ("-" if q < 0 else "") + ".".join(groups) + "," + frac


def _qty(value):
    """Menge ohne überflüssige Nachkommastellen: 10 -> ``10``, 6.5 -> ``6,5``."""
    text = de_amount(value)
    whole, frac = text.split(",")
    frac = frac.rstrip("0")
    return f"{whole},{frac}" if frac else whole


def invoice_pdf(*, seller, buyer, number, issue, lines, vat_label, extra=(), heading="Rechnung"):
    """PDF einer klassischen Lieferantenrechnung (oder eines Kassenbons).

    ``seller``/``buyer``: Dicts mit name, street, postcode, city (+ seller: vat_id, iban).
    ``lines``: ``[(Bezeichnung, Menge, Einheit, Netto-Einzelpreis, USt-Satz)]``.
    Gibt ``(pdf_bytes, brutto)`` zurueck.
    """
    totals = _totals(lines)
    rows = [
        [(50, seller["name"], 14, True)],
        f"{seller['street']} · {seller['postcode']} {seller['city']}",
        f"{vat_label}: {seller['vat_id']}" if seller.get("vat_id") else None,
        None, None,
        buyer["name"],
        buyer.get("street") or "",
        f"{buyer.get('postcode', '')} {buyer.get('city', '')}".strip(),
        None, None,
        [(50, heading, 16, True)],
        [(50, "Rechnungsnummer:", 10, False), (170, number, 10, True),
         (330, "Rechnungsdatum:", 10, False), (440, issue.strftime("%d.%m.%Y"), 10, False)],
        None,
        [(50, "Bezeichnung", 10, True), (330, "Menge", 10, True), (390, "Einzelpreis", 10, True),
         (470, "USt", 10, True), (505, "Netto", 10, True)],
        "---",
    ]
    for name, qty, unit, price, rate in lines:
        net = (Decimal(qty) * Decimal(price)).quantize(_CENT, rounding=ROUND_HALF_UP)
        rows.append([(50, name, 10, False), (330, f"{_qty(qty)} {unit}", 10, False),
                     (390, de_amount(price), 10, False), (470, f"{Decimal(rate).normalize():f} %", 10, False),
                     (505, de_amount(net), 10, False)])
    rows.append("---")
    rows.append([(330, "Summe netto", 10, False), (505, de_amount(totals["net"]), 10, False)])
    for rate, (basis, tax) in sorted(totals["vat"].items()):
        rows.append([(330, f"zzgl. {Decimal(rate).normalize():f} % USt auf {de_amount(basis)}", 10, False),
                     (505, de_amount(tax), 10, False)])
    rows.append([(330, "Gesamtbetrag", 11, True), (490, f"{de_amount(totals['gross'])} EUR", 11, True)])
    rows.append(None)
    rows.extend(extra)
    if seller.get("iban"):
        rows.append(None)
        rows.append(f"Bankverbindung: IBAN {seller['iban']}")
    return simple_pdf(rows, title=f"{heading} {number}"), totals["gross"]


def _totals(lines):
    vat = {}
    net_total = Decimal("0")
    for _name, qty, _unit, price, rate in lines:
        net = (Decimal(qty) * Decimal(price)).quantize(_CENT, rounding=ROUND_HALF_UP)
        net_total += net
        basis, _ = vat.get(Decimal(rate), (Decimal("0"), Decimal("0")))
        vat[Decimal(rate)] = (basis + net, Decimal("0"))
    for rate, (basis, _) in vat.items():
        vat[rate] = (basis, (basis * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP))
    tax_total = sum((t for _, t in vat.values()), Decimal("0"))
    return {"net": net_total, "vat": vat, "tax": tax_total, "gross": net_total + tax_total}


# ---------------------------------------------------------------------------
# Lieferanten-E-Rechnung (CII)
# ---------------------------------------------------------------------------

_UNITS = {"Stk": "H87", "h": "HUR", "m": "MTR", "m³": "MTQ", "pauschal": "LS", "Probe": "H87"}


def supplier_einvoice(*, xrechnung, number, issue, due, seller, buyer, lines, buyer_reference,
                      notes=(), delivery=None):
    """XML einer Lieferanten-E-Rechnung an die Genossenschaft. Gibt ``(xml_bytes, brutto)`` zurueck.

    ``xrechnung=True`` -> Profil XRechnung 3.0, sonst EN 16931 (ZUGFeRD/Factur-X).
    """
    from app.einvoice import cii
    from app.einvoice.model import (Address, Delivery, EInvoice, Line, Party, Payment, VatBreakdown,
                                    PROFILE_EN16931, PROFILE_XRECHNUNG)

    totals = _totals(lines)
    e_lines = []
    for i, (name, qty, unit, price, rate) in enumerate(lines, start=1):
        net = (Decimal(qty) * Decimal(price)).quantize(_CENT, rounding=ROUND_HALF_UP)
        e_lines.append(Line(line_id=str(i), name=name, quantity=Decimal(qty),
                            unit_code=_UNITS.get(unit, "C62"), net_price=Decimal(price),
                            net_amount=net, vat_category="S", vat_rate=Decimal(rate)))
    vat = [VatBreakdown(category="S", rate=rate, taxable_amount=basis, tax_amount=tax)
           for rate, (basis, tax) in sorted(totals["vat"].items())]

    def party(d, *, contact=False):
        return Party(
            name=d["name"],
            address=Address(line1=d.get("street") or "", postcode=d.get("postcode") or "",
                            city=d.get("city") or "", country_code="DE"),
            vat_id=d.get("vat_id") or None, endpoint=d.get("email") or None,
            contact_name=(d.get("contact") if contact else None),
            contact_phone=(d.get("phone") if contact else None),
            contact_email=(d.get("email") if contact else None))

    e = EInvoice(
        number=number, issue_date=issue, type_code="380", currency="EUR",
        seller=party(seller, contact=True), buyer=party(buyer), lines=e_lines, vat=vat,
        line_total=totals["net"], tax_total=totals["tax"], grand_total=totals["gross"],
        due_payable=totals["gross"], due_date=due,
        payment_terms=f"Zahlbar bis {due.strftime('%d.%m.%Y')} ohne Abzug.",
        notes=list(notes),
        delivery=Delivery(date=delivery or issue),
        payment=Payment(means_code="58", iban=seller["iban"].replace(" ", ""),
                        account_name=seller["name"], reference=number),
        profile=PROFILE_XRECHNUNG if xrechnung else PROFILE_EN16931,
        buyer_reference=buyer_reference)
    return cii.serialize(e), totals["gross"]


def zugferd_pdf(pdf, xml):
    """Hängt das XML als ``factur-x.xml`` an das PDF (ZUGFeRD-/Factur-X-Hybrid).

    Der Eingangs-Parser liest den Anhang wie bei einer echten Lieferantenrechnung; ein
    PDF/A-3-Profil ist dafür nicht nötig (die Demo-Datei ist keine archivfähige Vorlage).
    """
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.add_attachment("factur-x.xml", xml)
    writer._ID = None    # keine zufällige Datei-ID -> gleiche Bytes bei jedem Seed
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


# ---------------------------------------------------------------------------
# Belegablage des Demo-Datensatzes
# ---------------------------------------------------------------------------

def net_for(gross, rate):
    """Netto, das mit ``rate`` % USt (kaufmännisch gerundet) genau ``gross`` ergibt."""
    gross, rate = Decimal(gross), Decimal(rate)
    net = (gross / (1 + rate / 100)).quantize(_CENT, rounding=ROUND_HALF_UP)
    for delta in (0, 1, -1, 2, -2):
        cand = net + delta * _CENT
        if cand + (cand * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP) == gross:
            return cand
    return net


def split_net(net, shares):
    """Netto auf Positionen aufteilen (letzte Position bekommt den Rest)."""
    parts, rest = [], Decimal(net)
    for share in shares[:-1]:
        part = (Decimal(net) * Decimal(str(share))).quantize(_CENT, rounding=ROUND_HALF_UP)
        parts.append(part)
        rest -= part
    return parts + [rest]


def _supplier_iban(code, index):
    from app.seed.demo_locale import iban
    if code == "DE":
        return iban("DE", "70150000" + f"{4711000 + index * 7919:010d}")
    return iban("AT", "20111" + f"{8231000 + index * 7919:011d}")


def seed_documents(db, loc, *, admin, now, plan_bookings, bridge_bookings, accounts,
                   projects, giro):
    """Beispielbelege: PDF-Rechnungen an Buchungen, ein erkannter Beleg im Eingang, ein
    abgelegter Beleg — in Deutschland zusaetzlich Eingangs-E-Rechnungen (XRechnung-XML und
    ZUGFeRD-PDF), verbucht als Einzel- bzw. Sammelbuchung, eine noch im Eingang.

    Die Dateien landen wie Uploads im Dateibaum des Mandanten (``store_generated`` mit Status
    ``Neu`` und Ereignis ``uploaded``); kein Commit — rollt der Seed zurueck, raeumt der
    Dokumenten-Service die geschriebenen Dateien wieder weg.
    """
    from datetime import timedelta

    from app import country
    from app.accounting import incoming_service
    from app.customers.numbers import assign_numbers
    from app.documents import extract, service as doc_svc
    from app.einvoice import incoming
    from app.models import Account, Customer, Document
    from app.seed.demo_locale import AT_SUPPLIERS, DE_SUPPLIERS, de_vat_id
    from app.settings_service import get_wg

    is_de = loc.code == "DE"
    std = loc.std_rate
    profile = country.profile(loc.code)
    ident = loc.identity
    buyer = {
        "name": get_wg("name") or ident["name"],
        "street": get_wg("street") or ident["street"],
        "postcode": get_wg("postal_code") or ident["postal_code"],
        "city": get_wg("city") or ident["city"],
    }

    # --- Lieferanten (Kontakte mit Lieferantennummer) ---------------------
    suppliers, seller = {}, {}
    table = DE_SUPPLIERS if is_de else AT_SUPPLIERS
    for index, (key, row) in enumerate(table.items()):
        if is_de:
            name, street, plz, city, vat_base, email, phone = row
            vat_id = de_vat_id(vat_base)
        else:
            name, street, plz, city, vat_id = row
            email = phone = None
        seller[key] = {"name": name, "street": street, "postcode": plz, "city": city,
                       "vat_id": vat_id, "email": email, "phone": phone,
                       "iban": _supplier_iban(loc.code, index), "contact": "Buchhaltung"}
        if key == "labor":
            continue        # neuer Lieferant: der Beleg im Eingang bietet „Lieferant anlegen“ an
        c = Customer(name=name, is_company=True, is_customer=False, is_supplier=True, active=True,
                     strasse=street, plz=plz, ort=city, land=profile.name, email=email,
                     phone=phone, vat_id=vat_id)
        assign_numbers(c)
        db.session.add(c)
        suppliers[key] = c
    db.session.flush()

    counts = {"documents": 0, "documents_linked": 0, "documents_inbox": 0,
              "documents_filed": 0, "incoming_einvoices": 0, "suppliers": len(suppliers)}

    def store(data, filename, *, kind=Document.KIND_INVOICE, title=None, number=None,
              document_date=None, amount=None, supplier=None, recognise=False):
        ext = doc_svc.sniff(data)
        doc = doc_svc.store_generated(
            Document.AREA_ACCOUNTING, kind, data, ext, original_name=filename,
            title=None if recognise else title, number=None if recognise else number,
            document_date=None if recognise else document_date,
            amount=None if recognise else amount, user_id=admin.id,
            status=Document.STATUS_NEW, action="uploaded")
        if ext == "pdf":
            text = extract.extract_pdf_text(data)
            doc.text_status, doc.text_content = text.status, text.text or None
        try:
            doc_svc.apply_incoming(doc, incoming.parse(data, filename))
            counts["incoming_einvoices"] += 1
        except incoming.IncomingError:
            pass            # PDF ohne E-Rechnungsdaten: normaler Beleg
        if recognise:
            doc_svc.autofill(doc, admin.id)          # „erkannt“ — wartet auf Bestätigung
        elif supplier is not None:
            doc.supplier_id = supplier.id
        counts["documents"] += 1
        return doc

    def pdf_invoice(key, number, issue, lines, heading="Rechnung", extra=()):
        return invoice_pdf(seller=seller[key], buyer=buyer, number=number, issue=issue,
                           lines=lines, vat_label=profile.vat_id_label, extra=extra,
                           heading=heading)

    def link(doc, booking):
        doc_svc.link(doc, booking=booking, user_id=admin.id)
        counts["documents_linked"] += 1

    def day(offset):
        return min(now, max(date(now.year, 1, 2), now - timedelta(days=offset)))

    # --- Belege zu vorhandenen Buchungen ---------------------------------
    b = plan_bookings.get("Büromaterial Toner")
    if b is not None:
        issued = b.date - timedelta(days=2)
        number = f"R-{b.date.year}-0318"
        n1, n2 = split_net(net_for(-b.amount, std), (0.7, 0.3))
        pdf, gross = pdf_invoice("buero", number, issued, [
            ("Toner-Kartusche schwarz, Laserdrucker", 1, "Stk", n1, std),
            ("Kopierpapier A4, 500 Blatt", 1, "Stk", n2, std)])
        link(store(pdf, f"Rechnung_{number}.pdf", title=seller["buero"]["name"], number=number,
                   document_date=issued, amount=gross, supplier=suppliers["buero"]), b)

    b = plan_bookings.get("Hydrantenwartung Sommer")
    if b is not None:
        issued = b.date - timedelta(days=5)
        number = f"AR-{b.date.year}-1142"
        pdf, gross = pdf_invoice("armaturen", number, issued, [
            ("Hydrantenwartung Sommer: Funktionsprüfung, Dichtungen, Entwässerung (6 Stk.)",
             1, "pauschal", net_for(-b.amount, std), std)])
        link(store(pdf, f"{number}.pdf", title=seller["armaturen"]["name"], number=number,
                   document_date=issued, amount=gross, supplier=suppliers["armaturen"]), b)

    b = plan_bookings.get("Leitungstausch Material")
    if b is not None:
        key = "armaturen" if is_de else "baumarkt"
        issued = b.date - timedelta(days=4)
        number = f"LS-{b.date.year}-0207"
        n1, n2 = split_net(net_for(-b.amount, std), (0.72, 0.28))
        pdf, gross = pdf_invoice(key, number, issued, [
            ("PE-Rohr da 110 x 10,0 SDR 11, 120 m", 1, "pauschal", n1, std),
            ("Formteile, Schweißmuffen, Anbohrschellen", 1, "pauschal", n2, std)])
        link(store(pdf, f"Lieferung_{number}.pdf", title=seller[key]["name"], number=number,
                   document_date=issued, amount=gross, supplier=suppliers[key]), b)

    # Kassenbon zur juengsten laufenden Bueromaterial-Buchung (nur wenn es die gibt)
    office = [x for x in bridge_bookings if x.account_id == accounts["buero"].id]
    if office:
        b = office[-1]
        number = f"Bon {b.date.strftime('%y%m%d')}-17"
        pdf, gross = pdf_invoice("buero", number, b.date, [
            ("Büro- und Versandmaterial", 1, "pauschal", net_for(-b.amount, std), std)],
            heading="Kassenbon", extra=("Bar bezahlt. Vielen Dank für Ihren Einkauf!",))
        link(store(pdf, f"Kassenbon_{b.date.isoformat()}.pdf", kind=Document.KIND_RECEIPT,
                   title=seller["buero"]["name"], number=number, document_date=b.date,
                   amount=gross, supplier=suppliers["buero"]), b)

    # --- Eingang: Beleg mit erkannten Angaben (noch nicht bestätigt, nicht gebucht) ---
    issued = day(9)
    pdf, _gross = pdf_invoice("elektro", f"{issued.year}-{issued.strftime('%m')}-0457", issued, [
        (f"Prüfung elektrische Anlage {loc.names['pump']} "
         f"({'DGUV V3' if is_de else 'ÖVE E 8101'})", 1, "pauschal", Decimal("400.00"), std),
        ("Fahrtkosten", 1, "pauschal", Decimal("25.00"), std)],
        extra=("Zahlbar innerhalb von 14 Tagen ohne Abzug.",))
    store(pdf, f"Rechnung_Elektro_{issued.isoformat()}.pdf", recognise=True)
    counts["documents_inbox"] += 1

    # --- Abgelegt ohne Buchung (Bestätigung, kein Rechnungsbeleg) ---------
    letter = simple_pdf([
        [(50, "Demo Versicherung AG", 14, True)],
        "Abteilung Haftpflicht · Postfach 100 · 10115 Berlin" if is_de
        else "Abteilung Haftpflicht · Postfach 100 · 1010 Wien",
        None, None, buyer["name"], buyer["street"], f"{buyer['postcode']} {buyer['city']}",
        None, None,
        [(50, f"Versicherungsbestätigung {now.year}", 14, True)],
        None,
        "Hiermit bestätigen wir den Versicherungsschutz Ihrer Betriebshaftpflicht",
        "(Wasserversorgung, Leitungsnetz, Hochbehälter und Quellfassungen).",
        f"Versicherungsschein-Nr. HV-{now.year}-55102, Laufzeit 01.01. bis 31.12.{now.year}.",
        None,
        "Diese Bestätigung ist kein Rechnungsbeleg.",
    ], title=f"Versicherungsbestätigung {now.year}")
    doc = store(letter, f"Versicherungsbestaetigung_{now.year}.pdf", kind=Document.KIND_OTHER,
                title=f"Versicherungsbestätigung Haftpflicht {now.year}",
                document_date=date(now.year, 1, 2))
    doc_svc.shelve(doc, admin.id)
    counts["documents_filed"] += 1

    if not is_de:
        db.session.flush()
        return counts

    # --- Deutschland: Eingangs-E-Rechnungen --------------------------------
    acc_meeting = Account(code="230", name="Versammlungen und Bewirtung",
                          description="Vorstandssitzungen, Mitgliederversammlung")
    db.session.add(acc_meeting)
    db.session.flush()
    water = loc.water_rate

    # 1) XRechnung (reines XML) des Tiefbauers zum Rohrbruch -> Einzelbuchung
    issued = day(18)
    number = f"TL-{issued.year}-0815"
    xml, _gross = supplier_einvoice(
        xrechnung=True, number=number, issue=issued, due=issued + timedelta(days=30),
        seller=seller["tiefbau"], buyer=buyer, buyer_reference="Kd-Nr. 20417",
        notes=(f"Rohrbruch {loc.names['street_break']} (Setzung) — Aufgrabung, Rohrtausch, "
               "Wiederherstellung der Oberfläche.",),
        lines=[("Aufgrabung und Verbau Rohrgraben", 1, "pauschal", Decimal("980.00"), std),
               ("Rohrtausch DN 100 PE inkl. Formteile", 1, "pauschal", Decimal("640.00"), std),
               ("Bagger mit Fahrer", Decimal("6.5"), "h", Decimal("95.00"), std),
               ("Wiederherstellung Asphaltdecke", 12, "m²", Decimal("38.50"), std)])
    doc = store(xml, f"XRechnung_{number}.xml")
    incoming_service.book(doc, supplier=suppliers["tiefbau"], account_id=accounts["reparatur"].id,
                          booking_date=issued, user_id=admin.id, project_id=projects[1].id,
                          real_account_id=giro.id)
    counts["documents_linked"] += 1

    # 2) ZUGFeRD-PDF des Gasthofs (Speisen 7 %, Getränke 19 %) -> Sammelbuchung
    issued = day(30)
    number = f"{issued.year}/{issued.strftime('%m')}-118"
    lines = [("Brotzeitteller", 22, "Stk", Decimal("9.80"), water),
             ("Schweinebraten mit Knödel", 6, "Stk", Decimal("13.50"), water),
             ("Getränke (Bier, Limonade, Kaffee)", 1, "pauschal", Decimal("148.20"), std)]
    xml, _gross = supplier_einvoice(
        xrechnung=False, number=number, issue=issued, due=issued + timedelta(days=14),
        seller=seller["gasthof"], buyer=buyer, buyer_reference="Vorstandssitzung",
        notes=("Bewirtung nach der Vorstandssitzung.",), lines=lines)
    pdf, _gross = pdf_invoice(
        "gasthof", number, issued, lines,
        extra=("Diese Rechnung enthält eine ZUGFeRD-E-Rechnung (factur-x.xml).",))
    doc = store(zugferd_pdf(pdf, xml), f"Rechnung_{number.replace('/', '-')}.pdf")
    incoming_service.book(doc, supplier=suppliers["gasthof"], account_id=acc_meeting.id,
                          booking_date=issued, user_id=admin.id, project_id=projects[3].id,
                          real_account_id=giro.id)
    counts["documents_linked"] += 1

    # 3) XRechnung des Labors — Lieferant noch unbekannt, liegt im Eingang
    issued = day(6)
    number = f"LAB-{issued.year}-2291"
    xml, _gross = supplier_einvoice(
        xrechnung=True, number=number, issue=issued, due=issued + timedelta(days=30),
        seller=seller["labor"], buyer=buyer, buyer_reference="WV-HABACH",
        notes=("Trinkwasseruntersuchung nach TrinkwV, Routinebefund.",),
        lines=[("Probenahme durch akkreditierten Probenehmer", 3, "Probe", Decimal("45.00"), std),
               ("Mikrobiologie (E. coli, Coliforme, Enterokokken, Koloniezahl)", 3, "Probe",
                Decimal("62.00"), std),
               ("Chemische Untersuchung Gruppe A", 3, "Probe", Decimal("88.00"), std),
               ("Fahrtkosten", 1, "pauschal", Decimal("35.00"), std)])
    store(xml, f"XRechnung_{number}.xml")
    counts["documents_inbox"] += 1
    db.session.flush()
    return counts
