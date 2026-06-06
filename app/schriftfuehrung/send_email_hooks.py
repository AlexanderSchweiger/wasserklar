"""Hook-Registry für den Sitzungseinladungs-E-Mail-Versand.

Spiegel von ``app/invoices/send_email_hooks.py`` und
``app/dunning/send_email_hooks.py`` — per Default no-op. Der SaaS-Layer
registriert beim App-Startup einen Hook, der den kundengebundenen
Abmelde-Footer (Widerruf des Schriftverkehrs per E-Mail) anhängt. OSS-Standalone
bleibt dadurch frei von SaaS-spezifischer Logik.

Anders als bei Rechnung/Mahnung wird hier der **Empfänger-Kunde** an den Hook
gereicht — eine Sitzungseinladung hat keinen eigenen E-Mail-trackbaren
Subject-Datensatz, und der relevante Anker (Abmelde-Link) ist ohnehin
kundengebunden.

Verwendung:

    # OSS-Route (immer):
    run_before_send(customer, msg)
    send_mail(msg)

    # SaaS-Init (einmalig):
    from app.schriftfuehrung.send_email_hooks import register_before_send
    register_before_send(my_hook)
"""

from __future__ import annotations

from typing import Callable, List

_BEFORE_SEND_HOOKS: List[Callable[[object, object], None]] = []


def register_before_send(hook: Callable[[object, object], None]) -> None:
    """Registriert einen Callback, der vor dem Versand mit (customer, msg) aufgerufen wird.

    Mehrfach-Registrierung desselben Callables wird ignoriert (für idempotente
    SaaS-Init-Aufrufe).
    """
    if hook not in _BEFORE_SEND_HOOKS:
        _BEFORE_SEND_HOOKS.append(hook)


def run_before_send(customer, msg) -> None:
    """Ruft alle registrierten Hooks der Reihe nach mit (customer, msg) auf.

    Hooks dürfen ``msg`` mutieren (Body/HTML-Footer, ``extra_headers``).
    Exceptions werden geloggt, brechen den Versand aber nicht ab — ein
    Footer-Problem darf den Mail-Versand nicht verhindern.
    """
    from flask import current_app

    for hook in _BEFORE_SEND_HOOKS:
        try:
            hook(customer, msg)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "meeting send_email before-hook failed: %s",
                getattr(hook, "__name__", hook),
            )
