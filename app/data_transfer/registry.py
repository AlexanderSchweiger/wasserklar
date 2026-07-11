"""Registry: welche Models in welche Kategorie fallen, FK-Insert-Order,
Jahresfilter-Spalten und natuerliche Schluessel fuer den Merge-Modus.

Das ist die einzige Stelle, an der Tabellen aufgezaehlt werden — wer ein
neues Model hinzufuegt, muss es entweder hier eintragen (CATEGORIES +
INSERT_ORDER, ggf. FOREIGN_KEYS/NATURAL_KEYS/YEAR_FILTERS) oder bewusst in
EXCLUDED_TABLES aufnehmen, sonst wird es vom Export nicht erfasst.

Das erzwingt der Guard-Test
``tests/integration/test_data_transfer_registry.py`` — er schlaegt fehl,
sobald eine physische Tabelle weder registriert noch ausgeschlossen ist.
"""

from app.models import (
    Customer, Property, PropertyOwnership, WaterMeter,
    WaterTariff, TaxRate, Account, RealAccount, Project,
    FiscalYear, MeterReadingAccessCode, MeterReading, MeterReplacement,
    MeterTour, MeterTourStop,
    Invoice, InvoiceItem, BillingRun, BillingPeriod, OpenItem,
    BookingGroup, Booking, Transfer, RealAccountYearBalance,
    DunningPolicy, DunningStage, DunningNotice,
    AppSetting, InvoiceCounter, CustomerCounter,
    NetworkPlan, NetworkFeature, MaintenanceLog, SpringYield, Incident,
    WaterSample, LabResult,
    CustomerWgProfile, PropertyWgProfile, WgFunction,
    Note, ReadingCorrection,
    Role, RolePermission,
    BankStatement, BankStatementLine, BankStatementLineAllocation,
    InvoiceEmailOptInCode, CustomerEmailConsentLog, EmailSuppression,
    Meeting, MeetingAgendaItem, MeetingInvitation, MeetingDeliveryLog,
    MeetingAttendance, MeetingResolution, MeetingProtocol,
    SchriftverkehrDocument,
    Circular, CircularRecipient, CircularDeliveryLog,
)

# Spalten die auf users.id verweisen — werden beim Import auf NULL gesetzt,
# weil 'users' in EXCLUDED_TABLES ist und IDs im Ziel-System abweichen.
NULL_ON_IMPORT_COLS = {
    MeterReading: ["created_by_id"],
    MeterReplacement: ["created_by_id"],
    MeterTour: ["created_by_id"],
    MeterReadingAccessCode: ["created_by_id"],
    ReadingCorrection: ["created_by_id"],
    BillingRun: ["created_by_id"],
    Invoice: ["created_by_id"],
    Transfer: ["created_by_id"],
    Booking: ["created_by_id"],
    BookingGroup: ["created_by_id"],
    OpenItem: ["created_by_id"],
    FiscalYear: ["closed_by_id"],
    DunningNotice: ["reset_by_id", "created_by_id"],
    NetworkPlan: ["created_by_id", "updated_by_id"],
    NetworkFeature: ["created_by_id"],
    MaintenanceLog: ["created_by_id"],
    SpringYield: ["created_by_id"],
    Incident: ["created_by_id"],
    WaterSample: ["created_by_id"],
    Note: ["created_by_id"],
    BankStatement: ["uploaded_by_id"],
    InvoiceEmailOptInCode: ["created_by_id"],
    Meeting: ["created_by_id"],
    MeetingDeliveryLog: ["user_id"],
    MeetingResolution: ["created_by_id"],
    MeetingProtocol: ["created_by_id"],
    SchriftverkehrDocument: ["created_by_id"],
    Circular: ["created_by_id"],
    CircularDeliveryLog: ["user_id"],
}


# Modelle pro Kategorie. Reihenfolge spielt hier keine Rolle —
# fuer FK-sicheres Insert ist INSERT_ORDER massgeblich.
CATEGORIES = {
    "stammdaten": [
        TaxRate, FiscalYear, Account, RealAccount, Project,
        Customer, Property, PropertyOwnership, WaterMeter,
        BillingPeriod, MeterReadingAccessCode, WaterTariff,
        NetworkPlan, NetworkFeature, MaintenanceLog, SpringYield, Incident,
        WaterSample, LabResult,
        CustomerWgProfile, PropertyWgProfile, WgFunction,
        InvoiceEmailOptInCode, CustomerEmailConsentLog,
        Meeting, MeetingAgendaItem, MeetingInvitation, MeetingDeliveryLog,
        MeetingAttendance, MeetingResolution, MeetingProtocol,
        SchriftverkehrDocument,
        Circular, CircularRecipient, CircularDeliveryLog,
        Note,
    ],
    "buchungen": [
        MeterReading, MeterReplacement, BillingRun, Invoice, InvoiceItem, OpenItem,
        MeterTour, MeterTourStop,
        ReadingCorrection,
        BookingGroup, Booking, Transfer, RealAccountYearBalance,
        BankStatement, BankStatementLine, BankStatementLineAllocation,
        InvoiceCounter, CustomerCounter,
    ],
    "mahnwesen": [
        DunningPolicy, DunningStage, DunningNotice,
    ],
    "einstellungen": [
        AppSetting,
        Role, RolePermission,
        EmailSuppression,
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
    # Rollen/Rechte zuerst — RolePermission.role_id → Role; sonst FK-frei.
    Role, RolePermission,
    TaxRate, FiscalYear, Account, RealAccount, Project,
    Customer, Property, PropertyOwnership, WaterMeter,
    CustomerWgProfile, PropertyWgProfile, WgFunction,
    # Consent/Opt-in: kunden-gebunden → NACH Customer.
    InvoiceEmailOptInCode, CustomerEmailConsentLog,
    BillingPeriod, MeterReadingAccessCode, WaterTariff, MeterReading,
    MeterReplacement,
    BillingRun, Invoice, InvoiceItem, OpenItem, ReadingCorrection,
    # Touren NACH MeterReplacement + Invoice (Stop-FKs zeigen auf beide).
    MeterTour, MeterTourStop,
    BookingGroup, Booking, Transfer, RealAccountYearBalance,
    # Bankauszug NACH Booking/BookingGroup/Invoice/OpenItem/Account/RealAccount
    # (BankStatementLine referenziert alle). Line VOR Allocation (FK line_id).
    BankStatement, BankStatementLine, BankStatementLineAllocation,
    DunningPolicy, DunningStage, DunningNotice,
    AppSetting, EmailSuppression, InvoiceCounter, CustomerCounter,
    NetworkPlan, NetworkFeature, MaintenanceLog, SpringYield, Incident,
    WaterSample, LabResult,   # WaterSample VOR LabResult (FK water_sample_id)
    # Schriftfuehrung: Meeting-Kinder referenzieren Meeting + Customer.
    # MeetingAgendaItem VOR MeetingResolution (FK agenda_item_id).
    Meeting, MeetingAgendaItem, MeetingInvitation, MeetingDeliveryLog,
    MeetingAttendance, MeetingResolution, MeetingProtocol,
    SchriftverkehrDocument,
    # Rundschreiben: Circular referenziert WaterSample/Incident (beide oben) +
    # Self-FK predecessor_id (zweiter Pass). Kinder referenzieren Circular + Customer.
    Circular, CircularRecipient, CircularDeliveryLog,
    # Note ans Ende: sein polymorphes entity_id zeigt potenziell auf JEDE der
    # obigen Tabellen (Customer/Property/Invoice/Booking). Beim Voll-Ersatz
    # bleiben IDs erhalten → korrekt. Im Merge-Modus wird entity_id NICHT
    # remappt (kein FK-Eintrag unten, da kein einzelnes Zielmodell) — bekannte
    # Einschraenkung: nach einem Merge in eine vorbefuellte DB kann eine Notiz
    # auf die falsche Entitaet zeigen. Voll-Backup/Restore ist der Regelpfad.
    Note,
]


# Spalten, die per Jahr gefiltert werden koennen (fuer "Buchungen"-Kategorie).
# Wert: Spaltenname (Integer-Jahr) ODER ("date_year", "<spalte>") fuer
# Date-/DateTime-Spalten, bei denen das Jahr per EXTRACT gezogen wird.
YEAR_FILTERS = {
    MeterReading: ("date_year", "reading_date"),
    MeterReplacement: ("date_year", "replacement_date"),
    BillingRun: ("date_year", "created_at"),
    Invoice: ("date_year", "date"),
    OpenItem: "period_year",
    ReadingCorrection: ("date_year", "created_at"),
    Booking: ("date_year", "date"),
    Transfer: ("date_year", "date"),
    RealAccountYearBalance: "year",
    InvoiceCounter: "year",
    Incident: ("date_year", "detected_at"),
    WaterSample: ("date_year", "sample_date"),
    # BankStatementLine per Buchungsdatum; BankStatement (Parent) wird immer
    # voll exportiert (billig, haelt statement_id-Refs gueltig), Allocation
    # (Grandchild) kaskadiert ueber die gefilterten Line-IDs (services.py).
    BankStatementLine: ("date_year", "booking_date"),
}


# Natuerliche Schluessel fuer Merge-Modus: Tuple aus Spalten, die einen
# Datensatz eindeutig identifizieren (auch dialektuebergreifend stabil).
# None bedeutet: kein natuerlicher Schluessel — im Merge-Modus immer Insert
# mit neuer ID, kein Update existierender Records.
NATURAL_KEYS = {
    Role: ("name",),                    # name ist unique
    RolePermission: ("role_id", "permission_key"),  # Composite-PK (role_id remapped)
    TaxRate: ("rate",),
    FiscalYear: ("year",),
    Account: ("name",),                 # code ist optional, name ist nullable=False
    RealAccount: ("name",),             # iban ist optional
    Project: ("name",),                 # name ist unique=True
    Customer: ("customer_number",),     # unique; fallback handled in serializer wenn None
    Property: ("object_number",),       # unique; fallback handled in serializer wenn None
    PropertyOwnership: ("property_id", "customer_id", "valid_from"),
    CustomerWgProfile: ("customer_id",),   # 1:1, PK=FK
    PropertyWgProfile: ("property_id",),   # 1:1, PK=FK
    WgFunction: ("customer_id", "function"),
    WaterMeter: ("meter_number",),
    BillingPeriod: ("name",),
    MeterReadingAccessCode: ("customer_id", "billing_period_id"),
    WaterTariff: ("name", "valid_from"),
    MeterReading: ("meter_id", "billing_period_id"),
    MeterReplacement: ("old_meter_id",),  # ein alter Zaehler wird hoechstens einmal ersetzt
    MeterTour: None,                    # kein natuerlicher Schluessel — immer Insert
    MeterTourStop: None,                # Kind einer MeterTour — immer Insert
    BillingRun: None,                   # kein natuerlicher Schluessel
    Invoice: ("invoice_number",),
    InvoiceItem: None,
    OpenItem: None,
    ReadingCorrection: None,
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
    NetworkPlan: None,                  # immer Insert (Voll-Replace ersetzt den Seed-Hauptplan)
    NetworkFeature: None,               # kein natuerlicher Schluessel — immer Insert
    MaintenanceLog: None,
    SpringYield: ("feature_id", "measurement_date"),  # je Quelle hoechstens 1 Messung/Tag
    Incident: None,                     # kein stabiler natuerlicher Schluessel — immer Insert
    WaterSample: None,                  # je Stelle koennen mehrere Befunde/Tag existieren — immer Insert
    LabResult: None,                    # Kind eines WaterSample — immer Insert
    Note: None,                         # Freitext — kein natuerlicher Schluessel, immer Insert
    # Bankauszug: Statement per (Konto, Datei-Hash) dedupliziert (uq_stmt_hash) —
    # sonst wuerde ein Re-Import denselben Auszug doppelt anlegen bzw. den
    # Unique-Constraint verletzen. Zeilen/Allocations sind Kinder → immer Insert.
    BankStatement: ("real_account_id", "file_hash"),
    BankStatementLine: None,
    BankStatementLineAllocation: None,
    # DSGVO-Mail: Opt-in 1:1 pro Kunde, Suppression 1:1 pro Adresse (beide unique);
    # Consent-Log ist append-only → immer Insert.
    InvoiceEmailOptInCode: ("customer_id",),
    CustomerEmailConsentLog: None,
    EmailSuppression: ("email",),
    # Schriftfuehrung: Sitzung + alle Kinder ohne stabilen natuerlichen
    # Schluessel → immer Insert (Voll-Replace ist der Regelpfad).
    Meeting: None,
    MeetingAgendaItem: None,
    MeetingInvitation: None,
    MeetingDeliveryLog: None,
    MeetingAttendance: None,
    MeetingResolution: None,
    MeetingProtocol: None,
    SchriftverkehrDocument: None,
    # Rundschreiben: kein stabiler natuerlicher Schluessel → immer Insert.
    Circular: None,
    CircularRecipient: None,
    CircularDeliveryLog: None,
}


# FK-Spalten je Model: alt-ID (im Export) → neu-ID (nach Insert).
# Wird im Merge-Modus zum Remappen genutzt; im Vollersatz unveraendert.
# Format: {column_name: target_model}
FOREIGN_KEYS = {
    RolePermission: {"role_id": Role},
    PropertyOwnership: {"property_id": Property, "customer_id": Customer},
    InvoiceEmailOptInCode: {"customer_id": Customer},
    CustomerEmailConsentLog: {"customer_id": Customer},
    CustomerWgProfile: {"customer_id": Customer},
    PropertyWgProfile: {"property_id": Property},
    WgFunction: {"customer_id": Customer},
    WaterMeter: {"property_id": Property},
    MeterReadingAccessCode: {"customer_id": Customer, "billing_period_id": BillingPeriod},
    MeterReading: {"meter_id": WaterMeter, "self_service_code_id": MeterReadingAccessCode,
                   "billing_period_id": BillingPeriod},
    MeterReplacement: {"property_id": Property, "old_meter_id": WaterMeter,
                       "new_meter_id": WaterMeter, "billing_period_id": BillingPeriod},
    MeterTourStop: {"tour_id": MeterTour, "meter_id": WaterMeter,
                    "property_id": Property, "replacement_id": MeterReplacement,
                    "invoice_id": Invoice},
    BillingRun: {"billing_period_id": BillingPeriod},
    Invoice: {"customer_id": Customer, "property_id": Property, "billing_run_id": BillingRun,
              "billing_period_id": BillingPeriod},
    InvoiceItem: {"invoice_id": Invoice, "project_id": Project,
                  "dunning_notice_id": DunningNotice,        # zweiter Pass
                  "reading_correction_id": ReadingCorrection},  # zweiter Pass
    OpenItem: {"customer_id": Customer, "invoice_id": Invoice, "account_id": Account},
    ReadingCorrection: {"customer_id": Customer, "meter_id": WaterMeter,
                        "billing_period_id": BillingPeriod,
                        "source_reading_id": MeterReading,
                        "source_invoice_id": Invoice, "applied_invoice_id": Invoice},
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
    NetworkPlan: {"source_plan_id": NetworkPlan},  # Self-FK, zweiter Pass
    NetworkFeature: {"plan_id": NetworkPlan, "property_id": Property,
                     "meter_id": WaterMeter,
                     "source_feature_id": NetworkFeature},  # source_feature_id: Self-FK, zweiter Pass
    MaintenanceLog: {"feature_id": NetworkFeature},
    SpringYield: {"feature_id": NetworkFeature},
    Incident: {"customer_id": Customer, "property_id": Property, "feature_id": NetworkFeature},
    WaterSample: {"feature_id": NetworkFeature},
    LabResult: {"water_sample_id": WaterSample},
    # Bankauszug. Die matched_*/booking_*-FKs sind optional (nullable): im
    # Merge werden sie remappt bzw. auf existierende Ziel-Rows gehaengt.
    BankStatement: {"real_account_id": RealAccount},
    BankStatementLine: {"statement_id": BankStatement, "matched_invoice_id": Invoice,
                        "matched_open_item_id": OpenItem, "matched_customer_id": Customer,
                        "override_account_id": Account, "booking_id": Booking,
                        "booking_group_id": BookingGroup},
    BankStatementLineAllocation: {"line_id": BankStatementLine,
                                  "open_item_id": OpenItem, "account_id": Account},
    # Schriftfuehrung.
    MeetingAgendaItem: {"meeting_id": Meeting},
    MeetingInvitation: {"meeting_id": Meeting, "customer_id": Customer},
    MeetingDeliveryLog: {"meeting_id": Meeting, "customer_id": Customer},
    MeetingAttendance: {"meeting_id": Meeting, "customer_id": Customer},
    MeetingResolution: {"meeting_id": Meeting, "agenda_item_id": MeetingAgendaItem},
    MeetingProtocol: {"meeting_id": Meeting},
    # Rundschreiben.
    Circular: {"water_sample_id": WaterSample, "incident_id": Incident,
               "predecessor_id": Circular},  # predecessor_id: Self-FK, zweiter Pass
    CircularRecipient: {"circular_id": Circular, "customer_id": Customer},
    CircularDeliveryLog: {"circular_id": Circular, "customer_id": Customer},
}


# FK-Spalten, die im ersten Insert-Pass auf NULL gesetzt werden und in einem
# zweiten Pass per UPDATE nachgesetzt werden — noetig wenn das Ziel-Model
# erst spaeter inserted wird (zirkulaere oder Self-FKs).
DEFERRED_FK_UPDATES = {
    Booking: ["storno_of_id"],
    InvoiceItem: ["dunning_notice_id", "reading_correction_id"],
    NetworkPlan: ["source_plan_id"],         # Self-FK
    NetworkFeature: ["source_feature_id"],   # Self-FK
    Circular: ["predecessor_id"],            # Self-FK (Entwarnung → Abkochempfehlung)
}


# Komplett vom Export ausgeschlossene Tabellen (System/Auth, instance-bound,
# Audit-Logs die ohne User-Export sinnlos waeren).
EXCLUDED_TABLES = {
    "users",
    "user_preferences",
    "alembic_version",
    "fiscal_year_reopen_logs",  # Audit-Log mit User-FK; ohne User-Export sinnlos
    "feature_photos",           # Fotos liegen als Dateien im instance-Volume, nicht im JSON-Export
    "incident_photos",          # Fotos liegen als Dateien im instance-Volume; separates FS-Backup noetig (Evidenz!)
    # --- Bewusst NICHT exportiert (ephemer / SaaS-gespeist / Secrets) ---
    "email_events",             # polymorpher Zustell-Audit-Trail (subject_id ohne FK); hochvolumig, im SaaS vom Postmark-Webhook neu gespeist. EmailSuppression (die operative Sperrliste) wird dagegen exportiert.
    "admin_notifications",      # In-App-Hinweise; im SaaS vom Platform-Notification-Stream gespeist, im OSS-Standalone ungenutzt
    "admin_notification_reads", # Pro-User-Lesestatus (User-FK); ohne User-Export sinnlos
    "async_jobs",               # ephemere Hintergrund-Job-Queue; Ergebnisdateien liegen transient im instance-Volume
    "api_keys",                 # nur SHA-256-Hashes; Secrets gehoeren nicht in ein portables Backup, Keys werden neu ausgegeben
    "hydrant_share_links",      # oeffentliche Freigabe-Tokens (Capability-URLs); aus Sicherheitsgruenden neu erzeugen statt transportieren
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
