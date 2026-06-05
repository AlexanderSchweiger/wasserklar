"""Import des oesterreichischen **Wasserleitungskatasters (WLK)** als Shapefiles.

Ein WLK-Export (vom Planer/Vermesser) besteht aus mehreren ESRI-Shapefiles —
typischerweise je eines fuer Leitungen (``*_LEITUNG``), Einbauten/Armaturen
(``*_EINBAU``), Speicher/Behaelter (``*_SPEICHER``) und Sonstiges/Schaechte
(``*_SONST``). Geometrie liegt in einem oesterreichischen Gauss-Krueger-System
(MGI/Bessel, z.B. GK M31 = EPSG:31255), die Sachdaten in der ``.dbf`` mit den
WLK-Standard-Feldern (``L_ART``, ``E_ART``, ``L_MAT``, ``L_DN`` ...).

Dieses Modul ist bewusst **frei von Flask-/DB-Abhaengigkeiten**: es liest ein
hochgeladenes ZIP, reprojiziert jede Geometrie nach WGS84 (lng/lat — das, was
der GeoJSON-Import und Leaflet erwarten) und liefert eine Liste angereicherter
**GeoJSON-Features** zurueck. Die DB-seitige Erzeugung der ``NetworkFeature``-
und ``MaintenanceLog``-Objekte uebernimmt ``services.build_feature_from_geojson``
aus genau diesen Properties. So bleibt der Konverter rein und unit-testbar.

``pyshp`` und ``pyproj`` werden **lazy** importiert (siehe ``_load_libs``) und
mit einer freundlichen Meldung quittiert, falls sie fehlen — analog dazu, wie
das OSS-App WeasyPrint behandelt. Damit laedt das Modul (und das Technik-
Blueprint) auch in einer Dev-Umgebung ohne die GIS-Libs.

Das **Code->Typ-Mapping** weiter unten ist das Herzstueck der Generik: bekannte
WLK-Codes werden auf die Vokabular-Typen des Leitungsplans abgebildet, unbekannte
fallen pro Layer auf einen sinnvollen Default zurueck — und die Roh-Codes/
Anmerkungen landen immer in ``notes``, sodass nie Information verloren geht und
der Bearbeiter auf der Karte nachklassifizieren kann.
"""

import os
import re
import tempfile
import zipfile
from datetime import date

from app.network import vocab


class WlkImportError(Exception):
    """Benutzerfreundlicher Fehler (Meldung wird als Flash gezeigt)."""


# ---------------------------------------------------------------------------
# Lazy-Imports (pyshp / pyproj) — wie WeasyPrint optional
# ---------------------------------------------------------------------------

def _load_libs():
    """Importiert ``shapefile`` (pyshp) und ``pyproj`` erst bei Bedarf.

    Wirft ``WlkImportError`` mit Installationshinweis, falls eine Lib fehlt —
    damit crasht das Blueprint-Laden (das dieses Modul nur importiert, nicht
    benutzt) nie an einer fehlenden GIS-Abhaengigkeit.
    """
    try:
        import shapefile  # pyshp
        import pyproj
    except ImportError as exc:  # pragma: no cover - haengt von der Umgebung ab
        raise WlkImportError(
            "Für den Shapefile-Import werden die Pakete 'pyshp' und 'pyproj' "
            "benötigt. Bitte installieren: pip install pyshp pyproj"
        ) from exc
    return shapefile, pyproj


def dependencies_available():
    """True, wenn pyshp+pyproj importierbar sind (fuer UI-Hinweise)."""
    try:
        _load_libs()
        return True
    except WlkImportError:
        return False


# ---------------------------------------------------------------------------
# Geometrie-Klassifikation (pyshp-Shape-Type -> Punkt/Linie)
# ---------------------------------------------------------------------------

# pyshp-Konstanten: POINT=1, POLYLINE=3, POLYGON=5, MULTIPOINT=8,
# POINTZ=11, POLYLINEZ=13, MULTIPOINTZ=18, POINTM=21, POLYLINEM=23, MULTIPOINTM=28
_POINT_SHAPE_TYPES = {1, 8, 11, 18, 21, 28}
_LINE_SHAPE_TYPES = {3, 13, 23}


def _geometry_kind(shape_type):
    if shape_type in _POINT_SHAPE_TYPES:
        return "point"
    if shape_type in _LINE_SHAPE_TYPES:
        return "line"
    return None  # Polygon/Multipatch -> nicht unterstuetzt


# ---------------------------------------------------------------------------
# WLK-Code -> Vokabular-Typ  (das generische Herzstueck)
# ---------------------------------------------------------------------------

# Strangart (L_ART) -> Linien-Typ
_LINE_TYPE_MAP = {
    "VL": "versorgungsleitung",    # Versorgungsleitung
    "HL": "hauptleitung",          # Hauptleitung
    "RL": "hauptleitung",          # Ringleitung -> Hauptleitung (Ring ist Bauform, kein eigener Typ mehr)
    "ZL": "zubringer",             # Zubringerleitung
    "TL": "zubringer",             # Transportleitung
    "FL": "zubringer",             # Foerderleitung
    "AL": "hausanschlussleitung",  # Anschlussleitung
    "HAL": "hausanschlussleitung",
}

# Einbau-Art (E_ART) -> Punkt-Typ
_EINBAU_TYPE_MAP = {
    "ABSP": "schieber",   # Absperrschieber
    "ABS": "schieber",
    "SCH": "schieber",    # Schieber
    "SCHI": "schieber",
    "AV": "schieber",     # Absperrventil
    "HYD": "hydrant",     # Hydrant
    "HYO": "hydrant",     # Oberflurhydrant
    "HYU": "hydrant",     # Unterflurhydrant
    "OH": "hydrant",      # Oberflurhydrant
    "UH": "hydrant",      # Unterflurhydrant
    "LH": "hydrant",      # Loeschhydrant
    "ANSO": "anbohrschelle",  # Anbohrschelle
    "ANB": "anbohrschelle",   # Anbohrung
    "HA": "hausanschluss",    # Hausanschluss
    "QU": "quelle",       # Quelle
    "QF": "quelle",       # Quellfassung
    "PW": "pumpe",        # Pumpwerk
    "DE": "pumpe",        # Druckerhoehung
    "PN": "probenahme",   # Probenahmestelle
    "PRO": "probenahme",
    "EL": "entlueftung",  # Entlüftungsventil
    "ENL": "entlueftung", # Entlüftungsventil (Variante)
    "LV": "entlueftung",  # Luftventil
    "BEL": "entlueftung", # Belüftungs-/Entlüftungsventil
    "AUS": "auslauf",     # Auslauf
    "ASL": "auslauf",     # Auslass
    "ENT": "auslauf",     # Entleerung
    # "SO" (Sonstiges) bewusst NICHT gemappt: erst die Anmerkung pruefen
    # ("HA" -> Hausanschluss), sonst Layer-Default "sonstiges".
}

# Speicher-Art (SP_ART) -> Punkt-Typ
_SPEICHER_TYPE_MAP = {
    "HB": "behaelter",    # Hochbehaelter
    "TB": "behaelter",    # Tiefbehaelter
    "WB": "behaelter",    # Wasserbehaelter
    "BH": "behaelter",
    "PW": "pumpe",        # Pumpwerk
    "QU": "quelle",
    "QF": "quelle",
}

# Sonstiges-Art (SO_ART) -> Punkt-Typ
_SONST_TYPE_MAP = {
    "ABAS": "verteiler",  # Absperr-/Armaturenschacht
    "SCH": "verteiler",   # Schacht
    "SS": "verteiler",    # Schieberschacht
    "WVS": "verteiler",
    "ELS": "verteiler",   # Entleerungsschacht
    "QU": "quelle",
    "QF": "quelle",
    "QUF": "quelle",      # Quellfassung
    "QSS": "quelle",      # Quellsammelschacht
    "PN": "probenahme",
    "PRO": "probenahme",
    "PW": "pumpe",
    "AUS": "auslauf",     # Auslauf
    "ASL": "auslauf",     # Auslass
    "ENT": "auslauf",     # Entleerung
    "EL": "entlueftung",  # Entlüftungsventil
    "LV": "entlueftung",  # Luftventil
}

# Exakter (case-insensitiver) Treffer der ganzen Anmerkung -> Typ. Fuer kurze
# Abkuerzungen, die als Substring zu gefaehrlich waeren (z.B. "HA" steckt in
# "scHAcht"). Wird VOR den Substring-Stichworten geprueft.
_AANM_EXACT = {
    "ha": "hausanschluss",
    "ha-schieber": "schieber",
}

# Fallback-Klassifikation ueber Stichworte in der Anmerkung (``*_AANM``),
# wenn der Code unbekannt ist. Reihenfolge = Prioritaet.
_AANM_KEYWORDS = [
    ("hydrant", "hydrant"),
    ("schieber", "schieber"),
    ("absperr", "schieber"),
    ("ventil", "schieber"),
    ("quell", "quelle"),
    ("behälter", "behaelter"),
    ("behaelter", "behaelter"),
    ("speicher", "behaelter"),
    ("pumpe", "pumpe"),
    ("druckerhöh", "pumpe"),
    ("probe", "probenahme"),
    ("leitungsende", "leitungsende"),
    ("wechsel", "materialwechsel"),   # Material- und/oder Dimensionswechsel
    ("anbohr", "anbohrschelle"),
    ("hausanschl", "hausanschluss"),
    ("entlüft", "entlueftung"),
    ("belüft", "entlueftung"),
    ("luftventil", "entlueftung"),
    ("auslauf", "auslauf"),
    ("auslass", "auslauf"),
    ("entleer", "auslauf"),
    ("schacht", "verteiler"),
    ("verteil", "verteiler"),
]

# Default-Punkt-Typ je WLK-Layer, wenn weder Code noch Anmerkung greifen.
# Einbau-Default bewusst "sonstiges" (NICHT "schieber") — unbekannte Einbauten
# sollen nicht faelschlich als Armatur erscheinen.
_POINT_DEFAULT_BY_LAYER = {
    "einbau": "sonstiges",
    "speicher": "behaelter",
    "sonstiges": "verteiler",
}

# Materialkuerzel (L_MAT) -> Vokabular-Vorschlag (Freitext bleibt sonst erhalten)
_MATERIAL_MAP = {
    "PE": "PE", "PEHD": "PE", "HDPE": "PE", "PE-HD": "PE",
    "PVC": "PVC", "PVCU": "PVC", "PVC-U": "PVC",
    "GG": "Guss (GG)", "GUSS": "Guss (GG)", "GG G": "Guss (GG)",
    "GGG": "Duktilguss (GGG)", "DG": "Duktilguss (GGG)", "DGG": "Duktilguss (GGG)",
    "DUKTIL": "Duktilguss (GGG)",
    "ST": "Stahl", "STAHL": "Stahl", "STZ": "Stahl",
    "AZ": "Eternit / AZ", "ETERNIT": "Eternit / AZ", "FZ": "Eternit / AZ",
    "B": "Beton", "BE": "Beton", "BETON": "Beton",
    "CU": "Kupfer", "KUPFER": "Kupfer",
}

# Lagegenauigkeit (``*_LAG_ERM``: V=Vermessung, D=Digitalisierung, S=Schaetzung)
_ACCURACY_MAP = {
    "V": "exakt",       # vermessen / eingemessen
    "E": "exakt",
    "D": "gut",         # digitalisiert
    "G": "gut",
    "S": "geschaetzt",  # geschaetzt
}


def classify_line(code, aanm):
    code = (code or "").strip().upper()
    if code in _LINE_TYPE_MAP:
        return _LINE_TYPE_MAP[code]
    kw = _classify_by_keyword(aanm)
    if kw in vocab.LINE_TYPES:
        return kw
    return "sonstige_leitung"


def classify_point(layer, code, aanm):
    code = (code or "").strip().upper()
    table = {
        "einbau": _EINBAU_TYPE_MAP,
        "speicher": _SPEICHER_TYPE_MAP,
        "sonstiges": _SONST_TYPE_MAP,
    }.get(layer, {})
    if code in table:
        return table[code]
    kw = _classify_by_keyword(aanm)
    if kw in vocab.POINT_TYPES:
        return kw
    return _POINT_DEFAULT_BY_LAYER.get(layer, "sonstiges")


def _classify_by_keyword(text):
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in _AANM_EXACT:           # ganze Anmerkung exakt (z.B. "HA")
        return _AANM_EXACT[t]
    for needle, key in _AANM_KEYWORDS:
        if needle in t:
            return key
    return None


def normalize_material(raw):
    if not raw:
        return None
    key = raw.strip().upper().replace("-", "").replace(" ", "")
    return _MATERIAL_MAP.get(key, raw.strip())


def map_accuracy(raw):
    if not raw:
        return None
    return _ACCURACY_MAP.get(raw.strip().upper(), None)


# ---------------------------------------------------------------------------
# Parser fuer Freitext-Felder
# ---------------------------------------------------------------------------

# Reihenfolge = Prioritaet. Spezifische Muster MUESSEN vor dem generischen
# "jährlich" stehen, da "halbjährlich"/"vierteljährlich" den Substring enthalten.
_INTERVAL_PATTERNS = [
    (re.compile(r"alle\s+(\d+)\s*jahr", re.I), lambda m: int(m.group(1)) * 12),
    (re.compile(r"alle\s+(\d+)\s*monat", re.I), lambda m: int(m.group(1))),
    (re.compile(r"halbj[aä]hrlich|2\s*x\s*j[aä]hrlich", re.I), lambda m: 6),
    (re.compile(r"viertelj[aä]hrlich|quartal", re.I), lambda m: 3),
    (re.compile(r"monatlich", re.I), lambda m: 1),
    (re.compile(r"(j[aä]hrlich|einmal\s+(pro|im)\s+jahr|1\s*x\s*j[aä]hrlich)", re.I), lambda m: 12),
]


def parse_interval_months(text):
    """``"alle 2 Jahre"`` -> 24, ``"jährlich"`` -> 12, ``"halbjährlich"`` -> 6 ..."""
    if not text:
        return None
    t = str(text).strip()
    for pat, fn in _INTERVAL_PATTERNS:
        m = pat.search(t)
        if m:
            try:
                return fn(m)
            except (ValueError, IndexError):
                continue
    return None


def parse_wlk_date(text):
    """Robust gegen WLK-Eigenheiten wie ``"2012-00-00"`` (Tag/Monat 0) und
    ``"25.05.1951"``. Liefert ein ISO-Datum (``YYYY-MM-DD``) oder ``None``.
    Tag/Monat 0 werden auf 1 geklemmt (haeufig steht nur das Jahr fest)."""
    if not text:
        return None
    t = str(text).strip()
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", t)        # YYYY-MM-DD
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\D+(\d{1,2})\D+(\d{4})", t)    # DD.MM.YYYY
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.fullmatch(r"(\d{4})", t)                      # nur Jahr
            if m:
                y, mo, d = int(m.group(1)), 1, 1
            else:
                return None
    mo = min(max(mo, 1), 12)
    d = min(max(d, 1), 28)
    if not (1800 <= y <= 2100):
        return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _to_int(value):
    try:
        if value in (None, ""):
            return None
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# DBF-Feld-Zugriff (generisch ueber WLK-Varianten)
# ---------------------------------------------------------------------------

def _g(rec, *names):
    """Erster nicht-leerer Wert aus mehreren Feldnamen-Kandidaten."""
    for n in names:
        if n in rec:
            v = rec[n]
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return None


def _detect_prefix(field_names):
    """Dominantes WLK-Praefix (``L`` / ``E`` / ``SP`` / ``SO``) der DBF-Felder."""
    counts = {}
    for name in field_names:
        if "_" in name:
            pref = name.split("_", 1)[0].upper()
            counts[pref] = counts.get(pref, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


_PREFIX_LAYER = {"L": "leitung", "E": "einbau", "SP": "speicher", "SO": "sonstiges"}


def _build_notes(layer, prefix, rec):
    """Sammelt die fachlich wertvollen WLK-Felder als lesbare Notiz, damit beim
    Mapping nichts verloren geht."""
    parts = []

    def add(label, *names, suffix=""):
        v = _g(rec, *names)
        if v:
            parts.append(f"{label}: {v}{suffix}")

    add("Kat.-Nr.", f"{prefix}_BEZ", f"{prefix}_ID", "ID1")
    add("Anmerkung", f"{prefix}_AANM", f"{prefix}_ZU_BES")
    add("Eigentümer", f"{prefix}_EIGEN")
    add("Betreiber", f"{prefix}_BETR")
    add("Wasserrecht", f"{prefix}_GZ")
    add("WR-Datum", f"{prefix}_WR_DAT")
    if layer == "leitung":
        add("Druckstufe PN", "L_PN")
        add("Verlegung von Knoten", "L_VON_K")
        add("bis Knoten", "L_BIS_K")
    if layer == "einbau":
        add("Fabrikat", "E_FAB")
        add("Tiefe", "E_TIEFE", suffix=" m")
        add("GOK-Höhe", "E_GOK", suffix=" m")
    if layer == "speicher":
        add("Volumen", "SP_VOL", suffix=" m³")
        add("Anzahl Kammern", "SP_ANZ_WK")
    return "\n".join(parts) or None


def _maintenance_props(rec, feature_type):
    """Wartungs-Hinweise aus den ``E_WA_*``-Feldern -> maintenance-Properties.
    Liefert ``{}`` wenn keine Wartungsinfo vorhanden ist."""
    interval = parse_interval_months(_g(rec, "E_WA_INT", "WA_INT"))
    last = parse_wlk_date(_g(rec, "E_LWA_DAT", "LWA_DAT"))
    anm = _g(rec, "E_WA_ANM", "WA_ANM")
    if interval is None and last is None and not anm:
        return {}
    # Pruefungsart aus dem Feature-Typ ableiten (WLK liefert dafuer keinen
    # einheitlichen Code).
    kind = {
        "hydrant": "spuelung",
        "schieber": "funktionspruefung",
    }.get(feature_type, "inspektion")
    props = {"maintenance_kind": kind}
    if interval is not None:
        props["maintenance_interval_months"] = interval
    if last:
        props["maintenance_last_date"] = last
    if anm:
        props["maintenance_notes"] = anm
    return props


# ---------------------------------------------------------------------------
# Geometrie -> GeoJSON (reprojiziert)
# ---------------------------------------------------------------------------

def _point_features(shape, transformer):
    """Ein GeoJSON-Point je (reprojiziertem) Stuetzpunkt — MultiPoint mit 1
    Punkt ergibt also 1 Feature (der WLK-Normalfall)."""
    out = []
    for (x, y) in shape.points:
        lng, lat = transformer.transform(x, y)
        out.append({"type": "Point", "coordinates": [lng, lat]})
    return out


def _line_features(shape, transformer):
    """Ein GeoJSON-LineString je Polyline-Part (mehrteilige Linien werden
    getrennt). Parts mit < 2 Punkten werden verworfen."""
    pts = shape.points
    parts = list(shape.parts) or [0]
    bounds = list(parts) + [len(pts)]
    out = []
    for i in range(len(parts)):
        seg = pts[bounds[i]:bounds[i + 1]]
        if len(seg) < 2:
            continue
        coords = []
        for (x, y) in seg:
            lng, lat = transformer.transform(x, y)
            coords.append([lng, lat])
        out.append({"type": "LineString", "coordinates": coords})
    return out


# ---------------------------------------------------------------------------
# Hauptkonvertierung
# ---------------------------------------------------------------------------

def _looks_like_lonlat(bbox):
    """Heuristik: Koordinaten sehen schon nach WGS84-Grad aus."""
    xmin, ymin, xmax, ymax = bbox
    return -180 <= xmin <= 180 and -180 <= xmax <= 180 and -90 <= ymin <= 90 and -90 <= ymax <= 90


def _make_transformer(pyproj, prj_text, bbox):
    """Transformer von der Shapefile-CRS nach WGS84 (lng/lat). Ohne ``.prj``:
    Pass-through, falls die Koordinaten schon nach Grad aussehen, sonst Fehler."""
    if prj_text:
        try:
            src = pyproj.CRS.from_wkt(prj_text)
        except Exception as exc:
            raise WlkImportError(f"Projektion (.prj) nicht lesbar: {exc}") from exc
        return pyproj.Transformer.from_crs(src, "EPSG:4326", always_xy=True), src.name
    if _looks_like_lonlat(bbox):
        return pyproj.Transformer.from_crs("EPSG:4326", "EPSG:4326", always_xy=True), "WGS84 (angenommen)"
    raise WlkImportError(
        "Keine Projektionsdatei (.prj) gefunden und die Koordinaten sind nicht "
        "in Grad — das Koordinatensystem ist unbekannt. Bitte die .prj ins ZIP "
        "legen oder vorab nach EPSG:4326 exportieren."
    )


def _convert_one(shapefile, pyproj, shp_path):
    """Verarbeitet ein einzelnes Shapefile -> (features, layer, crs_name, skipped)."""
    base = shp_path[:-4]
    prj_text = None
    if os.path.exists(base + ".prj"):
        with open(base + ".prj", encoding="utf-8", errors="replace") as f:
            prj_text = f.read()

    # Austria-DBF ist i.d.R. cp1252; pyshp respektiert ein .cpg falls vorhanden.
    reader = shapefile.Reader(base, encoding="cp1252", encodingErrors="replace")
    kind = _geometry_kind(reader.shapeType)
    if kind is None:
        reader.close()
        return [], None, None, reader.numRecords  # unbekannte Geometrie -> alles skippen

    transformer, crs_name = _make_transformer(pyproj, prj_text, reader.bbox)

    field_names = [f[0] for f in reader.fields if f[0] != "DeletionFlag"]
    prefix = _detect_prefix(field_names) or ("L" if kind == "line" else "E")
    layer = _PREFIX_LAYER.get(prefix, "leitung" if kind == "line" else "sonstiges")

    features, skipped = [], 0
    for sr in reader.iterShapeRecords():
        rec = sr.record.as_dict()
        code = _g(rec, f"{prefix}_ART")
        aanm = _g(rec, f"{prefix}_AANM", f"{prefix}_ZU_BES")
        name = _g(rec, f"{prefix}_AANM", f"{prefix}_BEZ")
        year = _to_int(_g(rec, f"{prefix}_INBE"))
        accuracy = map_accuracy(_g(rec, f"{prefix}_LAG_ERM"))
        notes = _build_notes(layer, prefix, rec)

        if kind == "line":
            ftype = classify_line(code, aanm)
            geoms = _line_features(sr.shape, transformer)
            material = normalize_material(_g(rec, "L_MAT", "L_MATERIAL"))
            dn = _to_int(_g(rec, "L_DN", "L_PROFIL", "L_NW"))
        else:
            ftype = classify_point(layer, code, aanm)
            geoms = _point_features(sr.shape, transformer)
            material, dn = None, None

        if not geoms:
            skipped += 1
            continue

        props = {
            "feature_type": ftype,
            "name": name,
            "accuracy": accuracy,
            "material": material,
            "dimension_dn": dn,
            "year_built": year,
            "notes": notes,
        }
        if kind == "point":
            props.update(_maintenance_props(rec, ftype))

        for geom in geoms:
            features.append({"type": "Feature", "geometry": geom, "properties": props})

    reader.close()
    return features, layer, crs_name, skipped


def _iter_shp_paths(root):
    """Alle .shp unter ``root`` (rekursiv)."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(".shp"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _safe_extract(zip_path, dest):
    """Entpackt nur regulaere Dateien als Basename (Zip-Slip-sicher)."""
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not name:
                continue
            with zf.open(info) as src, open(os.path.join(dest, name), "wb") as dst:
                dst.write(src.read())
            extracted += 1
            if extracted > 2000:
                raise WlkImportError("ZIP enthält zu viele Dateien.")
    return extracted


def convert_zip(file_or_path):
    """Liest ein hochgeladenes Shapefile-ZIP und liefert
    ``{"features": [...], "stats": {...}}``.

    ``file_or_path`` ist ein Werkzeug-``FileStorage`` (bzw. ein file-like mit
    ``.read``) oder ein Pfad zu einer ``.zip``. Wirft ``WlkImportError`` mit
    benutzerfreundlicher Meldung bei harten Fehlern (fehlende Libs, kaputtes ZIP,
    keine Shapefiles)."""
    shapefile, pyproj = _load_libs()

    with tempfile.TemporaryDirectory(prefix="wlk_import_") as tmp:
        # Upload/Pfad in eine lokale .zip materialisieren.
        if hasattr(file_or_path, "read"):
            zip_path = os.path.join(tmp, "upload.zip")
            if hasattr(file_or_path, "save"):       # Werkzeug-FileStorage
                file_or_path.save(zip_path)
            else:                                    # sonstiges file-like
                with open(zip_path, "wb") as fh:
                    fh.write(file_or_path.read())
        else:
            zip_path = file_or_path

        if not zipfile.is_zipfile(zip_path):
            raise WlkImportError(
                "Die Datei ist kein gültiges ZIP. Bitte die Shapefiles "
                "(.shp/.shx/.dbf/.prj) gemeinsam in ein ZIP packen und hochladen."
            )

        extract_dir = os.path.join(tmp, "x")
        os.makedirs(extract_dir, exist_ok=True)
        _safe_extract(zip_path, extract_dir)

        shp_paths = _iter_shp_paths(extract_dir)
        if not shp_paths:
            raise WlkImportError("Im ZIP wurde kein Shapefile (.shp) gefunden.")

        all_features = []
        layer_counts = {}      # layer -> Anzahl Features
        type_counts = {}       # vocab-Label -> Anzahl
        crs_names = set()
        total_skipped = 0
        maintenance_count = 0

        for shp in shp_paths:
            try:
                feats, layer, crs_name, skipped = _convert_one(shapefile, pyproj, shp)
            except WlkImportError:
                raise
            except Exception as exc:  # defensiv: ein kaputtes Shapefile soll den Rest nicht killen
                total_skipped += 1
                layer_counts[f"Fehler in {os.path.basename(shp)}"] = str(exc)
                continue
            total_skipped += skipped
            if crs_name:
                crs_names.add(crs_name)
            for feat in feats:
                all_features.append(feat)
                label = vocab.feature_type_label(feat["properties"]["feature_type"])
                type_counts[label] = type_counts.get(label, 0) + 1
                if any(k.startswith("maintenance_") for k in feat["properties"]):
                    maintenance_count += 1
            if layer:
                layer_counts[layer] = layer_counts.get(layer, 0) + len(feats)

        if not all_features:
            raise WlkImportError(
                "Es konnten keine importierbaren Objekte erzeugt werden "
                "(nur nicht unterstützte Geometrien?)."
            )

        stats = {
            "total": len(all_features),
            "type_counts": type_counts,
            "layer_counts": layer_counts,
            "crs_names": sorted(crs_names),
            "skipped": total_skipped,
            "maintenance_count": maintenance_count,
            "shapefiles": [os.path.basename(p) for p in shp_paths],
        }
        return {"features": all_features, "stats": stats}
