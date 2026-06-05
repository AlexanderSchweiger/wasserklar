"""Datei-Ablage für die Schriftführung — Jahres-Unterordner im
``schriftverkehr``-Verzeichnis, mit Versionierung analog zu den
Rechnungsdokumenten ([invoices/routes.py] ``_get_doc_dir``/``_versioned_path``).

Der Ordner reitet auf dem pro Request umgebogenen ``PDF_DIR`` (genau wie
``technik.services.technik_upload_dir``): OSS-standalone landet er unter
``instance/schriftverkehr``, im SaaS pro Tenant unter
``instance/tenants/<slug>/schriftverkehr`` — die Tenant-Trennung ist damit
geschenkt, ohne die SaaS-Schicht anzufassen.
"""
import os
import re

from flask import current_app


def get_schriftverkehr_dir(year):
    """Jahres-Unterordner für den Schriftverkehr (Geschwister von ``PDF_DIR``);
    legt ihn an."""
    base = os.path.join(os.path.dirname(current_app.config["PDF_DIR"]), "schriftverkehr")
    doc_dir = os.path.join(base, str(year))
    os.makedirs(doc_dir, exist_ok=True)
    return doc_dir


def versioned_path(doc_dir, basename, ext):
    """Eindeutiger Pfad; hängt ``_V2``, ``_V3`` … an, wenn das Original existiert
    (historische Versionen bleiben erhalten). ``ext`` ohne führenden Punkt."""
    base = os.path.join(doc_dir, f"{basename}.{ext}")
    if not os.path.exists(base):
        return base
    v = 2
    while True:
        cand = os.path.join(doc_dir, f"{basename}_V{v}.{ext}")
        if not os.path.exists(cand):
            return cand
        v += 1


_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slugify_filename(text, default="dokument"):
    """Macht aus beliebigem Text einen dateinamen-tauglichen Slug
    (ASCII, keine Leerzeichen) — Basis für die Dateinamen im Archiv."""
    text = (text or "").strip().replace(" ", "_")
    text = _SLUG_RE.sub("", text)
    text = text.strip("._-")
    return text or default
