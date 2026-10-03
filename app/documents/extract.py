"""Belegtext aus digitalen PDFs lesen und daraus Angaben vorschlagen (Belegablage, Stufe 2).

Kein OCR: gelesen wird nur die **Textebene** eines PDFs (``pypdf``). Ein Scan oder Foto liefert
keinen Text — dafuer ist die OCR-Stufe vorgesehen. Die Datei bleibt unberuehrt; Text und
Vorschlaege sind abgeleitete Daten, die der Nutzer bestaetigt (``Document.meta_auto``).

Das Modul kennt weder Flask noch die Datenbank: ``extract_pdf_text`` und ``suggest`` sind reine
Funktionen. Die Heuristiken sind bewusst konservativ — lieber nichts vorschlagen als etwas
Falsches —, und jeder Treffer ist ein **Vorschlag**, nie eine Buchungsgrundlage.

Gelesen wird in zwei Schritten: erst der Text im Layout-Modus (haelt Tabellenspalten
zusammen, damit „Rechnungsnummer | Datum“ ueber seinen Werten stehen bleibt), bei einem Fehler
im Plain-Modus. Seitenzahl, Textmenge und Zeit sind begrenzt, damit ein praeparierte Datei
den Upload nicht blockiert.
"""
# Pflicht, nicht Zierde: ``Suggestions`` hat ein Feld ``date``. In ``date: date | None = None`` setzt
# Python < 3.14 erst ``date = None`` und wertet danach die Annotation aus (``None | None`` → TypeError
# beim Import — der Container laeuft auf 3.12, lokal 3.14 wertet Annotationen verzoegert aus).
from __future__ import annotations

import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# Eine Pruefziffer-Logik im Repo: die der E-Rechnung (BR-DE-19).
from app.einvoice.rules import _iban_valid as iban_valid

MAX_PAGES = 12            # mehr Seiten enthalten fast nie zusaetzliche Kopfdaten
MAX_CHARS = 60_000        # Obergrenze des gespeicherten Textes
TIME_BUDGET = 8.0         # Sekunden, danach wird mit dem bisher Gelesenen weitergemacht
MIN_LETTERS = 15          # weniger Buchstaben = praktisch kein Text (Scan)

STATUS_OK = "ok"
STATUS_EMPTY = "empty"    # PDF ohne Textebene (Scan)
STATUS_ERROR = "error"    # nicht lesbar


@dataclass
class TextResult:
    status: str
    text: str = ""
    pages: int = 0
    truncated: bool = False


@dataclass
class Suggestions:
    kind: str | None = None
    number: str | None = None
    date: date | None = None
    amount: Decimal | None = None
    vat_ids: list = field(default_factory=list)
    ibans: list = field(default_factory=list)

    @property
    def core(self):
        """Die Angaben, die in die Belegfelder uebernommen werden."""
        return {"kind": self.kind, "number": self.number, "document_date": self.date, "amount": self.amount}

    def __bool__(self):
        return any(self.core.values()) or bool(self.vat_ids) or bool(self.ibans)


# ---------------------------------------------------------------------------
# Text lesen
# ---------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WIDE_SPACE = re.compile(r"[ \t]{3,}")
_BLANKS = re.compile(r"\n{3,}")


def clean_text(raw):
    """Normalisiert gelesenen Text: keine Steuerzeichen, kein Wildwuchs an Leerraum, immer
    gueltiges UTF-8 (lose Surrogate wuerden das Speichern in der DB abbrechen)."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    text = _CTRL.sub(" ", text)
    lines = [_WIDE_SPACE.sub("  ", line).rstrip() for line in text.split("\n")]
    text = _BLANKS.sub("\n\n", "\n".join(lines)).strip("\n")
    return text.encode("utf-8", "ignore").decode("utf-8")


def _page_text(page):
    try:
        return page.extract_text(extraction_mode="layout")
    except Exception:  # noqa: BLE001 — defekte Schrift/Struktur: Plain-Modus versuchen
        pass
    try:
        return page.extract_text()
    except Exception:  # noqa: BLE001
        return ""


def extract_pdf_text(data, *, max_pages=MAX_PAGES, max_chars=MAX_CHARS, budget=TIME_BUDGET):
    """Liest die Textebene eines PDFs. Wirft nie: ein Fehler ist ``status="error"``."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            return TextResult(STATUS_ERROR)
        total = len(reader.pages)
        deadline = time.monotonic() + budget
        parts, size, read = [], 0, 0
        truncated = total > max_pages
        for index in range(min(total, max_pages)):
            if time.monotonic() > deadline:
                truncated = True
                break
            part = clean_text(_page_text(reader.pages[index]))
            read += 1
            if part:
                parts.append(part)
                size += len(part) + 2
            if size >= max_chars:
                truncated = True
                break
    except Exception:  # noqa: BLE001 — defekte PDFs sind kein Systemfehler
        return TextResult(STATUS_ERROR)
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    if sum(ch.isalpha() for ch in text) < MIN_LETTERS:
        return TextResult(STATUS_EMPTY, "", read, truncated)
    return TextResult(STATUS_OK, text, read, truncated)


# ---------------------------------------------------------------------------
# Hilfen fuer die Heuristiken
# ---------------------------------------------------------------------------

_MONTHS = {
    "jänner": 1, "januar": 1, "jan": 1, "feber": 2, "februar": 2, "feb": 2, "märz": 3, "maerz": 3,
    "mär": 3, "mrz": 3, "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_NUM = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")
_DATE_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_TEXT = re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?\s+(\d{{4}})(?!\d)", re.I)

# Betraege: deutsch (1.234,56) und englisch (1,234.50); die letzte Trennung ist das Komma bzw. der Punkt
# der Nachkommastellen. Kein Treffer inmitten eines Datums (12.03.2026) oder einer laengeren Zahl.
_AMOUNT = re.compile(
    r"(?<![\d.,])(?:\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2}|\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})(?!\d|[.,]\d)")

_VAT = re.compile(
    r"(?<![A-Z0-9])(?:ATU\s?\d{8}|DE\s?\d{9}|CZ\d{8,10}|HU\d{8}|IT\d{11}|SI\d{8}|SK\d{10}|PL\d{10}"
    r"|NL\d{9}B\d{2}|FR[A-Z0-9]{2}\s?\d{9}|BE0?\d{9,10}|LU\d{8}|HR\d{11}|CHE[\s.-]?\d{3}[\s.]?\d{3}[\s.]?\d{3})"
    r"(?![A-Za-z0-9])")
_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?(?![A-Z0-9])")


def normalize_vat_id(value):
    return re.sub(r"[\s.\-]", "", value or "").upper()


def normalize_iban(value):
    return re.sub(r"\s+", "", value or "").upper()


def _plausible(day, today):
    return 2000 <= day.year <= today.year + 1 and day <= today + timedelta(days=31)


def _dates_in(line, today):
    """``[(start, ende, date)]`` aller plausiblen Daten einer Zeile, nach Position sortiert."""
    found = []
    for m in _DATE_NUM.finditer(line):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(y) if len(y) == 4 else 2000 + int(y)
        found.append((m.start(), m.end(), d, mo, year))
    for m in _DATE_ISO.finditer(line):
        found.append((m.start(), m.end(), int(m.group(3)), int(m.group(2)), int(m.group(1))))
    for m in _DATE_TEXT.finditer(line):
        found.append((m.start(), m.end(), int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3))))
    result = []
    for start, end, d, mo, year in sorted(found):
        try:
            day = date(year, mo, d)
        except ValueError:
            continue
        if _plausible(day, today) and not any(start < e and s < end for s, e, _ in result):
            result.append((start, end, day))
    return result


def parse_amount(text):
    """``"1.234,56"`` / ``"1,234.50"`` → ``Decimal`` (sonst ``None``)."""
    last = max(text.rfind(","), text.rfind("."))
    if last < 1:
        return None
    try:
        value = Decimal(f"{re.sub(r'[.,]', '', text[:last])}.{text[last + 1:]}")
    except InvalidOperation:
        return None
    return value if Decimal("0") < value < Decimal("100000000") else None


def _amounts_in(line):
    """``[(start, ende, Decimal)]`` aller Betraege einer Zeile."""
    out = []
    for m in _AMOUNT.finditer(line):
        value = parse_amount(m.group(0))
        if value is not None:
            out.append((m.start(), m.end(), value))
    return out


def _below(lines, index, start, end, finder, rows=2):
    """Wert **unter** einer Spaltenueberschrift: in den naechsten ``rows`` nicht leeren Zeilen
    der Fund, dessen Spalte die der Ueberschrift (``start``..``end``) am staerksten ueberlappt."""
    seen = 0
    for line in lines[index + 1:]:
        if not line.strip():
            continue
        best, best_overlap = None, 0
        for s, e, value in finder(line):
            overlap = min(e, end + 3) - max(s, start - 3)
            if overlap > best_overlap:
                best, best_overlap = value, overlap
        if best is not None:
            return best
        seen += 1
        if seen >= rows:
            break
    return None


# ---------------------------------------------------------------------------
# Belegdatum
# ---------------------------------------------------------------------------

_DATE_STRONG = re.compile(
    r"(?<![a-zäöüß])(?:rechnungsdatum|belegdatum|ausstellungsdatum|gutschriftsdatum|gutschrift\s+vom"
    r"|rechnung\s+vom|beleg\s+vom|datum\s+der\s+rechnung|invoice\s+date|date\s+of\s+issue"
    r"|bon[-\s]?datum|kaufdatum)")
_DATE_WEAK = re.compile(r"(?<![a-zäöüß])(?:datum|date)(?![a-zäöüß])")
_DATE_BAD = re.compile(
    r"fällig|faellig|zahlungsziel|zahlbar|liefer|leistungs|zahlungsfrist|skonto|gültig|gueltig|verfall|ablauf"
    r"|mahn|\bdue\b|valid|geburt|bis\s+zum|innerhalb")
_FILLER = re.compile(r"[\s:.\-–]*(?:vom|am|von|date)?[\s:.\-–]*", re.I)


def _date_for_keys(lines, pattern, today):
    for index, line in enumerate(lines):
        low = line.lower()
        for key in pattern.finditer(low):
            dates = _dates_in(line, today)
            for start, _end, day in dates:
                if start >= key.end() and _FILLER.fullmatch(low[key.end():start]):
                    return day
            if not any(s >= key.end() for s, _, _ in dates):
                # Ueberschriftzeile („Rechnungsdatum | Faelligkeit“): Datum unter der Spalte
                found = _below(lines, index, key.start(), key.end(),
                               lambda ln: [(s, e, d) for s, e, d in _dates_in(ln, today)])
                if found:
                    return found
    return None


def find_date(lines, today):
    for pattern in (_DATE_STRONG, _DATE_WEAK):
        found = _date_for_keys(lines, pattern, today)
        if found:
            return found
    for line in lines[:60]:
        if _DATE_BAD.search(line.lower()):
            continue
        dates = _dates_in(line, today)
        if dates:
            return dates[0][2]
    return None


# ---------------------------------------------------------------------------
# Belegnummer
# ---------------------------------------------------------------------------

_NUMBER_KEY = re.compile(
    r"(?<![a-zäöüß])(?:rechnungs?[-\s]?(?:nummer|nr|no)\.?|re[-.\s]{0,2}(?:nr|nummer)\.?|rg[-.\s]{0,2}(?:nr|nummer)\.?"
    r"|beleg[-\s]?(?:nummer|nr|no)\.?|gutschrifts?[-\s]?(?:nummer|nr|no)\.?|bon[-\s]?(?:nummer|nr)\.?"
    r"|quittungs?[-\s]?(?:nummer|nr)\.?|invoice[-\s]?(?:number|no|nr|#)\.?"
    r"|credit[-\s]?note[-\s]?(?:number|no|nr)\.?|document[-\s]?(?:number|no)\.?)")
_NUMBER_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_\-.]{0,29}")
_NUMBER_HEADING = re.compile(
    r"^\s*(?:rechnung|gutschrift|invoice)\s+(?:nr\.?\s*|no\.?\s*|#\s*)?([A-Za-z0-9][A-Za-z0-9/_\-.]{1,29})\s*$",
    re.I)


def _number_token(token):
    token = token.strip().rstrip(".,;:")
    if not token or not any(ch.isdigit() for ch in token):
        return None
    if _DATE_NUM.fullmatch(token) or _DATE_ISO.fullmatch(token):
        return None
    return token[:30]


def find_number(lines):
    def tokens(line):
        return [(m.start(), m.end(), t) for m in re.finditer(r"\S+", line)
                if (t := _number_token(m.group(0))) is not None]

    for index, line in enumerate(lines):
        low = line.lower()
        for key in _NUMBER_KEY.finditer(low):
            gap = len(re.match(r"[\s:#.\-–]*", low[key.end():]).group(0))
            m = _NUMBER_VALUE.match(line, key.end() + gap)
            token = _number_token(m.group(0)) if m else None
            if token:
                return token
            found = _below(lines, index, key.start(), key.end(), tokens)
            if found:
                return found
    for line in lines[:40]:
        m = _NUMBER_HEADING.match(line)
        if m and (token := _number_token(m.group(1))):
            return token
    return None


# ---------------------------------------------------------------------------
# Bruttobetrag
# ---------------------------------------------------------------------------

_AMOUNT_KEYS = (
    (3, ("gesamtbetrag", "rechnungsbetrag", "rechnungssumme", "gesamtsumme", "endbetrag", "endsumme",
         "zahlbetrag", "zahlungsbetrag", "zu zahlen", "zu bezahlen", "bruttobetrag", "gesamt brutto",
         "summe brutto", "brutto gesamt", "gesamtpreis", "fälliger betrag", "faelliger betrag",
         "gutschriftsbetrag", "total due", "amount due", "invoice total", "grand total")),
    (2, ("brutto", "inkl. ust", "inkl. mwst", "inkl. mehrwertsteuer", "inkl. umsatzsteuer", "total", "gesamt")),
    (1, ("summe", "betrag", "sum", "amount")),
)
_AMOUNT_EXCLUDE = re.compile(
    r"zwischensumme|zwischentotal|subtotal|skonto|teilbetrag|anzahlung|pfand|rückgeld|rueckgeld|gegeben"
    r"|bezahlt|retour|rabatt|abzüglich|abzueglich|vorauszahlung|einzelpreis|stückpreis|stueckpreis"
    r"|unit price|gesamtmenge|gesamtgewicht")
_TAX_WORDS = re.compile(r"\bust\b|mwst|steuer|\bvat\b|\btax\b")      # auch „Umsatzsteuerbetrag“
_GROSS_HINT = re.compile(r"inkl|brutto|gesamt|total|zu zahlen|zu bezahlen|incl")


def _keyword(low):
    """``(Gewicht, Ende)`` des staerksten Betrags-Stichworts der Zeile (``(0, 0)`` = keins)."""
    for weight, words in _AMOUNT_KEYS:
        hits = [(low.find(w), w) for w in words if w in low]
        if hits:
            pos, word = min(hits)
            return weight, pos, pos + len(word)
    return 0, 0, 0


def find_amount(lines):
    candidates = []
    for index, line in enumerate(lines):
        low = line.lower()
        weight, key_start, key_end = _keyword(low)
        if not weight or _AMOUNT_EXCLUDE.search(low):
            continue
        if "netto" in low and "brutto" not in low:
            continue
        if _TAX_WORDS.search(low) and not _GROSS_HINT.search(low):
            continue
        amounts = _amounts_in(line)
        right = [a for a in amounts if a[0] >= key_end]         # Wert rechts vom Stichwort
        if right:
            candidates.append((weight, right[-1][2]))
        elif amounts:
            candidates.append((weight, amounts[0][2]))
        elif weight == 3:
            # „Rechnungsbetrag | Zahlungsziel“ als Spaltenkopf: der Betrag steht darunter
            found = _below(lines, index, key_start, key_end, _amounts_in)
            if found is not None:
                candidates.append((weight, found))
    if candidates:
        return max(candidates)[1]
    with_currency = [value for line in lines if re.search(r"€|\beur\b", line.lower())
                     for _s, _e, value in _amounts_in(line)]
    return max(with_currency) if with_currency else None


# ---------------------------------------------------------------------------
# Belegart, UID, IBAN
# ---------------------------------------------------------------------------

def find_kind(lines):
    head = "\n".join(lines[:25]).lower()
    if re.search(r"\b(?:gutschrift|gutschriftsanzeige|stornorechnung|credit\s+note)\b", head):
        return "credit_note"
    if re.search(r"\b(?:rechnung|invoice)\b", head):
        return "invoice"
    if re.search(r"\b(?:kassenbon|kassabon|kassenbeleg|quittung)\b|\bbon[-\s]?nr\b", head):
        return "receipt"
    if re.search(r"\bkontoauszug\b", head):
        return "bank_statement"
    return None


def find_vat_ids(text, own=()):
    skip = {normalize_vat_id(v) for v in own if v}
    found = []
    for m in _VAT.finditer(text):
        value = normalize_vat_id(m.group(0))
        if value not in skip and value not in found:
            found.append(value)
    return found


_IBAN_LENGTH = {"AT": 20, "DE": 22, "CH": 21, "LI": 21, "IT": 27, "FR": 27, "NL": 18, "BE": 16, "LU": 20,
                "CZ": 24, "SK": 24, "HU": 28, "SI": 19, "PL": 28, "HR": 21, "ES": 24, "PT": 25, "DK": 18, "SE": 24}


def find_ibans(text, own=()):
    skip = {normalize_iban(v) for v in own if v}
    found = []
    for m in _IBAN.finditer(text):
        value = normalize_iban(m.group(0))
        # ein folgendes Wort („... 2345 BIC“) haengt der Treffer sonst an die IBAN an
        value = value[:_IBAN_LENGTH.get(value[:2], len(value))]
        if value not in skip and value not in found and iban_valid(value):
            found.append(value)
    return found


def suggest(text, *, own_vat_ids=(), own_ibans=(), today=None):
    """Schlaegt Angaben aus dem Belegtext vor. ``own_*`` = die eigenen Kennungen des Mandanten
    (stehen als Empfaenger auf jeder Rechnung und wuerden sonst als „Lieferant“ erkannt)."""
    if not text or not text.strip():
        return Suggestions()
    today = today or date.today()
    lines = text.split("\n")
    return Suggestions(
        kind=find_kind(lines), number=find_number(lines), date=find_date(lines, today),
        amount=find_amount(lines), vat_ids=find_vat_ids(text, own_vat_ids),
        ibans=find_ibans(text, own_ibans))


# ---------------------------------------------------------------------------
# Suche im Text
# ---------------------------------------------------------------------------

def normalize_name(value):
    """Klein, ohne Akzente/Satzzeichen, einfache Leerzeichen — fuer den Namensvergleich."""
    value = unicodedata.normalize("NFKD", (value or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", " ", value.replace("ß", "ss")).strip()


def snippet(text, term, width=60):
    """``(davor, treffer, danach)`` um die erste Fundstelle von ``term`` — oder ``None``."""
    term = (term or "").strip()
    if not text or not term:
        return None
    flat = re.sub(r"\s+", " ", text)
    pos = flat.lower().find(term.lower())
    if pos < 0:
        return None
    start, end = max(0, pos - width), min(len(flat), pos + len(term) + width)
    return (("… " if start else "") + flat[start:pos], flat[pos:pos + len(term)],
            flat[pos + len(term):end] + (" …" if end < len(flat) else ""))
