"""Einzige Stelle, die WeasyPrint-``HTML``-Dokumente baut.

WeasyPrints Standard-Fetcher laedt jede URL aus dem HTML — auch ``file://`` — und
bettet z. B. ``<a rel="attachment" href="…">`` als PDF-Anhang ein. In die PDF-Vorlagen
gelangen Texte, die der Mandant pflegt; ohne Sperre koennte ein solcher Text
Dateien des Servers (fremde Mandanten, Secrets) in ein PDF holen (SECURITY_AUDIT #11).

Alle Vorlagen bringen ihre Ressourcen als ``data:``-URI mit (Logo, QR-Codes), Schriften
kommen ueber fontconfig. Deshalb wird **nur** ``data:`` geladen; alles andere bleibt leer
(WeasyPrint protokolliert eine Warnung und rendert weiter).

``from app.pdf_html import HTML`` verhaelt sich wie ``from weasyprint import HTML``: fehlt
WeasyPrint, wirft schon der Import ``ImportError``/``OSError`` und die Fallbacks der
Aufrufer greifen. WeasyPrint wird dafuer bei jedem Zugriff frisch aufgeloest (Modul-
``__getattr__``), nicht einmal beim ersten Import — sonst bliebe ein einmal geladener
Stand (oder ein Test-Ersatzmodul) fuer immer haengen.
"""

from __future__ import annotations

import importlib

ALLOWED_PROTOCOLS = ("data",)


class BlockedURL(ValueError):
    """Eine Ressource ausserhalb von ``ALLOWED_PROTOCOLS`` (WeasyPrint faengt das ab)."""


def is_allowed(url) -> bool:
    scheme = str(url or "").split(":", 1)[0].strip().lower()
    return scheme in ALLOWED_PROTOCOLS


def _check(url):
    if not is_allowed(url):
        raise BlockedURL(f"Blockierte PDF-Ressource: {str(url)[:80]}")


def url_fetcher():
    """Fetcher, der nur ``data:`` laedt (WeasyPrint >= 68: ``URLFetcher``, sonst Funktion)."""
    weasyprint = importlib.import_module("weasyprint")
    try:
        base = importlib.import_module("weasyprint.urls").URLFetcher
    except (ImportError, AttributeError):
        def fetch(url, *args, **kwargs):
            _check(url)
            return weasyprint.default_url_fetcher(url, *args, **kwargs)
        return fetch

    class DataOnlyFetcher(base):
        def __init__(self):
            super().__init__(allowed_protocols=ALLOWED_PROTOCOLS, allow_redirects=False)

        def fetch(self, url, headers=None):
            _check(url)
            return super().fetch(url, headers)

    return DataOnlyFetcher()


def _html(*, string, base_url=None, **kwargs):
    """``weasyprint.HTML`` mit dem gesperrten Fetcher. Nur ``string=`` (keine Dateien/URLs)."""
    for forbidden in ("url_fetcher", "filename", "url", "file_obj", "guess"):
        if forbidden in kwargs:
            raise TypeError(f"app.pdf_html.HTML erlaubt kein {forbidden!r}")
    weasyprint = importlib.import_module("weasyprint")
    return weasyprint.HTML(string=string, base_url=base_url, url_fetcher=url_fetcher(), **kwargs)


def __getattr__(name):
    if name == "HTML":
        importlib.import_module("weasyprint")  # ImportError/OSError, wenn WeasyPrint fehlt
        return _html
    raise AttributeError(name)
