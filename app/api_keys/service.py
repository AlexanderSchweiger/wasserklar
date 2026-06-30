"""API-Schluessel: Erzeugung, Hashing, Verifikation.

Key-Format (Stripe-Stil):  ``qs_live_<slug>_<hex>``
  * ``qs``     Produkt-Praefix (quellstube)
  * ``live``   Environment-Marker (``test`` ist reserviert)
  * ``slug``   Tenant-Slug — NICHT geheim (= Subdomain); dient der
               Tenant-Zuordnung und als Defense-in-Depth-Cross-Check
  * ``hex``    48 Hex-Zeichen Zufall (``secrets.token_hex(24)`` = 192 Bit) —
               der einzige geheime Teil

Der Slug ist ein DNS-Label (Buchstaben/Ziffern/Bindestrich, **kein**
Unterstrich) und der Hex-Teil enthaelt nur ``0-9a-f`` — deshalb laesst sich der
Key zuverlaessig per ``split("_")`` in genau vier Segmente zerlegen.

Gespeichert wird ausschliesslich ``sha256(full_key)`` (hex). Der Klartext wird
genau einmal bei der Erzeugung zurueckgegeben und danach verworfen. Da der
geheime Teil hochentropisch ist (192 Bit), genuegt ein schneller Hash (SHA-256)
— ein langsames Passwort-Hashing (argon2/bcrypt) ist hier unnoetig.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime
from typing import List, Optional, Tuple

from app.extensions import db
from app.models import ApiKey

KEY_PRODUCT = "qs"
KEY_ENV = "live"
_SECRET_BYTES = 24  # token_hex(24) -> 48 Hex-Zeichen = 192 Bit

# Slug = DNS-Label: Kleinbuchstaben/Ziffern/Bindestrich, kein Unterstrich.
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def hash_key(full_key: str) -> str:
    """SHA-256-Hex des vollstaendigen Schluessels."""
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _prefix(full_key: str) -> str:
    """Anzeigbarer, nicht-geheimer Ausschnitt: ``qs_live_<slug>_<8hex>…``."""
    head, _, secret = full_key.rpartition("_")
    return f"{head}_{secret[:8]}…"


def generate_key(slug: str) -> str:
    """Neuen Klartext-Schluessel fuer einen Tenant erzeugen (nicht gespeichert)."""
    secret = secrets.token_hex(_SECRET_BYTES)
    return f"{KEY_PRODUCT}_{KEY_ENV}_{slug}_{secret}"


def parse_slug(full_key: str) -> Optional[str]:
    """Tenant-Slug aus dem Key extrahieren oder ``None`` bei Formatfehler."""
    if not full_key:
        return None
    parts = full_key.split("_")
    if len(parts) != 4:
        return None
    product, env, slug, secret = parts
    if product != KEY_PRODUCT or env != KEY_ENV:
        return None
    if not _SLUG_RE.match(slug):
        return None
    if not secret or not re.fullmatch(r"[0-9a-f]+", secret):
        return None
    return slug


def create_api_key(
    *,
    slug: str,
    label: str,
    scopes: List[str],
    mcp_enabled: bool = False,
    created_by_id: Optional[int] = None,
) -> Tuple[ApiKey, str]:
    """Schluessel anlegen und den Klartext **einmalig** zurueckgeben.

    Erwartet, dass der ``search_path`` bereits auf das Tenant-Schema zeigt
    (die Tabelle ``api_keys`` lebt im Tenant-Schema). Gibt ``(api_key, full_key)``
    zurueck — ``full_key`` ist der Klartext, der danach nicht mehr rekonstruierbar
    ist.
    """
    full_key = generate_key(slug)
    api_key = ApiKey(
        label=label.strip()[:120] or "API-Schluessel",
        key_prefix=_prefix(full_key),
        key_hash=hash_key(full_key),
        scopes=",".join(sorted(set(scopes))),
        mcp_enabled=bool(mcp_enabled),
        created_by_id=created_by_id,
    )
    db.session.add(api_key)
    db.session.commit()
    return api_key, full_key


def verify_key(full_key: str, *, touch: bool = True) -> Optional[ApiKey]:
    """Aktiven (nicht widerrufenen) Schluessel zum Klartext finden, sonst ``None``.

    Erwartet den ``search_path`` auf dem Tenant-Schema. Aktualisiert
    ``last_used_at`` hoechstens minuetlich, um Schreiblast pro Request zu sparen.
    """
    if parse_slug(full_key) is None:
        return None
    api_key = ApiKey.query.filter_by(
        key_hash=hash_key(full_key), revoked_at=None
    ).first()
    if api_key is None:
        return None
    if touch:
        now = datetime.utcnow()
        if api_key.last_used_at is None or (now - api_key.last_used_at).total_seconds() > 60:
            api_key.last_used_at = now
            db.session.commit()
    return api_key
