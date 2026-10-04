"""Länderspezifische Inhalte des Demo-Datensatzes (Österreich / Deutschland).

``demo.py`` baut beide Datensätze mit demselben Code; was sich unterscheidet —
Ort und Netz-Geometrie, Namen, Banken, Labore, Fachbegriffe in Texten,
Steuersätze und Tarife — steht hier. Die Werte für Österreich reproduzieren den
bisherigen Datensatz Zeichen für Zeichen (Hagenberg im Mühlkreis); Deutschland
spielt in Habach (Oberbayern, ``demo_geo_de.py``).

Alle Namen, Firmen, Kennungen und Kontodaten sind **erfunden**. Kennungen, die
eine Prüfziffer haben (IBAN, USt-IdNr., Leitweg-ID), werden mit gültiger
Prüfziffer erzeugt, damit E-Rechnungs-Prüfungen nicht an den Testdaten scheitern
— sie gehören zu keinem echten Konto bzw. Unternehmen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.seed import demo_geo, demo_geo_de


# ---------------------------------------------------------------------------
# Prüfziffern
# ---------------------------------------------------------------------------

def iban(country_code: str, bban: str) -> str:
    """IBAN mit gültiger Prüfziffer (ISO 13616, MOD 97-10), in Vierergruppen."""
    digits = "".join(str(int(ch, 36)) for ch in (bban + country_code + "00"))
    check = 98 - int(digits) % 97
    raw = f"{country_code}{check:02d}{bban}"
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def de_vat_id(base8: str) -> str:
    """Deutsche USt-IdNr. ``DE`` + 8 Ziffern + Prüfziffer (ISO 7064, MOD 11,10)."""
    product = 10
    for ch in base8:
        total = (int(ch) + product) % 10 or 10
        product = (2 * total) % 11
    check = (11 - product) % 10
    return f"DE{base8}{check}"


def leitweg_id(base: str) -> str:
    """Leitweg-ID ``Grob-Fein`` + Prüfziffer (MOD 97-10, wie ``einvoice.leitweg``)."""
    from app.einvoice import leitweg
    return f"{base}-{leitweg.check_digit(base)}"


# ---------------------------------------------------------------------------
# Locale
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DemoLocale:
    code: str
    geo: object                      # Modul mit der Netz-Geometrie
    # USt-Sätze der Beispielbelege: Wasser (ermäßigt) und Normalsatz
    water_rate: Decimal
    std_rate: Decimal
    # Buchungsjahre umsatzsteuerpflichtig? (AT-Demo: nein, wie bisher)
    vat_liable: bool
    # Tarife: (Grundgebühr, Zusatzgebühr, Wasserpreis) Vorjahr / laufend
    tariff_prev: tuple
    tariff_curr: tuple
    # Wassercent je m³ und Stichtag (None = keine Abgabe)
    water_levy: Decimal | None
    water_levy_from: date | None
    # Bankkonten: (Name, Beschreibung, IBAN)
    bank_giro: tuple
    bank_credit: tuple
    labs: tuple
    maintenance_by: str
    contractor: str
    crew: str
    venue: str
    names: dict                      # Anlagen, Straßenbezüge, Texte (siehe unten)
    identity: dict                   # Stammdaten der Demo-Genossenschaft (wg.*)


_AT = DemoLocale(
    code="AT",
    geo=demo_geo,
    water_rate=Decimal("10.00"),
    std_rate=Decimal("20.00"),
    vat_liable=False,
    tariff_prev=(Decimal("32.00"), Decimal("8.00"), Decimal("1.40")),
    tariff_curr=(Decimal("36.00"), Decimal("9.00"), Decimal("1.55")),
    water_levy=None,
    water_levy_from=None,
    bank_giro=("Girokonto Raika", "Hauptkonto WG", "AT12 3456 7890 1111 0000"),
    bank_credit=("Kreditkonto Bauspar", "Investitionskredit Quellsanierung",
                 "AT12 3456 7890 2222 0000"),
    labs=("Landeslabor OÖ", "AGES Linz", "Hydro-Labor GmbH"),
    maintenance_by="Wassermeister Huber",
    contractor="Tiefbau Mayr GmbH",
    crew="Eigene Crew",
    venue="Gasthaus zur Quelle, Saal",
    names={
        "reservoir": "Hochbehälter Sonnberg",
        "reservoir_notes": "Nutzinhalt 150 m³, zwei Kammern.",
        "pump": "Druckerhöhung Sonnberg",
        "pump_ref": "die Druckerhöhung",
        "springs": ("Quelle Brunnertal", "Quelle Lärchwald", "Quelle Steinbründl"),
        "air_valve": "Entlüftung Hochpunkt Sonnberg",
        "air_valve_notes": "Automatischer Be-/Entlüfter am Hochpunkt der Hauptleitung.",
        "pressure_reducer_notes": "Druckminderer am Eintritt in die tiefer liegende Unterortszone.",
        "drain_notes": "Entleerung am Tiefpunkt des Netzes in den Vorfluter.",
        "tie_in_notes": "Verbindungsleitung zur Nachbargenossenschaft — "
                        "Übergabeschieber normal geschlossen.",
        "main_line_1": "Hauptleitung Hochbehälter–Ortseingang",
        "main_line_2": "Hauptleitung Ortseingang–Ortsverteiler",
        # Straßen, auf die sich Störungen/Beschlüsse/Buchungen beziehen
        "street_leak": "Gruberstraße",
        "street_pressure": "Löschfeld",
        "street_slow_leak": "Hauptstraße",
        "street_break": "Raiffeisenstraße",
        "street_hydrant_repair": "Kirchenplatz",
        "street_valve_repair": "Weingarten",
        # Begriffe in Texten
        "cash_report": "Kassabericht",
        "misc_item": "Allfälliges",
        "of_chairman": "des Obmanns",
        "state_funding": "des Landes",
        "advance_payment": "Akontozahlung Wasser offen",
        "email_domain": "example.at",
    },
    identity={
        "name": "Demo-WG Hagenberg",
        "street": "Postfach 12",
        "postal_code": "4232",
        "city": "Hagenberg im Mühlkreis",
        "address": "Postfach 12, 4232 Hagenberg im Mühlkreis",
        "email": "kontakt@wg-hagenberg.example",
        "phone": "07236 99999",
        "contact_name": "Maria Leitner",
        "iban": "AT12 3456 7890 1111 0000",
        "bic": "RZOOAT2LXXX",
        "account_holder": "Demo-WG Hagenberg",
    },
)

# Deutsche Demo-Kennungen (fiktiv, mit gültiger Prüfziffer)
_DE_GIRO = iban("DE", "70169599" + "0000470110")
_DE_CREDIT = iban("DE", "70169599" + "0000470129")

_DE = DemoLocale(
    code="DE",
    geo=demo_geo_de,
    water_rate=Decimal("7.00"),
    std_rate=Decimal("19.00"),
    vat_liable=True,
    tariff_prev=(Decimal("48.00"), Decimal("12.00"), Decimal("1.95")),
    tariff_curr=(Decimal("54.00"), Decimal("12.00"), Decimal("2.15")),
    # Bayerischer Wassercent (Wasserentnahmeentgelt): 10 ct/m³ ab 1.7.2026 —
    # im Tarif als Position mit Stichtag, wird zeitanteilig abgerechnet.
    water_levy=Decimal("0.10"),
    water_levy_from=date(2026, 7, 1),
    bank_giro=("Girokonto Raiffeisenbank", "Hauptkonto der Genossenschaft", _DE_GIRO),
    bank_credit=("Darlehenskonto Quellsanierung", "Investitionsdarlehen (KfW-Förderkredit)",
                 _DE_CREDIT),
    labs=("Trinkwasserlabor Isarwinkel GmbH", "Oberland-Analytik GmbH",
          "Labor Dr. Brunnhuber"),
    maintenance_by="Wasserwart Huber",
    contractor="Tiefbau Lindner GmbH",
    crew="Eigene Mannschaft",
    venue="Gasthof zur Post, Saal",
    names={
        "reservoir": "Hochbehälter Habach",
        "reservoir_notes": "Nutzinhalt 300 m³ in zwei Kammern, davon 100 m³ Löschwasserreserve.",
        "pump": "Pumpwerk Höhlmühler Graben",
        "pump_ref": "das Pumpwerk",
        "springs": ("Quelle Höhlmühle", "Quelle Riedholz", "Quelle Kühbach"),
        "air_valve": "Entlüftung Hochpunkt Steinberg",
        "air_valve_notes": "Automatischer Be-/Entlüfter am Hochpunkt des Ortsnetzes.",
        "pressure_reducer_notes": "Druckminderer vor der tiefer liegenden Ostzone "
                                  "(Heubachweg, Dürnhauser Straße).",
        "drain_notes": "Entleerung am Tiefpunkt des Netzes in den Heubach.",
        "tie_in_notes": "Verbindungsleitung zum Nachbarversorger (Neubaugebiet "
                        "Antdorfer Straße) — Übergabeschieber normal geschlossen.",
        "main_line_1": "Hauptleitung Hochbehälter–Höhlmühler Straße",
        "main_line_2": "Hauptleitung Höhlmühler Straße–Ortsverteiler",
        "street_leak": "Ährenanger",
        "street_pressure": "Dürnhauser Straße",
        "street_slow_leak": "Hauptstraße",
        "street_break": "St.-Ulrich-Straße",
        "street_hydrant_repair": "Schulweg",
        "street_valve_repair": "Hofmark",
        "cash_report": "Kassenbericht",
        "misc_item": "Verschiedenes",
        "of_chairman": "des Vorsitzenden",
        "state_funding": "des Freistaats",
        "advance_payment": "Abschlagszahlung Wasser offen",
        "email_domain": "example.de",
    },
    identity={
        "name": "Demo-WG Habach eG",
        "street": "Postfach 1120",
        "postal_code": "82392",
        "city": "Habach",
        "address": "Postfach 1120, 82392 Habach",
        "email": "kontakt@wg-habach.example",
        "phone": "089 99998 120",
        "contact_name": "Martina Holzer",
        "iban": _DE_GIRO,
        "bic": "GENODEF1XXX",
        "account_holder": "Demo-WG Habach eG",
        "vat_id": de_vat_id("29071541"),
        "tax_number": "168/123/45670",
        "register_number": "GnR 999 (Amtsgericht München)",
    },
)

LOCALES = {"AT": _AT, "DE": _DE}


def locale_for(code) -> DemoLocale:
    """Locale zum Ländercode; unbekannt -> Österreich (wie ``app.country``)."""
    return LOCALES.get((code or "").upper(), _AT)


# ---------------------------------------------------------------------------
# Deutsche Namen und Firmen
# ---------------------------------------------------------------------------

# (Vorname, Anrede)
DE_FIRST_NAMES = (
    ("Josef", "Herr"), ("Maria", "Frau"), ("Johann", "Herr"), ("Anna", "Frau"),
    ("Georg", "Herr"), ("Theresia", "Frau"), ("Michael", "Herr"), ("Elisabeth", "Frau"),
    ("Franz", "Herr"), ("Katharina", "Frau"), ("Martin", "Herr"), ("Monika", "Frau"),
    ("Andreas", "Herr"), ("Christine", "Frau"), ("Thomas", "Herr"), ("Barbara", "Frau"),
    ("Stefan", "Herr"), ("Sabine", "Frau"), ("Alois", "Herr"), ("Rosa", "Frau"),
    ("Sebastian", "Herr"), ("Andrea", "Frau"), ("Matthias", "Herr"), ("Claudia", "Frau"),
    ("Florian", "Herr"), ("Petra", "Frau"), ("Markus", "Herr"), ("Gabriele", "Frau"),
    ("Anton", "Herr"), ("Veronika", "Frau"), ("Korbinian", "Herr"), ("Magdalena", "Frau"),
    ("Benedikt", "Herr"), ("Ursula", "Frau"), ("Lorenz", "Herr"), ("Johanna", "Frau"),
    ("Xaver", "Herr"), ("Brigitte", "Frau"), ("Leonhard", "Herr"), ("Kreszenz", "Frau"),
)
DE_LAST_NAMES = (
    "Huber", "Bauer", "Maier", "Schmid", "Hofmann", "Lechner", "Wagner", "Fischer",
    "Zellner", "Reiser", "Sedlmayr", "Kögl", "Strobl", "Kreitmair", "Pfaffenzeller",
    "Echter", "Wörl", "Rieder", "Steigenberger", "Fürst", "Bichler", "Holzer",
    "Mangold", "Sappl", "Resch", "Daisenberger", "Gröbl", "Hartl", "Kirchmair",
    "Neuner", "Ostler", "Pöttinger", "Rauch", "Schöttl", "Thaler", "Vogl", "Weiß",
    "Zach", "Gerg", "Bals",
)

# Unternehmer-Kunden (§ 14 UStG): (Name, ist Firma, Branche/Notiz, USt-IdNr.-Basis)
DE_BUSINESSES = (
    ("Landgasthof Ostler GmbH & Co. KG", True, "Gastronomie mit Fremdenzimmern", "13572468"),
    ("Zimmerei Bichler GmbH", True, "Zimmerei, Bauwasser über Standrohr", "24681357"),
    ("Kfz-Werkstatt Sappl e.K.", True, "Kfz-Werkstatt mit Waschplatz", "31415926"),
    ("Biohof Kirchmair GbR", True, "Milchviehbetrieb, Tränkwasser", "27182818"),
    # Personen, die zugleich Unternehmer sind (Vermieter, Einzelunternehmen)
    ("Rieder Theresia", False, "Ferienwohnungen (Vermietung)", "16180339"),
    ("Holzer Franz", False, "Bäckerei (Einzelunternehmen)", "14142135"),
)

# Behörden mit XRechnung. Leitweg-ID: Grobadressierung 09 (Bayern) 190 (Lkr. Weilheim-Schongau)
# + die bewusst nicht vergebene Gemeindekennung 99999 — fiktiv, aber mit gültiger Prüfziffer.
DE_PUBLIC = (
    # (Name, Leitweg-Basis, E-Mail-Postfach | None, Bestellreferenz | None, Notiz)
    ("Gemeinde Habach", "0919099999-0001", "rechnungseingang", None,
     "Rathaus und Feuerwehrhaus — Rechnungen nur als XRechnung über die Leitweg-ID."),
    ("Schulverband Habach-Antdorf", "0919099999-0002", None, "SV-2026-014",
     "Grundschule — XRechnung ohne E-Mail-Postfach, Zustellung über die Leitweg-ID."),
)

# Lieferanten (Eingangsrechnungen, Belege): (Name, Straße, PLZ, Ort, USt-IdNr.-Basis, E-Mail, Telefon)
DE_SUPPLIERS = {
    "tiefbau": ("Tiefbau Lindner GmbH", "Gewerbestraße 7", "82377", "Penzberg", "19283746",
                "rechnung@tiefbau-lindner.example", "089 99998 301"),
    "armaturen": ("Armaturen Oberland GmbH", "Industriestraße 22", "82362", "Weilheim i. OB",
                  "28374651", "buchhaltung@armaturen-oberland.example", "089 99998 302"),
    "labor": ("Trinkwasserlabor Isarwinkel GmbH", "Laborweg 3", "83646", "Bad Tölz",
              "37465128", "rechnung@labor-isarwinkel.example", "089 99998 303"),
    "gasthof": ("Gasthof zur Post Habach", "Dorfplatz 2", "82392", "Habach", "46512837",
                "post@gasthof-post.example", "089 99998 304"),
    "buero": ("Schreibwaren Gröbl", "Marktplatz 5", "82362", "Weilheim i. OB", "51283746",
              "info@schreibwaren-groebl.example", "089 99998 305"),
    "baumarkt": ("Baustoffe Pöttinger KG", "Am Bahnhof 12", "82377", "Penzberg", "62837451",
                 "kasse@baustoffe-poettinger.example", "089 99998 306"),
    "elektro": ("Elektro Schöttl", "Kirchweg 9", "82393", "Iffeldorf", "73845126",
                "info@elektro-schoettl.example", "089 99998 307"),
}

# Lieferanten der Beispielbelege in Österreich: (Name, Straße, PLZ, Ort, UID)
AT_SUPPLIERS = {
    "armaturen": ("Armaturen Mühlviertel GmbH", "Gewerbepark 4", "4240", "Freistadt",
                  "ATU12345675"),
    "buero": ("Papier & Büro Aichinger", "Hauptplatz 3", "4232", "Hagenberg im Mühlkreis",
              "ATU23456789"),
    "baumarkt": ("Baustoffe Leitner KG", "Linzer Straße 18", "4222", "Langenstein",
                 "ATU34567891"),
    "elektro": ("Elektro Eder", "Bahnhofstraße 9", "4221", "Steyregg", "ATU45678912"),
}
