"""Hook-Registry für den Mahn-E-Mail-Versand.

Spiegel von ``app/invoices/send_email_hooks.py`` — per Default no-op. Der
SaaS-Layer registriert beim App-Startup einen Postmark-Hook, der die
Metadata-Header (tenant, subject_type='dunning', subject_id) setzt, damit der
Platform-Webhook Delivery-/Bounce-Events ins Tenant-Schema zurueckschreibt.
OSS-Standalone bleibt dadurch frei von SaaS-spezifischer Logik.

Verwendung:

    # OSS-Route (immer):
    run_before_send(notice, msg)
    send_mail(msg)
    msg_id = read_message_id(msg)  # gesetzt vom SaaS-Hook oder None

    # SaaS-Init (einmalig):
    from app.dunning.send_email_hooks import register_before_send
    register_before_send(my_hook)
"""

from __future__ import annotations

from typing import Callable, List

_BEFORE_SEND_HOOKS: List[Callable[[object, object], None]] = []


def register_before_send(hook: Callable[[object, object], None]) -> None:
    """Registriert einen Callback, der vor dem Versand mit (notice, msg) aufgerufen wird.

    Mehrfach-Registrierung desselben Callables wird ignoriert (für idempotente
    SaaS-Init-Aufrufe).
    """
    if hook not in _BEFORE_SEND_HOOKS:
        _BEFORE_SEND_HOOKS.append(hook)


def run_before_send(notice, msg) -> None:
    """Ruft alle registrierten Hooks der Reihe nach mit (notice, msg) auf.

    Hooks dürfen ``msg.extra_headers`` mutieren (z. B. Postmark-Metadata,
    X-PM-Message-Id). Exceptions werden geloggt, brechen den Versand aber
    nicht ab — Tracking-Probleme dürfen den Mail-Versand nicht verhindern.
    """
    from flask import current_app

    for hook in _BEFORE_SEND_HOOKS:
        try:
            hook(notice, msg)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "dunning send_email before-hook failed: %s",
                getattr(hook, "__name__", hook),
            )


def read_message_id(msg) -> str | None:
    """Liest die für den Webhook-Join relevante MessageID aus den Headern.

    SaaS-Postmark-Hook setzt ``X-PM-Message-Id`` vor dem Versand; im
    OSS-Standalone-Pfad ist der Header nicht gesetzt → None.
    """
    headers = getattr(msg, "extra_headers", None) or {}
    return headers.get("X-PM-Message-Id")
