"""Belegablage: Dateityp wird an den Magic Bytes erkannt, nie an der Endung."""
import pytest

from app.documents import service as svc

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16


class TestSniff:
    @pytest.mark.parametrize("data, ext", [
        (b"%PDF-1.7\n%...", "pdf"),
        (b"\n  %PDF-1.4", "pdf"),                      # Leerraum vor dem Header tolerieren die Viewer
        (JPG, "jpg"), (PNG, "png"), (WEBP, "webp"),
        (b"<?xml version='1.0'?><Invoice/>", "xml"),
        (b"\xef\xbb\xbf<Invoice/>", "xml"),            # UTF-8-BOM
    ])
    def test_known_types(self, data, ext):
        assert svc.sniff(data) == ext

    @pytest.mark.parametrize("data", [
        b"GIF89a" + b"\x00" * 20,
        b"II*\x00" + b"\x00" * 20,                     # TIFF
        b"PK\x03\x04" + b"\x00" * 20,                  # ZIP/DOCX
        b"MZ" + b"\x00" * 20,                          # ausfuehrbar
        b"plain text",
        b"",
    ])
    def test_everything_else_is_refused(self, data):
        with pytest.raises(svc.DocumentError, match="nicht unterstützt"):
            svc.sniff(data)

    def test_heic_gets_its_own_hint(self):
        with pytest.raises(svc.DocumentError, match="HEIC"):
            svc.sniff(HEIC)

    def test_the_extension_does_not_matter(self):
        # ein PDF bleibt ein PDF, auch wenn es "foto.jpg" heisst — und umgekehrt
        assert svc.sniff(b"%PDF-1.7 rest") == "pdf"
        assert svc.sniff(JPG) == "jpg"


class TestFormatSize:
    @pytest.mark.parametrize("n, text", [
        (0, "0 B"), (900, "900 B"), (2048, "2 KB"), (1_572_864, "1,5 MB"),
        (12 * 1024 * 1024, "12 MB"), (3 * 1024 ** 3, "3,0 GB"), (None, "0 B"),
    ])
    def test_german_sizes(self, n, text):
        assert svc.format_size(n) == text


class TestParseDecimal:
    @pytest.mark.parametrize("raw, expected", [
        ("12,34", "12.34"), ("1.234,56", "1234.56"), ("12.34", "12.34"), (" 5 ", "5"), ("", None), (None, None),
    ])
    def test_valid(self, raw, expected):
        from decimal import Decimal
        assert svc.parse_decimal(raw) == (Decimal(expected) if expected is not None else None)

    @pytest.mark.parametrize("raw", ["abc", "-5", "NaN", "Infinity", "1,2,3"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            svc.parse_decimal(raw)
