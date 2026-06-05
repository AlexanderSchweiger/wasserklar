"""Tests fuer den WLK-Shapefile-Import (Technik-Modul).

Drei Ebenen:
- ``TestParsers`` / ``TestClassify``: reine Mapping-Funktionen (ohne pyshp/pyproj).
- ``TestConverter``: ``wlk_import.convert_zip`` auf einem in-memory erzeugten
  Shapefile-ZIP in echten Gauss-Krueger-M31-Koordinaten (pyshp + pyproj noetig).
- ``TestHttpShapefileImport``: Upload -> Vorschau -> Commit ueber die Routen,
  inkl. Reprojektion und erzeugtem MaintenanceLog.
"""
import io
import zipfile

import pytest

from app.extensions import db
from app.models import NetworkFeature, MaintenanceLog, User
from app.network import wlk_import as wlk
from tests.conftest import _ensure_role

# Reales GK M31 (EPSG:31255, MGI/Bessel) — identisch zu den WASKAT-Shapes.
GK31_PRJ = (
    'PROJCS["GK_31",GEOGCS["GCS_MGI",DATUM["D_MGI",SPHEROID["Bessel_1841",'
    '6377397.155,299.1528128]],PRIMEM["Greenwich",0.0],UNIT["Degree",'
    '0.0174532925199433]],PROJECTION["Transverse_Mercator"],PARAMETER['
    '"False_Easting",0.0],PARAMETER["False_Northing",-5000000.0],PARAMETER['
    '"Central_Meridian",13.33333333333333],PARAMETER["Scale_Factor",1.0],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)

# Punkt im Treffling-Netz (entspricht ~46.843 N, 13.526 E).
_PX, _PY = 14751.0, 189314.0


def _wlk_zip_bytes():
    """Baut ein ZIP mit zwei Shapefiles (Leitung-Polyline + Einbau-Multipoint)
    in GK M31. Ueberspringt den Test, wenn pyshp/pyproj fehlen."""
    shapefile = pytest.importorskip("shapefile")
    pytest.importorskip("pyproj")

    def _layer(shape_type, fields, geom_writer, record):
        shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
        w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type)
        for name, typ, size in fields:
            w.field(name, typ, size=size)
        geom_writer(w)
        w.record(**record)
        w.close()
        return shp.getvalue(), shx.getvalue(), dbf.getvalue()

    lshp, lshx, ldbf = _layer(
        shapefile.POLYLINE,
        [("L_ART", "C", 2), ("L_MAT", "C", 3), ("L_DN", "N", 5),
         ("L_INBE", "N", 5), ("L_BEZ", "C", 36)],
        lambda w: w.line([[(_PX, _PY), (_PX + 9, _PY + 6)]]),
        {"L_ART": "VL", "L_MAT": "PE", "L_DN": 80, "L_INBE": 1975, "L_BEZ": "L1"},
    )
    # POINT (nicht MULTIPOINT): pyshp 3.0.9 hat einen Writer-Bug bei multipoint.
    # Das Lesen von MULTIPOINT ist ueber die echten WASKAT-Daten abgedeckt; der
    # Konverter behandelt POINT/MULTIPOINT beim Lesen ohnehin identisch.
    eshp, eshx, edbf = _layer(
        shapefile.POINT,
        [("E_ART", "C", 4), ("E_AANM", "C", 100), ("E_INBE", "N", 5),
         ("E_LAG_ERM", "C", 3), ("E_WA_INT", "C", 30), ("E_LWA_DAT", "C", 10)],
        lambda w: w.point(_PX, _PY),
        {"E_ART": "ABSP", "E_AANM": "Schieber", "E_INBE": 1989,
         "E_LAG_ERM": "V", "E_WA_INT": "alle 2 Jahre", "E_LWA_DAT": "2012-00-00"},
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("leitung.shp", lshp)
        zf.writestr("leitung.shx", lshx)
        zf.writestr("leitung.dbf", ldbf)
        zf.writestr("leitung.prj", GK31_PRJ)
        zf.writestr("einbau.shp", eshp)
        zf.writestr("einbau.shx", eshx)
        zf.writestr("einbau.dbf", edbf)
        zf.writestr("einbau.prj", GK31_PRJ)
    return buf.getvalue()


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": password})


@pytest.fixture
def active_plan(app):
    """Aktiver Ziel-Plan fuer den Import (Features brauchen ``plan_id``)."""
    from app.models import NetworkPlan
    p = NetworkPlan(name="Testplan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
    db.session.add(p)
    db.session.commit()
    return p


# ---------------------------------------------------------------------------
# Reine Mapping-Funktionen (keine GIS-Libs noetig)
# ---------------------------------------------------------------------------

class TestParsers:
    @pytest.mark.parametrize("text,expected", [
        ("alle 2 Jahre", 24),
        ("alle 6 Monate", 6),
        ("jährlich", 12),
        ("halbjährlich", 6),
        ("vierteljährlich", 3),
        ("monatlich", 1),
        ("", None),
        ("Sichtprüfung nach Bedarf", None),
    ])
    def test_interval(self, text, expected):
        assert wlk.parse_interval_months(text) == expected

    @pytest.mark.parametrize("text,expected", [
        ("2012-00-00", "2012-01-01"),   # Tag/Monat 0 -> 1 geklemmt
        ("2012-05-25", "2012-05-25"),
        ("25.05.1951", "1951-05-25"),
        ("1975", "1975-01-01"),
        ("", None),
        ("kein Datum", None),
    ])
    def test_date(self, text, expected):
        assert wlk.parse_wlk_date(text) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("PE", "PE"), ("ggg", "Duktilguss (GGG)"), ("GUSS", "Guss (GG)"),
        ("ST", "Stahl"), ("Sonderzeug", "Sonderzeug"), ("", None),
    ])
    def test_material(self, raw, expected):
        assert wlk.normalize_material(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("V", "exakt"), ("D", "gut"), ("S", "geschaetzt"), ("?", None), ("", None),
    ])
    def test_accuracy(self, raw, expected):
        assert wlk.map_accuracy(raw) == expected


class TestClassify:
    @pytest.mark.parametrize("code,aanm,expected", [
        ("VL", "", "versorgungsleitung"),
        ("AL", "", "hausanschlussleitung"),
        ("ZL", "", "zubringer"),
        ("HL", "", "hauptleitung"),
        ("XX", "", "sonstige_leitung"),       # unbekannt -> Default
    ])
    def test_line(self, code, aanm, expected):
        assert wlk.classify_line(code, aanm) == expected

    @pytest.mark.parametrize("layer,code,aanm,expected", [
        ("einbau", "ABSP", "", "schieber"),
        ("einbau", "HYO", "Oberflurhydrant", "hydrant"),
        ("einbau", "ANSO", "Anbohrschelle", "anbohrschelle"),
        ("einbau", "SO", "HA", "hausanschluss"),       # Code unbekannt -> AANM "HA"
        ("einbau", "SO", "Leitungsende", "leitungsende"),
        ("einbau", "SO", "Material - Dimensionswechsel", "materialwechsel"),
        ("einbau", "SO", "Dimensions- u./od. Materialwechsel", "materialwechsel"),
        ("einbau", "SO", "Froschmaul", "sonstiges"),    # weiterhin Layer-Default
        ("speicher", "HB", "", "behaelter"),
        ("sonstiges", "QUF", "Quellfassung", "quelle"),
        ("sonstiges", "ABAS", "Schieberschacht", "verteiler"),
    ])
    def test_point(self, layer, code, aanm, expected):
        assert wlk.classify_point(layer, code, aanm) == expected


# ---------------------------------------------------------------------------
# Konverter (pyshp + pyproj)
# ---------------------------------------------------------------------------

class TestConverter:
    def test_reproject_and_map(self):
        res = wlk.convert_zip(io.BytesIO(_wlk_zip_bytes()))
        feats = res["features"]
        assert res["stats"]["total"] == 2
        assert "GK_31" in res["stats"]["crs_names"]
        assert res["stats"]["maintenance_count"] == 1

        line = next(f for f in feats if f["geometry"]["type"] == "LineString")
        lp = line["properties"]
        assert lp["feature_type"] == "versorgungsleitung"
        assert lp["material"] == "PE"
        assert lp["dimension_dn"] == 80
        assert lp["year_built"] == 1975

        point = next(f for f in feats if f["geometry"]["type"] == "Point")
        pp = point["properties"]
        assert pp["feature_type"] == "schieber"
        assert pp["accuracy"] == "exakt"
        assert pp["year_built"] == 1989
        assert pp["maintenance_interval_months"] == 24
        assert pp["maintenance_last_date"] == "2012-01-01"
        assert pp["maintenance_kind"] == "funktionspruefung"

        # Reprojektion: Koordinaten muessen in Kaernten/Treffling landen.
        lng, lat = point["geometry"]["coordinates"]
        assert 46.8 < lat < 46.9
        assert 13.5 < lng < 13.6

    def test_not_a_zip_raises(self):
        pytest.importorskip("shapefile")
        pytest.importorskip("pyproj")
        with pytest.raises(wlk.WlkImportError):
            wlk.convert_zip(io.BytesIO(b"das ist kein ZIP"))


# ---------------------------------------------------------------------------
# HTTP-Flow: Upload -> Vorschau -> Commit
# ---------------------------------------------------------------------------

class TestHttpShapefileImport:
    def test_preview_then_commit(self, client, admin, active_plan):
        zip_bytes = _wlk_zip_bytes()
        _login(client)

        # Schritt 1: Upload -> Vorschau (noch nichts in der DB).
        r = client.post(
            "/network/import/shapefile",
            data={"file": (io.BytesIO(zip_bytes), "kataster.zip")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Vorschau" in body
        assert "GK_31" in body
        assert NetworkFeature.query.count() == 0

        # Schritt 2: Commit (Token sitzt in der Session).
        r2 = client.post("/network/import/shapefile/commit", follow_redirects=False)
        assert r2.status_code == 302
        assert NetworkFeature.query.count() == 2

        # Ein Schieber-Punkt mit reprojizierten Koordinaten + Wartungslog.
        schieber = NetworkFeature.query.filter_by(feature_type="schieber").one()
        assert 46.8 < schieber.lat < 46.9
        assert 13.5 < schieber.lng < 13.6
        log = MaintenanceLog.query.filter_by(feature_id=schieber.id).one()
        assert log.interval_months == 24
        assert log.next_due is not None

    def test_commit_without_session_redirects(self, client, admin, active_plan):
        _login(client)
        r = client.post("/network/import/shapefile/commit", follow_redirects=False)
        assert r.status_code == 302
        assert NetworkFeature.query.count() == 0
