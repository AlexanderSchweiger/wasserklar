"""Integration-Tests fuer die Notiz-Service-Schicht (app/notes/services.py).

Deckt die N+1-freien Lade-Helfer (pinned-Filter, leere ID-Liste), die
Scope-/Farb-/Existenz-Validierung und die Entity-Aufloesung fuer die Uebersicht
ab. Reine DB-Tests (kein HTTP).
"""
from app.extensions import db
from app.models import Customer, Property, Note
from app.notes import services as svc


def _note(entity_type, entity_id, body="x", color="yellow", pinned=True):
    n = Note(entity_type=entity_type, entity_id=entity_id, body=body,
             color=color, pinned=pinned)
    db.session.add(n)
    db.session.commit()
    return n


class TestValidation:
    def test_is_valid_scope(self):
        assert svc.is_valid_scope("tenant")
        assert svc.is_valid_scope("customer")
        assert svc.is_valid_scope("booking")
        assert not svc.is_valid_scope("bogus")
        assert not svc.is_valid_scope("")

    def test_normalize_color_allowlist(self):
        assert svc.normalize_color("pink") == "pink"
        assert svc.normalize_color("bogus") == svc.DEFAULT_COLOR
        assert svc.normalize_color(None) == svc.DEFAULT_COLOR

    def test_entity_exists(self):
        c = Customer(name="Kunde A")
        db.session.add(c)
        db.session.commit()
        assert svc.entity_exists("tenant", None) is True
        assert svc.entity_exists("customer", c.id) is True
        assert svc.entity_exists("customer", 999999) is False
        assert svc.entity_exists("bogus", c.id) is False


class TestLoaders:
    def test_notes_for_filters_pinned_and_scope(self):
        c = Customer(name="Kunde B")
        db.session.add(c)
        db.session.commit()
        _note("customer", c.id, body="pinned", pinned=True)
        _note("customer", c.id, body="unpinned", pinned=False)
        _note("tenant", None, body="tenant note", pinned=True)

        pinned = svc.notes_for("customer", c.id)
        assert [n.body for n in pinned] == ["pinned"]
        # pinned_only=False zieht auch geloeste Notizen
        alln = svc.notes_for("customer", c.id, pinned_only=False)
        assert {n.body for n in alln} == {"pinned", "unpinned"}
        # Tenant-Scope ignoriert entity_id
        assert [n.body for n in svc.tenant_notes()] == ["tenant note"]

    def test_notes_for_entity_without_id_returns_empty(self):
        assert svc.notes_for("customer", None) == []

    def test_notes_by_entity_for_batches_and_skips_empty(self):
        c1 = Customer(name="C1")
        c2 = Customer(name="C2")
        db.session.add_all([c1, c2])
        db.session.commit()
        _note("customer", c1.id, body="a", pinned=True)
        _note("customer", c1.id, body="b", pinned=True)
        _note("customer", c2.id, body="c", pinned=True)
        _note("customer", c2.id, body="hidden", pinned=False)

        m = svc.notes_by_entity_for("customer", [c1.id, c2.id])
        assert len(m[c1.id]) == 2
        assert [n.body for n in m[c2.id]] == ["c"]   # unpinned ausgeschlossen
        # leere ID-Liste -> {} (kein IN ()-Sonderfall)
        assert svc.notes_by_entity_for("customer", []) == {}


class TestEntityDisplay:
    def test_display_for_entity_and_tenant(self):
        c = Customer(name="Frau Muster")
        p = Property(object_type="Haus", object_number="OBJ-9")
        db.session.add_all([c, p])
        db.session.commit()
        nc = _note("customer", c.id)
        npr = _note("property", p.id)
        nt = _note("tenant", None)

        dc = svc.entity_display(nc)
        assert dc["label"] == "Kontakt"
        assert dc["name"] == "Frau Muster"
        assert dc["endpoint"] == "customers.detail"
        assert dc["id_arg"] == "customer_id"

        dp = svc.entity_display(npr)
        assert dp["name"] == "OBJ-9"

        dt = svc.entity_display(nt)
        assert dt["name"] is None
        assert dt["endpoint"] is None
