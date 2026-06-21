"""OFX-Parser (Open Financial Exchange) fuer den Bankauszug-Import.

George (Erste/Sparkasse) bietet im Retail-Banking **kein** camt.053/MT940 an,
wohl aber **OFX** unter dem Label „MS Money". Dieser Parser deckt beide
OFX-Spielarten ab, die George anbietet:

- **OFX 2.x** (XML, „MS Money Sunset Deluxe"): wohlgeformtes XML mit
  schliessenden Tags und `<?xml?>` + `<?OFX?>`-Prolog.
- **OFX 1.x** (SGML, „MS Money 2000"): Header als `KEY:VALUE`-Block, Werte-Tags
  ohne schliessendes Pendant (`<TRNAMT>113.75` bis zum naechsten Tag).

Statt zweier getrennter Parser tokenisieren wir beide Varianten mit demselben
kleinen Stack-Tokenizer: Ein Tag mit direkt folgendem Text ist ein Blatt
(auto-close beim Wert), ein Tag mit folgendem Tag ein Aggregat; schliessende
Tags (nur 2.x) werden passend gepoppt bzw. geschluckt. Damit ist der Parser
dependency-frei (nur stdlib) und robust gegen die SGML-Eigenheiten.

Feld-Mapping (George-OFX):
    DTPOSTED            -> booking_date          (YYYYMMDDHHMMSS[.mmm])
    DTUSER/DTAVAIL      -> value_date
    TRNAMT             -> amount                (bereits vorzeichenbehaftet)
    FITID              -> tx_id                 (stabile, eindeutige TX-ID)
    NAME / PAYEE/NAME   -> counterparty_name     (32-Zeichen-gekuerzt!)
    BANKACCTTO/ACCTID   -> counterparty_iban
    MEMO               -> purpose               (traegt meist die Rechnungsnr.)
    BANKACCTFROM/ACCTID -> account_iban          (Auto-Match RealAccount)
    LEDGERBAL/BALAMT    -> closing_balance
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.bank_import.parsers.types import ParsedLine, ParsedStatement


# Tag-Token: <TAG ...> oder </TAG>. PIs (<?xml?>, <?OFX?>) matchen bewusst NICHT
# (nach '<' folgt '?', kein '/' und kein Tag-Name-Zeichen).
_TAG_RE = re.compile(r"<(/?)([A-Za-z0-9._]+)(?:\s[^>]*)?>")


class _Node:
    __slots__ = ("tag", "value", "children")

    def __init__(self, tag: str):
        self.tag = tag
        self.value: str | None = None
        self.children: list["_Node"] = []

    def child(self, tag: str) -> "_Node | None":
        tag = tag.upper()
        return next((c for c in self.children if c.tag == tag), None)

    def child_text(self, tag: str) -> str | None:
        node = self.child(tag)
        return node.value if node is not None else None

    def first_descendant(self, tag: str) -> "_Node | None":
        """Erster Knoten mit diesem Tag irgendwo im Teilbaum (BFS)."""
        tag = tag.upper()
        queue = list(self.children)
        while queue:
            node = queue.pop(0)
            if node.tag == tag:
                return node
            queue.extend(node.children)
        return None

    def descendants(self, tag: str) -> list["_Node"]:
        tag = tag.upper()
        out: list[_Node] = []
        queue = list(self.children)
        while queue:
            node = queue.pop(0)
            if node.tag == tag:
                out.append(node)
            queue.extend(node.children)
        return out


def _build_tree(body: str) -> _Node:
    # Alles vor dem <OFX>-Root verwerfen (1.x-KEY:VALUE-Header bzw. 2.x-Prolog).
    idx = body.find("<OFX>")
    if idx == -1:
        m = re.search(r"<OFX\b", body)
        idx = m.start() if m else 0
    body = body[idx:]

    root = _Node("ROOT")
    stack: list[_Node] = [root]
    pos = 0

    for m in _TAG_RE.finditer(body):
        text = body[pos : m.start()]
        pos = m.end()
        closing, tag = m.group(1), m.group(2).upper()

        if text.strip():
            # Werttext gehoert zum aktuell offenen Blatt -> setzen und schliessen.
            leaf = stack[-1]
            leaf.value = html.unescape(text.strip())
            if len(stack) > 1:
                stack.pop()

        if closing:
            # Schliessendes Tag (2.x): passenden offenen Knoten poppen. Ist er
            # (als Blatt) schon auto-geschlossen, ignorieren.
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break
        else:
            node = _Node(tag)
            stack[-1].children.append(node)
            stack.append(node)

    return root


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    m = re.match(r"\s*(\d{8})", value)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        # OFX nutzt '.' als Dezimaltrenner; manche Banken liefern ',' -> tolerieren.
        return Decimal(value.strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def parse(content: bytes) -> ParsedStatement:
    text = content.decode("utf-8", errors="replace")
    root = _build_tree(text)

    # Giro (STMTRS) oder Kreditkarte (CCSTMTRS).
    stmt = root.first_descendant("STMTRS") or root.first_descendant("CCSTMTRS")
    if stmt is None:
        raise ValueError("OFX: kein <STMTRS>/<CCSTMTRS>-Element gefunden.")

    currency = stmt.child_text("CURDEF")

    acct_from = stmt.first_descendant("BANKACCTFROM") or stmt.first_descendant("CCACCTFROM")
    account_iban = acct_from.child_text("ACCTID") if acct_from is not None else None
    if account_iban:
        account_iban = account_iban.strip() or None

    statement_reference = root.first_descendant("TRNUID")
    statement_reference = statement_reference.value if statement_reference is not None else None

    closing_balance = None
    ledgerbal = stmt.first_descendant("LEDGERBAL")
    if ledgerbal is not None:
        closing_balance = _decimal(ledgerbal.child_text("BALAMT"))

    lines: list[ParsedLine] = []
    all_dates: list[date] = []

    tranlist = stmt.first_descendant("BANKTRANLIST")
    txns = tranlist.descendants("STMTTRN") if tranlist is not None else stmt.descendants("STMTTRN")

    for tx in txns:
        amount = _decimal(tx.child_text("TRNAMT"))
        if amount is None:
            continue

        booking_date = _parse_date(tx.child_text("DTPOSTED"))
        value_date = _parse_date(tx.child_text("DTAVAIL") or tx.child_text("DTUSER"))
        if booking_date is None:
            booking_date = value_date
        if booking_date is None:
            continue
        all_dates.append(booking_date)

        counterparty_name = tx.child_text("NAME")
        if not counterparty_name:
            payee = tx.child("PAYEE")
            if payee is not None:
                counterparty_name = payee.child_text("NAME")

        acct_to = tx.child("BANKACCTTO") or tx.child("CCACCTTO")
        counterparty_iban = acct_to.child_text("ACCTID") if acct_to is not None else None

        lines.append(
            ParsedLine(
                booking_date=booking_date,
                value_date=value_date,
                amount=amount,
                currency=currency or "EUR",
                counterparty_name=(counterparty_name or "").strip() or None,
                counterparty_iban=(counterparty_iban or "").strip() or None,
                purpose=(tx.child_text("MEMO") or "").strip() or None,
                end_to_end_id=None,  # OFX kennt keine EndToEndId
                tx_id=(tx.child_text("FITID") or "").strip() or None,
            )
        )

    return ParsedStatement(
        format="ofx",
        account_iban=account_iban,
        statement_reference=statement_reference,
        opening_balance=None,  # OFX-STMTRS kennt keinen Eroeffnungssaldo
        closing_balance=closing_balance,
        currency=currency or "EUR",
        booking_date_from=min(all_dates) if all_dates else None,
        booking_date_to=max(all_dates) if all_dates else None,
        lines=lines,
    )
