"""Belegablage: relative Schluessel, kein Ausbruch aus dem Mandanten-Dateibaum, nie ueberschreiben."""
import os
from datetime import date

import pytest

from app.documents import storage

SHA = "ab12cd34" + "0" * 56


@pytest.fixture
def root(app, tmp_path, monkeypatch):
    """Mandanten-Wurzel = Elternordner von PDF_DIR."""
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


class TestKeys:
    @pytest.mark.parametrize("key", [
        "documents/2026/10/12_ab12cd34.pdf",
        "documents/2026/01/1_ab12cd34.jpg",
        "documents/2026/01/99999_ab12cd34.xml",
        "incoming/2026/5_ab12cd34_Rechnung-1.xml",
        "incoming/ohne-datum/5_ab12cd34_r.pdf",
    ])
    def test_valid(self, key):
        assert storage.is_valid_key(key)

    @pytest.mark.parametrize("key", [
        None, "", 5, "../etc/passwd", "/etc/passwd", "documents/../../x.pdf",
        "documents/2026/10/12_ab12cd34.pdf/../../x", "documents\\2026\\10\\12_ab12cd34.pdf",
        "documents/2026/10/12_ab12cd34.exe", "documents/2026/10/12_AB12CD34.pdf",
        "documents/2026/10/12_ab12cd3.pdf", "documents/20266/10/12_ab12cd34.pdf",
        "documents/٢٠٢٦/10/12_ab12cd34.pdf",              # Ziffern anderer Schriften
        "incoming/2026/5_ab12cd34_a/b.xml", "incoming/2026/5_ab12cd34_",
        "C:\\Windows\\win.ini", "\\\\server\\share\\x.pdf", "documents/2026/10/12_ab12cd34.pdf\n",
    ])
    def test_invalid(self, key):
        assert not storage.is_valid_key(key)

    def test_new_key(self):
        assert storage.new_key(12, SHA, "pdf", date(2026, 10, 2)) == "documents/2026/10/12_ab12cd34.pdf"
        with pytest.raises(storage.StorageError):
            storage.new_key(1, SHA, "exe")


class TestPaths:
    def test_inside_the_tenant_root(self, app, root):
        path = storage.path_for("documents/2026/10/12_ab12cd34.pdf")
        assert path == (root / "documents" / "2026" / "10" / "12_ab12cd34.pdf").resolve()

    def test_invalid_keys_have_no_path(self, app, root):
        assert storage.path_for("../x.pdf") is None and storage.path_for(None) is None

    def test_the_root_follows_pdf_dir(self, app, root, tmp_path, monkeypatch):
        """Im SaaS biegt die Mandanten-Middleware PDF_DIR je Request um — die Ablage folgt."""
        other = tmp_path / "other" / "pdfs"
        other.mkdir(parents=True)
        monkeypatch.setitem(app.config, "PDF_DIR", str(other))
        assert storage.path_for("documents/2026/10/1_ab12cd34.pdf").is_relative_to((tmp_path / "other").resolve())

    def test_a_symlink_out_of_the_tenant_is_refused(self, app, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, root / "documents", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks sind hier nicht erlaubt")
        assert storage.path_for("documents/2026/10/1_ab12cd34.pdf") is None


class TestPut:
    def test_writes_reads_and_deletes(self, app, root):
        key = "documents/2026/10/1_ab12cd34.pdf"
        storage.put(key, b"%PDF-1.7 inhalt")
        assert storage.exists(key) and storage.read(key) == b"%PDF-1.7 inhalt"
        assert storage.size_of(key) == len(b"%PDF-1.7 inhalt")
        assert storage.delete(key) and not storage.exists(key)
        assert storage.delete(key)                       # schon weg ist kein Fehler

    def test_never_overwrites(self, app, root):
        key = "documents/2026/10/1_ab12cd34.pdf"
        storage.put(key, b"erste Fassung")
        with pytest.raises(storage.StorageError, match="schon eine Datei"):
            storage.put(key, b"zweite Fassung")
        assert storage.read(key) == b"erste Fassung"

    def test_leaves_no_temp_file_behind(self, app, root):
        key = "documents/2026/10/1_ab12cd34.pdf"
        storage.put(key, b"x")
        assert [p.name for p in (root / "documents" / "2026" / "10").iterdir()] == ["1_ab12cd34.pdf"]

    def test_an_invalid_key_is_refused(self, app, root):
        with pytest.raises(storage.StorageError, match="Ungültiger"):
            storage.put("../boese.pdf", b"x")
        assert not (root.parent / "boese.pdf").exists()

    def test_reading_a_missing_file(self, app, root):
        assert storage.read("documents/2026/10/7_ab12cd34.pdf") is None
        assert storage.size_of("documents/2026/10/7_ab12cd34.pdf") == 0
        assert storage.send("documents/2026/10/7_ab12cd34.pdf", download_name="x", mimetype="application/pdf") is None

    def test_free_bytes(self, app, root):
        assert storage.free_bytes() > 0                  # auch wenn der Ordner noch nicht existiert
