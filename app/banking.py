"""Bankkennungen: IBAN, BIC, SEPA-Glaeubiger-ID und der SEPA-Zeichensatz.

Reine Funktionen ohne Flask/DB — genutzt vom Kontaktformular, den Import-Wizards,
der E-Rechnung (BR-DE-19), der Belegtext-Erkennung und dem Lastschrift-Export.
Bewusst ohne Fremdpaket (schwifty/stdnum): die Pruefziffern sind ISO 7064
MOD 97-10, die Laengen stehen unten.
"""
from __future__ import annotations

import re
import unicodedata

# IBAN-Laenge je Land im SEPA-Raum (EPC-Liste). Ein Land, das hier fehlt, ist kein
# SEPA-Land — eine solche IBAN kann geprueft, aber nicht per SEPA-Lastschrift
# belastet werden.
IBAN_LENGTH = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22,
    "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GI": 23,
    "GL": 18, "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IS": 26, "IT": 27, "LI": 21,
    "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MT": 31, "NL": 18, "NO": 15, "PL": 28,
    "PT": 25, "RO": 24, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "VA": 22,
}

_IBAN_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
_BIC_SHAPE = re.compile(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?")
_CREDITOR_ID_SHAPE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{3}[A-Z0-9]{1,28}")
# Laenge der Glaeubiger-ID je Land (nur wo sicher bekannt; sonst nur Form + Pruefziffer).
_CREDITOR_ID_LENGTH = {"AT": 18, "DE": 18}

# EPC-Zeichensatz fuer SEPA-Nachrichten (Basic Latin, „SEPA-Zeichensatz“).
_SEPA_ALLOWED = re.compile(r"[^A-Za-z0-9/\-?:().,'+ ]")
# Kennungen (Mandatsreferenz, End-to-End-ID, MsgId): ohne Leerzeichen.
_SEPA_ID = re.compile(r"[A-Za-z0-9/\-?:().,'+]{1,35}")
_TRANSLIT = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    # zerfallen nicht per NFKD in Grundbuchstabe + Akzent
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "Ae", "œ": "oe", "Œ": "Oe", "ł": "l", "Ł": "L",
    "đ": "d", "Đ": "D", "ð": "d", "þ": "th", "ı": "i",
    "&": "+", "€": "EUR", "–": "-", "—": "-", "„": "'", "“": "'", "”": "'", "‚": "'",
    "‘": "'", "’": "'", "\"": "'", "_": "-", ";": ",", "!": ".", "#": "", "*": "",
}


def _mod97(text):
    """Rest nach ISO 7064 MOD 97-10; Buchstaben zaehlen als 10..35."""
    return int("".join(str(int(ch, 36)) for ch in text)) % 97


# ---------------------------------------------------------------------------
# IBAN
# ---------------------------------------------------------------------------

def normalize_iban(value):
    """Ohne Leerzeichen, in Grossbuchstaben (``None`` → ``""``)."""
    return re.sub(r"\s+", "", value or "").upper()


def iban_valid(value):
    """True bei gueltiger Form, passender Laenge (fuer bekannte Laender) und Pruefziffer."""
    iban = normalize_iban(value)
    if not _IBAN_SHAPE.fullmatch(iban):
        return False
    expected = IBAN_LENGTH.get(iban[:2])
    if expected is not None and len(iban) != expected:
        return False
    return _mod97(iban[4:] + iban[:4]) == 1


def iban_is_sepa(value):
    """True, wenn das Land der IBAN im SEPA-Raum liegt."""
    return normalize_iban(value)[:2] in IBAN_LENGTH


def format_iban(value):
    """IBAN in Vierergruppen („AT61 1904 3002 3457 3201“); leer bleibt leer."""
    iban = normalize_iban(value)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def mask_iban(value):
    """IBAN fuer Briefe: Land, Pruefziffer und die letzten vier Stellen sichtbar."""
    iban = normalize_iban(value)
    if len(iban) <= 8:
        return format_iban(iban)
    masked = iban[:4] + "•" * (len(iban) - 8) + iban[-4:]
    return " ".join(masked[i:i + 4] for i in range(0, len(masked), 4))


def iban_bank_code(value):
    """Bankleitzahl-Teil der IBAN (fuer „anderes Kreditinstitut?“ bei Mandatsaenderung)."""
    iban = normalize_iban(value)
    return {"AT": iban[4:9], "DE": iban[4:12]}.get(iban[:2], iban[4:8])


# ---------------------------------------------------------------------------
# BIC
# ---------------------------------------------------------------------------

def normalize_bic(value):
    return re.sub(r"\s+", "", value or "").upper()


def bic_valid(value):
    """BIC mit 8 oder 11 Stellen (ISO 9362)."""
    return bool(_BIC_SHAPE.fullmatch(normalize_bic(value)))


# ---------------------------------------------------------------------------
# SEPA-Glaeubiger-Identifikationsnummer (Creditor Identifier)
# ---------------------------------------------------------------------------

def normalize_creditor_id(value):
    return re.sub(r"\s+", "", value or "").upper()


def creditor_id_valid(value):
    """Glaeubiger-ID ``CCkkZZZ…``: Pruefziffer kk (MOD 97-10) ueber die nationale
    Kennung + Land + „00“ — die Geschaeftsbereichskennung (Stellen 5–7, meist ZZZ)
    zaehlt nicht mit."""
    cid = normalize_creditor_id(value)
    if not _CREDITOR_ID_SHAPE.fullmatch(cid):
        return False
    expected = _CREDITOR_ID_LENGTH.get(cid[:2])
    if expected is not None and len(cid) != expected:
        return False
    national = re.sub(r"[^A-Z0-9]", "", cid[7:])
    check = 98 - _mod97(national + cid[:2] + "00")
    return f"{check:02d}" == cid[2:4]


# ---------------------------------------------------------------------------
# SEPA-Zeichensatz
# ---------------------------------------------------------------------------

def sepa_text(value, max_len=140):
    """Text im EPC-Zeichensatz: Umlaute umschreiben, Akzente weg, Unerlaubtes zu
    Leerzeichen, mehrfache Leerzeichen zusammenziehen, auf ``max_len`` kuerzen."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (value or ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _SEPA_ALLOWED.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len].rstrip()


def sepa_id_valid(value):
    """Kennung (Mandatsreferenz, End-to-End-ID) im SEPA-Zeichensatz: 1–35 Zeichen,
    keine Leerzeichen, kein „/“ am Anfang/Ende, kein „//“."""
    value = value or ""
    if not _SEPA_ID.fullmatch(value):
        return False
    return not (value.startswith("/") or value.endswith("/") or "//" in value)
