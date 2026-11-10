"""Einstellungen und Beschriftungen der Teilrechnungen.

Alle Werte liegen als ``AppSetting`` unter ``installments.*`` und werden nur
ueber diese Funktionen gelesen (``AppSetting.get`` ist der einzige Lesepfad,
damit ``setting_overrides`` fuer Vorschauen greift).
"""
from decimal import Decimal, InvalidOperation

ENABLED_KEY = "installments.enabled"
DUE_DATES_KEY = "installments.due_dates"
ROUNDING_KEY = "installments.rounding"
MIN_ANNUAL_KEY = "installments.min_annual_amount"
CREDIT_MODE_KEY = "installments.credit_mode"
TITLE_KEY = "installments.title"
# Gebuehrenarten (``ChargeType.key``), die NICHT in den Teilbetrag eingehen —
# sie verrechnet erst die Jahresabrechnung. Gespeichert werden bewusst die
# ausgeschlossenen Schluessel: eine neue Gebuehrenart ist damit automatisch
# dabei, und ohne Eintrag gilt wie bisher „alle“.
EXCLUDED_CHARGES_KEY = "installments.excluded_charges"

DEFAULT_DUE_DATES = "15.02.;15.05.;15.08.;15.11."
ROUNDING_CHOICES = (
    ("0", "nicht runden (auf Cent)"),
    ("1", "auf volle Euro"),
    ("5", "auf 5 Euro"),
    ("10", "auf 10 Euro"),
)
DEFAULT_ROUNDING = "1"

CREDIT_OFFSET = "offset"
CREDIT_REFUND = "refund"
CREDIT_MODES = (
    (CREDIT_OFFSET, "mit den nächsten Teilrechnungen verrechnen"),
    (CREDIT_REFUND, "auszahlen (Rücküberweisung)"),
)

# Alle Keys, die die Darstellung gesperrter Belege beeinflussen (Render-Cache).
RENDER_KEYS = (TITLE_KEY,)


def _get(key, default=None):
    from app.models import AppSetting
    try:
        value = AppSetting.get(key)
    except Exception:  # noqa: BLE001 — Tabelle fehlt (Migration offen)
        return default
    return default if value is None else value


def is_enabled():
    """Ist das Teilrechnungs-Verfahren beim Mandanten eingeschaltet?"""
    return str(_get(ENABLED_KEY, "false")).lower() == "true"


def due_dates_text():
    return (_get(DUE_DATES_KEY) or "").strip() or DEFAULT_DUE_DATES


def rounding_step():
    """Rundungsschritt des Teilbetrags in Euro (0 = Cent)."""
    raw = str(_get(ROUNDING_KEY, DEFAULT_ROUNDING) or DEFAULT_ROUNDING)
    return Decimal(raw) if raw in {k for k, _ in ROUNDING_CHOICES} else Decimal(DEFAULT_ROUNDING)


def min_annual_amount():
    raw = str(_get(MIN_ANNUAL_KEY, "") or "").strip().replace(",", ".")
    if not raw:
        return Decimal("0")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal("0")
    return value if value > 0 else Decimal("0")


def credit_mode():
    raw = _get(CREDIT_MODE_KEY, CREDIT_OFFSET)
    return raw if raw in (CREDIT_OFFSET, CREDIT_REFUND) else CREDIT_OFFSET


def excluded_charge_keys():
    """Gebuehrenart-Schluessel, die nicht in den Teilbetrag eingehen.

    Der Wasserverbrauch ist nie ausgeschlossen: er ist die Grundlage der
    Planung und der Rueckfall fuer Konto/USt der Teilrechnung."""
    from app.models import ChargeType
    raw = str(_get(EXCLUDED_CHARGES_KEY, "") or "")
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    keys.discard(ChargeType.KEY_WATER)
    return frozenset(keys)


def _charge_type_choices():
    """Aktive Gebuehrenarten fuer die Einstellungskarte (Katalog-Reihenfolge)."""
    from app.models import ChargeType
    try:
        types = (ChargeType.query.filter(ChargeType.active.is_(True))
                 .order_by(ChargeType.sort_order, ChargeType.id).all())
    except Exception:  # noqa: BLE001 — Tabelle fehlt (Migration offen)
        return []
    excluded = excluded_charge_keys()
    return [{"key": ct.key, "label": ct.label, "per_m3": ct.is_per_m3,
             "fixed": ct.key == ChargeType.KEY_WATER,
             "included": ct.key not in excluded} for ct in types]


def excluded_charge_labels():
    """Bezeichnungen der ausgeschlossenen Gebuehrenarten (fuer Hinweise)."""
    from app.models import ChargeType
    excluded = excluded_charge_keys()
    if not excluded:
        return []
    types = (ChargeType.query.filter(ChargeType.key.in_(list(excluded)))
             .order_by(ChargeType.sort_order, ChargeType.id).all())
    return [ct.label for ct in types]


def installment_title():
    """Belegtitel der Teilrechnung: Mandanten-Text, sonst Landesstandard."""
    from app import country
    custom = (_get(TITLE_KEY) or "").strip()
    return custom or country.term("installment_title")


def installment_word():
    """„Teilbetrag“ (AT) / „Abschlag“ (DE)."""
    from app import country
    return country.term("installment_word")


def installment_words():
    """Mehrzahl: „Teilbeträge“ (AT) / „Abschläge“ (DE)."""
    from app import country
    return country.term("installment_words")


def annual_settlement_title():
    from app import country
    return country.term("annual_settlement_title")


# Kennzeichen (``InvoiceItem.charge_key``) der Zeilen, die das Verfahren selbst
# erzeugt. Keine Gebuehrenarten — Uebersichten zaehlen sie in die eigene Spalte
# „Teilbeträge“, nie in die Gebuehrenspalten.
KEY_INSTALLMENT = "installment"              # Teilbetrag auf der Teilrechnung
KEY_DEDUCTION = "installment_deduction"      # Gegenverrechnung auf der Jahresabrechnung
KEY_CREDIT_CARRY = "credit_carry"            # Guthaben-Uebertrag (Kappung auf 0)
INTERNAL_KEYS = frozenset({KEY_INSTALLMENT, KEY_DEDUCTION, KEY_CREDIT_CARRY})


def form_values():
    """Aktuelle Werte fuer die Einstellungskarte."""
    return {
        "enabled": is_enabled(),
        "due_dates": due_dates_text(),
        "rounding": str(rounding_step().normalize()) if rounding_step() else "0",
        "min_annual": (str(min_annual_amount()).replace(".", ",")
                       if min_annual_amount() else ""),
        "credit_mode": credit_mode(),
        "title": (_get(TITLE_KEY) or ""),
        "default_title": _default_title(),
        "rounding_choices": ROUNDING_CHOICES,
        "credit_modes": CREDIT_MODES,
        "charge_types": _charge_type_choices(),
    }


def _default_title():
    from app import country
    return country.term("installment_title")


def save_from_form(form):
    """Speichert die Karte „Teilrechnungen“ (nur mit Marker
    ``installment_fields=1`` aufrufen). Gibt Fehlermeldungen zurueck; ein
    ungueltiges Feld bleibt unveraendert."""
    from app.installments.schedule import ScheduleError, format_due_dates, parse_due_dates
    from app.models import AppSetting
    errors = []
    AppSetting.set(ENABLED_KEY, "true" if form.get("installments_enabled") else "false")
    raw = (form.get("installments_due_dates") or "").strip()
    try:
        AppSetting.set(DUE_DATES_KEY, format_due_dates(parse_due_dates(raw or DEFAULT_DUE_DATES)))
    except ScheduleError as exc:
        errors.append(f"Fälligkeiten nicht übernommen: {exc}")
    rounding = form.get("installments_rounding")
    if rounding in {k for k, _ in ROUNDING_CHOICES}:
        AppSetting.set(ROUNDING_KEY, rounding)
    raw_min = (form.get("installments_min_annual") or "").strip().replace(".", "").replace(",", ".")
    if not raw_min:
        AppSetting.set(MIN_ANNUAL_KEY, None)
    else:
        try:
            value = Decimal(raw_min)
            if value < 0:
                raise InvalidOperation
            AppSetting.set(MIN_ANNUAL_KEY, str(value.quantize(Decimal("0.01"))))
        except InvalidOperation:
            errors.append("Mindest-Jahresbetrag nicht übernommen: bitte einen Betrag angeben.")
    mode = form.get("installments_credit_mode")
    if mode in (CREDIT_OFFSET, CREDIT_REFUND):
        AppSetting.set(CREDIT_MODE_KEY, mode)
    title = (form.get("installments_title") or "").strip()[:60]
    AppSetting.set(TITLE_KEY, title or None)
    if form.get("installments_charge_fields"):
        _save_excluded_charges(form)
    return errors


def _save_excluded_charges(form):
    """Ausgeschlossen = aktive Gebuehrenarten ohne Haekchen. Ausgeschlossene,
    inzwischen inaktive Arten bleiben ausgeschlossen (aeltere Tarife rechnen
    sie weiter ab)."""
    from app.models import AppSetting, ChargeType
    getlist = getattr(form, "getlist", None)
    chosen = set(getlist("installments_charges") if getlist else
                 (form.get("installments_charges") or []))
    active = {ct.key for ct in ChargeType.query.filter(ChargeType.active.is_(True)).all()}
    excluded = (set(excluded_charge_keys()) - active) | (active - chosen)
    excluded.discard(ChargeType.KEY_WATER)
    AppSetting.set(EXCLUDED_CHARGES_KEY, ",".join(sorted(excluded)) or None)
