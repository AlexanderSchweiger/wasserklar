"""Gebührenarten-Katalog + individuelle Gebühren (Stammdaten-Seite der Tarife).

* ``ensure_system_charge_types`` — idempotenter Seed der System-Arten
  (Wasser, Wassercent, Grund-/Zusatzgebühr). Laeuft in der Alembic-Migration
  (jeder neue Tenant), bei ``init-db``/Reset und in den Tests.
* ``apply_override_form`` — liest den Abschnitt „Individuelle Gebühren" aus dem
  Kunden- bzw. Objektformular und schreibt ``ChargeOverride``-Zeilen.
"""
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import ChargeOverride, ChargeType, TariffComponent, WaterTariff

# (key, label, calc_type, is_levy, overridable, sort_order)
SYSTEM_CHARGE_TYPES = (
    (ChargeType.KEY_WATER, "Wasserverbrauch", ChargeType.CALC_PER_M3, False, False, 10),
    (ChargeType.KEY_WATER_LEVY, "Wasserentnahmeentgelt", ChargeType.CALC_PER_M3, True, False, 15),
    (ChargeType.KEY_BASE_FEE, "Grundgebühr", ChargeType.CALC_FLAT, False, True, 20),
    (ChargeType.KEY_ADDITIONAL_FEE, "Zusatzgebühr", ChargeType.CALC_FLAT, False, True, 30),
)

# Nur in diesen Laendern ist der Wassercent (Landesabgabe je m³) von Haus aus
# aktiv; anderswo steht er im Katalog bereit, ist aber ausgeblendet.
LEVY_COUNTRIES = {"DE"}

# Auswahl im Kunden-/Objektformular.
MODE_INHERIT = ""
MODE_AMOUNT = "amount"
MODE_EXEMPT = "exempt"


def ensure_system_charge_types(country_code=None):
    """Legt fehlende System-Gebührenarten an (flush, kein Commit).

    Bestehende Zeilen bleiben unangetastet — auch ein vom Mandanten
    umbenanntes Label oder ein deaktivierter Wassercent.
    """
    from app import country
    code = country.normalize_code(country_code) or country.current_code()
    existing = {ct.key for ct in ChargeType.query.all()}
    added = []
    for key, label, calc, is_levy, overridable, sort in SYSTEM_CHARGE_TYPES:
        if key in existing:
            continue
        active = True
        if key == ChargeType.KEY_WATER_LEVY:
            active = code in LEVY_COUNTRIES
        ct = ChargeType(key=key, label=label, calc_type=calc, is_levy=is_levy,
                        overridable=overridable, is_system=True, active=active,
                        sort_order=sort)
        db.session.add(ct)
        added.append(ct)
    if added:
        db.session.flush()
    return added


def charge_type(key):
    return ChargeType.query.filter_by(key=key).first()


def charge_types(*, include_inactive=False):
    q = ChargeType.query
    if not include_inactive:
        q = q.filter(ChargeType.active.is_(True))
    return q.order_by(ChargeType.sort_order, ChargeType.label).all()


def overridable_charge_types():
    """Gebührenarten fuer den Abschnitt „Individuelle Gebühren" — auch
    inaktive, solange sie ueberschreibbar sind (ein Override auf eine
    ausgeblendete Art soll sichtbar bleiben und entfernt werden koennen)."""
    return (ChargeType.query
            .filter(ChargeType.overridable.is_(True))
            .order_by(ChargeType.sort_order, ChargeType.label).all())


def next_custom_key():
    """Technischer Schluessel fuer eine eigene Gebührenart (``custom_<n>``)."""
    n = 1
    keys = {k for (k,) in db.session.query(ChargeType.key).all()}
    while f"custom_{n}" in keys:
        n += 1
    return f"custom_{n}"


def overrides_by_type(owner):
    """``{charge_type_id: ChargeOverride}`` eines Kunden oder Objekts."""
    if owner is None or owner.id is None:
        return {}
    return {ov.charge_type_id: ov for ov in owner.charge_overrides}


def override_form_rows(owner, form=None):
    """Zeilen fuer den Formularabschnitt: je ueberschreibbarer Gebührenart
    ``{"ct", "mode", "amount"}`` — aus dem Formular (nach Fehler) oder dem
    gespeicherten Stand."""
    current = overrides_by_type(owner)
    rows = []
    for ct in overridable_charge_types():
        if form is not None:
            mode = form.get(f"ov_mode_{ct.id}", MODE_INHERIT)
            amount = form.get(f"ov_amount_{ct.id}", "")
        else:
            ov = current.get(ct.id)
            if ov is None:
                mode, amount = MODE_INHERIT, ""
            elif ov.amount is None:
                mode, amount = MODE_EXEMPT, ""
            else:
                mode = MODE_AMOUNT
                amount = format_amount(ov.amount, ct.is_per_m3)
        if not ct.active and mode == MODE_INHERIT:
            continue  # ausgeblendete Art ohne Override nicht anbieten
        rows.append({"ct": ct, "mode": mode, "amount": amount})
    return rows


def format_amount(value, per_m3=False):
    """Betrag fuer ein Formularfeld: deutsches Komma, 4 (je m³) bzw. 2 Stellen."""
    if value is None:
        return ""
    return f"{Decimal(str(value)):.{4 if per_m3 else 2}f}".replace(".", ",")


def parse_amount(raw):
    """Betrag aus einem Formularfeld: ``"1.234,56"``, ``"1234,56"`` und
    ``"1234.56"`` werden akzeptiert. Leer/ungueltig -> ``None``."""
    raw = (raw or "").strip().replace(" ", "")
    if not raw:
        return None
    # "1.234,56" / "1234,56" / "1234.56"
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _parse_override_form(form):
    """``({charge_type_id: (mode, amount)}, error)`` — nur Gebührenarten, deren
    Auswahlfeld im Formular steht (sonst bleibt ihr Stand unveraendert).

    Komfort: ein eingetippter Betrag bei „wie Tarif" gilt als eigener Betrag.
    """
    parsed = {}
    for ct in overridable_charge_types():
        field = f"ov_mode_{ct.id}"
        if field not in form:
            continue
        mode = form.get(field, MODE_INHERIT) or MODE_INHERIT
        raw = (form.get(f"ov_amount_{ct.id}") or "").strip()
        if mode == MODE_INHERIT and raw:
            mode = MODE_AMOUNT
        amount = None
        if mode == MODE_AMOUNT:
            amount = parse_amount(raw)
            if amount is None:
                return None, f"Bitte für „{ct.label}“ einen gültigen Betrag angeben."
            if amount < 0:
                return None, f"Der Betrag für „{ct.label}“ darf nicht negativ sein."
            if amount != amount.quantize(Decimal("0.0001")):
                return None, f"Höchstens vier Nachkommastellen bei „{ct.label}“."
        elif mode not in (MODE_INHERIT, MODE_EXEMPT):
            continue
        parsed[ct.id] = (mode, amount)
    return parsed, None


def validate_override_form(form):
    """Deutsche Fehlermeldung oder ``None`` — ohne Seiteneffekte."""
    return _parse_override_form(form)[1]


def apply_override_form(owner, form):
    """Schreibt die individuellen Gebühren aus ``form`` auf ``owner`` (Kunde
    oder Objekt). Validiert zuerst alles und aendert bei einem Fehler nichts.
    Gibt eine deutsche Fehlermeldung zurueck oder ``None``.
    """
    parsed, err = _parse_override_form(form)
    if err:
        return err
    current = overrides_by_type(owner)
    for ct_id, (mode, amount) in parsed.items():
        existing = current.get(ct_id)
        if mode == MODE_INHERIT:
            if existing is not None:
                owner.charge_overrides.remove(existing)
            continue
        if existing is None:
            existing = ChargeOverride(charge_type_id=ct_id)
            owner.charge_overrides.append(existing)
        existing.amount = amount
    return None


def overrides_display(owner):
    """Kurzliste fuer Detailseiten: ``[(label, text)]`` der gesetzten Overrides."""
    out = []
    for ov in sorted(owner.charge_overrides,
                     key=lambda o: (o.charge_type.sort_order, o.charge_type.label)):
        ct = ov.charge_type
        if ov.amount is None:
            text = "entfällt"
        elif ct.is_per_m3:
            text = f"{Decimal(str(ov.amount)):.4f} €/m³".replace(".", ",")
        else:
            text = f"{Decimal(str(ov.amount)):.2f} €".replace(".", ",")
        out.append((ct.label, text))
    return out


def tariff_form_params(tariff, *, name=None, valid_from=None, amounts=None):
    """Formularwerte (Dict) fuer ein neues Tarif-Formular auf Basis eines
    bestehenden Tarifs — fuer „Tarif kopieren" und die Plankostenrechnung.

    ``amounts`` (``{charge_key: Decimal}``) ersetzt Betraege bzw. nimmt eine
    Gebührenart neu auf, die der Quelltarif nicht hat (z.B. eine neue
    Grundgebuehr aus einem Tarifpaket).
    """
    def _fmt(value, per_m3):
        if value is None:
            return ""
        return f"{Decimal(str(value)):.{4 if per_m3 else 2}f}".replace(".", ",")

    def _tax(rate):
        if rate is None:
            return ""
        return format(Decimal(str(rate)).normalize(), "f")

    amounts = dict(amounts or {})
    params = {
        "name": name if name is not None else (tariff.name if tariff else ""),
        "valid_from": valid_from if valid_from is not None else (
            tariff.valid_from if tariff else ""),
        "valid_to": "",
        "notes": (tariff.notes or "") if tariff else "",
    }
    seen = set()
    for comp in (tariff.components if tariff else []):
        ct = comp.charge_type
        amount = amounts.pop(ct.key, comp.amount)
        params[f"comp_on_{ct.id}"] = "1"
        params[f"comp_label_{ct.id}"] = comp.label
        params[f"comp_amount_{ct.id}"] = _fmt(amount, ct.is_per_m3)
        params[f"comp_tax_{ct.id}"] = _tax(comp.tax_rate)
        params[f"comp_account_{ct.id}"] = comp.account_id or ""
        params[f"comp_valid_from_{ct.id}"] = (
            comp.valid_from.isoformat() if comp.valid_from else "")
        seen.add(ct.key)
    for key, amount in amounts.items():
        if key in seen or amount is None:
            continue
        ct = charge_type(key)
        if ct is None:
            continue
        params[f"comp_on_{ct.id}"] = "1"
        params[f"comp_label_{ct.id}"] = ct.label
        params[f"comp_amount_{ct.id}"] = _fmt(amount, ct.is_per_m3)
    return params


_WATER_RATE = object()   # Sentinel: USt = Wasser-Satz des Mandanten


def build_tariff(*, name, valid_from, water_price, valid_to=None, base_fee=None,
                 additional_fee=None, water_levy=None, tax_rate=_WATER_RATE,
                 labels=None, accounts=None, notes=None, include_empty_fees=True):
    """Legt einen Tarif mit Standardpositionen an (Seeds, Tests, Demo) —
    ``flush``, kein Commit.

    ``tax_rate`` gilt fuer alle Positionen (Default: Wasser-Satz des
    Mandanten, ``None`` = keine USt). ``labels``/``accounts`` sind Dicts
    ``{charge_key: Wert}``. Grund- und Zusatzgebuehr werden — wie in der
    Datenmigration — auch ohne Betrag angelegt (``include_empty_fees``), damit
    individuelle Gebuehren wirken koennen.
    """
    from app import tax_service
    ensure_system_charge_types()
    if tax_rate is _WATER_RATE:
        tax_rate = tax_service.water_tax_rate()
    labels = labels or {}
    accounts = accounts or {}
    tariff = WaterTariff(name=name, valid_from=valid_from, valid_to=valid_to, notes=notes)
    specs = [
        (ChargeType.KEY_WATER, water_price, True),
        (ChargeType.KEY_WATER_LEVY, water_levy, False),
        (ChargeType.KEY_BASE_FEE, base_fee, include_empty_fees),
        (ChargeType.KEY_ADDITIONAL_FEE, additional_fee, include_empty_fees),
    ]
    for key, amount, keep_empty in specs:
        if amount is None and not keep_empty:
            continue
        ct = charge_type(key)
        tariff.components.append(TariffComponent(
            charge_type=ct,
            label=labels.get(key, ct.label),
            amount=(Decimal(str(amount)) if amount is not None else None),
            tax_rate=(Decimal(str(tax_rate)) if tax_rate is not None else None),
            account_id=accounts.get(key),
            sort_order=ct.sort_order,
        ))
    db.session.add(tariff)
    db.session.flush()
    return tariff

