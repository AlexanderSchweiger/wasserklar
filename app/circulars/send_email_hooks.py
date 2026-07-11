"""Hook-Registry für den Rundschreiben-E-Mail-Versand.

Spiegel von ``app/schriftfuehrung/send_email_hooks.py`` — per Default no-op. Der
SaaS-Layer registriert beim App-Startup Hooks, die (a) Postmark-Metadaten an die
Nachricht heften und (b) den kundengebundenen Abmelde-Footer anhängen (nur bei
Nicht-Notfall-Rundschreiben). OSS-Standalone bleibt dadurch frei von
SaaS-spezifischer Logik.

Anders als bei Rechnung/Mahnung wird hier die **Empfänger-Zeile**
(``CircularRecipient``) an den Hook gereicht: sie führt sowohl zum Kunden
(``recipient.customer`` — Abmelde-Footer) als auch zum trackbaren Subjekt
(``recipient.id`` — Postmark-Metadata) und zur Art (``recipient.circular.kind``
— Notfall-Erkennung für den Footer-Skip).

Verwendung:

    # OSS-Route (immer):
    run_before_send(recipient, msg)
    send_mail(msg)

    # SaaS-Init (einmalig):
    from app.circulars.send_email_hooks import register_before_send
    register_before_send(my_hook)
"""

from __future__ import annotations

from typing import Callable, List

_BEFORE_SEND_HOOKS: List[Callable[[object, object], None]] = []


def register_before_send(hook: Callable[[object, object], None]) -> None:
    """Registriert einen Callback, der vor dem Versand mit (recipient, msg)
    aufgerufen wird. Mehrfach-Registrierung desselben Callables wird ignoriert
    (für idempotente SaaS-Init-Aufrufe)."""
    if hook not in _BEFORE_SEND_HOOKS:
        _BEFORE_SEND_HOOKS.append(hook)


def run_before_send(recipient, msg) -> None:
    """Ruft alle registrierten Hooks der Reihe nach mit (recipient, msg) auf.

    Hooks dürfen ``msg`` mutieren (Body/HTML-Footer, ``extra_headers``).
    Exceptions werden geloggt, brechen den Versand aber nicht ab — ein
    Footer-/Tracking-Problem darf den (Notfall-)Versand nicht verhindern.
    """
    from flask import current_app

    for hook in _BEFORE_SEND_HOOKS:
        try:
            hook(recipient, msg)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "circular send_email before-hook failed: %s",
                getattr(hook, "__name__", hook),
            )


def read_message_id(msg) -> str | None:
    """Liest die Message-ID aus den ``extra_headers`` (falls ein Hook eine
    gesetzt hat) — analog zu ``app/invoices/send_email_hooks.py``."""
    headers = getattr(msg, "extra_headers", None) or {}
    return headers.get("Message-ID") or headers.get("Message-Id")
