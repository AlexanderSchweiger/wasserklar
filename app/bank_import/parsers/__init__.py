from app.bank_import.parsers.types import ParsedLine, ParsedStatement
from app.bank_import.parsers import camt053, mt940


def detect_format(content: bytes) -> str:
    head = content[:1024].lstrip()

    if head.startswith(b"<?xml") or b"BkToCstmrStmt" in head or b"Document" in head[:200]:
        return "camt053"

    if b":25:" in head or b":20:" in head:
        # MT942 enthaelt typischerweise das Feld :90D: / :90C: (Floor limit indicators)
        # oder :34F: (Floor limit indicator). MT940 hat :60F: und :62F:.
        if b":90D:" in content or b":90C:" in content or b":34F:" in content:
            return "mt942"
        return "mt940"

    raise ValueError("Format konnte nicht erkannt werden (weder camt.053 noch MT940/MT942).")


def parse(content: bytes, format: str) -> ParsedStatement:
    if format == "camt053":
        return camt053.parse(content)
    if format in ("mt940", "mt942"):
        return mt940.parse(content, format)
    raise ValueError(f"Unbekanntes Format: {format}")


__all__ = ["ParsedLine", "ParsedStatement", "detect_format", "parse"]
