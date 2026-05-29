"""Erweiterungspunkt fuer zusaetzlichen Template-Kontext beim PDF-Rendering.

Die OSS-App kennt nur ihre eigenen Kontext-Variablen (``invoice``, ``design``,
``contact_info``). Aufsetzende Schichten (SaaS) koennen weitere Variablen
beisteuern — z.B. ``email_signup`` (URL + QR-Code fuer die Rechnung-per-Mail-
Selbstregistrierung) — ohne den OSS-Render-Pfad zu forken.

Gleiches Muster wie ``send_email_hooks``: Provider werden beim App-Start
registriert; ``build_pdf_context`` ruft sie zur Render-Zeit auf und merged ihre
Rueckgaben. Ein Provider bekommt ``for_email`` mitgeteilt, damit er Inhalte
unterdruecken kann, die nur auf der **gedruckten** Rechnung sinnvoll sind.
"""

from __future__ import annotations

from typing import Callable, Optional

# Provider-Signatur: fn(invoice, *, for_email: bool) -> dict | None
_PROVIDERS: list[Callable[..., Optional[dict]]] = []


def register_pdf_context_provider(fn: Callable[..., Optional[dict]]) -> None:
    if fn not in _PROVIDERS:
        _PROVIDERS.append(fn)


def reset_pdf_context_providers() -> None:
    """Nur fuer Tests / wiederholte App-Erzeugung."""
    _PROVIDERS.clear()


def build_pdf_context(invoice, *, for_email: bool) -> dict:
    """Sammelt den Zusatzkontext aller registrierten Provider."""
    ctx: dict = {}
    for fn in _PROVIDERS:
        try:
            extra = fn(invoice, for_email=for_email)
        except Exception:  # noqa: BLE001 - ein Provider darf das PDF nicht killen
            from flask import current_app
            current_app.logger.exception("PDF-Context-Provider fehlgeschlagen")
            extra = None
        if extra:
            ctx.update(extra)
    return ctx
