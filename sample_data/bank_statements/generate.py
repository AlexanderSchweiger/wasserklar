"""Generator fuer die drei Demo-Bank-Auszuege.

Erzeugt MT940, MT942 und camt.053 mit identischem Buchungs-Inhalt — passend
zu den Open Items / Rechnungen aus ``flask --app run seed-demo``.

Aufruf:
    .venv/Scripts/python sample_data/bank_statements/generate.py

Schreibt drei Dateien neben dieses Skript:
    giro_2025-09.mt940.sta
    giro_2025-09.mt942.sta
    giro_2025-09.camt053.xml

Nach der Generierung wird automatisch jede Datei durch die OSS-Parser geschickt,
damit ein Schreibfehler sofort auffaellt.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# OSS-Repo-Wurzel zum sys.path hinzufuegen, damit ``from app.bank_import...``
# beim Self-Test funktioniert (Skript-Aufruf unabhaengig vom cwd).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

ACCOUNT_IBAN_FORMATTED = "AT12 3456 7890 1111 0000"
ACCOUNT_IBAN_COMPACT = ACCOUNT_IBAN_FORMATTED.replace(" ", "")
CURRENCY = "EUR"
OPENING_BALANCE = Decimal("8500.00")
STATEMENT_NUMBER = 9
SEQUENCE_NUMBER = 1


@dataclass
class Tx:
    booking_date: date
    amount: Decimal  # positiv = Eingang
    counterparty_name: str
    counterparty_iban: str
    purpose: str
    end_to_end_id: str


# Sieben Buchungen — sechs decken Open Items / Rechnungen aus dem Seed ab,
# eine ist fremd (Energie AG, kein Kunde der WG).
TRANSACTIONS: list[Tx] = [
    Tx(date(2025, 9, 2),  Decimal("120.00"), "Karl Weidinger",
       "AT11 1111 1111 1111 0006", "Anschlussgebuehr Gartenzaehler Nachruestung", "E2E-2025-09-001"),
    Tx(date(2025, 9, 3),  Decimal("8.50"),   "Petra Voglhuber",
       "AT11 1111 1111 1111 0013", "Saeumniszuschlag manuell", "E2E-2025-09-002"),
    Tx(date(2025, 9, 4),  Decimal("218.46"), "Brigitte Kogler",
       "AT11 1111 1111 1111 0080", "Rechnung RE-2024-0004", "E2E-2025-09-003"),
    Tx(date(2025, 9, 7),  Decimal("200.00"), "Edith Fischer",
       "AT11 1111 1111 1111 0017", "RE-2024-0005 Teilzahlung", "E2E-2025-09-004"),
    Tx(date(2025, 9, 8),  Decimal("163.02"), "Hildegard Wagner",
       "AT11 1111 1111 1111 0089", "Rechnung RE-2024-0011", "E2E-2025-09-005"),
    Tx(date(2025, 9, 10), Decimal("229.24"), "Monika Leitner",
       "AT11 1111 1111 1111 0031", "Rechnung RE-2024-0019", "E2E-2025-09-006"),
    Tx(date(2025, 9, 12), Decimal("285.40"), "Energie AG Oberoesterreich",
       "AT22 2222 2222 2222 0000", "Akonto-Rueckverrechnung Q2/2025", "E2E-2025-09-007"),
]


def _closing_balance() -> Decimal:
    return OPENING_BALANCE + sum((t.amount for t in TRANSACTIONS), Decimal("0"))


def _amt_de(value: Decimal) -> str:
    """1234.56 -> '1234,56' (deutsches Komma, MT940-Konvention)."""
    return f"{value:.2f}".replace(".", ",")


def _amt_us(value: Decimal) -> str:
    return f"{value:.2f}"


# ---------------------------------------------------------------------------
# MT940
# ---------------------------------------------------------------------------

def render_mt940(transactions: list[Tx]) -> str:
    """Standard-MT940-Auszug. Komma als Dezimaltrenner, IBAN als Konto-ID."""
    from_date = transactions[0].booking_date
    to_date = transactions[-1].booking_date
    closing = _closing_balance()

    lines: list[str] = []
    lines.append(":20:STARTUMSE")
    lines.append(f":25:{ACCOUNT_IBAN_COMPACT}")
    lines.append(f":28C:{STATEMENT_NUMBER:05d}/{SEQUENCE_NUMBER:03d}")
    lines.append(f":60F:C{from_date:%y%m%d}{CURRENCY}{_amt_de(OPENING_BALANCE)}")

    for tx in transactions:
        cd = "C" if tx.amount > 0 else "D"
        amount_abs = abs(tx.amount)
        lines.append(
            f":61:{tx.booking_date:%y%m%d}{tx.booking_date:%m%d}"
            f"{cd}{_amt_de(amount_abs)}NTRF{tx.end_to_end_id}"
        )
        # :86: Verwendungszweck — SWIFT-konformer Aufbau mit Sub-Feldern
        # ?00 GVC, ?20-?29 SVWZ, ?32 Name, ?33 Forts. Name, ?38 IBAN
        name1 = tx.counterparty_name[:27]
        name2 = tx.counterparty_name[27:54]
        iban = tx.counterparty_iban.replace(" ", "")
        purpose_lines = [tx.purpose[i:i+27] for i in range(0, len(tx.purpose), 27)] or [""]
        purpose_block = "".join(
            f"?{20 + idx:02d}{chunk}" for idx, chunk in enumerate(purpose_lines[:10])
        )
        block_86 = f":86:166?00GUTSCHRIFT{purpose_block}?32{name1}"
        if name2:
            block_86 += f"?33{name2}"
        block_86 += f"?38{iban}"
        lines.append(block_86)

    lines.append(f":62F:C{to_date:%y%m%d}{CURRENCY}{_amt_de(closing)}")
    lines.append("-")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# MT942 (Zwischen-Saldenmitteilung)
# ---------------------------------------------------------------------------

def render_mt942(transactions: list[Tx]) -> str:
    """MT942 — Zwischenmitteilung. Statt :60F:/:62F: gibt's :34F:-Floor-Limit
    und :90C:/:90D:-Summary. Sonst identische Transaktionsfelder."""
    from_date = transactions[0].booking_date
    to_date = transactions[-1].booking_date

    credits = [t for t in transactions if t.amount > 0]
    debits = [t for t in transactions if t.amount < 0]
    credit_sum = sum((t.amount for t in credits), Decimal("0"))
    debit_sum = sum((-t.amount for t in debits), Decimal("0"))

    lines: list[str] = []
    lines.append(":20:ZWISUMSE")
    lines.append(f":25:{ACCOUNT_IBAN_COMPACT}")
    lines.append(f":28C:{STATEMENT_NUMBER:05d}/{SEQUENCE_NUMBER:03d}")
    lines.append(f":34F:{CURRENCY}0,00")
    lines.append(f":13D:{to_date:%y%m%d}1200+0100")

    for tx in transactions:
        cd = "C" if tx.amount > 0 else "D"
        amount_abs = abs(tx.amount)
        lines.append(
            f":61:{tx.booking_date:%y%m%d}{tx.booking_date:%m%d}"
            f"{cd}{_amt_de(amount_abs)}NTRF{tx.end_to_end_id}"
        )
        name1 = tx.counterparty_name[:27]
        name2 = tx.counterparty_name[27:54]
        iban = tx.counterparty_iban.replace(" ", "")
        purpose_lines = [tx.purpose[i:i+27] for i in range(0, len(tx.purpose), 27)] or [""]
        purpose_block = "".join(
            f"?{20 + idx:02d}{chunk}" for idx, chunk in enumerate(purpose_lines[:10])
        )
        block_86 = f":86:166?00GUTSCHRIFT{purpose_block}?32{name1}"
        if name2:
            block_86 += f"?33{name2}"
        block_86 += f"?38{iban}"
        lines.append(block_86)

    lines.append(f":90D:{len(debits)}{CURRENCY}{_amt_de(debit_sum)}")
    lines.append(f":90C:{len(credits)}{CURRENCY}{_amt_de(credit_sum)}")
    lines.append("-")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# camt.053
# ---------------------------------------------------------------------------

def render_camt053(transactions: list[Tx]) -> str:
    """ISO 20022 camt.053.001.02 — Bank to Customer Statement."""
    from_date = transactions[0].booking_date
    to_date = transactions[-1].booking_date
    closing = _closing_balance()
    iban = ACCOUNT_IBAN_COMPACT

    ntries: list[str] = []
    for idx, tx in enumerate(transactions, start=1):
        cd_dbt = "CRDT" if tx.amount > 0 else "DBIT"
        amt_abs = _amt_us(abs(tx.amount))
        party_iban = tx.counterparty_iban.replace(" ", "")
        # Bei Eingang (CRDT) ist Gegenpartei der Debtor (Sender),
        # bei Ausgang (DBIT) der Creditor (Empfaenger).
        if tx.amount > 0:
            party_block = f"""        <RltdPties>
          <Dbtr><Nm>{_xml_escape(tx.counterparty_name)}</Nm></Dbtr>
          <DbtrAcct><Id><IBAN>{party_iban}</IBAN></Id></DbtrAcct>
        </RltdPties>"""
        else:
            party_block = f"""        <RltdPties>
          <Cdtr><Nm>{_xml_escape(tx.counterparty_name)}</Nm></Cdtr>
          <CdtrAcct><Id><IBAN>{party_iban}</IBAN></Id></CdtrAcct>
        </RltdPties>"""

        ntries.append(f"""    <Ntry>
      <Amt Ccy="{CURRENCY}">{amt_abs}</Amt>
      <CdtDbtInd>{cd_dbt}</CdtDbtInd>
      <Sts>BOOK</Sts>
      <BookgDt><Dt>{tx.booking_date.isoformat()}</Dt></BookgDt>
      <ValDt><Dt>{tx.booking_date.isoformat()}</Dt></ValDt>
      <AcctSvcrRef>BANKREF-{idx:04d}</AcctSvcrRef>
      <BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>ESCT</SubFmlyCd></Fmly></Domn></BkTxCd>
      <NtryDtls>
        <TxDtls>
          <Refs>
            <AcctSvcrRef>BANKREF-{idx:04d}</AcctSvcrRef>
            <EndToEndId>{_xml_escape(tx.end_to_end_id)}</EndToEndId>
          </Refs>
          <Amt Ccy="{CURRENCY}">{amt_abs}</Amt>
          <CdtDbtInd>{cd_dbt}</CdtDbtInd>
{party_block}
          <RmtInf><Ustrd>{_xml_escape(tx.purpose)}</Ustrd></RmtInf>
        </TxDtls>
      </NtryDtls>
    </Ntry>""")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
 <BkToCstmrStmt>
  <GrpHdr>
   <MsgId>SEED-{from_date:%Y%m%d}-{to_date:%Y%m%d}</MsgId>
   <CreDtTm>{to_date.isoformat()}T18:00:00</CreDtTm>
  </GrpHdr>
  <Stmt>
   <Id>STMT-{STATEMENT_NUMBER:05d}-{SEQUENCE_NUMBER:03d}</Id>
   <ElctrncSeqNb>{STATEMENT_NUMBER}</ElctrncSeqNb>
   <CreDtTm>{to_date.isoformat()}T18:00:00</CreDtTm>
   <FrToDt>
    <FrDtTm>{from_date.isoformat()}T00:00:00</FrDtTm>
    <ToDtTm>{to_date.isoformat()}T23:59:59</ToDtTm>
   </FrToDt>
   <Acct>
    <Id><IBAN>{iban}</IBAN></Id>
    <Ccy>{CURRENCY}</Ccy>
   </Acct>
   <Bal>
    <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
    <Amt Ccy="{CURRENCY}">{_amt_us(OPENING_BALANCE)}</Amt>
    <CdtDbtInd>CRDT</CdtDbtInd>
    <Dt><Dt>{from_date.isoformat()}</Dt></Dt>
   </Bal>
   <Bal>
    <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
    <Amt Ccy="{CURRENCY}">{_amt_us(closing)}</Amt>
    <CdtDbtInd>CRDT</CdtDbtInd>
    <Dt><Dt>{to_date.isoformat()}</Dt></Dt>
   </Bal>
{chr(10).join(ntries)}
  </Stmt>
 </BkToCstmrStmt>
</Document>
"""
    return xml


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _selftest(path: str, expected_format: str, expected_count: int) -> None:
    """Datei zur Sicherheit durch den OSS-Parser jagen."""
    from app.bank_import.parsers import detect_format, parse

    with open(path, "rb") as f:
        content = f.read()
    detected = detect_format(content)
    if detected != expected_format:
        raise SystemExit(
            f"FEHLER {path}: detect_format -> {detected!r}, erwartet {expected_format!r}"
        )
    stmt = parse(content, detected)
    if len(stmt.lines) != expected_count:
        raise SystemExit(
            f"FEHLER {path}: {len(stmt.lines)} Zeilen geparst, erwartet {expected_count}"
        )
    print(
        f"  OK {os.path.basename(path):40s} format={detected} "
        f"lines={len(stmt.lines)} iban={stmt.account_iban} "
        f"open={stmt.opening_balance} close={stmt.closing_balance}"
    )


def main() -> None:
    targets = [
        ("giro_2025-09.mt940.sta",   render_mt940(TRANSACTIONS),   "mt940"),
        ("giro_2025-09.mt942.sta",   render_mt942(TRANSACTIONS),   "mt942"),
        ("giro_2025-09.camt053.xml", render_camt053(TRANSACTIONS), "camt053"),
    ]
    for name, content, _fmt in targets:
        path = os.path.join(_HERE, name)
        mode = "wb" if name.endswith(".xml") else "w"
        if mode == "wb":
            with open(path, "wb") as f:
                f.write(content.encode("utf-8"))
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
        print(f"geschrieben: {path}")

    print("\nSelf-Test (Parsen durch OSS-Parser):")
    for name, _content, fmt in targets:
        _selftest(os.path.join(_HERE, name), fmt, len(TRANSACTIONS))


if __name__ == "__main__":
    main()
