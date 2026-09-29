"""Geocoding-Dispatcher: je Mandanten-Land der passende Adressdienst.

* Oesterreich — BEV-Adressregister (amtlich, Open Data) als lokaler
  SQLite-Index (``bev_geocode``).
* Deutschland — OpenStreetMap ueber einen Photon-Server (``photon_geocode``);
  amtliche Hauskoordinaten sind dort nicht bundesweit frei.

Aufrufer (Liegenschafts-Abgleich, REST-API ``/v1/geocode``) sprechen nur mit
diesem Modul und fangen ``GeocodingError``.
"""
from app import country

PROVIDER_BEV = "bev"
PROVIDER_PHOTON = "photon"

LABELS = {
    PROVIDER_BEV: "BEV-Adressregister",
    PROVIDER_PHOTON: "OpenStreetMap (Photon)",
}


class GeocodingError(Exception):
    """Benutzerfreundlicher Fehler (Flash- bzw. API-Meldung)."""


def provider():
    return PROVIDER_BEV if country.current_code() == country.COUNTRY_AT else PROVIDER_PHOTON


def status():
    """Zustand des Dienstes fuer den Abgleich-Dialog: ``provider``, ``label``,
    ``available`` und — beim BEV — die Index-Infos."""
    prov = provider()
    info = {"provider": prov, "label": LABELS[prov], "available": False, "index": None}
    if prov == PROVIDER_BEV:
        from flask import current_app
        from app.properties import bev_geocode
        idx = bev_geocode.index_info(current_app.config["BEV_INDEX_PATH"])
        info.update(available=idx is not None, index=idx)
    else:
        from app.properties import photon_geocode
        info["available"] = photon_geocode.is_configured()
    return info


def geocode_properties(*, only_missing=True):
    """Liegenschaften des Mandanten geocoden. Ergebnis: ``total``,
    ``geocoded``, ``not_found`` (Labels)."""
    if provider() == PROVIDER_BEV:
        from app.properties import bev_geocode
        try:
            return bev_geocode.geocode_properties(only_missing=only_missing)
        except bev_geocode.BevImportError as exc:
            raise GeocodingError(str(exc)) from exc
    from app.properties import photon_geocode
    try:
        return photon_geocode.geocode_properties(only_missing=only_missing)
    except photon_geocode.PhotonError as exc:
        raise GeocodingError(str(exc)) from exc


def geocode_address(strasse, hausnummer, *, plz=None, ort=None):
    """Einzeladresse -> ``{"lat", "lng"}`` oder ``None`` (kein DB-Schreiben)."""
    if provider() == PROVIDER_BEV:
        from app.properties import bev_geocode
        try:
            return bev_geocode.geocode_address(strasse, hausnummer, plz=plz, ort=ort)
        except bev_geocode.BevImportError as exc:
            raise GeocodingError(str(exc)) from exc
    from app.properties import photon_geocode
    try:
        coord = photon_geocode.lookup(strasse, hausnummer, plz, ort)
    except photon_geocode.PhotonError as exc:
        raise GeocodingError(str(exc)) from exc
    return {"lat": coord[0], "lng": coord[1]} if coord else None
