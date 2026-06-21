import re
import unicodedata
from decimal import Decimal

from sqlalchemy import or_

from app.models import (
    BankStatementLine,
    Customer,
    Invoice,
    OpenItem,
)


INVOICE_NR_RE = re.compile(r"\b(\d{4}-\d{5})\b")
# Toleranter Fallback fuer zerhackte Verwendungszwecke (siehe _find_invoice_number).
INVOICE_NR_LOOSE_RE = re.compile(r"(20\d{2})-?(\d{5})")


def _as_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _find_invoice_number(purpose: str) -> str | None:
    """Rechnungsnummer (YYYY-NNNNN) aus dem Verwendungszweck extrahieren.

    Erst der strikte Match. Schlaegt der fehl, ein toleranter zweiter Versuch:
    Banken (z.B. George/Erste via OFX) zerhacken lange SEPA-Verwendungszwecke in
    Fixed-Width-Segmente und fuegen sie mit Leerzeichen zusammen — die
    Rechnungsnummer bricht dann mitten durch ("2 026-00206", "2026 00046").
    Wir entfernen die Leerzeichen und suchen erneut. Da der Aufrufer den Treffer
    anschliessend gegen eine real existierende Rechnung prueft, sind
    rekonstruierte Falsch-Nummern folgenlos.
    """
    m = INVOICE_NR_RE.search(purpose)
    if m:
        return m.group(1)
    despaced = re.sub(r"\s+", "", purpose)
    m = INVOICE_NR_LOOSE_RE.search(despaced)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def match_line(line: BankStatementLine) -> None:
    """Automatisches Matching fuer eine Bankauszug-Zeile.

    Reihenfolge:
    1. Rechnungsnummer (Format YYYY-NNNNN) im Verwendungszweck
    2. Name des Absenders -> Kunde -> offene OPs (exakter Betrags-Match bevorzugt)

    Aendert die Felder matched_invoice_id, matched_open_item_id,
    matched_customer_id, match_type und selected am Line-Objekt. Kein commit.
    """
    # Default: alle Zeilen sind vorausgewaehlt — der Nutzer waehlt explizit
    # ab, was er NICHT verbuchen will (Zinsen, Spesen, manuell-zu-pruefende
    # Sonderfaelle). Mehr Zeilen sind "Standardfall verbuchen" als
    # "manuell triagen".
    line.selected = True

    if _as_decimal(line.amount) <= 0:
        return

    # 1) Rechnungsnummer im Verwendungszweck
    if line.purpose:
        num = _find_invoice_number(line.purpose)
        if num:
            inv = Invoice.query.filter_by(invoice_number=num).first()
            if inv and inv.open_item and inv.open_item.status in (
                OpenItem.STATUS_OPEN,
                OpenItem.STATUS_PARTIAL,
            ):
                line.matched_invoice_id = inv.id
                line.matched_open_item_id = inv.open_item.id
                line.matched_customer_id = inv.customer_id
                line.match_type = BankStatementLine.MATCH_INVOICE_NUMBER
                line.selected = True
                return

    # 2) Name (reihenfolgeunabhaengig) + Betrag
    _match_by_name_amount(line)


# Akademische Titel + Verbindungswoerter, die kein Namensbestandteil sind und
# beim Token-Vergleich ignoriert werden. OFX-`NAME` von George traegt oft Titel
# ("DDipl.-Ing.", "Dr.", "Mag.") und bei Paaren Verbinder ("und"/"oder"/"u.").
_TITLE_TOKENS = {
    "dr", "ddr", "mag", "mmag", "di", "ddi", "dipl", "ddipl", "ing", "prof",
    "dkfm", "bsc", "msc", "ba", "ma", "bakk", "med", "univ", "hr", "kommr",
}
_CONNECTOR_TOKENS = {"und", "oder", "u", "o", "uo", "bzw", "sowie", "geb", "fam"}


def _normalize_name_tokens(name: str | None) -> set[str]:
    """Name in eine reihenfolgeunabhaengige Menge normalisierter Tokens zerlegen.

    Kleinschreibung, Akzente/Umlaute entfernt (ue/oe/ae -> u/o/a, ss fuer
    scharfes S), Titel und Verbinder verworfen, Tokens < 2 Zeichen weg. Dient
    dem reihenfolgeunabhaengigen *Vergleich* — "Thomas Petutschnig" (Bank) und
    "Petutschnig Thomas" (Kunde) ergeben dieselbe Menge.
    """
    if not name:
        return set()
    s = name.lower().replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    tokens = re.findall(r"[a-z0-9]+", s)
    return {
        t for t in tokens
        if len(t) >= 2 and t not in _TITLE_TOKENS and t not in _CONNECTOR_TOKENS
    }


def _name_search_tokens(name: str | None) -> list[str]:
    """Tokens fuer den ILIKE-Vorfilter — Akzente/Umlaute BLEIBEN erhalten.

    Wichtig getrennt von _normalize_name_tokens: ein entakzentuiertes "turk"
    wuerde den DB-Namen "Türk" per ILIKE NICHT treffen (u != ü). Hier behalten
    wir die Umlaute, damit der Vorfilter akzentbehaftete Kundennamen findet;
    der eigentliche (entakzentuierte) Mengenvergleich kommt danach.
    """
    if not name:
        return []
    s = name.lower().replace("ß", "ss")
    tokens = re.findall(r"[a-zà-ÿ0-9]+", s)
    return [
        t for t in tokens
        if len(t) >= 3 and t not in _TITLE_TOKENS and t not in _CONNECTOR_TOKENS
    ]


def _name_tokens_match(bank: set[str], cust: set[str]) -> bool:
    """Reihenfolgeunabhaengiger Namensabgleich mit milder Trunkierungs-Toleranz.

    Mindestens zwei gemeinsame Tokens, und (fuer laengere Namen) hoechstens
    eines darf abweichen — faengt die 32-Zeichen-Kuerzung des OFX-`NAME` ab
    ("...Christin" vs. "Christine"), bleibt bei zweiteiligen Namen aber exakt.
    """
    common = bank & cust
    return len(common) >= 2 and len(common) >= min(len(bank), len(cust)) - 1


def _match_by_name_amount(line: BankStatementLine) -> None:
    """Zuordnung ueber Gegenpartei-Name + Betrag, wenn die Rechnungsnummer fehlt.

    1. Kunden grob per Namens-Token vorfiltern (OR-ILIKE), dann reihenfolge-
       unabhaengig abgleichen.
    2. Eindeutiger offener Posten mit exakt passendem Betrag -> direkt zuordnen.
    3. Sonst, wenn der Name eindeutig auf einen Kunden zeigt -> nur Kunde
       vormerken (User waehlt den OP im Dropdown).
    """
    bank_tokens = _normalize_name_tokens(line.counterparty_name)
    if len(bank_tokens) < 2:
        return

    search_tokens = _name_search_tokens(line.counterparty_name)
    if not search_tokens:
        return
    candidates = (
        Customer.query
        .filter(Customer.active.is_(True))
        .filter(or_(*[Customer.name.ilike(f"%{t}%") for t in search_tokens]))
        .all()
    )
    matched = [c for c in candidates if _name_tokens_match(bank_tokens, _normalize_name_tokens(c.name))]
    if not matched:
        return

    amount = _as_decimal(line.amount)
    ops = (
        OpenItem.query.filter(
            OpenItem.customer_id.in_([c.id for c in matched]),
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        ).all()
    )

    exact = [op for op in ops if _as_decimal(op.open_balance) == amount]
    if len(exact) == 1:
        op = exact[0]
        line.matched_open_item_id = op.id
        line.matched_invoice_id = op.invoice_id
        line.matched_customer_id = op.customer_id
        line.match_type = BankStatementLine.MATCH_NAME
        line.selected = True
        return

    # Kein eindeutiger Betrags-Treffer: nur Kunde vormerken, wenn eindeutig.
    distinct = {c.id for c in matched}
    if len(distinct) == 1:
        line.matched_customer_id = matched[0].id
        return
    custs_with_ops = {op.customer_id for op in ops}
    if len(custs_with_ops) == 1:
        line.matched_customer_id = next(iter(custs_with_ops))
