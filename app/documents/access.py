"""Wer sieht welche Dokumente? — die eine Stelle fuer die Sichtbarkeit im Dokumentenregister.

Der Bereich eines ``Document`` (``area``) haengt an genau einem Recht; ``records`` (Schriftfuehrung)
gibt es nur im Mandant-Typ Wassergenossenschaft. Administratoren haben implizit jedes Recht
(``Role.has_permission``). Wer ein Dokument ausserhalb seiner Bereiche anfragt, bekommt ein 404 —
nicht 403, damit die Existenz nicht verraten wird.

Die bereichseigenen Seiten (Belege unter ``accounting``, Rechnungen, Schriftfuehrung) pruefen weiter
ihr eigenes Recht; diese Helfer brauchen alle Wege, die **bereichsuebergreifend** Dokumente zeigen
oder ausliefern (die Dokumentenuebersicht der SaaS).
"""
from app.auth.permissions import (
    PERM_BUCHHALTUNG, PERM_MAHNWESEN, PERM_RECHNUNGEN, PERM_SCHRIFTFUEHRUNG,
)
from app.models import Document

AREA_PERMISSIONS = {
    Document.AREA_ACCOUNTING: PERM_BUCHHALTUNG,
    Document.AREA_INVOICES: PERM_RECHNUNGEN,
    Document.AREA_DUNNING: PERM_MAHNWESEN,
    Document.AREA_RECORDS: PERM_SCHRIFTFUEHRUNG,
}
AREAS = tuple(AREA_PERMISSIONS)


def area_enabled(area):
    """Gibt es den Bereich in diesem Mandanten? (Schriftfuehrung nur fuer Wassergenossenschaften.)"""
    if area == Document.AREA_RECORDS:
        from app.settings_service import is_wassergenossenschaft
        return is_wassergenossenschaft()
    return area in AREA_PERMISSIONS


def visible_areas(user):
    """Die Bereiche, deren Dokumente ``user`` sehen darf — in fester Reihenfolge."""
    if user is None or not getattr(user, "is_authenticated", False):
        return []
    return [area for area in AREAS if area_enabled(area) and user.has_permission(AREA_PERMISSIONS[area])]


def can_view(user, doc):
    return doc is not None and doc.area in visible_areas(user)


def area_filter(user):
    """SQL-Ausdruck fuer ``Document.query.filter(...)``: nur Dokumente sichtbarer Bereiche."""
    areas = visible_areas(user)
    return Document.area.in_(areas) if areas else Document.id.is_(None)
