"""Hook-Registry für den Rechnungs-E-Mail-Versand.

Per Default no-op — relevante Aufrufer (SaaS) registrieren ihre Hooks beim
App-Startup. Hält OSS-Standalone-Pfad frei von SaaS-spezifischer Logik
(Postmark-Metadata, MessageID-Vorbelegung), erlaubt aber dem SaaS-Layer,
auf den Versand zu hängen, ohne die OSS-Route zu forken.

Verwendung:

    # OSS-Route (immer):
    run_before_send(invoice, msg)
    send_mail(msg)
    msg_id = read_message_id(msg)  # gesetzt vom SaaS-Hook oder None

    # SaaS-Init (einmalig):
    from app.invoices.send_email_hooks import register_before_send
    register_before_send(my_hook)
"""

from __future__ import annotations

from typing import Callable, List

_BEFORE_SEND_HOOKS: List[Callable[[object, object], None]] = []


def register_before_send(hook: Callable[[object, object], None]) -> None:
    """Registriert einen Callback, der vor dem Versand mit (invoice, msg) aufgerufen wird.

    Mehrfach-Registrierung desselben Callables wird ignoriert (für idempotente
    SaaS-Init-Aufrufe).
    """
    if hook not in _BEFORE_SEND_HOOKS:
        _BEFORE_SEND_HOOKS.append(hook)


def run_before_send(invoice, msg) -> None:
    """Ruft alle registrierten Hooks der Reihe nach mit (invoice, msg) auf.

    Hooks dürfen `msg.extra_headers` mutieren (z. B. Postmark-Metadata,
    X-PM-Message-Id). Exceptions werden geloggt, brechen den Versand aber
    nicht ab — Tracking-Probleme dürfen den Mail-Versand nicht verhindern.
    """
    from flask import current_app

    for hook in _BEFORE_SEND_HOOKS:
        try:
            hook(invoice, msg)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "send_email before-hook failed: %s", getattr(hook, "__name__", hook)
            )


def read_message_id(msg) -> str | None:
    """Liest die für den Webhook-Join relevante MessageID aus den Headern.

    SaaS-Postmark-Hook setzt `X-PM-Message-Id` vor dem Versand auf eine
    selbst generierte UUID — Postmark übernimmt diesen Header als MessageID.
    Im OSS-Standalone-Pfad ist der Header nicht gesetzt → None.
    """
    headers = getattr(msg, "extra_headers", None) or {}
    return headers.get("X-PM-Message-Id")
