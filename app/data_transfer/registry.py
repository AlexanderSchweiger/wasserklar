"""Registry: welche Models in welche Kategorie fallen, FK-Insert-Order,
Jahresfilter-Spalten und natuerliche Schluessel fuer den Merge-Modus.

Das ist die einzige Stelle, an der Tabellen aufgezaehlt werden — wer ein
neues Model hinzufuegt, muss es auch hier eintragen, sonst wird es vom
Export nicht erfasst.
"""

from app.models import (
    Customer, Property, PropertyOwnership, WaterMeter,
    WaterTariff, TaxRate, Account, RealAccount, Project,
    FiscalYear, MeterReadingAccessCode, MeterReading,
    Invoice, InvoiceItem, BillingRun, OpenItem,
    BookingGroup, Booking, Transfer, RealAccountYearBalance,
    DunningPolicy, DunningStage, DunningNotice,
    AppSetting, InvoiceCounter, CustomerCounter,
)

# Spalten die auf users.id verweisen — werden beim Import auf NULL gesetzt,
# weil 'users' in EXCLUDED_TABLES ist und IDs im Ziel-System abweichen.
NULL_ON_IMPORT_COLS = {
    MeterReading: ["created_by_id"],
    MeterReadingAccessCode: ["created_by_id"],
    BillingRun: ["created_by_id"],
    Invoice: ["created_by_id"],
    Transfer: ["created_by_id"],
    Booking: ["created_by_id"],
    BookingGroup: ["created_by_id"],
    OpenItem: ["created_by_id"],
    FiscalYear: ["closed_by_id"],
    DunningNotice: ["reset_by_id", "created_by_id"],
}


# Modelle pro Kategorie. Reihenfolge spielt hier keine Rolle —
# fuer FK-sicheres Insert ist INSERT_ORDER massgeblich.
CATEGORIES = {
    "stammdaten": [
        TaxRate, FiscalYear, Account, RealAccount, Project,
        Customer, Property, PropertyOwnership, WaterMeter,
        MeterReadingAccessCode, WaterTariff,
    ],
    "buchungen": [
        MeterReading, BillingRun, Invoice, InvoiceItem, OpenItem,
        BookingGroup, Booking, Transfer, RealAccountYearBalance,
        InvoiceCounter, CustomerCounter,
    ],
    "mahnwesen": [
        DunningPolicy, DunningStage, DunningNotice,
    ],
    "einstellungen": [
        AppSetting,
    ],
}


# FK-topologische Insert-Reihenfolge ueber alle Kategorien.
# - MeterReadingAccessCode VOR MeterReading (FK self_service_code_id)
# - BookingGroup VOR Booking (FK group_id)
# - InvoiceItem VOR DunningNotice (FK fee_invoice_item_id),
#   ABER: InvoiceItem.dunning_notice_id verweist umgekehrt auf DunningNotice
#   → wird in zweitem Pass nachgesetzt (siehe services.py).
# - Booking.storno_of_id (Self-FK) → ebenfalls zweiter Pass.
INSERT_ORDER = [
    TaxRate, FiscalYear, Account, RealAccount, Project,
    Customer, Property, PropertyOwnership, WaterMeter,
    MeterReadingAccessCode, WaterTariff, MeterReading,
    BillingRun, Invoice, InvoiceItem, OpenItem,
    BookingGroup, Booking, Transfer, RealAccountYearBalance,
    DunningPolicy, DunningStage, DunningNotice,
    AppSetting, InvoiceCounter, CustomerCounter,
]


# Spalten, die per Jahr gefiltert werden koennen (fuer "Buchungen"-Kategorie).
# Wert: Spaltenname (Integer-Jahr) ODER ("date_col", "year") fuer Date-Spalten,
# bei denen das Jahr per EXTRACT/strftime gezogen wird.
YEAR_FILTERS = {
    MeterReading: "year",
    BillingRun: "period_year",
    Invoice: ("date", "period_year"),  # period_year bevorzugt, fallback date
    OpenItem: "period_year",
    Booking: ("date_year",),            # year aus date extrahieren
    Transfer: ("date_year",),
    RealAccountYearBalance: "year",
    InvoiceCounter: "year",
}


# Natuerliche Schluessel fuer Merge-Modus: Tuple aus Spalten, die einen
# Datensatz eindeutig identifizieren (auch dialektuebergreifend stabil).
# None bedeutet: kein natuerlicher Schluessel — im Merge-Modus immer Insert
# mit neuer ID, kein Update existierender Records.
NATURAL_KEYS = {
    TaxRate: ("rate",),
    FiscalYear: ("year",),
    Account: ("name",),                 # code ist optional, name ist nullable=False
    RealAccount: ("name",),             # iban ist optional
    Project: ("name",),                 # name ist unique=True
    Customer: ("customer_number",),     # unique; fallback handled in serializer wenn None
    Property: ("object_number",),       # unique; fallback handled in serializer wenn None
    PropertyOwnership: ("property_id", "customer_id", "valid_from"),
    WaterMeter: ("meter_number",),
    MeterReadingAccessCode: ("customer_id", "year"),
    WaterTariff: ("name", "valid_from"),
    MeterReading: ("meter_id", "year"),
    BillingRun: None,                   # kein natuerlicher Schluessel
    Invoice: ("invoice_number",),
    InvoiceItem: None,
    OpenItem: None,
    BookingGroup: None,
    Booking: None,
    Transfer: None,
    RealAccountYearBalance: ("real_account_id", "year"),
    DunningPolicy: ("name",),
    DunningStage: ("policy_id", "level"),
    DunningNotice: None,
    AppSetting: ("key",),
    InvoiceCounter: ("year",),
    CustomerCounter: ("id",),           # Singleton id=1
}


# FK-Spalten je Model: alt-ID (im Export) → neu-ID (nach Insert).
# Wird im Merge-Modus zum Remappen genutzt; im Vollersatz unveraendert.
# Format: {column_name: target_model}
FOREIGN_KEYS = {
    PropertyOwnership: {"property_id": Property, "customer_id": Customer},
    WaterMeter: {"property_id": Property},
    MeterReadingAccessCode: {"customer_id": Customer},
    MeterReading: {"meter_id": WaterMeter, "self_service_code_id": MeterReadingAccessCode},
    BillingRun: {"account_id": Account},
    Invoice: {"customer_id": Customer, "property_id": Property, "billing_run_id": BillingRun},
    InvoiceItem: {"invoice_id": Invoice, "account_id": Account, "project_id": Project,
                  "dunning_notice_id": DunningNotice},  # zweiter Pass
    OpenItem: {"customer_id": Customer, "invoice_id": Invoice, "account_id": Account},
    BookingGroup: {"invoice_id": Invoice, "customer_id": Customer},
    Booking: {"account_id": Account, "invoice_id": Invoice, "open_item_id": OpenItem,
              "project_id": Project, "real_account_id": RealAccount,
              "customer_id": Customer, "group_id": BookingGroup,
              "storno_of_id": Booking},  # Self-FK, zweiter Pass
    Transfer: {"from_real_account_id": RealAccount, "to_real_account_id": RealAccount},
    RealAccountYearBalance: {"real_account_id": RealAccount},
    DunningStage: {"policy_id": DunningPolicy},
    DunningNotice: {"invoice_id": Invoice, "stage_id": DunningStage,
                    "fee_invoice_item_id": InvoiceItem},
}


# FK-Spalten, die im ersten Insert-Pass auf NULL gesetzt werden und in einem
# zweiten Pass per UPDATE nachgesetzt werden — noetig wenn das Ziel-Model
# erst spaeter inserted wird (zirkulaere oder Self-FKs).
DEFERRED_FK_UPDATES = {
    Booking: ["storno_of_id"],
    InvoiceItem: ["dunning_notice_id"],
}


# Komplett vom Export ausgeschlossene Tabellen (System/Auth, instance-bound,
# Audit-Logs die ohne User-Export sinnlos waeren).
EXCLUDED_TABLES = {
    "users",
    "user_preferences",
    "alembic_version",
    "fiscal_year_reopen_logs",  # Audit-Log mit User-FK; ohne User-Export sinnlos
}


# AppSetting-Keys, die nicht exportiert werden duerfen.
# - mail.smtp_password ist Fernet-verschluesselt mit instance-spezifischem Key
EXCLUDED_APPSETTING_KEYS_PREFIXES = (
    "mail.smtp_password",
)


def is_excluded_setting(key: str) -> bool:
    """True wenn der AppSetting-Key vom Export ausgeschlossen werden muss."""
    return any(key.startswith(p) for p in EXCLUDED_APPSETTING_KEYS_PREFIXES)


def category_for_model(model):
    """Liefert die Kategorie ('stammdaten', 'buchungen', ...) eines Models."""
    for cat, models in CATEGORIES.items():
        if model in models:
            return cat
    return None


def models_for_selection(selection: dict):
    """Aus den ausgewaehlten Kategorien die FK-geordnete Modell-Liste bauen."""
    selected = set()
    for cat, models in CATEGORIES.items():
        if selection.get(cat):
            selected.update(models)
    return [m for m in INSERT_ORDER if m in selected]
