"""Mapping von Flask-Endpoints zu Doku-Slugs.

Wird vom Help-Button in `app/templates/base.html` ueber den Context-Processor
in `app/__init__.py` ausgewertet. Unbekannte Endpoints fallen auf den
Doku-Index zurueck.
"""

ENDPOINT_TO_DOC = {
    # Dashboard
    "main.dashboard": "dashboard",

    # Stammdaten
    "customers.index":             "mitglieder",
    "customers.detail":            "mitglieder",
    "customers.new":               "mitglieder#anlegen",
    "customers.edit":              "mitglieder",

    "properties.index":            "objekte",
    "properties.detail":           "objekte",
    "properties.new":              "objekte#anlegen",
    "properties.edit":             "objekte",

    "meters.index":                "wasserzaehler",
    "meters.meter_new":            "wasserzaehler#zaehler-anlegen",
    "meters.meter_edit":           "wasserzaehler",
    "meters.meter_replace":        "wasserzaehler#wechsel",
    "meters.readings":             "wasserzaehler#ablesung",
    "meters.add_reading":          "wasserzaehler#ablesung",
    "meters.bulk_read":            "wasserzaehler#bulk",
    "meters.import_upload":        "wasserzaehler#csv-import",
    "meters.import_preview":       "wasserzaehler#csv-import",
    "meters.import_result":        "wasserzaehler#csv-import",

    # Rechnungen / OP
    "invoices.index":              "rechnungen",
    "invoices.detail":             "rechnungen#status",
    "invoices.new":                "rechnungen#einzeln",
    "invoices.edit":               "rechnungen",
    "invoices.generate":           "rechnungen#rechnungslauf",
    "invoices.billing_runs":       "rechnungen#rechnungslauf",
    "invoices.billing_run_detail": "rechnungen#rechnungslauf",
    "invoices.tariffs":            "rechnungen#tarife",
    "invoices.tariff_new":         "rechnungen#tarife",
    "invoices.tariff_edit":        "rechnungen#tarife",
    "invoices.email_settings":     "einstellungen#mail",

    # Buchhaltung
    "accounting.bookings":           "buchhaltung#buchungen",
    "accounting.booking_new":        "buchhaltung#buchungen",
    "accounting.booking_edit":       "buchhaltung#buchungen",
    "accounting.transfers":          "buchhaltung#umbuchungen",
    "accounting.transfer_new":       "buchhaltung#umbuchungen",
    "accounting.fiscal_years":       "buchhaltung#jahresabschluss",
    "accounting.fiscal_year_new":    "buchhaltung#jahresabschluss",
    "accounting.fiscal_year_close":  "buchhaltung#jahresabschluss",
    "accounting.fiscal_year_reopen": "buchhaltung#jahresabschluss",
    "accounting.real_accounts":      "buchhaltung#bankkonten",
    "accounting.real_account_new":   "buchhaltung#bankkonten",
    "accounting.real_account_edit":  "buchhaltung#bankkonten",
    "accounting.accounts":           "buchhaltung#kontenplan",
    "accounting.account_new":        "buchhaltung#kontenplan",
    "accounting.account_edit":       "buchhaltung#kontenplan",
    "accounting.open_items":         "buchhaltung#offene-posten",
    "accounting.open_item_invoice":  "buchhaltung#offene-posten",
    "accounting.report":             "buchhaltung",
    "accounting.ust":                "buchhaltung",

    # Projekte
    "projects.index":              "projekte",

    # Mahnwesen
    "dunning.index":               "mahnwesen",
    "dunning.notices":             "mahnwesen#uebersicht",
    "dunning.notice_detail":       "mahnwesen#uebersicht",
    "dunning.notice_defer":        "mahnwesen#defer",
    "dunning.run":                 "mahnwesen#mahnlauf",
    "dunning.run_execute":         "mahnwesen#mahnlauf",
    "dunning.policies":            "mahnwesen#vorlagen",
    "dunning.policy_new":          "mahnwesen#vorlagen",
    "dunning.policy_edit":         "mahnwesen#vorlagen",

    # Stammdaten-Import
    "import_csv.upload":           "csv-import",
    "import_csv.preview":          "csv-import#schritte",

    # Verwaltung
    "settings.index":              "einstellungen",
    "auth.users":                  "einstellungen#benutzer",
    "auth.user_new":               "einstellungen#benutzer",
    "auth.user_edit":              "einstellungen#benutzer",

    # SaaS-Endpoints (existieren nur, wenn die SaaS-Blueprints registriert
    # sind — das Mapping schadet sonst nicht).
    "billing.index":               "abonnement",
    "support.index":               "faq",
    "support.new":                 "faq",
    "files.index":                 "einstellungen",
    "export.index":                "abonnement#export",
    "backups.index":               "abonnement#export",
    "bank_import.index":           "buchhaltung",
}


def help_url_for(endpoint, base_url):
    """Baut die Help-URL fuer den aktuellen Request-Endpoint.

    - `endpoint` darf None sein (z.B. 404-Fall) — fallback auf Index.
    - `base_url` ist `app.config['HELP_BASE_URL']`. Leerer String =
      Help-Button deaktivieren (Caller checkt das Template-seitig).
    - Slug kann einen `#anchor`-Suffix enthalten — der wird unveraendert
      an die URL angehaengt.
    """
    if not base_url:
        return ""
    base = base_url.rstrip("/")
    slug = ENDPOINT_TO_DOC.get(endpoint or "", "")
    if not slug:
        return base
    return f"{base}/{slug}"
