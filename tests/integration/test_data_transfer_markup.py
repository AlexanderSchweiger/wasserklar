"""Der Datenimport bereinigt HTML-Felder wie die Formulare (SECURITY_AUDIT #11).

Kontakttext, Einladungs- und Protokolltexte gibt die App als HTML aus (auch in PDFs).
Die Formulare schicken sie durch ``sanitize_rich_text``; eine praeparierte Export-ZIP
umging das bisher.
"""

from __future__ import annotations

import io

from app.data_transfer.services import _sanitize_imported_markup, export_to_zip
from app.extensions import db
from app.models import AppSetting, Meeting, MeetingProtocol
from app.settings_service import sanitize_rich_text
from tests.integration.test_data_transfer_path_safety import (  # noqa: F401  (Fixtures)
    _fresh_session, _import, _tamper, tenant,
)

EVIL = ('Tel. <b>0732</b><br><a rel="attachment" href="file:///etc/hostname">x</a>'
        '<img src="file:///etc/hostname"><script>alert(1)</script>')
SAFE = sanitize_rich_text(EVIL)
SELECTION = {"stammdaten": False, "buchungen": False, "mahnwesen": False,
             "einstellungen": True, "years": []}


def test_sanitizer_keeps_formatting_and_drops_markup():
    assert SAFE.startswith("Tel. <b>0732</b><br>")
    for bad in ("<a", "<img", "<script", "href", "file:"):
        assert bad not in SAFE
    assert sanitize_rich_text(SAFE) == SAFE   # idempotent: normale Exporte bleiben gleich


def test_rich_text_records_are_sanitized():
    records = {
        AppSetting: [{"key": "invoice.contact_info", "value": EVIL},
                     {"key": "wg.logo", "value": "file:///etc/hostname"},
                     {"key": "wg.name", "value": "<b>bleibt Text</b>"}],
        Meeting: [{"id": 1, "intro_text": EVIL, "closing_text": None}],
        MeetingProtocol: [{"id": 1, "content_html": EVIL}],
    }
    _sanitize_imported_markup(records)
    settings = {r["key"]: r["value"] for r in records[AppSetting]}
    assert settings["invoice.contact_info"] == SAFE
    assert settings["wg.logo"] is None
    assert settings["wg.name"] == "<b>bleibt Text</b>"   # Klartextfeld: escaped die Vorlage
    assert records[Meeting][0] == {"id": 1, "intro_text": SAFE, "closing_text": None}
    assert records[MeetingProtocol][0]["content_html"] == SAFE


def test_crafted_settings_zip_is_sanitized_on_import(app, tenant):  # noqa: F811
    AppSetting.set("invoice.contact_info", "Tel. <b>1</b>")
    db.session.commit()
    buf = io.BytesIO()
    export_to_zip({**SELECTION, "include_pdfs": False}, buf, exported_by="test")

    def edit(records):
        for rec in records:
            if rec["key"] == "invoice.contact_info":
                rec["value"] = EVIL
        records.append({"key": "wg.logo", "value": "file:///etc/hostname"})

    _import(_tamper(buf.getvalue(), edit={"app_settings": edit}), tenant)
    assert AppSetting.get("invoice.contact_info") == SAFE
    assert not AppSetting.get("wg.logo")
