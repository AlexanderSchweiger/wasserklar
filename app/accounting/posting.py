"""Festschreiben (Verbuchen) von Buchungen — wann eine offene Buchung automatisch gesperrt wird.

Eine Buchung ist „Offen“, bis sie „Verbucht“ wird; danach sind Datum, Betrag, Belegnummer und das
Löschen gesperrt (Korrektur nur per Storno). Dieses Modul entscheidet **nur, wann** der nächtliche
Lauf (``mark-posted`` → ``accounting.services.auto_post_bookings``) verbucht. Die Sperren selbst
hängen weiter am Status.

Modi (``AppSetting`` ``accounting.posting_mode``, Einstellungen → Buchhaltung):

* ``period`` (Standard, auch ohne Eintrag) — je **Voranmeldungszeitraum** (Monat bzw. Quartal aus
  ``tax.vat_return_period``): alle Buchungen bis zum Zeitraumende werden am Ende des Sperrtags
  verbucht (Tag ``accounting.posting_lock_day`` im Folgemonat bzw. zweitfolgenden Monat,
  ``accounting.posting_lock_month_offset``; Vorgabe aus dem Länderprofil: AT 15./zweitfolgender
  Monat, DE 10./Folgemonat — jeweils die Abgabefrist). Der Lauf um 00:05 am Tag danach verbucht.
* ``daily`` — wie früher: alles bis zum Vortag.
* ``off`` — nie automatisch; nur ohne aktives Übergabeformat erlaubt (sonst gilt ``period``).

Unabhängig vom Modus verbuchen „Zeitraum abschließen“ (USt-Voranmeldung), der Jahresabschluss und
eine Übergabe an die Steuerberatung sofort. Ein verbuchter Satz wird nie wieder offen.

Die Funktionen im ersten Teil sind rein (kein Flask, keine DB) und unit-getestet.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

MODE_KEY = "accounting.posting_mode"
LOCK_DAY_KEY = "accounting.posting_lock_day"
LOCK_OFFSET_KEY = "accounting.posting_lock_month_offset"

MODE_PERIOD = "period"
MODE_DAILY = "daily"
MODE_OFF = "off"
MODES = (MODE_PERIOD, MODE_DAILY, MODE_OFF)

PERIOD_MONTH = "month"
PERIOD_QUARTER = "quarter"

LOCK_DAY_MIN, LOCK_DAY_MAX = 1, 28      # bis 28: jeder Monat hat den Tag
LOCK_OFFSETS = (1, 2)                   # Folgemonat / zweitfolgender Monat
WARN_DAYS = 5                           # Vorwarnung ab so vielen Tagen vor dem Sperrtag


# ---------------------------------------------------------------------------
# Reine Regeln
# ---------------------------------------------------------------------------

def _months_per_period(period):
    return 1 if period == PERIOD_MONTH else 3


def period_start_for(d, period):
    """Erster Tag des Monats bzw. Quartals, in dem ``d`` liegt."""
    if period == PERIOD_MONTH:
        return date(d.year, d.month, 1)
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def period_end_for(d, period):
    """Letzter Tag des Monats bzw. Quartals, in dem ``d`` liegt."""
    start = period_start_for(d, period)
    end_month = start.month + _months_per_period(period) - 1
    return date(start.year, end_month, calendar.monthrange(start.year, end_month)[1])


def previous_period_end(period_end, period):
    """Ende des Zeitraums vor dem, der am ``period_end`` endet."""
    return period_start_for(period_end, period) - timedelta(days=1)


def next_period_end(period_end, period):
    """Ende des Zeitraums nach dem, der am ``period_end`` endet."""
    return period_end_for(period_end + timedelta(days=1), period)


def lock_date(period_end, day, month_offset):
    """Sperrtag eines Zeitraums: Tag ``day`` im ``month_offset``-ten Monat nach dem Zeitraumende."""
    month_index = period_end.year * 12 + (period_end.month - 1) + month_offset
    return date(month_index // 12, month_index % 12 + 1, day)


def cutoff(today, mode, period, day, month_offset):
    """Bis zu welchem Datum (einschließlich) der Lauf am ``today`` verbucht — ``None`` = nichts.

    ``daily``: gestern. ``period``: das späteste Zeitraumende, dessen Sperrtag schon **vorbei** ist
    (am Sperrtag selbst bleibt der Zeitraum noch offen; gesperrt wird „am Ende des Tages“).
    """
    if mode == MODE_OFF:
        return None
    if mode == MODE_DAILY:
        return today - timedelta(days=1)
    end = period_end_for(today, period)
    # Höchstens ein paar Schritte: der Sperrtag liegt maximal zwei Monate nach dem Zeitraumende.
    for _ in range(12):
        if lock_date(end, day, month_offset) < today:
            return end
        end = previous_period_end(end, period)
    return end


def lock_date_for(booking_date, mode, period, day, month_offset):
    """Tag, an dessen Ende eine Buchung vom ``booking_date`` automatisch verbucht wird (``None`` = nie)."""
    if mode == MODE_OFF:
        return None
    if mode == MODE_DAILY:
        return booking_date
    return lock_date(period_end_for(booking_date, period), day, month_offset)


def parse_lock_day(raw):
    """Formular-/Einstellungswert → Sperrtag (1–28) oder ``None``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if LOCK_DAY_MIN <= value <= LOCK_DAY_MAX else None


def parse_lock_offset(raw):
    """Formular-/Einstellungswert → Monatsversatz (1 oder 2) oder ``None``."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value in LOCK_OFFSETS else None


# ---------------------------------------------------------------------------
# Einstellungen des Mandanten
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PostingSettings:
    mode: str               # gespeicherter Modus (für das Formular)
    effective_mode: str     # gilt tatsächlich (``off`` mit aktiver Übergabe → ``period``)
    period: str             # ``month`` | ``quarter``
    lock_day: int
    month_offset: int
    default_lock_day: int
    default_month_offset: int
    handover_active: bool

    @property
    def off_blocked(self):
        """„Aus“ ist gewählt, gilt aber nicht, weil ein Übergabeformat aktiv ist."""
        return self.mode == MODE_OFF and self.effective_mode != MODE_OFF


_G_KEY = "_posting_settings"


def _read():
    from app import country, tax_service
    from app.accounting import handover
    from app.models import AppSetting

    profile = country.current_profile()
    default_day = profile.posting_lock_day
    default_offset = profile.posting_lock_month_offset
    mode = AppSetting.get(MODE_KEY)
    mode = mode if mode in MODES else MODE_PERIOD
    handover_active = handover.is_active()
    effective = MODE_PERIOD if (mode == MODE_OFF and handover_active) else mode
    return PostingSettings(
        mode=mode,
        effective_mode=effective,
        period=(PERIOD_MONTH if tax_service.vat_return_period() == tax_service.VAT_RETURN_MONTH
                else PERIOD_QUARTER),
        lock_day=parse_lock_day(AppSetting.get(LOCK_DAY_KEY)) or default_day,
        month_offset=parse_lock_offset(AppSetting.get(LOCK_OFFSET_KEY)) or default_offset,
        default_lock_day=default_day,
        default_month_offset=default_offset,
        handover_active=handover_active,
    )


def settings():
    """Einstellungen des Mandanten, je Request einmal gelesen (Listen fragen je Zeile).

    Der Cache ist an das Request-Objekt gebunden, nicht nur an ``g``: ``g`` gehört zum
    App-Kontext, und ein Request ohne eigenen App-Kontext (Test-Client in einem schon
    laufenden Kontext, Hintergrundarbeit) teilte ihn sonst mit dem vorigen.
    """
    from flask import g, has_request_context, request
    if not has_request_context():
        return _read()
    marker = id(request._get_current_object())
    cached = g.get(_G_KEY)
    if cached is None or cached[0] != marker:
        cached = (marker, _read())
        setattr(g, _G_KEY, cached)
    return cached[1]


def invalidate():
    """Nach dem Speichern der Einstellungen: im selben Request neu lesen."""
    from flask import g, has_request_context
    if has_request_context():
        g.pop(_G_KEY, None)


FORM_MARKER = "posting_fields"


def save_from_form(form):
    """Speichert die Karte „Buchungen festschreiben“ — nur mit Marker ``posting_fields=1``
    (ein Formular ohne die Felder ändert nichts). Läuft **nach** dem Speichern des
    Übergabeformats, damit „Aus“ gegen den neuen Stand geprüft wird. Liefert Meldungen
    ``[(text, kategorie)]``; committet nicht."""
    from app.accounting import handover
    from app.models import AppSetting

    if form.get(FORM_MARKER) != "1":
        return []
    messages = []
    mode = form.get("posting_mode")
    if mode in MODES:
        if mode == MODE_OFF and handover.is_active():
            messages.append(("Automatisches Verbuchen lässt sich nicht ausschalten, solange eine "
                             "Übergabe an die Steuerberatung eingeschaltet ist — es bleibt beim "
                             "Verbuchen je Voranmeldungszeitraum.", "warning"))
            mode = MODE_PERIOD
        AppSetting.set(MODE_KEY, mode)
    # Gleich der Länder-Vorgabe → kein Eintrag (zieht beim Landwechsel mit). Verglichen wird mit
    # der Vorgabe, die das Formular beim Anzeigen hatte (versteckte Felder) und mit der aktuellen —
    # sonst schriebe ein Landwechsel im selben Formular die alte Vorgabe fest.
    profile_day, profile_offset = _profile_defaults()
    shown_day = parse_lock_day(form.get("posting_lock_day_default"))
    shown_offset = parse_lock_offset(form.get("posting_lock_month_offset_default"))
    if "posting_lock_day" in form:
        raw = (form.get("posting_lock_day") or "").strip()
        day = parse_lock_day(raw)
        if raw and day is None:
            messages.append((f"Der Sperrtag muss zwischen {LOCK_DAY_MIN} und {LOCK_DAY_MAX} "
                             "liegen — nicht übernommen.", "warning"))
        elif not raw or day in (profile_day, shown_day):
            AppSetting.set(LOCK_DAY_KEY, None)
        else:
            AppSetting.set(LOCK_DAY_KEY, str(day))
    if "posting_lock_month_offset" in form:
        offset = parse_lock_offset(form.get("posting_lock_month_offset"))
        if offset is not None:
            AppSetting.set(LOCK_OFFSET_KEY,
                           None if offset in (profile_offset, shown_offset) else str(offset))
    invalidate()
    return messages


def _profile_defaults():
    from app import country
    profile = country.current_profile()
    return profile.posting_lock_day, profile.posting_lock_month_offset


def current_cutoff(today=None, cfg=None):
    cfg = cfg or settings()
    return cutoff(today or date.today(), cfg.effective_mode, cfg.period, cfg.lock_day,
                  cfg.month_offset)


def booking_lock_date(booking_date, today=None, cfg=None):
    """Anzeige „änderbar bis“: Tag, an dessen Ende die offene Buchung verbucht wird (``None`` = nie).

    Ist der Regeltag schon vorbei (die Buchung wurde nachträglich mit altem Datum erfasst), wird sie
    in der kommenden Nacht verbucht — dann heute.
    """
    if booking_date is None:
        return None
    cfg = cfg or settings()
    lock = lock_date_for(booking_date, cfg.effective_mode, cfg.period, cfg.lock_day,
                         cfg.month_offset)
    if lock is None:
        return None
    today = today or date.today()
    return max(lock, today)


def open_hint(booking_date):
    """Tooltip einer offenen Buchung in Listen: „Offen – änderbar bis 15.05.2027“."""
    lock = booking_lock_date(booking_date)
    if lock is None:
        return "Offen – wird nicht automatisch verbucht"
    if lock <= date.today():
        return "Offen – wird heute Nacht verbucht"
    return f"Offen – änderbar bis {lock:%d.%m.%Y}"


def period_label(period_end, period):
    """„März 2027“ bzw. „Q1/2027“."""
    from app.accounting.services import ust_period_label
    if period == PERIOD_MONTH:
        return ust_period_label(period_end.year, 0, period_end.month)
    return ust_period_label(period_end.year, (period_end.month - 1) // 3 + 1)


def period_key(period_end, period):
    """Zeitraum-Schlüssel der USt-Voranmeldung (``m3``/``q1``) für Links."""
    if period == PERIOD_MONTH:
        return f"m{period_end.month}"
    return f"q{(period_end.month - 1) // 3 + 1}"


@dataclass(frozen=True)
class NextLock:
    mode: str
    open_count: int          # alle offenen Buchungen
    lock_date: date = None   # nächster Sperrtag (period/daily)
    period_end: date = None  # bis dahin wird verbucht
    period_label: str = ""
    period_key: str = ""
    due_count: int = 0       # offene Buchungen, die am ``lock_date`` verbucht werden
    days_left: int = None
    overdue_count: int = 0   # Nachträge in schon gesperrte Zeiträume: werden heute Nacht verbucht

    @property
    def warn(self):
        return (self.mode == MODE_PERIOD and self.due_count > 0 and self.days_left is not None
                and self.days_left <= WARN_DAYS)


def next_lock(today=None, cfg=None):
    """Was als Nächstes verbucht wird — für die Hinweisleiste der Buchungsliste und das Dashboard."""
    from app.accounting.services import open_bookings_query
    from app.models import Booking

    today = today or date.today()
    cfg = cfg or settings()
    open_q = open_bookings_query(until=date.max)
    open_count = open_q.count()
    mode = cfg.effective_mode
    if mode == MODE_OFF or not open_count:
        return NextLock(mode=mode, open_count=open_count)
    if mode == MODE_DAILY:
        return NextLock(mode=mode, open_count=open_count, lock_date=today,
                        period_end=today - timedelta(days=1),
                        due_count=open_q.filter(Booking.date < today).count(), days_left=0)
    last = cutoff(today, mode, cfg.period, cfg.lock_day, cfg.month_offset)
    end = next_period_end(last, cfg.period)
    lock = lock_date(end, cfg.lock_day, cfg.month_offset)
    overdue = open_q.filter(Booking.date <= last).count()
    return NextLock(
        mode=mode, open_count=open_count, lock_date=lock, period_end=end,
        period_label=period_label(end, cfg.period), period_key=period_key(end, cfg.period),
        due_count=open_q.filter(Booking.date > last, Booking.date <= end).count(),
        days_left=(lock - today).days, overdue_count=overdue,
    )


# ---------------------------------------------------------------------------
# Nachbuchung in einen schon abgeschlossenen Zeitraum
# ---------------------------------------------------------------------------
# Erlaubt bleibt sie (gesperrt ist nur ein abgeschlossenes Buchungsjahr) — sie wird in der nächsten
# Nacht verbucht, ändert aber die Zahlen einer vermutlich schon abgegebenen Voranmeldung. Darauf
# weist das Buchungsformular hin.

CLOSED_UNTIL_KEY = "accounting.closed_until"   # letztes per „Zeitraum abschließen“ geschlossenes Ende


def mark_closed_until(period_end):
    """„Zeitraum abschließen“ merkt sich das späteste abgeschlossene Zeitraumende (committet nicht)."""
    from app.models import AppSetting
    current = closed_until()
    if current is None or period_end > current:
        AppSetting.set(CLOSED_UNTIL_KEY, period_end.isoformat())


def closed_until():
    from app.models import AppSetting
    raw = AppSetting.get(CLOSED_UNTIL_KEY)
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def closed_through(today=None, cfg=None):
    """Bis zu welchem Datum die Voranmeldung als abgeschlossen gilt (``None`` = nichts).

    Das ist das spätere von: per „Zeitraum abschließen“ geschlossen, und dem Zeitraum, dessen
    Sperrtag vorbei ist — Letzteres **unabhängig vom Modus**, denn der Sperrtag steht für die
    Abgabefrist, auch wenn täglich oder gar nicht automatisch verbucht wird.
    """
    cfg = cfg or settings()
    rule = cutoff(today or date.today(), MODE_PERIOD, cfg.period, cfg.lock_day, cfg.month_offset)
    marked = closed_until()
    candidates = [d for d in (rule, marked) if d is not None]
    return max(candidates) if candidates else None


def late_entry_context():
    """Daten für den Hinweis im Buchungsformular: ``{"closed_through": date|None, "vat_years": [...]}``.

    Ohne umsatzsteuerpflichtiges Buchungsjahr gibt es keine Voranmeldung → kein Hinweis.
    """
    from app.models import FiscalYear
    vat_years = [fy.year for fy in FiscalYear.query.filter_by(is_vat_liable=True).all()]
    if not vat_years:
        return {"closed_through": None, "vat_years": []}
    return {"closed_through": closed_through(), "vat_years": sorted(vat_years)}


def late_entry_warning(booking_date):
    """Text für eine Nachbuchung in einen abgeschlossenen, umsatzsteuerpflichtigen Zeitraum, sonst ``None``."""
    if booking_date is None:
        return None
    ctx = late_entry_context()
    through = ctx["closed_through"]
    if through is None or booking_date > through or booking_date.year not in ctx["vat_years"]:
        return None
    label = period_label(period_end_for(booking_date, settings().period), settings().period)
    return (f"{label} ist bereits abgeschlossen — die Umsatzsteuer-Voranmeldung dafür ist vermutlich "
            "schon abgegeben. Die Buchung zählt trotzdem zu diesem Zeitraum und wird in der nächsten "
            "Nacht verbucht. Bitte prüfen, ob die Voranmeldung berichtigt werden muss.")


def example_sentence(cfg=None, today=None):
    """Beispiel für die Einstellungskarte: „Q1/2027 wird am Ende des 15.05.2027 verbucht.“"""
    cfg = cfg or settings()
    today = today or date.today()
    mode = cfg.effective_mode
    if mode == MODE_OFF:
        return "Buchungen werden nicht automatisch verbucht."
    if mode == MODE_DAILY:
        return "Jede Buchung wird in der Nacht nach ihrem Buchungsdatum verbucht."
    last = cutoff(today, mode, cfg.period, cfg.lock_day, cfg.month_offset)
    end = next_period_end(last, cfg.period)
    lock = lock_date(end, cfg.lock_day, cfg.month_offset)
    return (f"{period_label(end, cfg.period)} wird am Ende des {lock:%d.%m.%Y} verbucht "
            "und ist ab dann gesperrt.")
