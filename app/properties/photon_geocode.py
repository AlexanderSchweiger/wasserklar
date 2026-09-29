"""Geocoding ueber einen Photon-Server (OpenStreetMap) — der Weg fuer
Deutschland.

Warum nicht wie in Oesterreich ein amtliches Register: die Amtlichen
Hauskoordinaten (HK-DE) sind nicht bundesweit Open Data (Lizenz ueber die
ZSHH, nur einzelne Laender frei). OpenStreetMap deckt deutsche Hausnummern
gut ab; Photon ist der schlanke, selbst hostbare Geocoder darauf (im SaaS als
eigener Container, siehe wasserklar-deploy). Die Adressen verlassen damit die
eigene Infrastruktur nicht (DSGVO).

Konfiguration: ``GEOCODER_PHOTON_URL`` (z.B. ``http://photon:2322``). Ohne
URL ist der Dienst „nicht verfuegbar" — die Koordinate laesst sich dann auf
der Liegenschafts-Seite manuell setzen.

Treffer werden streng geprueft (Strasse + Hausnummer, PLZ falls vorhanden),
damit keine Strassen-Mittelpunkte als Hausposition durchrutschen.
Nur stdlib (``urllib``), keine zusaetzliche Abhaengigkeit.
"""
import json
from datetime import datetime
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.properties.bev_geocode import _hnr_leading, _norm_hnr, _norm_plz, _norm_street


class PhotonError(Exception):
    """Benutzerfreundlicher Fehler (Flash- bzw. API-Meldung)."""


def base_url():
    from flask import current_app
    return (current_app.config.get("GEOCODER_PHOTON_URL") or "").strip().rstrip("/")


def is_configured():
    return bool(base_url())


def _fetch(query, url, timeout):
    req = Request(f"{url}/api?{urlencode({'q': query, 'limit': 5, 'lang': 'de'})}",
                  headers={"User-Agent": "wasserklar-geocoder"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (URLError, OSError, ValueError) as exc:
        raise PhotonError(f"Geocoding-Dienst nicht erreichbar ({exc}).") from exc


def lookup(strasse, hausnummer, plz=None, ort=None, *, url=None, timeout=5):
    """``(lat, lng)`` oder ``None``. Wirft ``PhotonError`` bei Dienstfehlern."""
    url = url or base_url()
    if not url:
        raise PhotonError("Kein Geocoding-Dienst konfiguriert (GEOCODER_PHOTON_URL).")
    street = _norm_street(strasse)
    hnr = _norm_hnr(hausnummer)
    if not street or not hnr:
        return None
    hnr_lead = _hnr_leading(hausnummer)
    want_plz = _norm_plz(plz)
    query = " ".join(p for p in (f"{strasse or ''} {hausnummer or ''}".strip(),
                                 (plz or "").strip(), (ort or "").strip()) if p)
    data = _fetch(query, url, timeout)
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        if _norm_street(props.get("street")) != street:
            continue
        got_hnr = _norm_hnr(props.get("housenumber"))
        if got_hnr not in (hnr, hnr_lead):
            continue
        if want_plz and props.get("postcode") and _norm_plz(props.get("postcode")) != want_plz:
            continue
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2:
            return float(coords[1]), float(coords[0])
    return None


def geocode_properties(*, only_missing=True):
    """Wie ``bev_geocode.geocode_properties`` — gleiches Ergebnis-Dict."""
    from app.extensions import db
    from app.models import Property

    if not is_configured():
        raise PhotonError(
            "Für Deutschland ist noch kein Geocoding-Dienst (Photon/OpenStreetMap) "
            "eingerichtet. Die Lage einzelner Liegenschaften können Sie auf deren "
            "Detailseite manuell auf der Karte setzen.")
    query = Property.query.filter_by(active=True)
    if only_missing:
        query = query.filter(Property.lat.is_(None))
    props = query.all()
    geocoded = 0
    not_found = []
    for prop in props:
        coord = lookup(prop.strasse, prop.hausnummer, prop.plz, prop.ort)
        if coord:
            prop.lat, prop.lng = coord
            prop.geocoded_at = datetime.utcnow()
            geocoded += 1
        else:
            not_found.append(prop.label())
    db.session.commit()
    return {"total": len(props), "geocoded": geocoded, "not_found": not_found}
