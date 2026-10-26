"""PDF-Erzeugung laedt nur ``data:``-Ressourcen (SECURITY_AUDIT #11).

Ohne Sperre bettet WeasyPrint lokale Dateien ein, die ein mandantengepflegter Text
referenziert. ``app.pdf_html`` ist die einzige Stelle, die ``weasyprint.HTML`` baut.
"""

from __future__ import annotations

import base64
import io
import re
import sys
import types
from pathlib import Path

import pytest

from app import pdf_html

APP_DIR = Path(__file__).resolve().parents[2] / "app"


class TestAllowedUrls:
    @pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "DATA:text/plain,x", " data:x"])
    def test_data_uris_are_allowed(self, url):
        assert pdf_html.is_allowed(url)

    @pytest.mark.parametrize("url", [
        "file:///etc/hostname", "FILE:///etc/hostname", "/etc/hostname", "http://127.0.0.1/x",
        "https://example.org/logo.png", "ftp://host/x", "", None, "relative/path.png",
    ])
    def test_everything_else_is_blocked(self, url):
        assert not pdf_html.is_allowed(url)
        with pytest.raises(pdf_html.BlockedURL):
            pdf_html._check(url)


class TestHtmlFactory:
    def test_missing_weasyprint_fails_on_import_like_before(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "weasyprint", None)  # Import -> ImportError
        with pytest.raises(ImportError):
            from app.pdf_html import HTML  # noqa: F401

    def test_every_document_gets_the_blocking_fetcher(self, monkeypatch):
        seen = {}

        class FakeHTML:
            def __init__(self, **kwargs):
                seen.update(kwargs)

        module = types.ModuleType("weasyprint")
        module.HTML = FakeHTML
        module.default_url_fetcher = lambda url, *a, **kw: {"string": b"ok"}
        monkeypatch.setitem(sys.modules, "weasyprint", module)
        monkeypatch.setitem(sys.modules, "weasyprint.urls", None)

        from app.pdf_html import HTML
        HTML(string="<p>x</p>")
        fetch = seen["url_fetcher"]
        assert seen["string"] == "<p>x</p>"
        assert fetch("data:text/plain,x") == {"string": b"ok"}
        with pytest.raises(pdf_html.BlockedURL):
            fetch("file:///etc/hostname")

    def test_own_fetcher_or_file_sources_are_refused(self, monkeypatch):
        module = types.ModuleType("weasyprint")
        module.HTML = lambda **kw: kw
        monkeypatch.setitem(sys.modules, "weasyprint", module)
        from app.pdf_html import HTML
        for kwargs in ({"url_fetcher": object()}, {"filename": "/etc/hostname"}, {"url": "file:///x"}):
            with pytest.raises(TypeError):
                HTML(string="x", **kwargs)


def test_no_module_builds_weasyprint_html_directly():
    """Neue PDF-Stellen muessen ueber ``app.pdf_html`` gehen, sonst fehlt die Sperre."""
    pattern = re.compile(r"from weasyprint import [^\n]*\bHTML\b|weasyprint\.HTML\(")
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in APP_DIR.rglob("*.py")
        if path.name != "pdf_html.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


_PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf00000301010018dd8db00000000049454e44ae426082")).decode()


def test_real_weasyprint_embeds_no_local_file_but_keeps_data_images(tmp_path):
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        pytest.skip("WeasyPrint nur im Container")
    from pypdf import PdfReader

    from app.pdf_html import HTML

    secret = tmp_path / "secret.txt"
    secret.write_text("PROBE-SECRET-123")
    uri = secret.as_uri()
    html = (f'<a rel="attachment" href="{uri}">x</a><link rel="attachment" href="{uri}">'
            f'<link rel="stylesheet" href="{uri}"><img src="data:image/png;base64,{_PNG_1PX}">')
    pdf = HTML(string=html, base_url=str(tmp_path)).write_pdf()

    reader = PdfReader(io.BytesIO(pdf))
    assert reader.attachments == {}
    assert b"PROBE-SECRET-123" not in pdf
    assert sum(len(page.images) for page in reader.pages) == 1
