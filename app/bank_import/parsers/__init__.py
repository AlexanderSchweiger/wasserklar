from app.bank_import.parsers.types import ParsedLine, ParsedStatement
from app.bank_import.parsers import camt053, mt940, ofx


def detect_format(content: bytes) -> str:
    head = content[:1024].lstrip()

    # OFX zuerst pruefen: OFX 2.x beginnt ebenfalls mit <?xml, traegt aber den
    # <?OFX?>-Prolog bzw. das <OFX>-Root (2.x) oder den OFXHEADER:-Block (1.x).
    upper = head.upper()
    if b"OFXHEADER" in upper or b"<OFX>" in upper or b"<?OFX" in upper:
        return "ofx"

    if head.startswith(b"<?xml") or b"BkToCstmrStmt" in head or b"Document" in head[:200]:
        return "camt053"

    if b":25:" in head or b":20:" in head:
        # MT942 enthaelt typischerweise das Feld :90D: / :90C: (Floor limit indicators)
        # oder :34F: (Floor limit indicator). MT940 hat :60F: und :62F:.
        if b":90D:" in content or b":90C:" in content or b":34F:" in content:
            return "mt942"
        return "mt940"

    raise ValueError(
        "Format konnte nicht erkannt werden (weder camt.053, MT940/MT942 noch OFX)."
    )


def parse(content: bytes, format: str) -> ParsedStatement:
    if format == "camt053":
        return camt053.parse(content)
    if format in ("mt940", "mt942"):
        return mt940.parse(content, format)
    if format == "ofx":
        return ofx.parse(content)
    raise ValueError(f"Unbekanntes Format: {format}")


__all__ = ["ParsedLine", "ParsedStatement", "detect_format", "parse"]
