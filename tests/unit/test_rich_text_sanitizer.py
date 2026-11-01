"""Sanitizer des Kontakttext-Editors (Rechnungskopf): Schriftgroessen-Spans."""
from app.settings_service import sanitize_rich_text


def test_keeps_font_size_span_in_editor_notation():
    # So serialisiert der Browser den Span, den der Editor erzeugt.
    raw = 'WG Alm<br><span style="font-size: 14pt;">Hauptstraße 1</span>'
    assert sanitize_rich_text(raw) == 'WG Alm<br><span style="font-size:14pt">Hauptstraße 1</span>'


def test_nested_sizes_keep_inner_size():
    raw = '<span style="font-size:12pt">a <span style="font-size:18pt">b</span> c</span>'
    assert sanitize_rich_text(raw) == (
        '<span style="font-size:12pt">a <span style="font-size:18pt">b</span> c</span>')


def test_dropped_span_does_not_close_outer_size():
    # Ein Span ohne gueltige Groesse (eingefuegter Fremdtext, Wert ausserhalb
    # der Grenzen) faellt weg — sein </span> darf den aeusseren nicht schliessen.
    raw = ('<span style="font-size:16pt">a <span style="color:red">b</span> c</span> d'
           '<span style="font-size:99pt">e</span>')
    assert sanitize_rich_text(raw) == '<span style="font-size:16pt">a b c</span> de'


def test_unbalanced_end_tags_are_ignored():
    assert sanitize_rich_text('a</span></b> b') == 'a b'
