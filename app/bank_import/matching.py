import re
import unicodedata
from decimal import Decimal

from sqlalchemy import func, or_

from app.banking import iban_valid, normalize_iban
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


def _open_item_for_number(num: str, amount: Decimal):
    """Offener Posten zur Belegnummer ``num``, passend zum Vorzeichen von ``amount``.

    Zwei Faelle, die beide ueber den Verwendungszweck laufen:

    * **Eingang (amount > 0)** — der Kunde zahlt, wir suchen seinen offenen
      Posten mit positivem Saldo.
    * **Ausgang (amount < 0)** — *wir* ueberweisen zurueck (Rueckzahlung aus
      einer Storno-Rechnung). Dann passt der negative OP der Gutschrift.
      Der Kassier tippt beim Ueberweisen aber gerne die **Original**-Nummer
      statt der Gutschriftsnummer in den Verwendungszweck — deshalb wird bei
      einem Ausgang zusaetzlich die Storno-Rechnung der gefundenen Rechnung
      geprueft.

    Gibt ``(invoice, open_item)`` zurueck oder ``(None, None)``.
    """
    inv = Invoice.query.filter_by(invoice_number=num).first()
    if inv is None:
        return None, None

    outgoing = amount < 0
    candidates = [inv]
    if outgoing:
        # Verwendungszweck nennt die Originalrechnung -> deren Gutschrift traegt
        # den Rueckzahlungs-Posten.
        credit = inv.active_credit_note
        if credit is not None:
            candidates.append(credit)

    for cand in candidates:
        op = cand.open_item
        if op is None:
            continue
        if op.status not in (OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL):
            continue
        # Vorzeichen muss zusammenpassen: eine Rueckueberweisung darf niemals
        # auf eine offene Forderung gebucht werden (und umgekehrt).
        if (_as_decimal(op.open_balance) < 0) != outgoing:
            continue
        return cand, op

    return None, None


def match_line(line: BankStatementLine) -> None:
    """Automatisches Matching fuer eine Bankauszug-Zeile.

    Reihenfolge:
    0. Sammelgutschrift eines Lastschrift-Einzugs
    1. Rechnungsnummer (Format YYYY-NNNNN) im Verwendungszweck
       (1b. Ruecklastschrift einer eingezogenen Rechnung)
    2. IBAN des Gegenkontos -> Kontakt -> offener Posten mit exaktem Betrag
    3. Name des Absenders -> Kunde -> offene OPs (exakter Betrags-Match bevorzugt)

    Gilt fuer **beide** Richtungen: Zahlungseingaenge auf offene Forderungen und
    Rueckueberweisungen (negativer Betrag) auf die negativen Offenen Posten aus
    einer Storno-Rechnung. Eine Zeile ohne Vorzeichen-Partner bleibt
    unzugeordnet — der Nutzer waehlt dann in der Vorschau ein Konto.

    Aendert die Felder matched_invoice_id, matched_open_item_id,
    matched_customer_id, match_type und selected am Line-Objekt. Kein commit.
    """
    # Default: alle Zeilen sind vorausgewaehlt — der Nutzer waehlt explizit
    # ab, was er NICHT verbuchen will (Zinsen, Spesen, manuell-zu-pruefende
    # Sonderfaelle). Mehr Zeilen sind "Standardfall verbuchen" als
    # "manuell triagen".
    line.selected = True

    amount = _as_decimal(line.amount)
    if amount == 0:
        return

    # 0) Sammelgutschrift eines Lastschrift-Einzugs (Betrag = offene Summe eines
    #    Laufs) — VOR der Rechnungsnummer: der Verwendungszweck einer Sammel-
    #    buchung nennt oft nur die erste Rechnung des Laufs.
    from app.direct_debit.services import match_bank_line
    if amount > 0 and match_bank_line(line):
        return

    # 1) Rechnungsnummer im Verwendungszweck
    if line.purpose:
        num = _find_invoice_number(line.purpose)
        if num:
            inv, op = _open_item_for_number(num, amount)
            if op is not None:
                line.matched_invoice_id = inv.id
                line.matched_open_item_id = op.id
                line.matched_customer_id = op.customer_id
                line.match_type = BankStatementLine.MATCH_INVOICE_NUMBER
                line.selected = True
                return

    # 1b) Ruecklastschrift einer eingezogenen, bezahlten Rechnung (negativ).
    if amount < 0 and match_bank_line(line):
        return

    # 2) IBAN des Gegenkontos -> Kontakt -> exakter offener Betrag. Ohne Betrags-
    #    Treffer bleibt nur der Kontakt vorgemerkt, und der Name darf es noch
    #    versuchen (z.B. zweiter Kontakt derselben Person ohne IBAN).
    if _match_by_iban(line):
        return
    iban_customer_id = line.matched_customer_id

    # 3) Name (reihenfolgeunabhaengig) + Betrag
    _match_by_name_amount(line)
    if iban_customer_id and line.matched_open_item_id is None:
        # Ohne Posten ist der Kontakt aus der IBAN verlaesslicher als einer aus
        # dem (oft gekuerzten) OFX-Namen.
        line.matched_customer_id = iban_customer_id


def _match_by_iban(line: BankStatementLine) -> bool:
    """Zuordnung ueber die IBAN des Gegenkontos, wenn die Rechnungsnummer fehlt.

    Die IBAN ist der stabile Anker: George-OFX liefert viele Eingaenge ohne
    Verwendungszweck und mit auf 32 Zeichen gekuerztem Namen.

    1. Kontakte mit dieser IBAN (``Customer.iban``) suchen — auch inaktive, ein
       ehemaliger Kontakt zahlt noch seine Schlussrechnung.
    2. Genau ein offener Posten dieser Kontakte mit exakt passendem Betrag ->
       zuordnen. Teilen sich mehrere Kontakte eine IBAN (Ehepaar mit gemeinsamem
       Konto), entscheidet also nur ein eindeutiger Betrag.
    3. Sonst nur den Kontakt vormerken, wenn er eindeutig ist (einziger Kontakt
       oder einziger mit offenen Posten).

    Das Vorzeichen traegt der Betragsvergleich mit: eine Rueckueberweisung
    (Gegenkonto = Empfaenger = Kontakt) trifft nur einen Gutschrifts-Posten.
    Gibt True zurueck, wenn ein offener Posten zugeordnet wurde.
    """
    iban = normalize_iban(line.counterparty_iban)
    if not iban_valid(iban):
        # OFX liefert im ACCTID mitunter eine nationale Kontonummer statt IBAN.
        return False

    # ``Customer.iban`` wird normalisiert gespeichert; replace/upper faengt
    # Altbestand mit Leerzeichen ab (in allen drei Dialekten vorhanden).
    customers = (
        Customer.query
        .filter(func.upper(func.replace(Customer.iban, " ", "")) == iban)
        .all()
    )
    if not customers:
        return False

    amount = _as_decimal(line.amount)
    ops = (
        OpenItem.query.filter(
            OpenItem.customer_id.in_([c.id for c in customers]),
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        ).all()
    )

    exact = [op for op in ops if _as_decimal(op.open_balance) == amount]
    if len(exact) == 1:
        op = exact[0]
        line.matched_open_item_id = op.id
        line.matched_invoice_id = op.invoice_id
        line.matched_customer_id = op.customer_id
        line.match_type = BankStatementLine.MATCH_IBAN
        line.selected = True
        return True

    if len(customers) == 1:
        line.matched_customer_id = customers[0].id
        return False
    custs_with_ops = {op.customer_id for op in ops}
    if len(custs_with_ops) == 1:
        line.matched_customer_id = next(iter(custs_with_ops))
    return False


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

    Funktioniert in beide Richtungen: der exakte Betragsvergleich gegen
    ``op.open_balance`` traegt das Vorzeichen mit, eine Rueckueberweisung
    (-120,00) trifft also nur den Gutschrifts-OP (-120,00) und nie eine offene
    Forderung. Bei einer Ausgangs-Zeile ist ``counterparty_name`` der
    Empfaenger — also ebenfalls der Kunde.
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
