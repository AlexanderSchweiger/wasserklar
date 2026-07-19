"""Guard-Flag fuer einen laufenden Voll-Import.

Der Import in :mod:`app.data_transfer.services` schreibt Records **roh** — er
ist die 1:1-Wiederherstellung eines Exports, keine fachliche Neuanlage. Er
laeuft aber ueber die ORM-Session, und aufgesetzte Schichten (SaaS) haengen
``before_flush``-Listener daran, die bei jeder Neuanlage Folgeobjekte
erzeugen. Waehrend eines Imports muessen die schweigen — sonst legt der
Listener beim Customer-Insert z.B. einen Opt-In-Code an und der Import
kollidiert danach mit dem mit-exportierten Code desselben Kunden
(``uq_invoice_email_optin_customer``). Fachlich waere es ohnehin falsch: der
wiederhergestellte Code ist der, der auf der gedruckten Rechnung des Kunden
steht.

Bewusst im OSS, obwohl der einzige heutige Nutzer die SaaS-Schicht ist: das
Flag beschreibt einen OSS-Zustand ("Voll-Import laeuft"), den jede Schicht
abfragen kann, ohne dass OSS die SaaS kennt.
"""

from __future__ import annotations

from contextlib import contextmanager

from flask import g

_FLAG = "data_transfer_import_active"


def is_import_active() -> bool:
    """True, solange ein ``import_from_zip``-Lauf offen ist.

    Ausserhalb eines App-Kontexts (kein ``g``) immer False — der Import
    laeuft immer innerhalb eines Kontexts (Route oder Flask-CLI).
    """
    try:
        return bool(g.get(_FLAG, False))
    except (RuntimeError, AttributeError):
        return False


@contextmanager
def import_active():
    """Markiert den umschlossenen Block als Voll-Import."""
    try:
        setattr(g, _FLAG, True)
        marked = True
    except (RuntimeError, AttributeError):
        marked = False
    try:
        yield
    finally:
        if marked:
            try:
                g.pop(_FLAG, None)
            except (RuntimeError, AttributeError):
                pass
