"""Geocoding der Liegenschaften aus dem **österreichischen Adressregister** (BEV).

Zwei klar getrennte Schritte (siehe ADR-Diskussion / CLAUDE.md):

1. **Index bauen** (``build_index``) — die ~3,3 Mio Adressen des Gratis-BEV-
   Stichtags-Downloads (CC BY 4.0) werden einmalig in eine kompakte SQLite-
   Datei gegossen: ``(plz, ort, strasse, hausnummer) -> WGS84-Koordinate``.
   Schwerer Schritt (100-MB-ZIP, CRS-Reprojektion) -> laeuft NICHT im Request,
   sondern via ``flask bev-refresh`` (OSS: Cron/manuell; SaaS: platform-
   scheduler 2x/Jahr) und schreibt den Index auf ein **geteiltes** Volume.
   Der BEV-Datensatz landet bewusst **nie** in der App-/Tenant-DB — er ist
   Referenz-, keine Mandanten-Daten.

2. **Liegenschaften abgleichen** (``geocode_properties``) — die paar Hundert
   eigenen Adressen werden gegen den vorhandenen Index nachgeschlagen und ihre
   ``lat``/``lng`` gesetzt. Schnell (indizierte Lookups), idempotent, on-demand
   per Button.

Das Modul ist bewusst dialekt-unabhaengig: der Index ist eine eigenstaendige
SQLite-Datei (stdlib ``sqlite3``), egal ob die App selbst auf SQLite, MariaDB
oder Postgres laeuft. ``pyproj`` wird **lazy** importiert (wie in
``app.network.wlk_import``), damit das Blueprint auch ohne die GIS-Lib laedt.

**Format-Annahme (header-getrieben, defensiv):** Das BEV-ZIP enthaelt
relationale CSVs (``ADRESSE.csv`` + ``STRASSE.csv`` + ``GEMEINDE.csv`` /
``ORTSCHAFT.csv``), ``;``-getrennt. Die Spaltennamen werden ueber Kandidaten-
Listen (``_COL_*``) aus dem Header ermittelt — die einzige Stelle, die bei einer
neuen BEV-Formatversion angepasst werden muss. Fehlt eine Pflichtspalte, wirft
``build_index`` mit dem tatsaechlichen Header eine klare Meldung.
"""

import csv
import io
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime


class BevImportError(Exception):
    """Benutzerfreundlicher Fehler (Flash- bzw. CLI-Meldung)."""


# ---------------------------------------------------------------------------
# Lazy pyproj (wie WeasyPrint / pyshp optional)
# ---------------------------------------------------------------------------

def _load_pyproj():
    try:
        import pyproj
    except ImportError as exc:  # pragma: no cover - haengt von der Umgebung ab
        raise BevImportError(
            "Für das BEV-Geocoding wird das Paket 'pyproj' benötigt. "
            "Bitte installieren: pip install pyproj"
        ) from exc
    return pyproj


def dependencies_available():
    """True, wenn pyproj importierbar ist (fuer UI-Hinweise)."""
    try:
        _load_pyproj()
        return True
    except BevImportError:
        return False


# ---------------------------------------------------------------------------
# Spalten-Kandidaten (header-getrieben). EINZIGE Stelle, die bei einer neuen
# BEV-Formatversion nachgezogen werden muss.
# ---------------------------------------------------------------------------

# ADRESSE.csv  (Spaltennamen verifiziert gegen BEV V1.5, Stichtagsdaten 2026)
_COL_ADR_SKZ = ("SKZ", "STRASSENKENNZIFFER", "STRKZ")
_COL_ADR_GKZ = ("GKZ", "GEMEINDEKENNZIFFER", "GEM_KENNZ")
_COL_ADR_OKZ = ("OKZ", "ORTSCHAFTSKENNZIFFER")
_COL_ADR_PLZ = ("PLZ", "POSTLEITZAHL")
# HNR_ADR_ZUSAMMEN ist die fertig zusammengesetzte Hausnummer (BEV); HAUSNRTEXT
# ist oft leer. Reihenfolge = Prioritaet (erster im Header gefundener gewinnt).
_COL_ADR_HNRTEXT = ("HNR_ADR_ZUSAMMEN", "HAUSNRTEXT", "HAUSNUMMERTEXT", "HNR_TEXT", "HAUSNUMMER", "HNR")
_COL_ADR_HNRZAHL = ("HAUSNRZAHL1", "HAUSNRZAHL", "HNRZAHL1")
_COL_ADR_HNRBUCH = ("HAUSNRBUCHSTABE1", "HAUSNRBUCHSTABE", "HNRBUCHSTABE1")
_COL_ADR_RW = ("RW", "RECHTSWERT", "X", "EAST", "GKX")
_COL_ADR_HW = ("HW", "HOCHWERT", "Y", "NORTH", "GKY")
_COL_ADR_EPSG = ("EPSG", "EPSGCODE", "EPSG_CODE", "CRS")

# STRASSE.csv  (SKZ ist bundesweit eindeutig -> einfacher Key)
_COL_STR_SKZ = ("SKZ", "STRASSENKENNZIFFER", "STRKZ")
_COL_STR_NAME = ("STRASSENNAME", "STRASSE", "STR_NAME", "NAME", "BEZEICHNUNG")

# GEMEINDE.csv (Key GKZ) / ORTSCHAFT.csv (Key GKZ+OKZ, Name = ORTSNAME)
_COL_GEM_GKZ = ("GKZ", "GEMEINDEKENNZIFFER")
_COL_GEM_NAME = ("GEMEINDENAME", "GEMEINDE", "NAME", "BEZEICHNUNG")
_COL_ORT_GKZ = ("GKZ", "GEMEINDEKENNZIFFER")
_COL_ORT_OKZ = ("OKZ", "ORTSCHAFTSKENNZIFFER")
_COL_ORT_NAME = ("ORTSNAME", "ORTSCHAFTSNAME", "ORTSCHAFT", "NAME", "BEZEICHNUNG")

# Gueltige GK-Meridianstreifen Oesterreichs (MGI/Austria GK). Der BEV-Download
# fuehrt den passenden Code je Adresse in der EPSG-Spalte. Whitelist als
# Schutz gegen Muell-Werte (sonst wuerde pyproj pro kaputter Zeile werfen).
_VALID_EPSG = {31254, 31255, 31256, 31257, 31258, 31259, 31287, 4326}


# ---------------------------------------------------------------------------
# Normalisierung — muss zwischen BEV-Daten und Property-Stammdaten konsistent
# sein, damit moeglichst viel matcht.
# ---------------------------------------------------------------------------

def _norm_street(value):
    """``"Dorfstraße"`` und ``"Dorf Strasse"`` -> ``"dorfstrasse"``.

    Lowercase, ß->ss, Satzzeichen weg, alle Leerzeichen entfernen (Variante
    mit/ohne Leerzeichen vor "strasse" matcht so trotzdem), "str." -> "strasse".
    """
    if not value:
        return ""
    s = value.strip().lower().replace("ß", "ss")
    s = re.sub(r"\bstr\.?\b", "strasse", s)
    s = re.sub(r"[^0-9a-zäöü]+", "", s)  # Umlaute behalten, Rest (inkl. Space) raus
    return s


def _norm_hnr(value):
    """``"12 a"`` / ``"12A"`` -> ``"12a"``. Leerzeichen/Satzzeichen weg, klein."""
    if not value:
        return ""
    return re.sub(r"[^0-9a-zäöü]+", "", value.strip().lower().replace("ß", "ss"))


def _hnr_leading(value):
    """Fallback-Hausnummer: nur die fuehrende Zahl + optionaler Buchstabe.

    ``"12a/3"`` / ``"12-14"`` -> ``"12a"``. Faengt Tuer-/Bereichsangaben ab,
    die das BEV im Top-Level der Adresse meist nicht fuehrt.
    """
    if not value:
        return ""
    m = re.match(r"\s*(\d+)\s*([a-zA-ZäöüÄÖÜ]?)", value.strip())
    if not m:
        return ""
    return (m.group(1) + m.group(2)).lower()


def _norm_ort(value):
    if not value:
        return ""
    return re.sub(r"[^0-9a-zäöü]+", "", value.strip().lower().replace("ß", "ss"))


def _norm_plz(value):
    if not value:
        return ""
    return re.sub(r"\D", "", str(value))


# ---------------------------------------------------------------------------
# CSV-Zugriff im ZIP (Encoding-Fallback, ;-Delimiter, Header-Mapping)
# ---------------------------------------------------------------------------

def _find_member(names, *keywords):
    """ZIP-Member zu einem Keyword. Exakter Stem-Treffer (``adresse.csv``) wird
    bevorzugt vor Praefix-Treffern (``adresse_gst.csv``)."""
    bases = [(n, n.rsplit("/", 1)[-1].lower()) for n in names]
    for n, base in bases:
        if any(base == k + ".csv" for k in keywords):
            return n
    for n, base in bases:
        if base.endswith(".csv") and any(base.startswith(k) for k in keywords):
            return n
    return None


def _detect_encoding(zf, member):
    sample = zf.read(member)[:8192]
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"  # decodet immer, ggf. mit Ersatzzeichen


def _open_reader(zf, member, encoding):
    """Streamender CSV-Reader (``;``-getrennt) ueber ein ZIP-Member."""
    raw = zf.open(member)
    text = io.TextIOWrapper(raw, encoding=encoding, errors="replace", newline="")
    return csv.reader(text, delimiter=";")


def _col_index(header, candidates, *, member, required=True):
    """Index der ersten passenden Spalte (case-insensitiv, getrimmt)."""
    norm = {h.strip().upper(): i for i, h in enumerate(header)}
    for cand in candidates:
        if cand in norm:
            return norm[cand]
    if required:
        raise BevImportError(
            f"In {member} keine Spalte für {candidates[0]} gefunden. "
            f"Vorhandene Spalten: {', '.join(header)}"
        )
    return None


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _download(url, dest, log):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "wasserklar-bev-geocode/1.0"})
    log(f"Lade BEV-Daten von {url} …")
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
        log(f"Download fertig: {total // (1024 * 1024)} MB.")


# ---------------------------------------------------------------------------
# Index bauen
# ---------------------------------------------------------------------------

def _load_lookup(zf, member, key_col_groups, name_cols, log):
    """``{key -> name}`` aus einer relationalen Hilfstabelle (STRASSE/GEMEINDE/
    ORTSCHAFT). ``key_col_groups`` ist eine Liste von Kandidaten-Tupeln — ein
    zusammengesetzter Schluessel (z. B. GKZ+OKZ fuer Ortschaften, deren OKZ nur
    je Gemeinde eindeutig ist). Liefert ``{}`` wenn das Member fehlt."""
    if not member:
        return {}
    enc = _detect_encoding(zf, member)
    reader = _open_reader(zf, member, enc)
    header = next(reader, None)
    if not header:
        return {}
    kis = [_col_index(header, cols, member=member) for cols in key_col_groups]
    ni = _col_index(header, name_cols, member=member)
    need = max(max(kis), ni)
    out = {}
    for row in reader:
        if len(row) <= need:
            continue
        key = "|".join(row[i].strip() for i in kis)
        name = row[ni].strip()
        if name and all(part for part in key.split("|")):
            out[key] = name
    log(f"  {os.path.basename(member)}: {len(out)} Einträge.")
    return out


def build_index(source, index_path, *, is_url=False, progress=None):
    """Baut den SQLite-Geocoding-Index aus dem BEV-ZIP.

    ``source`` ist ein Pfad zu einer ``.zip`` (``is_url=False``) oder eine URL
    (``is_url=True``). Schreibt zuerst in ``<index_path>.tmp`` und ersetzt das
    Ziel atomar — eine parallel laufende App liest waehrend des Baus noch den
    alten Index. Liefert ein Stats-Dict.
    """
    pyproj = _load_pyproj()

    def log(msg):
        if progress:
            progress(msg)

    transformers = {}

    def transform(epsg, rw, hw):
        t = transformers.get(epsg)
        if t is None:
            t = pyproj.Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
            transformers[epsg] = t
        lng, lat = t.transform(rw, hw)
        return lat, lng

    os.makedirs(os.path.dirname(os.path.abspath(index_path)) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="bev_") as tmp:
        if is_url:
            zip_path = os.path.join(tmp, "bev.zip")
            _download(source, zip_path, log)
        else:
            zip_path = source

        if not zipfile.is_zipfile(zip_path):
            raise BevImportError(
                "Die BEV-Quelle ist kein gültiges ZIP. Bitte die "
                "Adressregister-Stichtagsdaten (relationale CSV-Tabellen) als "
                "ZIP angeben."
            )

        tmp_index = index_path + ".tmp"
        if os.path.exists(tmp_index):
            os.remove(tmp_index)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            adresse_member = _find_member(names, "adresse")
            if not adresse_member:
                raise BevImportError(
                    "Im ZIP wurde keine ADRESSE.csv gefunden. Enthaltene Dateien: "
                    + ", ".join(n.rsplit("/", 1)[-1] for n in names[:30])
                )
            strasse_member = _find_member(names, "strasse", "straße")
            ort_member = _find_member(names, "ortschaft")
            gemeinde_member = _find_member(names, "gemeinde")

            log("Lese Hilfstabellen …")
            streets = _load_lookup(zf, strasse_member, [_COL_STR_SKZ], _COL_STR_NAME, log)
            # Ortsname bevorzugt aus ORTSCHAFT (passt zu Property.ort), sonst
            # Gemeinde. Ortschaft-OKZ ist nur je Gemeinde eindeutig -> GKZ+OKZ.
            orts = _load_lookup(zf, ort_member, [_COL_ORT_GKZ, _COL_ORT_OKZ], _COL_ORT_NAME, log)
            gemeinden = _load_lookup(zf, gemeinde_member, [_COL_GEM_GKZ], _COL_GEM_NAME, log)

            # ADRESSE.csv streamen + reprojizieren + in SQLite schreiben.
            enc = _detect_encoding(zf, adresse_member)
            reader = _open_reader(zf, adresse_member, enc)
            header = next(reader, None)
            if not header:
                raise BevImportError("ADRESSE.csv ist leer.")

            i_skz = _col_index(header, _COL_ADR_SKZ, member=adresse_member)
            i_plz = _col_index(header, _COL_ADR_PLZ, member=adresse_member)
            i_rw = _col_index(header, _COL_ADR_RW, member=adresse_member)
            i_hw = _col_index(header, _COL_ADR_HW, member=adresse_member)
            i_epsg = _col_index(header, _COL_ADR_EPSG, member=adresse_member, required=False)
            i_okz = _col_index(header, _COL_ADR_OKZ, member=adresse_member, required=False)
            i_gkz = _col_index(header, _COL_ADR_GKZ, member=adresse_member, required=False)
            i_hnrtext = _col_index(header, _COL_ADR_HNRTEXT, member=adresse_member, required=False)
            i_hnrzahl = _col_index(header, _COL_ADR_HNRZAHL, member=adresse_member, required=False)
            i_hnrbuch = _col_index(header, _COL_ADR_HNRBUCH, member=adresse_member, required=False)
            if i_hnrtext is None and i_hnrzahl is None:
                raise BevImportError(
                    f"In ADRESSE.csv keine Hausnummer-Spalte gefunden. "
                    f"Vorhandene Spalten: {', '.join(header)}"
                )
            if i_epsg is None:
                raise BevImportError(
                    "In ADRESSE.csv keine EPSG-Spalte gefunden — das "
                    "Koordinatensystem je Adresse ist dann unbekannt. "
                    f"Vorhandene Spalten: {', '.join(header)}"
                )

            conn = sqlite3.connect(tmp_index)
            try:
                conn.execute("PRAGMA journal_mode=OFF")
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute(
                    "CREATE TABLE addresses ("
                    "plz TEXT, ort TEXT, street TEXT, hnr TEXT, lat REAL, lng REAL)"
                )
                log("Lese ADRESSE.csv (reprojiziere nach WGS84) …")
                batch = []
                inserted = skipped = 0
                max_i = max(i for i in (i_skz, i_plz, i_rw, i_hw, i_epsg, i_okz,
                                        i_gkz, i_hnrtext, i_hnrzahl, i_hnrbuch)
                            if i is not None)
                for row in reader:
                    if len(row) <= max_i:
                        skipped += 1
                        continue
                    # Hausnummer: HAUSNRTEXT bevorzugt, sonst Zahl+Buchstabe.
                    if i_hnrtext is not None and row[i_hnrtext].strip():
                        hnr = _norm_hnr(row[i_hnrtext])
                    else:
                        zahl = row[i_hnrzahl].strip() if i_hnrzahl is not None else ""
                        buch = row[i_hnrbuch].strip() if i_hnrbuch is not None else ""
                        hnr = _norm_hnr(zahl + buch)
                    street = _norm_street(streets.get(row[i_skz].strip(), ""))
                    if not street or not hnr:
                        skipped += 1
                        continue
                    okz = row[i_okz].strip() if i_okz is not None else ""
                    gkz = row[i_gkz].strip() if i_gkz is not None else ""
                    ort = _norm_ort(
                        orts.get(f"{gkz}|{okz}") or gemeinden.get(gkz) or ""
                    )
                    plz = _norm_plz(row[i_plz])
                    # Koordinate
                    try:
                        epsg = int(float(row[i_epsg]))
                        if epsg not in _VALID_EPSG:
                            skipped += 1
                            continue
                        rw = float(row[i_rw].replace(",", "."))
                        hw = float(row[i_hw].replace(",", "."))
                    except (ValueError, TypeError):
                        skipped += 1
                        continue
                    lat, lng = transform(epsg, rw, hw)
                    batch.append((plz, ort, street, hnr, lat, lng))
                    if len(batch) >= 50000:
                        conn.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?)", batch)
                        inserted += len(batch)
                        batch = []
                        log(f"  … {inserted} Adressen verarbeitet")
                if batch:
                    conn.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?)", batch)
                    inserted += len(batch)

                log("Baue Indizes …")
                conn.execute("CREATE INDEX ix_plz ON addresses (plz, street, hnr)")
                conn.execute("CREATE INDEX ix_ort ON addresses (ort, street, hnr)")
                conn.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"
                )
                conn.executemany(
                    "INSERT INTO meta VALUES (?,?)",
                    [("built_at", datetime.utcnow().isoformat()),
                     ("addresses", str(inserted)),
                     ("source", os.path.basename(str(source)))],
                )
                conn.commit()
            finally:
                conn.close()

        os.replace(tmp_index, index_path)
        log(f"BEV-Index geschrieben: {index_path}")
        return {"addresses": inserted, "skipped": skipped, "index_path": index_path}


# ---------------------------------------------------------------------------
# Liegenschaften abgleichen
# ---------------------------------------------------------------------------

def index_info(index_path):
    """Meta-Infos des Index (built_at, addresses) oder None, wenn keiner da ist."""
    if not os.path.exists(index_path):
        return None
    try:
        conn = sqlite3.connect(index_path)
        try:
            rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
        return rows
    except sqlite3.Error:
        return None


def _lookup(conn, prop):
    street = _norm_street(prop.strasse)
    if not street:
        return None
    hnr = _norm_hnr(prop.hausnummer)
    hnr_lead = _hnr_leading(prop.hausnummer)
    plz = _norm_plz(prop.plz)
    ort = _norm_ort(prop.ort)

    # Priorisiert: PLZ-genauer Treffer; dann Ort-genauer; jeweils mit Fallback
    # auf die fuehrende Hausnummer (ohne Tuer/Bereich).
    attempts = []
    if plz:
        attempts.append(("plz", plz, hnr))
        if hnr_lead and hnr_lead != hnr:
            attempts.append(("plz", plz, hnr_lead))
    if ort:
        attempts.append(("ort", ort, hnr))
        if hnr_lead and hnr_lead != hnr:
            attempts.append(("ort", ort, hnr_lead))

    for keycol, keyval, hnr_try in attempts:
        if not hnr_try:
            continue
        sql = f"SELECT lat, lng FROM addresses WHERE {keycol}=? AND street=? AND hnr=? LIMIT 1"
        row = conn.execute(sql, (keyval, street, hnr_try)).fetchone()
        if row:
            return row[0], row[1]
    return None


def geocode_properties(*, only_missing=True, index_path=None):
    """Gleicht aktive Liegenschaften gegen den BEV-Index ab und setzt lat/lng.

    ``only_missing=True`` ueberspringt bereits geocodete (``lat`` gesetzt) —
    idempotenter Standardlauf nach dem Anlegen neuer Liegenschaften. ``False``
    rechnet alle neu (z.B. nach einem Index-Refresh).

    Liefert ein Stats-Dict: ``total``, ``geocoded``, ``not_found`` (Liste der
    Labels). Wirft ``BevImportError``, wenn kein Index vorhanden ist.
    """
    from flask import current_app
    from app.extensions import db
    from app.models import Property

    index_path = index_path or current_app.config["BEV_INDEX_PATH"]
    if not os.path.exists(index_path):
        raise BevImportError(
            "Es ist kein BEV-Adressindex vorhanden. Bitte zuerst "
            "`flask bev-refresh` ausführen (im SaaS stellt ihn der "
            "Plattform-Scheduler automatisch bereit)."
        )

    query = Property.query.filter_by(active=True)
    if only_missing:
        query = query.filter(Property.lat.is_(None))
    props = query.all()

    conn = sqlite3.connect(index_path)
    geocoded = 0
    not_found = []
    try:
        for prop in props:
            coord = _lookup(conn, prop)
            if coord:
                prop.lat, prop.lng = coord
                prop.geocoded_at = datetime.utcnow()
                geocoded += 1
            else:
                not_found.append(prop.label())
    finally:
        conn.close()

    db.session.commit()
    return {"total": len(props), "geocoded": geocoded, "not_found": not_found}
