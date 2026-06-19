"""Geschaetzte Zaehlerstaende + Korrekturposten (Gutschrift/Nachforderung).

Drei Aufgaben:

1. **Schaetzen** (``estimate_meter_value``): fehlt fuer einen Zaehler in einer
   Periode der Stand, wird er aus dem letzten bekannten Stand plus dem
   Durchschnittsverbrauch der letzten Jahre geschaetzt. Der User kann den
   Wert ueberschreiben; ``MeterReading.is_estimated`` markiert die Schaetzung.

2. **Abgleichen** (``build_correction``): wird ein echter Stand nachgereicht,
   der eine bereits *abgerechnete* Schaetzung ersetzt, entsteht ein
   vorzeichenbehafteter ``ReadingCorrection`` — Nachforderung (zu wenig
   abgerechnet) oder Gutschrift (zu viel abgerechnet). Preis + USt-Satz werden
   aus der urspruenglichen Schaetz-Rechnung uebernommen. Die Verbrauchskette
   selbst korrigiert sich ueber ``recompute_meter_chain`` von allein; der
   Korrekturposten verrechnet ausschliesslich die abgerechnete Schaetzperiode,
   nicht die Folgeperioden (kein Doppelzaehlen).

3. **Einziehen** (``apply_corrections_to_invoice``): offene Korrekturposten
   eines Kunden werden in seine naechste (Wasser-)Rechnung als eigene Position
   eingezogen. Eine Gutschrift wird nur soweit verrechnet, dass der
   Rechnungsbetrag nie unter 0 faellt; der Rest bleibt offen und wandert auf
   die uebernaechste Rechnung.

Dialekt-portabel: nur ORM-Queries, keine dialekt-spezifischen Konstrukte.
Convention wie ``meters.services``: der Caller committet.
"""
from decimal import Decimal, ROUND_DOWN

from app.extensions import db
from app.models import (
    MeterReading, BillingPeriod, Invoice, InvoiceItem, BillingRun,
    ReadingCorrection,
)

_ISSUED_STATUSES = (Invoice.STATUS_SENT, Invoice.STATUS_PAID, Invoice.STATUS_CREDIT)


# ---------------------------------------------------------------------------
# 1. Schaetzen
# ---------------------------------------------------------------------------

def average_consumption(meter, *, limit=5):
    """Durchschnittsverbrauch der letzten ``limit`` Ablesungen mit gesetztem
    Verbrauch. Gibt ``(avg_int, anzahl)`` zurueck bzw. ``(None, 0)`` wenn es
    keine Verbrauchshistorie gibt."""
    rows = (
        MeterReading.query
        .filter(
            MeterReading.meter_id == meter.id,
            MeterReading.consumption.isnot(None),
        )
        .order_by(MeterReading.reading_date.desc(), MeterReading.id.desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return None, 0
    n = len(rows)
    avg = round(sum(float(r.consumption) for r in rows) / n)
    return avg, n


def _base_value(meter, period):
    """Letzter bekannter Zaehlerstand VOR ``period`` (nach Perioden-Startdatum),
    sonst der ``initial_value`` des Zaehlers. ``None`` wenn beides fehlt."""
    prev = (
        MeterReading.query
        .join(BillingPeriod, BillingPeriod.id == MeterReading.billing_period_id)
        .filter(
            MeterReading.meter_id == meter.id,
            BillingPeriod.start_date < period.start_date,
        )
        .order_by(BillingPeriod.start_date.desc(), MeterReading.id.desc())
        .first()
    )
    if prev is not None:
        return prev.value
    return meter.initial_value


def estimate_meter_value(meter, period):
    """Schaetzwert fuer ``(meter, period)``: letzter Stand + Ø-Verbrauch.

    Gibt ein Dict ``{value, avg_consumption, base_value, n_basis}`` zurueck
    oder ``None``, wenn keine Basis vorhanden ist (kein Vorstand/initial_value
    ODER keine Verbrauchshistorie) — dann muss der User manuell eingeben.
    """
    if period is None:
        return None
    base = _base_value(meter, period)
    avg, n = average_consumption(meter)
    if base is None or avg is None:
        return None
    value = (Decimal(str(base)) + Decimal(avg)).quantize(Decimal("1"))
    return {
        "value": value,
        "avg_consumption": avg,
        "base_value": Decimal(str(base)),
        "n_basis": n,
    }


# ---------------------------------------------------------------------------
# 2. Abgleichen (echter Stand ersetzt abgerechnete Schaetzung)
# ---------------------------------------------------------------------------

def _issued_invoice_for(property_id, period_id):
    """Die ausgestellte (nicht Entwurf/storniert) Rechnung fuer Objekt+Periode."""
    if property_id is None or period_id is None:
        return None
    return (
        Invoice.query
        .filter(
            Invoice.property_id == property_id,
            Invoice.billing_period_id == period_id,
            Invoice.status.in_(_ISSUED_STATUSES),
        )
        .order_by(Invoice.id.desc())
        .first()
    )


def _consumption_price(invoice):
    """Preis (€/m³) + USt-Satz, mit dem der Verbrauch auf ``invoice`` abgerechnet
    wurde. Bevorzugt die Verbrauchsposition (Einheit m³), faellt auf den
    Tarif-Snapshot des Rechnungslaufs zurueck. ``(None, None)`` wenn nicht
    ermittelbar."""
    item = next(
        (it for it in invoice.items
         if it.unit == "m³" and not getattr(it, "is_dunning_fee", 0)
         and it.unit_price is not None and Decimal(str(it.unit_price)) > 0),
        None,
    )
    if item is not None:
        tax = Decimal(str(item.tax_rate)) if item.tax_rate else None
        return Decimal(str(item.unit_price)), tax
    if invoice.billing_run_id:
        run = db.session.get(BillingRun, invoice.billing_run_id)
        if run is not None and run.tariff_price_per_m3 is not None:
            return Decimal(str(run.tariff_price_per_m3)), None
    return None, None


def build_correction(reading, estimated_consumption, *, created_by_id=None):
    """Legt — falls noetig — einen ``ReadingCorrection`` an, wenn ein echter
    Stand eine *abgerechnete* Schaetzung ersetzt.

    ``reading`` muss bereits den ECHTEN Verbrauch tragen (``recompute_meter_chain``
    vorher laufen lassen); ``estimated_consumption`` ist der zuvor abgerechnete
    Schaetzverbrauch. Gibt den Korrekturposten oder ``None`` zurueck (kein
    Posten, wenn die Schaetzung nie abgerechnet wurde, der Verbrauch unbekannt
    ist oder die Differenz 0 betraegt). Caller committet.
    """
    if reading is None or estimated_consumption is None or reading.consumption is None:
        return None
    meter = reading.meter
    if meter is None:
        return None
    invoice = _issued_invoice_for(meter.property_id, reading.billing_period_id)
    if invoice is None:
        return None  # Schaetzung wurde nie abgerechnet -> nichts zu korrigieren

    delta = Decimal(str(reading.consumption)) - Decimal(str(estimated_consumption))
    if delta == 0:
        return None
    unit_price, tax_rate = _consumption_price(invoice)
    if unit_price is None:
        return None  # Preis nicht ermittelbar -> kein automatischer Posten
    amount = (delta * unit_price).quantize(Decimal("0.01"))
    if amount == 0:
        return None

    corr = ReadingCorrection(
        customer_id=invoice.customer_id,
        meter_id=meter.id,
        billing_period_id=reading.billing_period_id,
        source_reading_id=reading.id,
        source_invoice_id=invoice.id,
        estimated_consumption=Decimal(str(estimated_consumption)),
        real_consumption=Decimal(str(reading.consumption)),
        delta_m3=delta,
        unit_price=unit_price,
        tax_rate=tax_rate,
        amount=amount,
        remaining_amount=amount,
        status=ReadingCorrection.STATUS_OPEN,
        created_by_id=created_by_id,
    )
    db.session.add(corr)
    return corr


# ---------------------------------------------------------------------------
# 3. Einziehen in die naechste Rechnung
# ---------------------------------------------------------------------------

def open_corrections_for_customer(customer_id):
    """Offene/teils offene Korrekturposten eines Kunden, aelteste zuerst."""
    return (
        ReadingCorrection.query
        .filter(
            ReadingCorrection.customer_id == customer_id,
            ReadingCorrection.status.in_([
                ReadingCorrection.STATUS_OPEN,
                ReadingCorrection.STATUS_PARTIAL,
            ]),
        )
        .order_by(ReadingCorrection.created_at.asc(), ReadingCorrection.id.asc())
        .all()
    )


def _item_gross(net, rate):
    """Brutto einer Position GENAU so, wie ``Invoice.recalculate_total`` rechnet
    (USt pro Position auf 0,01 gerundet) — damit der Spielraum-Abgleich exakt
    mit dem spaeteren Rechnungsbetrag uebereinstimmt."""
    g = Decimal(str(net))
    if rate and rate > 0:
        g += (Decimal(str(net)) * Decimal(str(rate)) / Decimal("100")).quantize(Decimal("0.01"))
    return g


def _safe_partial_credit_net(headroom, rate):
    """Groesste (betragsmaessige) Netto-Teil-Gutschrift, deren Brutto den
    positiven ``headroom`` nicht ueberschreitet — der Rechnungsbetrag faellt so
    garantiert nie unter 0 (auch nicht um Rundungs-Cents). Gibt einen positiven
    Decimal zurueck (0 wenn nichts mehr passt)."""
    mult = Decimal("1") + (Decimal(str(rate)) / Decimal("100") if rate else Decimal("0"))
    net = (headroom / mult).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    # Wegen separater USt-Rundung kann das Brutto minimal ueber headroom liegen;
    # dann cent-weise verkleinern, bis es sicher passt.
    while net > 0 and _item_gross(net, rate) > headroom:
        net -= Decimal("0.01")
    return net if net > 0 else Decimal("0.00")


def customer_correction_balance(customer_id):
    """Signierte Summe der offenen Schätz-Korrekturen eines Kunden.

    < 0 = noch nicht verrechnetes **Guthaben** (Gutschrift), > 0 = offene
    **Nachforderung**, 0 = nichts offen. Beides wird beim nächsten
    Wasser-Rechnungslauf eingezogen (Gutschrift gekappt bei Betrag 0).
    """
    rows = open_corrections_for_customer(customer_id)
    return sum((Decimal(str(c.remaining_amount or 0)) for c in rows), Decimal("0"))


def _add_correction_item(inv, corr, net_amount, *, partial):
    """Fuegt der Rechnung eine Korrektur-Position hinzu (netto, vorzeichenbehaftet).

    ``reading_correction_id`` verlinkt die Position zurueck auf den Posten, damit
    das Loeschen der Rechnung den verrechneten Betrag exakt zurueckgeben kann.
    """
    period_name = corr.billing_period.name if corr.billing_period else ""
    meter_no = corr.meter.meter_number if corr.meter else "?"
    part_suffix = " (Teilbetrag, Rest folgt)" if partial else ""
    if corr.delta_m3 is None:
        # Carry-forward aus einer auf 0 gekappten Vorperioden-Rechnung
        # (negativer Verbrauch) — kein m³-Delta, daher anders beschriften.
        desc = (
            f"Gutschrift aus Vorperiode (Schätzkorrektur) {period_name}"
            f" – Zähler {meter_no}{part_suffix}"
        )
    else:
        label = "Gutschrift" if net_amount < 0 else "Nachverrechnung"
        if partial:
            qty_suffix = part_suffix
        else:
            delta_abs = abs(Decimal(str(corr.delta_m3 or 0)))
            qty_suffix = f" ({delta_abs.quantize(Decimal('1'))} m³)"
        desc = (
            f"{label} geschätzter Wasserverbrauch {period_name}"
            f" – Zähler {meter_no}{qty_suffix}"
        )
    # An die relationship anhaengen (nicht nur session.add), damit ``inv.items``
    # — von ``recalculate_total`` gelesen — die Position sofort sieht. Ein
    # blosses ``session.add(InvoiceItem(invoice_id=...))`` aktualisiert eine
    # bereits geladene Collection nicht und der Betrag bliebe falsch.
    inv.items.append(InvoiceItem(
        description=desc,
        quantity=Decimal("1"),
        unit="Pauschal",
        unit_price=net_amount,
        amount=net_amount,
        tax_rate=(corr.tax_rate if corr.tax_rate else None),
        reading_correction_id=corr.id,
    ))
    db.session.flush()


def apply_corrections_to_invoice(inv, customer_id):
    """Zieht offene Korrekturposten des Kunden in ``inv`` ein.

    Nachforderungen (amount > 0) werden immer voll als Position aufgenommen.
    Gutschriften (amount < 0) nur soweit, dass der Brutto-Rechnungsbetrag nie
    unter 0 faellt — der nicht verrechenbare Rest bleibt als ``remaining_amount``
    offen (Status ``Teilverrechnet``) und wandert auf die naechste Rechnung.

    Erwartet, dass die regulaeren Positionen (Verbrauch/Gebuehren) bereits an
    ``inv`` haengen. Caller committet.
    """
    corrs = open_corrections_for_customer(customer_id)
    if not corrs:
        return []

    applied = []
    surcharges = [c for c in corrs if Decimal(str(c.remaining_amount)) > 0]
    credits = [c for c in corrs if Decimal(str(c.remaining_amount)) < 0]

    # Nachforderungen: immer voll (erhoehen den Betrag).
    for c in surcharges:
        r = Decimal(str(c.remaining_amount))
        _add_correction_item(inv, c, r, partial=False)
        c.remaining_amount = Decimal("0.00")
        c.status = ReadingCorrection.STATUS_APPLIED
        c.applied_invoice_id = inv.id
        applied.append(c)

    # Verfuegbarer Brutto-Spielraum nach Verbrauch + Gebuehren + Nachforderungen.
    inv.recalculate_total()
    headroom = Decimal(str(inv.total_amount or 0))

    for c in credits:
        if headroom <= 0:
            break  # kein Spielraum mehr -> Rest wandert weiter
        r = Decimal(str(c.remaining_amount))  # negativ
        rate = Decimal(str(c.tax_rate)) if c.tax_rate else Decimal("0")
        full_gross = abs(_item_gross(r, rate))  # Brutto der vollen Gutschrift
        if full_gross <= headroom:
            apply_net = r  # voll verrechnen
            c.remaining_amount = Decimal("0.00")
            c.status = ReadingCorrection.STATUS_APPLIED
            headroom -= full_gross
            _add_correction_item(inv, c, apply_net, partial=False)
        else:
            # Nur soviel gutschreiben, dass der Betrag nie unter 0 faellt.
            net_pos = _safe_partial_credit_net(headroom, rate)
            if net_pos <= 0:
                break
            apply_net = -net_pos
            c.remaining_amount = (r - apply_net)  # bleibt negativ
            c.status = ReadingCorrection.STATUS_PARTIAL
            headroom -= abs(_item_gross(apply_net, rate))
            _add_correction_item(inv, c, apply_net, partial=True)
        c.applied_invoice_id = inv.id
        applied.append(c)

    inv.recalculate_total()
    return applied


def cap_invoice_at_zero(inv, *, customer_id, meter_id, period_id, tax_rate,
                        created_by_id=None):
    """Kappt eine Rechnung mit negativem Brutto-Betrag auf 0 und überträgt den
    Rest als Gutschrift auf die nächste Rechnung.

    Ein negativer Betrag entsteht v.a. durch **negativen Verbrauch** (die
    Verbrauchskette korrigiert eine zu hohe Vorperioden-Schätzung mit einer
    negativen Folgeperioden-Menge). Statt eine Minus-Rechnung auszustellen,
    wird hier eine Ausgleichsposition eingefügt (Betrag → 0) und der nicht
    ausgeglichene Rest als ``ReadingCorrection`` (Gutschrift) auf den nächsten
    Lauf vertagt — mathematisch identisch zum Minusbetrag, nur nie negativ.

    Erwartet, dass alle regulären Positionen + bereits eingezogene Korrekturen
    an ``inv`` hängen. Caller committet. Gibt die erzeugte Gutschrift oder None.
    """
    inv.recalculate_total()
    total = Decimal(str(inv.total_amount or 0))
    if total >= 0:
        return None

    rate = Decimal(str(tax_rate)) if tax_rate else Decimal("0")
    mult = Decimal("1") + rate / Decimal("100")
    # Netto-Ausgleich, der den Brutto-Betrag (>= 0) hebt; rundungssicher.
    offset_net = (-total / mult).quantize(Decimal("0.01"))
    item = InvoiceItem(
        invoice_id=inv.id,
        description="Guthaben aus Vorperiode – Übertrag auf nächste Rechnung",
        quantity=Decimal("1"), unit="Pauschal",
        unit_price=offset_net, amount=offset_net,
        tax_rate=(tax_rate if tax_rate else None),
    )
    inv.items.append(item)
    db.session.flush()
    inv.recalculate_total()
    # Wegen Pro-Position-USt-Rundung kann der Betrag minimal negativ bleiben ->
    # cent-weise erhöhen, bis er garantiert >= 0 ist.
    guard = 0
    while Decimal(str(inv.total_amount or 0)) < 0 and guard < 5:
        item.amount += Decimal("0.01")
        item.unit_price = item.amount
        db.session.flush()
        inv.recalculate_total()
        guard += 1

    # Übertragener Gutschrift-Betrag (netto, negativ) = -Ausgleich.
    carried = -Decimal(str(item.amount))
    corr = ReadingCorrection(
        customer_id=customer_id, meter_id=meter_id, billing_period_id=period_id,
        source_invoice_id=inv.id,
        estimated_consumption=None, real_consumption=None, delta_m3=None,
        unit_price=Decimal("0"), tax_rate=(tax_rate if tax_rate else None),
        amount=carried, remaining_amount=carried,
        status=ReadingCorrection.STATUS_OPEN, created_by_id=created_by_id,
    )
    db.session.add(corr)
    db.session.flush()
    return corr


def reverse_corrections_for_invoice(inv):
    """Macht alle Schätz-Korrektur-Effekte einer zu löschenden Rechnung rückgängig.

    Zwingend VOR dem Löschen der Rechnung aufrufen (Caller committet):
      1. **Verrechnete** Korrekturen (Positionen mit ``reading_correction_id``):
         der hier verrechnete Betrag wird der ``ReadingCorrection`` wieder
         gutgeschrieben (``remaining_amount``), Status neu abgeleitet.
      2. **Durch Kappung erzeugte** Korrekturen (``source_invoice_id == inv.id``):
         werden gelöscht — aber nur, wenn noch unverrechnet. Sonst ``ValueError``
         (zuerst die spätere Rechnung/den späteren Lauf löschen).
    """
    # 1) Verrechnete Korrekturen zurueckdrehen.
    for item in list(inv.items):
        cid = getattr(item, "reading_correction_id", None)
        if not cid:
            continue
        c = db.session.get(ReadingCorrection, cid)
        if c is None:
            continue
        c.remaining_amount = (
            Decimal(str(c.remaining_amount)) + Decimal(str(item.amount or 0)))
        amount = Decimal(str(c.amount))
        if c.remaining_amount == amount:
            c.status = ReadingCorrection.STATUS_OPEN
        elif c.remaining_amount == 0:
            c.status = ReadingCorrection.STATUS_APPLIED
        else:
            c.status = ReadingCorrection.STATUS_PARTIAL
        if c.applied_invoice_id == inv.id:
            c.applied_invoice_id = None

    # 2) Durch Kappung dieser Rechnung erzeugte Carry-forward-Gutschriften.
    created = ReadingCorrection.query.filter_by(source_invoice_id=inv.id).all()
    for c in created:
        if Decimal(str(c.remaining_amount)) != Decimal(str(c.amount)):
            raise ValueError(
                "Eine aus dieser Rechnung erzeugte Gutschrift wurde bereits "
                "(teilweise) verrechnet. Bitte zuerst die spätere Rechnung "
                "bzw. den späteren Rechnungslauf löschen."
            )
        db.session.delete(c)
    db.session.flush()
