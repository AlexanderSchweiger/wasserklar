"""Dubletten-Erkennung für Kunden.

Liefert ähnliche Kunden zu einem vorgegebenen Namen + Adresse. Basis ist
``difflib.SequenceMatcher`` auf normalisierten Namen; ein gleicher PLZ oder Ort
erhöht den Score zusätzlich. Schwellwert und Bonus sind hier hart codiert
(bewusste Entscheidung, siehe ADR-001).

Die Prüfung umfasst **aktive und inaktive** Kunden, um versehentliche
Neu-Anlage eines soft-deleted Datensatzes zu erkennen.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

from app.models import Customer


SIMILARITY_THRESHOLD = 0.80
LOCATION_BONUS = 0.10
MAX_RESULTS = 10


_whitespace_re = re.compile(r"\s+")
_punct_re = re.compile(r"[^\w\s]", flags=re.UNICODE)


def _normalize(value: str | None) -> str:
    """Normalisiert Strings für den Vergleich: lowercase, Satzzeichen weg,
    Mehrfach-Whitespace kollabiert, getrimmt."""
    if not value:
        return ""
    v = value.lower()
    v = _punct_re.sub(" ", v)
    v = _whitespace_re.sub(" ", v)
    return v.strip()


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def find_similar_customers(
    name: str,
    strasse: str | None = None,
    plz: str | None = None,
    ort: str | None = None,
    exclude_id: int | None = None,
) -> list[tuple[Customer, float]]:
    """Liefert Kunden, die dem übergebenen Namen/Adresse ähnlich sind.

    Rückgabe: Liste von ``(Customer, score)``-Tupeln, absteigend sortiert nach
    Score. Nur Einträge mit ``score >= SIMILARITY_THRESHOLD`` werden
    zurückgegeben (inklusive Location-Bonus auf 1.0 gekappt).

    ``exclude_id`` kann im Edit-Flow genutzt werden, um den Datensatz selbst
    nicht als Dublette zu sich selbst anzuzeigen.
    """
    normalized_name = _normalize(name)
    if not normalized_name:
        return []

    normalized_plz = (plz or "").strip()
    normalized_ort = _normalize(ort)

    # Alle Kunden laden — aktive UND inaktive, denn inaktive Kunden sollen
    # ebenfalls als Dubletten erkannt werden.
    query = Customer.query
    if exclude_id is not None:
        query = query.filter(Customer.id != exclude_id)

    results: list[tuple[Customer, float]] = []
    for c in query.all():
        candidate_name = _normalize(c.name)
        score = _name_similarity(normalized_name, candidate_name)
        if score < SIMILARITY_THRESHOLD - LOCATION_BONUS:
            # Auch mit Bonus nicht erreichbar — überspringen.
            continue

        # Location-Bonus: gleiche PLZ oder gleicher Ort erhöht den Score.
        if normalized_plz and c.plz and normalized_plz == c.plz.strip():
            score += LOCATION_BONUS
        elif normalized_ort and _normalize(c.ort) == normalized_ort:
            score += LOCATION_BONUS

        if score >= SIMILARITY_THRESHOLD:
            results.append((c, min(score, 1.0)))

    results.sort(key=lambda t: t[1], reverse=True)
    return results[:MAX_RESULTS]
