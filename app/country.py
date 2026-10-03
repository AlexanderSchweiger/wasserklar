"""Länderprofile (Österreich / Deutschland).

Single Source of Truth für alles, was sich zwischen den unterstuetzten
Laendern unterscheidet: Standard-Steuersaetze, USt-Satz fuer Wasser,
Nacheichfrist, Rechnungs-Pflichtangaben-Beschriftungen usw. Code, der einen
laenderabhaengigen Default braucht, fragt ``current_profile()`` — nie ein
hart codiertes "Österreich"/"10 %".

Das Land eines Mandanten liegt in der AppSetting ``org.country`` (ISO-Code).
Fehlt sie (frische OSS-Installation, Tests), gilt ``DEFAULT_COUNTRY`` aus der
Config und zuletzt Oesterreich — das war bis zur Einfuehrung der Profile der
fest verdrahtete Stand, bestehende Installationen verhalten sich also
unveraendert.

Die Profile liefern nur **Defaults**. Was der Mandant selbst pflegt
(Steuersaetze in ``tax_rates``, ``tax.water_rate``, Eichfrist …), hat immer
Vorrang — die Profile greifen beim Seeden, bei „Länder-Defaults übernehmen"
und als Fallback, solange nichts gepflegt ist.
"""
from dataclasses import dataclass
from decimal import Decimal

from flask import current_app

COUNTRY_AT = "AT"
COUNTRY_DE = "DE"
DEFAULT_COUNTRY = COUNTRY_AT

# AppSetting-Key des Mandanten-Landes.
SETTING_KEY = "org.country"


@dataclass(frozen=True)
class CountryProfile:
    code: str
    # Anzeigename und zugleich der Default fuer das Adress-Feld ``land``.
    name: str
    # Schreibweisen, die in Adressen als Inland gelten (casefold-Vergleich).
    aliases: tuple
    # Standard-Steuersaetze ((Satz, Bezeichnung), ...), aufsteigend.
    tax_rates: tuple
    # Standard-USt fuer Wasserlieferungen (AT: ermaessigt 10 %, DE: 7 %).
    water_tax_rate: Decimal
    # Nacheichfrist fuer Kaltwasserzaehler in Jahren + Hinweistext.
    calibration_years: int
    calibration_hint: str
    # Beschriftung der Umsatzsteuer-Identifikationsnummer.
    vat_id_label: str
    # Rechnungshinweis in nicht USt-pflichtigen Jahren (Kleinunternehmer).
    small_business_note: str
    # Karte: Startausschnitt + amtliche Basiskarten (Leaflet-Tile-Layer). Jeder
    # Layer: label, url (XYZ), role (default|grau|ortho), maxNativeZoom,
    # attribution. OpenStreetMap haengt basemaps.js fuer alle Laender an.
    map_center: tuple = (47.59, 14.14)
    map_zoom: int = 7
    base_layers: tuple = ()
    # Sichtbare Fachbegriffe, die sich zwischen AT und DE unterscheiden
    # (Obmann/Vorsitzender, Kassa/Kasse, Jänner/Januar …) — ueber ``term()``.
    terms: dict = None
    # Startwert der Einstellung „E-Rechnung" (``einvoice.enabled``) fuer **neue**
    # Mandanten: in DE gilt die B2B-Pflicht, in AT interessiert sie die meisten
    # Mandanten nicht (Bund ausgenommen — der schaltet sie in den Einstellungen ein).
    einvoice_default: bool = True
    # Aufbewahrungsfrist fuer Buchungsbelege in Jahren (ab Ende des Kalenderjahres) +
    # Hinweistext zu den Sonderfaellen — nur Anzeige in der Belegablage, keine Rechtsberatung.
    document_retention_years: int = 7
    document_retention_hint: str = ""
    # Frist fuer Geschaeftsbriefe (Mahnungen, Schriftverkehr) — gleiche Rechenregel, eigene Jahre.
    # Protokolle/Beschluesse haben keine Frist (dauerhaft, ``app/documents/retention.py``).
    business_letter_retention_years: int = 7
    business_letter_retention_hint: str = ""

    def default_tax_rate_values(self):
        return [rate for rate, _ in self.tax_rates]


_AT = CountryProfile(
    code=COUNTRY_AT,
    name="Österreich",
    aliases=("österreich", "oesterreich", "austria", "at", "aut"),
    tax_rates=(
        (Decimal("0"), "0 % – keine MwSt"),
        (Decimal("10"), "10 %"),
        (Decimal("13"), "13 %"),
        (Decimal("20"), "20 %"),
    ),
    water_tax_rate=Decimal("10"),
    calibration_years=5,
    calibration_hint="Österreich: 5 Jahre für Kaltwasserzähler (MEG)",
    vat_id_label="UID-Nummer",
    small_business_note=(
        "Umsatzsteuerbefreit – Kleinunternehmer gemäß § 6 Abs. 1 Z 27 UStG."
    ),
    terms={
        "cash": "Kassa", "cash_balance": "Kassastand",
        "january": "Jänner", "jan_short": "Jän",
        "this_year": "heuer",
        "chairman": "Obmann", "deputy_chairman": "Obmann-Stellvertreter",
        "treasurer": "Kassier", "auditor": "Rechnungsprüfer",
        "chairman_sign": "Obmann / Obfrau",
        "the_chairman": "der Obmann", "The_chairman": "Der Obmann",
        "role_treasurer": "Kassier",
        # Zahlungsaufforderung auf Rechnung/Beleg: „<pay_request>, den Betrag … auf
        # unser Konto <pay_verb>:“
        "pay_request": "Wir ersuchen Sie", "pay_verb": "einzuzahlen",
    },
    einvoice_default=False,
    document_retention_years=7,
    document_retention_hint=(
        "§ 132 BAO: 7 Jahre ab Ende des Kalenderjahres, länger solange ein Verfahren offen ist. "
        "Unterlagen zu Grundstücken: 22 Jahre (§ 18 Abs. 10 UStG)."
    ),
    business_letter_retention_years=7,
    business_letter_retention_hint=(
        "Geschäftsbriefe (Mahnungen, Schriftverkehr): 7 Jahre ab Ende des Kalenderjahres "
        "(§ 132 BAO, § 212 UGB)."
    ),
    map_center=(47.59, 14.14),
    map_zoom=7,
    # basemap.at (Verwaltungsgrundkarte Österreich, CC BY 4.0). Einzelhost
    # mapsneu.wien.gv.at — die alten Subdomains maps1..4 antworten nicht mehr.
    base_layers=(
        {"label": "Karte (basemap.at)", "role": "default", "maxNativeZoom": 19,
         "url": "https://mapsneu.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png",
         "attribution": 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>'},
        {"label": "Karte grau (basemap.at)", "role": "grau", "maxNativeZoom": 19,
         "url": "https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png",
         "attribution": 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>'},
        {"label": "Orthofoto (basemap.at)", "role": "ortho", "maxNativeZoom": 19,
         "url": "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
         "attribution": 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>'},
    ),
)

_DE = CountryProfile(
    code=COUNTRY_DE,
    name="Deutschland",
    aliases=("deutschland", "germany", "de", "deu", "brd"),
    tax_rates=(
        (Decimal("0"), "0 % – keine USt"),
        (Decimal("7"), "7 %"),
        (Decimal("19"), "19 %"),
    ),
    water_tax_rate=Decimal("7"),
    calibration_years=6,
    calibration_hint="Deutschland: 6 Jahre für Kaltwasserzähler (MessEV)",
    vat_id_label="USt-IdNr.",
    small_business_note="Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.",
    terms={
        "cash": "Kasse", "cash_balance": "Kassenstand",
        "january": "Januar", "jan_short": "Jan",
        "this_year": "dieses Jahr",
        "chairman": "Vorsitzender", "deputy_chairman": "stellv. Vorsitzender",
        "treasurer": "Kassierer", "auditor": "Kassenprüfer",
        "chairman_sign": "Vorsitzende/r",
        "the_chairman": "der Vorsitzende", "The_chairman": "Der Vorsitzende",
        "role_treasurer": "Kassierer",
        "pay_request": "Wir bitten Sie", "pay_verb": "zu überweisen",
    },
    document_retention_years=8,
    document_retention_hint=(
        "§ 147 AO / § 14b UStG: 8 Jahre für Buchungsbelege und Rechnungen (seit 2025), "
        "10 Jahre für Bücher und Jahresabschlüsse, 6 Jahre für Geschäftsbriefe; die Frist "
        "läuft nicht ab, solange die Festsetzungsfrist noch nicht abgelaufen ist."
    ),
    business_letter_retention_years=6,
    business_letter_retention_hint=(
        "Geschäftsbriefe (Mahnungen, Schriftverkehr): 6 Jahre ab Ende des Kalenderjahres "
        "(§ 147 AO, § 257 HGB)."
    ),
    map_center=(51.16, 10.45),
    map_zoom=6,
    # basemap.de Web Raster (BKG/AdV, WMTS im Web-Mercator-Raster). Ein
    # bundesweites Luftbild gibt es nicht frei — optional je Mandant als
    # WMS eines Bundeslandes (AppSettings map.ortho_wms_*).
    base_layers=(
        {"label": "Karte (basemap.de)", "role": "default", "maxNativeZoom": 18,
         "url": "https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0/"
                "de_basemapde_web_raster_farbe/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png",
         "attribution": '&copy; <a href="https://basemap.de" target="_blank" rel="noopener">basemap.de</a> / BKG'},
        {"label": "Karte grau (basemap.de)", "role": "grau", "maxNativeZoom": 18,
         "url": "https://sgx.geodatenzentrum.de/wmts_basemapde/tile/1.0.0/"
                "de_basemapde_web_raster_grau/default/GLOBAL_WEBMERCATOR/{z}/{y}/{x}.png",
         "attribution": '&copy; <a href="https://basemap.de" target="_blank" rel="noopener">basemap.de</a> / BKG'},
    ),
)

PROFILES = {COUNTRY_AT: _AT, COUNTRY_DE: _DE}

# Reihenfolge = Reihenfolge im Einstellungs-<select>.
COUNTRY_CHOICES = [(p.code, p.name) for p in (_AT, _DE)]


def normalize_code(code):
    """ISO-Code normalisieren; unbekannte/leere Werte -> ``None``."""
    if not code:
        return None
    code = str(code).strip().upper()
    return code if code in PROFILES else None


def profile(code=None):
    """Profil fuer ``code`` — ohne Code das des aktuellen Mandanten."""
    if code is None:
        return current_profile()
    return PROFILES.get(normalize_code(code) or DEFAULT_COUNTRY)


def current_code():
    """ISO-Code des aktuellen Mandanten (AppSetting > Config > AT).

    Der DB-Zugriff ist abgesichert wie ``settings_service.platform_relay_active``:
    beim App-Start (SaaS: Schema ``public`` ohne ``app_settings``) greift der
    Config-Default.
    """
    try:
        from app.models import AppSetting
        val = normalize_code(AppSetting.get(SETTING_KEY))
    except Exception:
        val = None
    if val:
        return val
    try:
        val = normalize_code(current_app.config.get("DEFAULT_COUNTRY"))
    except RuntimeError:  # ausserhalb eines App-Kontexts
        val = None
    return val or DEFAULT_COUNTRY


def current_profile():
    return PROFILES[current_code()]


def is_foreign(land, code=None):
    """True, wenn ``land`` (Adressfeld) NICHT das Land des Mandanten ist.

    Leer/``None`` gilt als Inland — Adressen ohne Land sind der Regelfall.
    Verglichen wird tolerant gegen Namen und Aliase des Profils
    (``"Deutschland"``, ``"DE"``, ``"Germany"`` …). Steuert, ob das Land in
    Anschriften (Rechnung, Mahnung, Brief) mitgedruckt wird.
    """
    if not land or not str(land).strip():
        return False
    prof = profile(code) if code else current_profile()
    value = str(land).strip().casefold()
    return value != prof.name.casefold() and value not in prof.aliases


def home_country_name():
    """Anzeigename des Mandanten-Landes — Default fuer neue Adressen."""
    return current_profile().name


# AppSetting-Keys eines optionalen Luftbild-Dienstes (WMS), z.B. die digitalen
# Orthophotos eines Bundeslandes. basemap.de hat kein freies Luftbild.
MAP_ORTHO_KEYS = ("map.ortho_wms_url", "map.ortho_wms_layers", "map.ortho_attribution")


def map_config():
    """Karten-Konfiguration fuer ``static/js/basemaps.js`` (JSON in
    ``_map_config.html``): Startausschnitt, amtliche Basiskarten des Landes
    und ein optionaler Luftbild-WMS des Mandanten."""
    prof = current_profile()
    cfg = {
        "country": prof.code,
        "center": list(prof.map_center),
        "zoom": prof.map_zoom,
        "layers": [dict(layer) for layer in prof.base_layers],
        "ortho_wms": None,
    }
    try:
        from app.models import AppSetting
        url, layers, attribution = (AppSetting.get(k) for k in MAP_ORTHO_KEYS)
    except Exception:
        url = layers = attribution = None
    if url and layers:
        cfg["ortho_wms"] = {"url": url, "layers": layers,
                            "attribution": attribution or ""}
    return cfg


def map_tile_hosts():
    """Origins aller Kartendienste (fuer eine Content-Security-Policy)."""
    from urllib.parse import urlsplit
    cfg = map_config()
    urls = [layer["url"] for layer in cfg["layers"]]
    if cfg["ortho_wms"]:
        urls.append(cfg["ortho_wms"]["url"])
    hosts = []
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in hosts:
                hosts.append(origin)
    return hosts


def role_treasurer_names():
    """Alle Schreibweisen der Kassier-Rolle („Kassier"/„Kassierer") — damit
    Seeds und Demo-Daten die Rolle auch nach einem Landwechsel wiederfinden
    und nicht doppelt anlegen."""
    return tuple(dict.fromkeys(
        p.terms["role_treasurer"] for p in PROFILES.values()))


def term(key, code=None):
    """Land-spezifischer Fachbegriff (``term("cash")`` -> "Kassa"/"Kasse").
    Unbekannte Keys fallen auf den oesterreichischen Begriff bzw. den Key."""
    prof = profile(code) if code else current_profile()
    return (prof.terms or {}).get(key) or (_AT.terms or {}).get(key) or key

