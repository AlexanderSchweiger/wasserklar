"""Registry-Vollstaendigkeit + Export/Import-Roundtrip fuer data_transfer.

Der Guard-Test in ``TestRegistryCompleteness`` faengt genau die Luecke ab, die
real passiert ist: ein neues Model, das weder in der Registry (CATEGORIES/
INSERT_ORDER) noch in ``EXCLUDED_TABLES`` steht, faellt sonst *lautlos* aus dem
Voll-Backup. Ab jetzt schlaegt in dem Fall dieser Test fehl statt der Nutzer
Daten zu verlieren.

Die Roundtrip-Tests verifizieren, dass der Serializer die zuvor vergessenen
Tabellen (Rollen/Rechte, Bankauszug, Schriftfuehrung, DSGVO-Mail) UND die
Geo-Koordinaten (``Property.lat/lng``, ``NetworkFeature.geometry``) unveraendert
durch Export → Import (replace) transportiert.
"""

import io

import pytest

from app.extensions import db
from app.data_transfer.registry import (
    CATEGORIES, INSERT_ORDER, EXCLUDED_TABLES, NATURAL_KEYS, FOREIGN_KEYS,
    YEAR_FILTERS, NULL_ON_IMPORT_COLS, DEFERRED_FK_UPDATES,
)
from app.data_transfer.services import (
    export_to_zip, extract_to_temp, import_from_zip,
)


def _registered_models():
    return {m for models in CATEGORIES.values() for m in models}


class TestRegistryCompleteness:
    def test_every_model_table_is_registered_or_excluded(self, app):
        """Jede physische Tabelle muss bewusst behandelt sein: exportiert
        (in der Registry) oder ausgeschlossen (EXCLUDED_TABLES). Ein neues
        Model, das in keiner der beiden Mengen liegt, ist der Bug."""
        registered = {m.__tablename__ for m in INSERT_ORDER}
        covered = registered | set(EXCLUDED_TABLES)
        all_tables = set(db.metadata.tables.keys())
        uncovered = all_tables - covered
        assert not uncovered, (
            "Diese Tabellen sind weder in der data_transfer-Registry noch in "
            f"EXCLUDED_TABLES aufgefuehrt: {sorted(uncovered)}. Neues Model? In "
            "registry.py eintragen (CATEGORIES + INSERT_ORDER + ggf. "
            "FOREIGN_KEYS/NATURAL_KEYS/YEAR_FILTERS) ODER bewusst mit Kommentar "
            "in EXCLUDED_TABLES aufnehmen."
        )

    def test_insert_order_matches_categories(self, app):
        """INSERT_ORDER und die Vereinigung aller CATEGORIES muessen exakt
        dieselbe Modell-Menge sein — sonst wird ein Model entweder nie
        exportiert (fehlt in CATEGORIES) oder nie inserted (fehlt in ORDER)."""
        cat_models = _registered_models()
        order_models = set(INSERT_ORDER)
        only_cat = [m.__name__ for m in cat_models - order_models]
        only_order = [m.__name__ for m in order_models - cat_models]
        assert cat_models == order_models, (
            f"Nur in CATEGORIES: {only_cat}; nur in INSERT_ORDER: {only_order}."
        )

    def test_each_model_in_exactly_one_category(self, app):
        seen = {}
        for cat, models in CATEGORIES.items():
            for m in models:
                seen.setdefault(m, []).append(cat)
        dupes = {m.__name__: cats for m, cats in seen.items() if len(cats) > 1}
        assert not dupes, f"Model in mehreren Kategorien: {dupes}"

    def test_registry_config_only_references_registered_models(self, app):
        """NATURAL_KEYS/FOREIGN_KEYS/... duerfen nur registrierte Models
        referenzieren — eine verwaiste Config-Zeile (Model spaeter aus ORDER
        entfernt) waere sonst wirkungslos."""
        registered = set(INSERT_ORDER)
        for name, mapping in (
            ("NATURAL_KEYS", NATURAL_KEYS),
            ("FOREIGN_KEYS", FOREIGN_KEYS),
            ("YEAR_FILTERS", YEAR_FILTERS),
            ("NULL_ON_IMPORT_COLS", NULL_ON_IMPORT_COLS),
            ("DEFERRED_FK_UPDATES", DEFERRED_FK_UPDATES),
        ):
            stray = {m.__name__ for m in mapping if m not in registered}
            assert not stray, f"{name} referenziert nicht-registrierte Models: {stray}"

    def test_fk_targets_are_registered(self, app):
        """Jedes FK-Ziel muss selbst exportiert werden, sonst schlaegt das
        Merge-Remapping fehl."""
        registered = set(INSERT_ORDER)
        for model, fkmap in FOREIGN_KEYS.items():
            for col, target in fkmap.items():
                assert target in registered, (
                    f"FK-Ziel {target.__name__} von {model.__name__}.{col} ist "
                    "nicht in der Registry."
                )


def _roundtrip_replace(tmp_path):
    """Voll-Export → Extract → Import(replace). Gibt das stats-Dict zurueck.

    Die DB-Rows sind vor und nach identisch (replace ersetzt sie durch die
    exportierten) — der Test prueft damit die Serialize→JSON→Deserialize-Treue.
    """
    selection = {"stammdaten": True, "buchungen": True, "mahnwesen": True,
                 "einstellungen": True, "include_pdfs": False, "years": []}
    buf = io.BytesIO()
    export_to_zip(selection, buf, exported_by="test")
    buf.seek(0)
    extract_dir, manifest = extract_to_temp(buf, str(tmp_path))
    stats = import_from_zip(extract_dir, manifest, mode="replace",
                            instance_path=str(tmp_path))
    db.session.remove()   # frische Session — Identity-Map nach Raw-DELETE leeren
    return stats


class TestGeoRoundtrip:
    def test_property_and_feature_coordinates_survive(self, app, tmp_path):
        from app.models import Property, NetworkPlan, NetworkFeature
        db.session.add(Property(object_type="Haus", strasse="Quellweg",
                                hausnummer="1", lat=47.812345, lng=13.045678))
        plan = NetworkPlan(name="Hauptplan")
        db.session.add(plan)
        db.session.flush()
        db.session.add(NetworkFeature(
            plan_id=plan.id, geometry_kind="point", feature_type="hydrant",
            geometry='{"type":"Point","coordinates":[13.045678,47.812345]}',
            lat=47.812345, lng=13.045678,
        ))
        db.session.commit()

        _roundtrip_replace(tmp_path)

        prop = Property.query.filter_by(strasse="Quellweg").one()
        assert prop.lat == pytest.approx(47.812345)
        assert prop.lng == pytest.approx(13.045678)
        feat = NetworkFeature.query.filter_by(feature_type="hydrant").one()
        assert feat.lat == pytest.approx(47.812345)
        assert feat.lng == pytest.approx(13.045678)
        assert '"Point"' in feat.geometry


class TestNewTablesRoundtrip:
    def test_roles_meetings_bank_suppressions_survive(self, app, tmp_path):
        from app.models import (
            Role, RolePermission, EmailSuppression, Meeting, MeetingAgendaItem,
            RealAccount, BankStatement, BankStatementLine,
        )
        # Rollen/Rechte (einstellungen) — Composite-PK RolePermission.
        role = Role(name="Kassier", description="Buchhaltung")
        db.session.add(role)
        db.session.flush()
        db.session.add(RolePermission(role_id=role.id, permission_key="buchhaltung"))

        # DSGVO-Sperrliste (einstellungen, FK-frei).
        db.session.add(EmailSuppression(
            email="bounce@example.org",
            reason=EmailSuppression.REASON_HARD_BOUNCE))

        # Schriftfuehrung (stammdaten) — Meeting + Kind.
        meeting = Meeting(meeting_type=Meeting.TYPE_BOARD, title="Vorstand Q3")
        db.session.add(meeting)
        db.session.flush()
        db.session.add(MeetingAgendaItem(meeting_id=meeting.id, position=1,
                                         title="Kassabericht"))

        # Bankauszug (buchungen) — Statement + Zeile mit Buchungsdatum.
        ra = RealAccount(name="Girokonto")
        db.session.add(ra)
        db.session.flush()
        stmt = BankStatement(format=BankStatement.FORMAT_CAMT053,
                             filename="k.xml", file_hash="abc123",
                             real_account_id=ra.id)
        db.session.add(stmt)
        db.session.flush()
        from datetime import date
        db.session.add(BankStatementLine(
            statement_id=stmt.id, line_index=0, booking_date=date(2025, 3, 1),
            amount=100, purpose="Test"))
        db.session.commit()

        _roundtrip_replace(tmp_path)

        assert Role.query.filter_by(name="Kassier").count() == 1
        assert RolePermission.query.filter_by(permission_key="buchhaltung").count() == 1
        assert EmailSuppression.query.filter_by(email="bounce@example.org").count() == 1
        assert BankStatement.query.filter_by(file_hash="abc123").count() == 1
        assert BankStatementLine.query.filter_by(purpose="Test").count() == 1
        m = Meeting.query.filter_by(title="Vorstand Q3").one()
        assert [ai.title for ai in m.agenda_items] == ["Kassabericht"]
