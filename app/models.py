from datetime import datetime, date
import sqlalchemy as sa
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db, login_manager


# ---------------------------------------------------------------------------
# E-Mail-Versand-Tracking (generisch, mehrere Versandarten)
# ---------------------------------------------------------------------------

class EmailTrackableMixin:
    """Gemeinsame Tracking-Felder fuer alles, was per E-Mail verschickt wird
    (Rechnung, Self-Service-Zugangscode, kuenftig Mahnung ...).

    Subklassen setzen ``EMAIL_SUBJECT_TYPE`` — der Diskriminator, ueber den
    ``EmailEvent`` und die Postmark-Webhook-Zuordnung den Datensatz finden.
    Bei reinem SMTP bleibt ``email_message_id`` NULL und ``last_email_status``
    immer ``"sent"``; ueber Postmark feuern Delivery-/Bounce-Webhooks nach.

    Die sechs Spalten werden als plain ``db.Column`` pro Subklassen-Tabelle
    kopiert (SQLAlchemy-Mixin-Konvention).
    """

    EMAIL_SUBJECT_TYPE = None  # von Subklasse gesetzt

    EMAIL_STATUS_SENT = "sent"
    EMAIL_STATUS_DELIVERED = "delivered"
    EMAIL_STATUS_BOUNCED_HARD = "bounced_hard"
    EMAIL_STATUS_BOUNCED_SOFT = "bounced_soft"
    EMAIL_STATUS_SPAM = "spam_complaint"
    EMAIL_STATUS_FAILED = "failed"

    EMAIL_STATUS_DE = {
        EMAIL_STATUS_SENT: "Versendet",
        EMAIL_STATUS_DELIVERED: "Zugestellt",
        EMAIL_STATUS_BOUNCED_HARD: "Unzustellbar",
        EMAIL_STATUS_BOUNCED_SOFT: "Verzögert",
        EMAIL_STATUS_SPAM: "Als Spam markiert",
        EMAIL_STATUS_FAILED: "Fehlgeschlagen",
    }

    email_message_id = db.Column(db.String(128), nullable=True, index=True)
    email_sent_at = db.Column(db.DateTime, nullable=True)
    email_recipient = db.Column(db.String(255), nullable=True)
    last_email_status = db.Column(db.String(32), nullable=True)
    last_email_status_at = db.Column(db.DateTime, nullable=True)
    last_email_bounce_detail = db.Column(db.String(512), nullable=True)

    @property
    def last_email_status_de(self):
        if not self.last_email_status:
            return None
        return self.EMAIL_STATUS_DE.get(self.last_email_status, self.last_email_status)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class Role(db.Model):
    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.String(255))
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    permissions = db.relationship(
        "RolePermission",
        backref="role",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def permission_keys(self):
        return {p.permission_key for p in self.permissions}

    def has_permission(self, key):
        return self.name == "Admin" or key in self.permission_keys

    def __repr__(self):
        return f"<Role {self.name}>"


class RolePermission(db.Model):
    __tablename__ = "role_permissions"

    role_id = db.Column(
        db.Integer,
        db.ForeignKey("roles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_key = db.Column(db.String(50), primary_key=True)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role_id = db.Column(
        db.Integer,
        db.ForeignKey("roles.id"),
        nullable=False,
    )
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Einladungs-Flow (SaaS-Feature, Logik liegt in der SaaS-Schicht):
    # invited_at  -> Zeitpunkt der (letzten) Einladung; NULL = nie eingeladen.
    # invitation_accepted_at -> gesetzt, sobald der User per Link sein Passwort
    # gesetzt und sich aktiviert hat. "Einladung ausstehend" :=
    # invited_at IS NOT NULL AND invitation_accepted_at IS NULL.
    invited_at = db.Column(db.DateTime, nullable=True)
    invitation_accepted_at = db.Column(db.DateTime, nullable=True)

    role = db.relationship("Role", lazy="joined")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role is not None and self.role.name == "Admin"

    def has_permission(self, key):
        return self.role is not None and self.role.has_permission(key)

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class UserPreference(db.Model):
    """Pro Benutzer gespeicherte Key/Value-Einstellung (z.B. per_page pro Listenseite)."""
    __tablename__ = "user_preferences"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = db.Column(db.String(80), nullable=False)
    value = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("user_id", "key", name="uq_user_preferences_user_key"),
    )


# ---------------------------------------------------------------------------
# Kunden
# ---------------------------------------------------------------------------

class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    customer_number = db.Column(db.Integer, unique=True, nullable=True)    # fortlaufende Kundennummer (nur fuer Kunden vergeben)
    externe_kennung = db.Column(db.String(100), nullable=True)             # optionale externe Kennung
    name = db.Column(db.String(200), nullable=False)
    # Aufgespaltener Name fuer die Brief-/Rechnungsanrede. ``name`` bleibt das
    # kombinierte Sortier-/Listen-/Suchfeld (Konvention "Nachname Vorname") und
    # wird beim Speichern aus last_name + first_name abgeleitet; die Einzelfelder
    # speisen ``letter_name`` (Anschrift) und ``salutation_line`` (Anrede).
    # Firmen haben nur ``name`` (Firmenname), keine Vor-/Nachnamen, keine Anrede.
    salutation = db.Column(db.String(10))    # "Herr" | "Frau" | "Familie" | None
    first_name = db.Column(db.String(100))   # Vorname (Person)
    last_name = db.Column(db.String(100))    # Nachname (Person/Familie)
    is_company = db.Column(db.Boolean, nullable=False, default=False,
                           server_default=sa.false())
    is_customer = db.Column(db.Boolean, default=True, nullable=False)
    is_supplier = db.Column(db.Boolean, default=False, nullable=False)
    strasse = db.Column(db.String(200))
    hausnummer = db.Column(db.String(20))
    plz = db.Column(db.String(10))
    ort = db.Column(db.String(100))
    land = db.Column(db.String(100), default="Österreich")
    email = db.Column(db.String(120))
    rechnung_per_email = db.Column(db.Boolean, default=False, nullable=False)
    phone = db.Column(db.String(50))
    member_since = db.Column(db.Date)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    base_fee_override = db.Column(db.Numeric(10, 2), nullable=True)       # überschreibt Tarif-Grundgebühr
    additional_fee_override = db.Column(db.Numeric(10, 2), nullable=True)  # überschreibt Tarif-Zusatzgebühr

    invoices = db.relationship("Invoice", backref="customer", lazy="dynamic")
    ownerships = db.relationship("PropertyOwnership", backref="customer", lazy="dynamic")
    # WG-spezifisch (Mandant-Typ Wassergenossenschaft): 1:1-Profil + mehrwertige
    # Funktionen. Cascade delete-orphan, damit beim Hard-Delete eines Kontakts
    # auch Profil und Funktionen verschwinden.
    wg_profile = db.relationship("CustomerWgProfile", uselist=False,
                                 back_populates="customer", cascade="all, delete-orphan")
    wg_functions = db.relationship("WgFunction", back_populates="customer",
                                   cascade="all, delete-orphan", order_by="WgFunction.id")

    @property
    def wants_email(self):
        """Master-Gate fuer jeden Kunden-Mailversand (Rechnung, Mahnung,
        Zaehlerablesungs-Zugangscode): True nur, wenn der Kunde den
        Schriftverkehr per E-Mail aktiviert hat UND eine Adresse hinterlegt
        ist. ``rechnung_per_email`` ist die Einwilligung, ``email`` die
        technische Voraussetzung — beides muss zutreffen, sonst wird der
        Mailversand weder angeboten noch durchgefuehrt."""
        return bool(self.email) and bool(self.rechnung_per_email)

    def address_display(self):
        parts = []
        street = " ".join(filter(None, [self.strasse, self.hausnummer]))
        if street:
            parts.append(street)
        city = " ".join(filter(None, [self.plz, self.ort]))
        if city:
            parts.append(city)
        if self.land and self.land != "Österreich":
            parts.append(self.land)
        return ", ".join(parts)

    @property
    def letter_name(self):
        """Name fuer die Anschrift in Briefen/Rechnungen: 'Vorname Nachname'
        bei Personen, 'Familie Nachname' bei Familien, Firmenname bei Firmen.
        Faellt auf das kombinierte ``name`` zurueck, solange Vor-/Nachname noch
        nicht gepflegt sind (Altbestand, Quick-Create, kombinierter Import)."""
        if self.is_company:
            return self.name
        if self.salutation == "Familie" and self.last_name:
            return f"Familie {self.last_name}"
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return full or self.name

    @property
    def salutation_line(self):
        """Komplette Anredezeile fuer Briefe/Mails (ohne abschliessendes Komma).

        Herr/Frau/Familie sprechen formell nur mit dem Nachnamen an; bei
        unbekannter Anrede wird geschlechtsneutral mit dem vollen Namen
        gegruesst, Firmen mit der Sammelanrede."""
        if self.is_company:
            return "Sehr geehrte Damen und Herren"
        if self.salutation == "Herr" and self.last_name:
            return f"Sehr geehrter Herr {self.last_name}"
        if self.salutation == "Frau" and self.last_name:
            return f"Sehr geehrte Frau {self.last_name}"
        if self.salutation == "Familie" and self.last_name:
            return f"Sehr geehrte Familie {self.last_name}"
        full = " ".join(p for p in (self.first_name, self.last_name) if p)
        return f"Sehr geehrte/r {full or self.name}"

    @property
    def wg_status(self):
        """Gespeicherter WG-Status (prospect|member|resigned); Default
        'member', solange kein Profil existiert (jeder Kontakt gilt als
        Mitglied, bis er ausdruecklich anders gesetzt wird)."""
        return self.wg_profile.status if self.wg_profile else "member"

    @property
    def wg_member_until(self):
        return self.wg_profile.member_until if self.wg_profile else None

    def function_keys(self):
        """Set der Funktions-Keys dieses Kontakts (siehe app.wg.FUNCTION_LABELS)."""
        return {f.function for f in self.wg_functions}

    def has_paid_shares(self):
        """True, wenn der Kontakt aktuell (aktive Ownership) mindestens eine
        Liegenschaft mit >=1 Anteil besitzt — Basis fuer den Mitglied-Vorschlag
        (hybrid). Dialekt-portabel via JOIN, kein dialektspezifisches SQL."""
        return db.session.query(PropertyOwnership.id).join(
            PropertyWgProfile,
            PropertyWgProfile.property_id == PropertyOwnership.property_id,
        ).filter(
            PropertyOwnership.customer_id == self.id,
            PropertyOwnership.valid_to.is_(None),
            PropertyWgProfile.shares > 0,
        ).first() is not None

    def ensure_wg_profile(self):
        """Liefert das WG-Profil; legt es bei Bedarf an (noch nicht committed)."""
        if self.wg_profile is None:
            self.wg_profile = CustomerWgProfile(status="member")
        return self.wg_profile

    def __repr__(self):
        return f"<Customer {self.name}>"


# ---------------------------------------------------------------------------
# Objekte (Liegenschaften)
# ---------------------------------------------------------------------------

class Property(db.Model):
    __tablename__ = "properties"

    TYPES = ["Haus", "Garten", "Sonstiges"]

    id = db.Column(db.Integer, primary_key=True)
    object_number = db.Column(db.String(50), unique=True, nullable=True)
    object_type = db.Column(db.String(50), nullable=False)  # Haus / Garten / Sonstiges
    strasse = db.Column(db.String(200))
    hausnummer = db.Column(db.String(20))
    plz = db.Column(db.String(10))
    ort = db.Column(db.String(100))
    land = db.Column(db.String(100), default="Österreich")
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    base_fee_override = db.Column(db.Numeric(10, 2), nullable=True)       # überschreibt Kunden-/Tarif-Grundgebühr
    additional_fee_override = db.Column(db.Numeric(10, 2), nullable=True)  # überschreibt Kunden-/Tarif-Zusatzgebühr

    meters = db.relationship("WaterMeter", backref="property", lazy="dynamic",
                             cascade="all, delete-orphan")
    ownerships = db.relationship("PropertyOwnership", backref="property", lazy="dynamic",
                                 order_by="PropertyOwnership.valid_from.desc()")
    invoices = db.relationship("Invoice", backref="property", lazy="dynamic")
    # WG-spezifisch (Mandant-Typ Wassergenossenschaft): Anteile + m2.
    wg_profile = db.relationship("PropertyWgProfile", uselist=False,
                                 back_populates="property", cascade="all, delete-orphan")

    def current_owner(self):
        return (
            PropertyOwnership.query
            .filter_by(property_id=self.id, valid_to=None)
            .first()
        )

    def address_display(self):
        parts = []
        street = " ".join(filter(None, [self.strasse, self.hausnummer]))
        if street:
            parts.append(street)
        city = " ".join(filter(None, [self.plz, self.ort]))
        if city:
            parts.append(city)
        if self.land and self.land != "Österreich":
            parts.append(self.land)
        return ", ".join(parts)

    def label(self):
        if self.object_number:
            return f"{self.object_number} – {self.address_display()}"
        return self.address_display() or f"Objekt #{self.id}"

    @property
    def wg_shares(self):
        """Anzahl Anteile in der Genossenschaft (0, solange kein Profil)."""
        return self.wg_profile.shares if self.wg_profile else 0

    @property
    def wg_area_m2(self):
        return self.wg_profile.area_m2 if self.wg_profile else None

    def ensure_wg_profile(self):
        """Liefert das WG-Profil; legt es bei Bedarf an (noch nicht committed)."""
        if self.wg_profile is None:
            self.wg_profile = PropertyWgProfile(shares=0)
        return self.wg_profile

    def __repr__(self):
        return f"<Property {self.label()}>"


class PropertyOwnership(db.Model):
    __tablename__ = "property_ownerships"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    valid_from = db.Column(db.Date, nullable=False)
    valid_to = db.Column(db.Date, nullable=True)

    def __repr__(self):
        return f"<PropertyOwnership property={self.property_id} customer={self.customer_id}>"


# ---------------------------------------------------------------------------
# Wassergenossenschaft (Mandant-Typ-spezifisch — 1:1-Profile + Funktionen)
# ---------------------------------------------------------------------------

class CustomerWgProfile(db.Model):
    """WG-spezifisches 1:1-Profil zu einem Kontakt (nur im Mandant-Typ
    Wassergenossenschaft befuellt). Skalare Mitglieds-Daten; die mehrwertigen
    Funktionen liegen in ``WgFunction``. ``member_since`` bleibt aus
    Kompatibilitaet auf ``Customer`` (Kundenauswertung nutzt es)."""
    __tablename__ = "customer_wg_profiles"

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), primary_key=True)
    # prospect | member | resigned  (siehe app.wg.STATUS_LABELS); Default = member
    status = db.Column(db.String(20), nullable=False, default="member",
                       server_default=db.text("'member'"))
    member_until = db.Column(db.Date, nullable=True)

    customer = db.relationship("Customer", back_populates="wg_profile")

    def __repr__(self):
        return f"<CustomerWgProfile customer={self.customer_id} status={self.status}>"


class WgFunction(db.Model):
    """Vorstands-/Pruef-Funktion eines Mitglieds (mehrwertig, 1:n zu Customer).
    ``function`` ist einer der Keys aus ``app.wg.FUNCTION_LABELS``."""
    __tablename__ = "wg_functions"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    function = db.Column(db.String(40), nullable=False)

    __table_args__ = (db.UniqueConstraint("customer_id", "function", name="uq_wg_function"),)

    customer = db.relationship("Customer", back_populates="wg_functions")

    def __repr__(self):
        return f"<WgFunction customer={self.customer_id} {self.function}>"


class PropertyWgProfile(db.Model):
    """WG-spezifisches 1:1-Profil zu einer Liegenschaft: Anteile + Quadratmeter
    (die m2 bestimmen die Anteils-Anzahl). Nur im WG-Modus befuellt."""
    __tablename__ = "property_wg_profiles"

    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), primary_key=True)
    shares = db.Column(db.Integer, nullable=False, default=0, server_default=db.text("0"))
    area_m2 = db.Column(db.Integer, nullable=True)

    property = db.relationship("Property", back_populates="wg_profile")

    def __repr__(self):
        return f"<PropertyWgProfile property={self.property_id} shares={self.shares}>"


# ---------------------------------------------------------------------------
# Wasserzähler
# ---------------------------------------------------------------------------

class WaterMeter(db.Model):
    __tablename__ = "water_meters"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=False)
    meter_number = db.Column(db.String(100), unique=True, nullable=False)
    location = db.Column(db.String(200))
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    installed_from = db.Column(db.Date, nullable=True)   # Einbaudatum
    installed_to = db.Column(db.Date, nullable=True)     # Ausbaudatum
    initial_value = db.Column(db.Numeric(12, 3), nullable=True)  # Stand bei Einbau
    eichjahr = db.Column(db.Integer, nullable=True)              # Eichjahr des Zählers
    # Klassifizierung Hauptzaehler ("main") vs. Subzaehler ("sub").
    # Subzaehler koennen optional auf einen Hauptzaehler via parent_meter_id
    # zeigen (max. eine Ebene; parent muss meter_type='main' sein -- wird in
    # der Route validiert, kein DB-Constraint, weil portabel ueber drei Dialekte).
    meter_type = db.Column(
        db.String(10), nullable=False,
        default="main", server_default=db.text("'main'"),
    )
    parent_meter_id = db.Column(
        db.Integer,
        db.ForeignKey("water_meters.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    readings = db.relationship("MeterReading", backref="meter", lazy="dynamic",
                               cascade="all, delete-orphan",
                               order_by="MeterReading.reading_date.desc()")

    parent_meter = db.relationship(
        "WaterMeter", remote_side="WaterMeter.id",
        backref=db.backref("sub_meters", lazy="dynamic"),
        foreign_keys=[parent_meter_id],
    )

    def last_reading(self):
        return self.readings.order_by(
            MeterReading.reading_date.desc(), MeterReading.id.desc()
        ).first()

    def is_main(self):
        return self.meter_type == "main"

    def is_sub(self):
        return self.meter_type == "sub"

    def type_label(self):
        return "Subzähler" if self.is_sub() else "Hauptzähler"

    def type_badge_class(self):
        return "bg-info text-white" if self.is_sub() else "bg-secondary text-white"

    def __repr__(self):
        return f"<WaterMeter {self.meter_number}>"


class MeterReading(db.Model):
    __tablename__ = "meter_readings"

    id = db.Column(db.Integer, primary_key=True)
    meter_id = db.Column(db.Integer, db.ForeignKey("water_meters.id"), nullable=False)
    billing_period_id = db.Column(
        db.Integer, db.ForeignKey("billing_periods.id"), nullable=False, index=True,
    )
    reading_date = db.Column(db.Date, nullable=False, default=date.today)
    value = db.Column(db.Numeric(12, 3), nullable=False)  # m³
    consumption = db.Column(db.Numeric(12, 3))             # m³ Verbrauch (berechnet)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Self-Service-Marker (SaaS): Eintrag stammt vom Kunden via Zugangscode.
    # created_by_id bleibt in dem Fall NULL (kein User), self_service_code_id
    # zeigt auf den genutzten Code (oder NULL, wenn der Code spaeter geloescht wurde).
    entered_via_self_service = db.Column(
        db.Boolean, default=False, nullable=False, index=True,
        server_default=db.text("false"),
    )
    self_service_code_id = db.Column(
        db.Integer,
        db.ForeignKey("meter_reading_access_codes.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    billing_period = db.relationship("BillingPeriod")

    __table_args__ = (
        db.UniqueConstraint("meter_id", "billing_period_id",
                            name="uq_meter_reading_period"),
    )

    def __repr__(self):
        return f"<MeterReading {self.meter.meter_number} {self.reading_date}: {self.value}>"


class MeterReplacement(db.Model):
    """Explizites Zaehlertausch-Event: alt->neu-Paarung + Snapshot der
    Tausch-Metadaten. Ersetzt die fruehere Datums-Heuristik (alter Zaehler
    ``active=False`` mit ``installed_to == neuer.installed_from`` am selben
    Objekt), die bei zwei am selben Tag am selben Objekt getauschten Zaehlern
    nicht aufloesbar war. ``property_id`` ist redundant zu
    ``old_meter.property_id``, wird aber direkt gehalten -> Per-Objekt-Abfragen
    ohne Join. ``final_value`` / ``new_initial_value`` sind Snapshots zum
    Tauschzeitpunkt (Audit/Backfill); die Live-Anzeige nutzt weiterhin die
    Ablesungen in ``meter_readings``."""
    __tablename__ = "meter_replacements"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(
        db.Integer, db.ForeignKey("properties.id"), nullable=False, index=True)
    # ondelete RESTRICT: ein dokumentierter Tausch darf nicht verwaisen.
    # Zaehler werden ohnehin soft-deleted (active=False) statt hart geloescht;
    # meter_delete bekommt zusaetzlich einen Guard (freundlicher Flash statt 500).
    old_meter_id = db.Column(
        db.Integer, db.ForeignKey("water_meters.id", ondelete="RESTRICT"),
        nullable=False, unique=True)
    new_meter_id = db.Column(
        db.Integer, db.ForeignKey("water_meters.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    billing_period_id = db.Column(
        db.Integer, db.ForeignKey("billing_periods.id"), nullable=False, index=True)
    replacement_date = db.Column(db.Date, nullable=False)
    final_value = db.Column(db.Numeric(12, 3), nullable=True)        # Endstand alt
    new_initial_value = db.Column(db.Numeric(12, 3), nullable=True)  # Anfangsstand neu
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    old_meter = db.relationship("WaterMeter", foreign_keys=[old_meter_id])
    new_meter = db.relationship("WaterMeter", foreign_keys=[new_meter_id])
    property = db.relationship("Property")
    billing_period = db.relationship("BillingPeriod")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return (f"<MeterReplacement old={self.old_meter_id} "
                f"new={self.new_meter_id} {self.replacement_date}>")


class MeterReadingAccessCode(EmailTrackableMixin, db.Model):
    """SaaS-Self-Service: Kurzer Zugangscode pro Kunde+Abrechnungsperiode
    fuer die Zaehlerstands-Selbsteingabe ohne User-Account.

    Klartext-Code (`code`) wird gespeichert, damit der Admin Briefe
    nachdrucken kann; Hash (`code_hash`) ist der Anker fuer den
    Constant-Time-Login-Check. Tenant-Schema-isoliert.

    Erbt ``EmailTrackableMixin`` → E-Mail-Versand-Status wird wie bei
    Rechnungen getrackt (Sent-Event + Postmark-Webhooks).
    """
    __tablename__ = "meter_reading_access_codes"

    EMAIL_SUBJECT_TYPE = "access_code"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    billing_period_id = db.Column(
        db.Integer, db.ForeignKey("billing_periods.id"), nullable=False, index=True,
    )
    code = db.Column(db.String(16), nullable=False)        # Format XXXX-XXXX
    code_hash = db.Column(db.String(255), nullable=False)
    expires_at = db.Column(db.Date, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    last_used_ip = db.Column(db.String(64), nullable=True)
    failed_attempts = db.Column(
        db.Integer, default=0, nullable=False, server_default=db.text("0"),
    )
    locked_until = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_via = db.Column(db.String(20), nullable=True)     # 'email' | 'letter'
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    customer = db.relationship(
        "Customer",
        backref=db.backref(
            "meter_access_codes",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    billing_period = db.relationship("BillingPeriod")
    # Polymorphe E-Mail-Events (kein FK → viewonly, Aufraeumen explizit beim Loeschen).
    email_events = db.relationship(
        "EmailEvent",
        primaryjoin=(
            "and_(foreign(EmailEvent.subject_id) == MeterReadingAccessCode.id, "
            "EmailEvent.subject_type == 'access_code')"
        ),
        order_by="(EmailEvent.occurred_at.desc(), EmailEvent.id.desc())",
        viewonly=True,
        lazy="select",
    )

    __table_args__ = (
        db.UniqueConstraint("customer_id", "billing_period_id",
                            name="uq_mrac_customer_period"),
        db.Index("ix_mrac_period_revoked_expires",
                 "billing_period_id", "revoked_at", "expires_at"),
    )

    @property
    def is_valid(self):
        if self.revoked_at:
            return False
        if self.expires_at < date.today():
            return False
        if self.locked_until and self.locked_until > datetime.utcnow():
            return False
        return True

    @property
    def has_been_used(self):
        return self.last_used_at is not None

    def __repr__(self):
        return (f"<MeterReadingAccessCode customer={self.customer_id} "
                f"period={self.billing_period_id}>")


class InvoiceEmailOptInCode(db.Model):
    """SaaS-Self-Service: Persistenter, kunden-gebundener Code, der (in einem
    zweiten Schritt) auf der gedruckten Rechnung steht — als Magic-Link + QR.

    Der Kunde meldet damit den elektronischen Rechnungsversand selbst an.
    Anders als ``MeterReadingAccessCode`` ist dieser Code **kunden-gebunden,
    persistent und laeuft NICHT ab** (er steht auf Papier, das der Kunde
    behaelt). Sicherheits-Anker ist der **Double-Opt-In per Mail**; der Code
    selbst verhindert nur Kunden-Enumeration. ``code`` enthaelt eine
    Luhn-mod-32-Pruefziffer (Format ``XXXX-XXXX-K``), die Tippfehler abfaengt,
    bevor ueberhaupt die DB befragt wird. ``code_hash`` dient als
    Revocation-Anker fuer die signierten Verwaltungs-Tokens. Tenant-isoliert.
    """
    __tablename__ = "invoice_email_optin_codes"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    code = db.Column(db.String(20), nullable=False)        # Format XXXX-XXXX-K
    code_hash = db.Column(db.String(255), nullable=False)
    failed_attempts = db.Column(
        db.Integer, default=0, nullable=False, server_default=db.text("0"),
    )
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    customer = db.relationship(
        "Customer",
        backref=db.backref(
            "invoice_email_optin_code",
            uselist=False,
            cascade="all, delete-orphan",
        ),
    )
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<InvoiceEmailOptInCode customer={self.customer_id}>"


class CustomerEmailConsentLog(db.Model):
    """Append-only DSGVO-Nachweis fuer Einwilligung/Abmeldung zum elektronischen
    Rechnungsversand. Eine Zeile pro Aktion. Tenant-isoliert.
    """
    __tablename__ = "customer_email_consent_log"

    # erlaubte Werte fuer `action`
    OPT_IN = "opt_in_confirmed"
    UNSUBSCRIBED = "unsubscribed"
    EMAIL_CHANGED = "email_changed"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    action = db.Column(db.String(32), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    consent_text_version = db.Column(db.String(20), nullable=True)
    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    customer = db.relationship(
        "Customer",
        backref=db.backref(
            "email_consent_log",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )

    def __repr__(self):
        return (f"<CustomerEmailConsentLog customer={self.customer_id} "
                f"{self.action}>")


class AdminNotification(db.Model):
    """Tenant-interne In-App-Benachrichtigung an die Tenant-Admins.

    Lebt im **Tenant-Schema** (nicht in der Platform-DB) — der Platform-Admin
    sieht sie deshalb nicht. Wird in der SaaS-Glocke neben den
    Platform-Notifications eingemischt (siehe saas/notifications). Lesestatus
    pro User in ``AdminNotificationRead`` (mehr-Admin-faehig).
    """
    __tablename__ = "admin_notifications"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(50), nullable=False)   # z.B. 'invoice_email_optin'
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False, default="")
    level = db.Column(
        db.String(20), nullable=False, default="info", server_default="info",
    )
    link_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True,
    )

    reads = db.relationship(
        "AdminNotificationRead",
        backref="notification",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<AdminNotification {self.kind} #{self.id}>"


class AdminNotificationRead(db.Model):
    """Pro-User-Lesestatus fuer ``AdminNotification``."""
    __tablename__ = "admin_notification_reads"

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(
        db.Integer,
        db.ForeignKey("admin_notifications.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    read_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint(
            "notification_id", "user_id", name="uq_admin_notif_read_user",
        ),
    )

    def __repr__(self):
        return (f"<AdminNotificationRead notif={self.notification_id} "
                f"user={self.user_id}>")


# ---------------------------------------------------------------------------
# Abrechnungsperioden
# ---------------------------------------------------------------------------

class BillingPeriod(db.Model):
    """Abrechnungsperiode — zentraler Gruppierungsschluessel fuer Zaehler-
    ablesungen, Zaehlertausche und Rechnungslaeufe (ersetzt die fruehere
    Kalenderjahr-Verdrahtung). ``start_date``/``end_date`` halten das
    "Von/Bis" der Periode (z.B. Juni–Juni).

    Es ist immer genau eine Periode aktiv — applikationsseitig erzwungen
    (kein portabler Partial-Index ueber SQLite/MySQL/Postgres). ``activate``
    setzt alle anderen inaktiv; ``current`` liefert die aktive Periode.
    """
    __tablename__ = "billing_periods"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)   # z.B. "2025/26"
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    active = db.Column(
        db.Boolean, default=False, nullable=False,
        server_default=db.text("false"),
    )
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def current(cls):
        """Die aktuell aktive Abrechnungsperiode (oder ``None``)."""
        return cls.query.filter_by(active=True).first()

    def activate(self):
        """Setzt diese Periode aktiv und alle anderen inaktiv.

        Caller ist fuer ``db.session.commit()`` zustaendig.
        """
        q = BillingPeriod.query
        if self.id is not None:
            q = q.filter(BillingPeriod.id != self.id)
        q.update({BillingPeriod.active: False}, synchronize_session=False)
        self.active = True

    def __repr__(self):
        return f"<BillingPeriod {self.name}>"


# ---------------------------------------------------------------------------
# Tarife
# ---------------------------------------------------------------------------

class WaterTariff(db.Model):
    __tablename__ = "water_tariffs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    valid_from = db.Column(db.Integer, nullable=False)   # Jahr
    valid_to = db.Column(db.Integer)                      # Jahr (None = aktuell gültig)
    base_fee = db.Column(db.Numeric(10, 2))               # Grundgebühr €; None = keine Position auf Rechnung
    base_fee_label = db.Column(db.String(100), default="Grundgebühr")
    additional_fee = db.Column(db.Numeric(10, 2))          # Zusatzgebühr €; None = keine Position auf Rechnung
    additional_fee_label = db.Column(db.String(100), default="Zusatzgebühr")
    price_per_m3 = db.Column(db.Numeric(10, 4), nullable=False)  # Preis pro m³ (4 Nachkommastellen)
    notes = db.Column(db.Text)

    def __repr__(self):
        return f"<WaterTariff {self.name} {self.valid_from}>"


# ---------------------------------------------------------------------------
# Rechnungsläufe
# ---------------------------------------------------------------------------

class BillingRun(db.Model):
    """Historisierter Rechnungslauf – gespeichert bei jeder Massenabrechnung."""
    __tablename__ = "billing_runs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    billing_period_id = db.Column(
        db.Integer, db.ForeignKey("billing_periods.id"), nullable=False,
    )

    # Snapshot des verwendeten Tarifs (historische Kopie)
    tariff_name = db.Column(db.String(100), nullable=False)
    tariff_valid_from = db.Column(db.Integer, nullable=True)
    tariff_valid_to = db.Column(db.Integer, nullable=True)
    tariff_base_fee = db.Column(db.Numeric(10, 2), nullable=True)
    tariff_base_fee_label = db.Column(db.String(100), nullable=True)
    tariff_additional_fee = db.Column(db.Numeric(10, 2), nullable=True)
    tariff_additional_fee_label = db.Column(db.String(100), nullable=True)
    tariff_price_per_m3 = db.Column(db.Numeric(10, 4), nullable=False)
    tariff_notes = db.Column(db.Text, nullable=True)

    invoices_created = db.Column(db.Integer, default=0, nullable=False)
    invoices_skipped = db.Column(db.Integer, default=0, nullable=False)
    sort_order = db.Column(db.String(20), nullable=True)

    SORT_ORDER_CHOICES = [
        ("customer_name",   "Kundenname"),
        ("customer_number", "Kundennummer"),
        ("object_number",   "Objektnummer"),
        ("address",         "Objektadresse (Straße, Hausnummer)"),
    ]

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    invoices = db.relationship("Invoice", backref="billing_run", lazy="dynamic")
    billing_period = db.relationship("BillingPeriod")

    def __repr__(self):
        return f"<BillingRun {self.billing_period_id} {self.created_at}>"


# ---------------------------------------------------------------------------
# Rechnungen
# ---------------------------------------------------------------------------

class Invoice(EmailTrackableMixin, db.Model):
    __tablename__ = "invoices"

    EMAIL_SUBJECT_TYPE = "invoice"

    STATUS_DRAFT = "Entwurf"
    STATUS_SENT = "Versendet"
    STATUS_PAID = "Bezahlt"
    STATUS_CANCELLED = "Storniert"
    STATUS_CREDIT = "Guthaben"
    ALL_STATUSES = [STATUS_DRAFT, STATUS_SENT, STATUS_PAID, STATUS_CANCELLED, STATUS_CREDIT]

    # Erlaubte manuelle Statuswechsel (Dropdown auf der Detailseite via
    # ``invoices.set_status``). Steuert den Lebenszyklus einer Rechnung:
    #   Entwurf  → Versendet            (Entwurf wird sonst gelöscht, nicht storniert)
    #   Versendet→ Bezahlt/Guthaben/Storniert
    #   Bezahlt  → Versendet/Guthaben/Storniert  (Korrekturen)
    #   Guthaben → Versendet/Bezahlt/Storniert
    #   Storniert→ (terminal, kein Wechsel mehr)
    # Ein Zurücksetzen auf 'Entwurf' ist nie erlaubt. Die ``pay``-Route folgt
    # ihrer eigenen Betrags-Logik und ist von dieser Tabelle unberührt.
    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT: [STATUS_SENT],
        STATUS_SENT: [STATUS_PAID, STATUS_CREDIT, STATUS_CANCELLED],
        STATUS_PAID: [STATUS_SENT, STATUS_CREDIT, STATUS_CANCELLED],
        STATUS_CREDIT: [STATUS_SENT, STATUS_PAID, STATUS_CANCELLED],
        STATUS_CANCELLED: [],
    }

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=True)
    billing_run_id = db.Column(db.Integer, db.ForeignKey("billing_runs.id"), nullable=True)
    billing_period_id = db.Column(db.Integer, db.ForeignKey("billing_periods.id"), nullable=True)
    date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default=STATUS_DRAFT)
    total_amount = db.Column(db.Numeric(10, 2), default=0)
    pdf_path = db.Column(db.String(500))
    doc_path = db.Column(db.String(500))   # gecachte .docx für gesperrte Rechnungen
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # E-Mail-Versand-Tracking-Spalten kommen aus EmailTrackableMixin.

    items = db.relationship("InvoiceItem", backref="invoice", lazy="select",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    bookings = db.relationship("Booking", backref="invoice", lazy="dynamic")
    billing_period = db.relationship("BillingPeriod")
    # Polymorphe E-Mail-Events (kein FK mehr → viewonly, Aufraeumen explizit).
    # Tiebreaker auf id desc — siehe Kommentar in invoices.email_events-Route.
    email_events = db.relationship(
        "EmailEvent",
        primaryjoin=(
            "and_(foreign(EmailEvent.subject_id) == Invoice.id, "
            "EmailEvent.subject_type == 'invoice')"
        ),
        order_by="(EmailEvent.occurred_at.desc(), EmailEvent.id.desc())",
        viewonly=True,
        lazy="select",
    )

    def recalculate_total(self):
        """Berechnet den Bruttobetrag (netto + USt) und speichert ihn in ``total_amount``.

        Positionen speichern den Nettobetrag. Ist für eine Position kein Steuersatz
        gesetzt, entspricht ihr Brutto- dem Nettobetrag (nicht umsatzsteuerpflichtig).

        **ADR-003:** Items mit ``is_dunning_fee=True`` werden IGNORIERT –
        ``total_amount`` enthält ausschließlich die Hauptforderung.
        """
        from decimal import Decimal
        gross = Decimal("0")
        for item in self.items:
            if getattr(item, "is_dunning_fee", 0):
                continue
            net = Decimal(str(item.amount or 0))
            gross += net
            if item.tax_rate and item.tax_rate > 0:
                rate = Decimal(str(item.tax_rate))
                gross += (net * rate / Decimal("100")).quantize(Decimal("0.01"))
        self.total_amount = gross

    @property
    def principal_total(self):
        """Hauptforderung = Summe der Nicht-Fee-Items (brutto). Entspricht ``total_amount``."""
        from decimal import Decimal
        gross = Decimal("0")
        for item in self.items:
            if getattr(item, "is_dunning_fee", 0):
                continue
            net = Decimal(str(item.amount or 0))
            gross += net
            if item.tax_rate and item.tax_rate > 0:
                rate = Decimal(str(item.tax_rate))
                gross += (net * rate / Decimal("100")).quantize(Decimal("0.01"))
        return gross

    @property
    def dunning_fee_total(self):
        """Summe aller aktiven Mahngebühr-Items."""
        from decimal import Decimal
        return sum(
            (Decimal(str(item.amount or 0)) for item in self.items if getattr(item, "is_dunning_fee", 0)),
            Decimal("0"),
        )

    @property
    def gross_total_with_fees(self):
        """Hauptforderung + alle Mahngebühren (für Mahn-PDF)."""
        return self.principal_total + self.dunning_fee_total

    @property
    def net_total(self):
        """Nettosumme (Summe der Positionsbeträge ohne USt)."""
        from decimal import Decimal
        return sum((Decimal(str(item.amount or 0)) for item in self.items
                     if not getattr(item, "is_dunning_fee", 0)), Decimal("0"))

    @property
    def tax_breakdown(self):
        """Aufschlüsselung der USt pro Satz als OrderedDict ``{rate: {"net", "tax"}}``."""
        from collections import OrderedDict
        from decimal import Decimal
        summary = OrderedDict()
        for item in self.items:
            if getattr(item, "is_dunning_fee", 0):
                continue
            rate = item.tax_rate
            if not rate or rate <= 0:
                continue
            rate_key = Decimal(str(rate))
            net = Decimal(str(item.amount or 0))
            tax = (net * rate_key / Decimal("100")).quantize(Decimal("0.01"))
            if rate_key not in summary:
                summary[rate_key] = {"net": Decimal("0"), "tax": Decimal("0")}
            summary[rate_key]["net"] += net
            summary[rate_key]["tax"] += tax
        return summary

    @property
    def paid_amount(self):
        from sqlalchemy import func
        from app.extensions import db
        result = db.session.query(func.sum(Booking.amount)).filter(
            Booking.invoice_id == self.id
        ).scalar()
        return result or 0

    @property
    def open_balance(self):
        from decimal import Decimal
        return Decimal(str(self.total_amount or 0)) - Decimal(str(self.paid_amount))

    @property
    def consumption(self):
        from decimal import Decimal
        return sum(
            (item.quantity or Decimal("0"))
            for item in self.items
            if item.unit == "m³"
        ) or None

    def __repr__(self):
        return f"<Invoice {self.invoice_number}>"


class EmailEvent(db.Model):
    """Audit-Trail der E-Mail-Zustellungsereignisse — polymorph ueber
    ``subject_type``/``subject_id`` (z.B. ``invoice`` oder ``access_code``).

    Im OSS-Standalone-Betrieb (SMTP) entsteht nur ein "Sent"-Event beim
    Versand. Im SaaS-Betrieb über Postmark feuert die Platform-Webhook
    zusätzlich Delivery-/Bounce-/SpamComplaint-Events nach.

    Idempotenz: Postmark retried Webhooks bei 5xx-Antworten. Der UNIQUE-
    Constraint (postmark_message_id, record_type) sorgt dafür, dass derselbe
    Event nicht doppelt landet. Mehrere unterschiedliche Record-Types zur
    selben MessageID sind erlaubt (Soft- → Hard-Bounce-Eskalation).

    Kein FK auf die Subjekt-Tabelle (waere bei Polymorphie nicht eindeutig) —
    Loeschen des Subjekts muss die Events explizit mit aufraeumen.
    """

    __tablename__ = "email_events"

    id = db.Column(db.Integer, primary_key=True)
    subject_type = db.Column(db.String(32), nullable=False)  # invoice, access_code, ...
    subject_id = db.Column(db.Integer, nullable=False)
    record_type = db.Column(db.String(32), nullable=False)  # Sent, Delivery, Bounce, SpamComplaint
    postmark_message_id = db.Column(db.String(128), nullable=True, index=True)
    recipient = db.Column(db.String(255), nullable=True)
    occurred_at = db.Column(db.DateTime, nullable=False)
    received_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    bounce_type = db.Column(db.String(64), nullable=True)  # HardBounce, SoftBounce, Transient ...
    description = db.Column(db.String(512), nullable=True)
    payload_json = db.Column(db.Text, nullable=True)  # roher Postmark-Payload für Debugging

    __table_args__ = (
        db.UniqueConstraint(
            "postmark_message_id",
            "record_type",
            name="uq_email_events_msgid_type",
        ),
        db.Index("ix_email_events_subject", "subject_type", "subject_id"),
    )

    def __repr__(self):
        return (f"<EmailEvent {self.subject_type}={self.subject_id} "
                f"type={self.record_type}>")


# Registry: Diskriminator → Model. Wird von der Platform-Webhook und den
# E-Mail-Verlauf-Views genutzt, um aus (subject_type, subject_id) den Datensatz
# aufzuloesen. Neue Mail-Versandarten hier eintragen.
EMAIL_SUBJECT_MODELS = {
    Invoice.EMAIL_SUBJECT_TYPE: Invoice,
    MeterReadingAccessCode.EMAIL_SUBJECT_TYPE: MeterReadingAccessCode,
}


def resolve_email_subject(session, subject_type, subject_id):
    """Laedt das E-Mail-Subjekt fuer (subject_type, subject_id) oder None."""
    model = EMAIL_SUBJECT_MODELS.get(subject_type)
    if model is None:
        return None
    return session.get(model, subject_id)


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    quantity = db.Column(db.Numeric(12, 3), default=1)
    unit = db.Column(db.String(20), default="Stk")
    unit_price = db.Column(db.Numeric(10, 4), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=True)  # MwSt in %; None = keine MwSt
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True)

    # Mahnwesen (ADR-003): Mahngebühr-Items
    is_dunning_fee = db.Column(db.Integer, default=0, nullable=False)
    dunning_notice_id = db.Column(db.Integer, db.ForeignKey("dunning_notices.id"), nullable=True)

    project = db.relationship("Project", foreign_keys=[project_id])

    def __repr__(self):
        return f"<InvoiceItem {self.description}>"


# ---------------------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------------------

class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(3), unique=True, nullable=True)  # 3-stelliges Kürzel (A-Z, 0-9)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    closed = db.Column(db.Boolean, default=False, nullable=False)
    color = db.Column(db.String(20), nullable=True, default="#3498db")

    bookings = db.relationship("Booking", backref="project", lazy="dynamic")

    def __repr__(self):
        return f"<Project {self.name}>"


# ---------------------------------------------------------------------------
# Steuersätze
# ---------------------------------------------------------------------------

class TaxRate(db.Model):
    __tablename__ = "tax_rates"

    id = db.Column(db.Integer, primary_key=True)
    rate = db.Column(db.Numeric(5, 2), nullable=False, unique=True)  # z. B. 0, 10, 13, 20
    label = db.Column(db.String(100), nullable=True)  # optionale Bezeichnung

    def __repr__(self):
        return f"<TaxRate {self.rate}%>"


# ---------------------------------------------------------------------------
# Buchhaltung
# ---------------------------------------------------------------------------

class RealAccount(db.Model):
    """Reales Bankkonto (z. B. Girokonto, Kreditkonto)."""
    __tablename__ = "real_accounts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(200))
    iban = db.Column(db.String(34))
    opening_balance = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    active = db.Column(db.Boolean, default=True)
    icon = db.Column(db.String(50), nullable=True, default="fa-university")
    is_default = db.Column(db.Boolean, default=False, nullable=False)

    bookings = db.relationship("Booking", backref="real_account", lazy="dynamic")

    def __repr__(self):
        return f"<RealAccount {self.name}>"


class Transfer(db.Model):
    """Umbuchung zwischen zwei Bankkonten (keine Einnahme/Ausgabe)."""
    __tablename__ = "transfers"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today, nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    from_real_account_id = db.Column(db.Integer, db.ForeignKey("real_accounts.id"), nullable=False)
    to_real_account_id = db.Column(db.Integer, db.ForeignKey("real_accounts.id"), nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    from_account = db.relationship("RealAccount", foreign_keys=[from_real_account_id],
                                   backref=db.backref("outgoing_transfers", lazy="dynamic"))
    to_account = db.relationship("RealAccount", foreign_keys=[to_real_account_id],
                                 backref=db.backref("incoming_transfers", lazy="dynamic"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<Transfer {self.date} {self.amount}>"


class RealAccountYearBalance(db.Model):
    """Gespeicherter Jahresabschluss-Kontostand eines Bankkontos."""
    __tablename__ = "real_account_year_balances"

    id = db.Column(db.Integer, primary_key=True)
    real_account_id = db.Column(db.Integer, db.ForeignKey("real_accounts.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    closing_balance = db.Column(db.Numeric(10, 2), nullable=False)

    real_account = db.relationship("RealAccount", backref=db.backref("year_balances", lazy="dynamic"))

    __table_args__ = (db.UniqueConstraint("real_account_id", "year", name="uq_real_account_year"),)

    def __repr__(self):
        return f"<RealAccountYearBalance {self.real_account_id} year={self.year} bal={self.closing_balance}>"


class Account(db.Model):
    __tablename__ = "accounts"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(3), unique=True, nullable=True)  # 3-stelliges Kürzel (A-Z, 0-9)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)

    bookings = db.relationship("Booking", backref="account", lazy="dynamic")

    def __repr__(self):
        return f"<Account {self.name}>"


class Booking(db.Model):
    __tablename__ = "bookings"

    STATUS_OFFEN = "Offen"
    STATUS_VERBUCHT = "Verbucht"
    STATUS_STORNIERT = "Storniert"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today, nullable=False)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)  # positiv = Einnahme, negativ = Ausgabe
    description = db.Column(db.String(500), nullable=False)
    reference = db.Column(db.String(100))   # Belegnummer
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    open_item_id = db.Column(db.Integer, db.ForeignKey("open_items.id"), nullable=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True)
    # Sammelbuchung: optionaler Header-Verweis. Wenn gesetzt, gehört diese
    # Buchung zu einer BookingGroup (siehe ADR-002). Storno/Edit-Operationen
    # laufen dann ausschliesslich über die Gruppe, nie einzeln.
    group_id = db.Column(db.Integer, db.ForeignKey("booking_groups.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), nullable=False, default="Offen")
    real_account_id = db.Column(db.Integer, db.ForeignKey("real_accounts.id"), nullable=True)
    storno_of_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True)
    storno_reason = db.Column(db.String(500), nullable=True)
    storno_date = db.Column(db.Date, nullable=True)

    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    tax_rate = db.Column(db.Numeric(5, 2), nullable=True)  # MwSt in %; None = keine MwSt
    customer = db.relationship("Customer", foreign_keys=[customer_id], backref=db.backref("bookings", lazy="dynamic"))

    storno_of = db.relationship("Booking", remote_side="Booking.id", foreign_keys="Booking.storno_of_id", backref=db.backref("storno_buchung", uselist=False))

    def __repr__(self):
        return f"<Booking {self.date} {self.amount}>"


class BookingGroup(db.Model):
    """Sammelbuchung — Header-Entität über mehreren ``Booking``-Kindern (ADR-002).

    Die buchhalterisch wirksamen Werte stehen ausschliesslich auf den Kindern
    (``Booking``). Der Header ist reine Metadaten- und Gruppierungsebene,
    damit eine Rechnung mit mehreren Steuersätzen / Konten / Projekten in der
    Buchungsliste gemeinsam sichtbar ist und gemeinsam storniert werden kann.
    """
    __tablename__ = "booking_groups"

    STATUS_AKTIV = "Aktiv"
    STATUS_STORNIERT = "Storniert"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, default=date.today, nullable=False)
    description = db.Column(db.String(500), nullable=False)
    reference = db.Column(db.String(100))              # Belegnummer
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    # denormalisierte Summe aller Kinder — via ``recompute_group_total`` aktuell halten
    total_amount = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    status = db.Column(db.String(20), default=STATUS_AKTIV, nullable=False)
    storno_reason = db.Column(db.String(500), nullable=True)
    storno_date = db.Column(db.Date, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship("Invoice", foreign_keys=[invoice_id],
                              backref=db.backref("booking_groups", lazy="dynamic"))
    customer = db.relationship("Customer", foreign_keys=[customer_id],
                               backref=db.backref("booking_groups", lazy="dynamic"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    children = db.relationship(
        "Booking",
        backref=db.backref("group", foreign_keys="Booking.group_id"),
        foreign_keys="Booking.group_id",
        lazy="select",
        order_by="Booking.id",
    )

    def __repr__(self):
        return f"<BookingGroup {self.date} {self.total_amount}>"


# ---------------------------------------------------------------------------
# Offene Posten (manuell angelegt)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Buchungsjahre
# ---------------------------------------------------------------------------

class FiscalYear(db.Model):
    __tablename__ = "fiscal_years"

    year = db.Column(db.Integer, primary_key=True)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    closed = db.Column(db.Boolean, default=False, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)
    closed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    is_vat_liable = db.Column(db.Boolean, default=False, nullable=False)  # umsatzsteuerpflichtig

    closed_by = db.relationship("User", foreign_keys=[closed_by_id])
    reopen_logs = db.relationship(
        "FiscalYearReopenLog", backref="fiscal_year_obj",
        lazy="dynamic", order_by="FiscalYearReopenLog.reopened_at.desc()",
    )

    def __repr__(self):
        return f"<FiscalYear {self.year} {'geschlossen' if self.closed else 'offen'}>"


class FiscalYearReopenLog(db.Model):
    __tablename__ = "fiscal_year_reopen_logs"

    id = db.Column(db.Integer, primary_key=True)
    fiscal_year_id = db.Column(db.Integer, db.ForeignKey("fiscal_years.year"), nullable=False)
    reopened_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    reopened_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.String(1000), nullable=False)

    reopened_by = db.relationship("User", foreign_keys=[reopened_by_id])

    def __repr__(self):
        return f"<FiscalYearReopenLog {self.fiscal_year_id} by {self.reopened_by_id}>"


class InvoiceCounter(db.Model):
    """Laufender Rechnungsnummer-Zähler pro Jahr."""
    __tablename__ = "invoice_counters"

    year = db.Column(db.Integer, primary_key=True)
    next_seq = db.Column(db.Integer, nullable=False, default=1)

    def __repr__(self):
        return f"<InvoiceCounter {self.year} next={self.next_seq}>"


class CustomerCounter(db.Model):
    """Laufender Kundennummer-Zähler (Singleton-Row, id=1)."""
    __tablename__ = "customer_counters"

    id = db.Column(db.Integer, primary_key=True, default=1)
    next_seq = db.Column(db.Integer, nullable=False, default=1)

    def __repr__(self):
        return f"<CustomerCounter next={self.next_seq}>"


class OpenItem(db.Model):
    __tablename__ = "open_items"

    STATUS_OPEN = "Offen"
    STATUS_PARTIAL = "Teilbezahlt"
    STATUS_PAID = "Bezahlt"
    STATUS_CREDIT = "Gutschrift"
    ALL_STATUSES = [STATUS_OPEN, STATUS_PARTIAL, STATUS_PAID, STATUS_CREDIT]

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    notes = db.Column(db.Text)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    date = db.Column(db.Date, default=date.today, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    period_year = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default=STATUS_OPEN, nullable=False)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", backref=db.backref("open_items", lazy="dynamic"))
    invoice = db.relationship("Invoice", backref=db.backref("open_item", uselist=False))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    account = db.relationship("Account", foreign_keys=[account_id])
    bookings = db.relationship("Booking", backref="open_item", lazy="dynamic",
                               foreign_keys="Booking.open_item_id")

    @property
    def paid_amount(self):
        from sqlalchemy import func
        from app.extensions import db as _db
        result = _db.session.query(func.sum(Booking.amount)).filter(
            Booking.open_item_id == self.id
        ).scalar()
        return result or 0

    @property
    def open_balance(self):
        from decimal import Decimal
        return Decimal(str(self.amount or 0)) - Decimal(str(self.paid_amount))

    def __repr__(self):
        return f"<OpenItem {self.description} {self.amount}>"


# ---------------------------------------------------------------------------
# Anwendungseinstellungen (Key-Value-Speicher)
# ---------------------------------------------------------------------------

class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        if row is None:
            db.session.add(cls(key=key, value=value))
        else:
            row.value = value

    def __repr__(self):
        return f"<AppSetting {self.key}>"


# ---------------------------------------------------------------------------
# Mahnwesen (ADR-003)
# ---------------------------------------------------------------------------

class DunningPolicy(db.Model):
    """Mahnvorlage – definiert einen benannten Satz von Mahnstufen."""
    __tablename__ = "dunning_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    is_default = db.Column(db.Boolean, default=False, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    stages = db.relationship("DunningStage", backref="policy", lazy="select",
                             order_by="DunningStage.level",
                             cascade="all, delete-orphan")

    def __repr__(self):
        return f"<DunningPolicy {self.name}>"


class DunningStage(db.Model):
    """Einzelne Mahnstufe innerhalb einer Policy."""
    __tablename__ = "dunning_stages"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("dunning_policies.id"), nullable=False)
    level = db.Column(db.Integer, nullable=False)              # 1, 2, 3, …
    name = db.Column(db.String(100), nullable=False)           # z.B. "1. Mahnung"
    days_after_due = db.Column(db.Integer, nullable=False)     # Tage nach Fälligkeit
    fee_fixed = db.Column(db.Numeric(10, 2), default=0)       # fixe Mahngebühr
    fee_percent = db.Column(db.Numeric(5, 2), default=0)      # prozentuale Gebühr
    fee_min = db.Column(db.Numeric(10, 2), nullable=True)     # Minimum bei %-Berechnung
    fee_max = db.Column(db.Numeric(10, 2), nullable=True)     # Maximum bei %-Berechnung
    new_due_days = db.Column(db.Integer, default=14)           # Nachfrist in Tagen
    print_title = db.Column(db.String(200), nullable=True)     # Titel auf Mahn-PDF
    email_subject = db.Column(db.String(200), nullable=True)   # Betreff der Mahn-Mail
    email_body = db.Column(db.Text, nullable=True)             # Mailtext (Platzhalter erlaubt)
    letter_intro = db.Column(db.Text, nullable=True)           # Einleitung im Mahnbrief (PDF/DOCX)
    letter_closing = db.Column(db.Text, nullable=True)         # Schlusstext im Mahnbrief (PDF/DOCX)
    color = db.Column(db.String(20), nullable=True)            # Badge-Farbe
    icon = db.Column(db.String(50), nullable=True)             # Font Awesome Icon
    active = db.Column(db.Boolean, default=True, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("policy_id", "level", name="uq_policy_level"),
    )

    def __repr__(self):
        return f"<DunningStage L{self.level} {self.name}>"


class DunningNotice(EmailTrackableMixin, db.Model):
    """Einzelne Mahnung zu einer Rechnung."""
    __tablename__ = "dunning_notices"

    EMAIL_SUBJECT_TYPE = "dunning"

    STATUS_AKTIV = "Aktiv"
    STATUS_ZURUECKGESETZT = "Zurückgesetzt"
    STATUS_STORNIERT = "Storniert"
    ALL_STATUSES = [STATUS_AKTIV, STATUS_ZURUECKGESETZT, STATUS_STORNIERT]

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    stage_id = db.Column(db.Integer, db.ForeignKey("dunning_stages.id"), nullable=True)

    # Snapshot-Felder (werden bei Erzeugung aus Stage kopiert, nie über FK lesen)
    level_snapshot = db.Column(db.Integer, nullable=False)
    name_snapshot = db.Column(db.String(100), nullable=False)
    print_title_snapshot = db.Column(db.String(200), nullable=True)

    issued_date = db.Column(db.Date, default=date.today, nullable=False)
    new_due_date = db.Column(db.Date, nullable=True)
    fee_amount = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    fee_invoice_item_id = db.Column(db.Integer, db.ForeignKey("invoice_items.id"), nullable=True)

    status = db.Column(db.String(20), default=STATUS_AKTIV, nullable=False)
    sent_via = db.Column(db.String(20), nullable=True)        # "email", "post", "pdf"
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_to = db.Column(db.String(200), nullable=True)        # E-Mail-Adresse oder "Post"
    pdf_path = db.Column(db.String(500), nullable=True)
    doc_path = db.Column(db.String(500), nullable=True)

    reset_at = db.Column(db.DateTime, nullable=True)
    reset_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reset_reason = db.Column(db.String(500), nullable=True)

    notes = db.Column(db.Text, nullable=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship("Invoice", backref=db.backref("dunning_notices", lazy="dynamic"))
    stage = db.relationship("DunningStage", foreign_keys=[stage_id])
    fee_invoice_item = db.relationship("InvoiceItem", foreign_keys=[fee_invoice_item_id])
    reset_by = db.relationship("User", foreign_keys=[reset_by_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<DunningNotice invoice={self.invoice_id} L{self.level_snapshot}>"


# DunningNotice nachtraeglich registrieren: EMAIL_SUBJECT_MODELS ist oben (vor
# dieser Klasse) definiert, damit Invoice/AccessCode dort stehen. Mahnungen
# tracken E-Mails ueber denselben polymorphen Webhook-Pfad (subject_type='dunning').
EMAIL_SUBJECT_MODELS[DunningNotice.EMAIL_SUBJECT_TYPE] = DunningNotice


# ---------------------------------------------------------------------------
# Bank-Auszuege (Import + Matching)
# ---------------------------------------------------------------------------

class BankStatement(db.Model):
    __tablename__ = "bank_statements"

    STATUS_PENDING = "pending"
    STATUS_COMMITTED = "committed"
    STATUS_PARTIAL = "partial"

    FORMAT_CAMT053 = "camt053"
    FORMAT_MT940 = "mt940"
    FORMAT_MT942 = "mt942"

    id = db.Column(db.Integer, primary_key=True)
    format = db.Column(db.String(20), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    file_hash = db.Column(db.String(64), nullable=False, index=True)
    real_account_id = db.Column(db.Integer, db.ForeignKey("real_accounts.id"), nullable=False)
    statement_reference = db.Column(db.String(100))
    booking_date_from = db.Column(db.Date)
    booking_date_to = db.Column(db.Date)
    opening_balance = db.Column(db.Numeric(12, 2))
    closing_balance = db.Column(db.Numeric(12, 2))
    currency = db.Column(db.String(3), default="EUR")
    status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    committed_at = db.Column(db.DateTime, nullable=True)

    real_account = db.relationship("RealAccount", foreign_keys=[real_account_id])
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])
    lines = db.relationship(
        "BankStatementLine",
        backref="statement",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="BankStatementLine.line_index",
    )

    __table_args__ = (
        db.UniqueConstraint("real_account_id", "file_hash", name="uq_stmt_hash"),
    )

    def __repr__(self):
        return f"<BankStatement {self.id} {self.format} {self.filename}>"


class BankStatementLine(db.Model):
    __tablename__ = "bank_statement_lines"

    MATCH_INVOICE_NUMBER = "invoice_number"
    MATCH_NAME = "name"
    MATCH_MANUAL = "manual"

    STATUS_PENDING = "pending"
    STATUS_COMMITTED = "committed"
    STATUS_SKIPPED = "skipped"

    id = db.Column(db.Integer, primary_key=True)
    statement_id = db.Column(db.Integer, db.ForeignKey("bank_statements.id"), nullable=False)
    line_index = db.Column(db.Integer, nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    value_date = db.Column(db.Date)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    currency = db.Column(db.String(3), default="EUR")
    counterparty_name = db.Column(db.String(200))
    counterparty_iban = db.Column(db.String(34))
    purpose = db.Column(db.Text)
    end_to_end_id = db.Column(db.String(100))
    tx_id = db.Column(db.String(100))

    matched_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    matched_open_item_id = db.Column(db.Integer, db.ForeignKey("open_items.id"), nullable=True)
    matched_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True)
    match_type = db.Column(db.String(20))

    override_account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=True)
    selected = db.Column(db.Boolean, default=False, nullable=False)
    line_status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True)
    booking_group_id = db.Column(db.Integer, db.ForeignKey("booking_groups.id"), nullable=True)

    matched_invoice = db.relationship("Invoice", foreign_keys=[matched_invoice_id])
    matched_open_item = db.relationship("OpenItem", foreign_keys=[matched_open_item_id])
    matched_customer = db.relationship("Customer", foreign_keys=[matched_customer_id])
    override_account = db.relationship("Account", foreign_keys=[override_account_id])
    booking = db.relationship("Booking", foreign_keys=[booking_id])
    booking_group = db.relationship("BookingGroup", foreign_keys=[booking_group_id])

    def __repr__(self):
        return f"<BankStatementLine {self.id} {self.booking_date} {self.amount}>"


# ---------------------------------------------------------------------------
# Technik / Wasserleitungsplan (Kartierung)
# ---------------------------------------------------------------------------

class NetworkPlan(db.Model):
    """Benannter Leitungsplan — Container fuer ``NetworkFeature``-Annotationen.

    Erlaubt mehrere parallele Plaene (z.B. operativer Hauptplan + Planungs-
    Sandkasten). Eine Kopie merkt sich ihren Ursprung in ``source_plan_id``,
    sodass ``technik.plan_merge`` die in der Kopie vorgenommenen Aenderungen
    (Geometrie/Sachdaten, inkl. Loeschungen) in den Quellplan zurueckspiegeln
    kann — man plant also in der Kopie, ohne den Hauptplan zu editieren.

    ``maintenance_enabled`` schaltet die Wartungs-/Pruef-Funktion je Plan; nur
    Plaene mit ``status='aktiv'`` UND ``maintenance_enabled`` treiben die
    Dashboard-Erinnerung „Faellige Pruefungen" (siehe ``services.inspections_due``).
    """
    __tablename__ = "network_plans"

    STATUS_DRAFT = "entwurf"
    STATUS_ACTIVE = "aktiv"
    STATUS_ARCHIVED = "archiviert"
    STATUSES = (STATUS_DRAFT, STATUS_ACTIVE, STATUS_ARCHIVED)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(
        db.String(20), nullable=False,
        default=STATUS_DRAFT, server_default=db.text("'entwurf'"),
    )
    maintenance_enabled = db.Column(
        db.Boolean, nullable=False, default=True, server_default=sa.true(),
    )
    description = db.Column(db.Text, nullable=True)

    # Herkunft einer Kopie (fuer „Aenderungen in den Quellplan uebertragen").
    source_plan_id = db.Column(
        db.Integer, db.ForeignKey("network_plans.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    features = db.relationship(
        "NetworkFeature", backref="plan", lazy="selectin",
        cascade="all, delete-orphan",
        foreign_keys="NetworkFeature.plan_id",
    )
    source_plan = db.relationship("NetworkPlan", remote_side=[id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])

    def feature_count(self):
        return len(self.features)

    def __repr__(self):
        return f"<NetworkPlan #{self.id} {self.name!r} {self.status}>"


class NetworkFeature(db.Model):
    """Annotation im Wasserleitungsplan — Punkt (Hydrant, Schieber, Quelle,
    Behaelter, Verteiler, Pumpe, Hausanschluss, Probenahmestelle) oder Linie
    (Versorgungs-/Haupt-/Ring-/Hausanschlussleitung).

    Geometrie wird als GeoJSON-Geometry-Objekt in ``geometry`` (Text) gehalten —
    dialekt-portabel (SQLite/MariaDB/Postgres), bewusst kein PostGIS. Punkte
    werden zusaetzlich nach ``lat``/``lng`` denormalisiert (schnelles
    Marker-Rendering ohne JSON-Parse), Linien bekommen ``length_m`` (Haversine,
    beim Speichern berechnet) als Basis fuer die Netzstatistik.
    """
    __tablename__ = "network_features"

    GEOMETRY_POINT = "point"
    GEOMETRY_LINE = "line"

    ACCURACY_ESTIMATED = "geschaetzt"
    ACCURACY_GOOD = "gut"
    ACCURACY_EXACT = "exakt"

    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(
        db.Integer, db.ForeignKey("network_plans.id"),
        nullable=False, index=True,
    )
    geometry_kind = db.Column(db.String(10), nullable=False)   # 'point' | 'line'
    feature_type = db.Column(db.String(40), nullable=False)    # hydrant, schieber, versorgungsleitung, ...
    name = db.Column(db.String(200), nullable=True)

    geometry = db.Column(db.Text, nullable=False)              # GeoJSON-Geometry (Point/LineString)
    lat = db.Column(db.Float, nullable=True)                   # nur Punkte
    lng = db.Column(db.Float, nullable=True)                   # nur Punkte
    length_m = db.Column(db.Float, nullable=True)              # nur Linien (Haversine)

    accuracy = db.Column(
        db.String(20), nullable=False,
        default=ACCURACY_ESTIMATED, server_default=db.text("'geschaetzt'"),
    )
    material = db.Column(db.String(60), nullable=True)
    dimension_dn = db.Column(db.Integer, nullable=True)
    year_built = db.Column(db.Integer, nullable=True)

    # Technische Detailfelder (auch aus WLK-Notizen extrahierbar, siehe
    # services.parse_note_fields): Fabrikat, Einbautiefe, GOK-Hoehe, Druckstufe.
    manufacturer = db.Column(db.String(120), nullable=True)         # Fabrikat (z. B. HAWLE)
    installation_depth_m = db.Column(db.Float, nullable=True)       # Einbautiefe in m
    ground_level_m = db.Column(db.Float, nullable=True)             # GOK-Hoehe (Gelaendeoberkante) in m ue. A.
    pressure_rating = db.Column(db.String(20), nullable=True)       # Druckstufe (z. B. "PN 10")

    notes = db.Column(db.Text, nullable=True)

    # Verknuepfung mit bestehenden Stammdaten (Hausanschluss -> Objekt/Zaehler).
    property_id = db.Column(
        db.Integer, db.ForeignKey("properties.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    meter_id = db.Column(
        db.Integer, db.ForeignKey("water_meters.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    # Abstammung fuer den Plan-Merge: zeigt auf das Quell-Feature im Quellplan,
    # aus dem dieses Feature beim Kopieren entstand. ``None`` = in dieser Kopie
    # neu gezeichnet (wird beim Merge im Quellplan neu angelegt + zurueckverlinkt).
    source_feature_id = db.Column(
        db.Integer, db.ForeignKey("network_features.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    linked_property = db.relationship("Property", foreign_keys=[property_id])
    linked_meter = db.relationship("WaterMeter", foreign_keys=[meter_id])

    photos = db.relationship(
        "FeaturePhoto", backref="feature", lazy="selectin",
        cascade="all, delete-orphan",
        order_by="FeaturePhoto.uploaded_at.asc()",
    )
    maintenance_logs = db.relationship(
        "MaintenanceLog", backref="feature", lazy="selectin",
        cascade="all, delete-orphan",
        order_by="MaintenanceLog.date.desc()",
    )

    def is_line(self):
        return self.geometry_kind == self.GEOMETRY_LINE

    def is_point(self):
        return self.geometry_kind == self.GEOMETRY_POINT

    def label(self):
        return self.name or f"{self.feature_type} #{self.id}"

    def __repr__(self):
        return f"<NetworkFeature {self.geometry_kind}:{self.feature_type} #{self.id}>"


class MaintenanceLog(db.Model):
    """Wartungs-/Pruefprotokoll-Eintrag zu einer NetworkFeature
    (Hydrantenspuelung, Schieber-Funktionspruefung, Inspektion ...).

    ``next_due`` treibt die Dashboard-Erinnerung „Faellige Pruefungen": der
    jeweils juengste Log je Feature mit ``next_due <= heute`` gilt als faellig.
    """
    __tablename__ = "maintenance_logs"

    KIND_FLUSH = "spuelung"
    KIND_FUNCTION_TEST = "funktionspruefung"
    KIND_MAINTENANCE = "wartung"
    KIND_INSPECTION = "inspektion"
    KIND_OTHER = "sonstiges"

    RESULT_OK = "ok"
    RESULT_DEFECT = "mangel"

    id = db.Column(db.Integer, primary_key=True)
    feature_id = db.Column(
        db.Integer, db.ForeignKey("network_features.id"),
        nullable=False, index=True,
    )
    date = db.Column(db.Date, nullable=False)
    kind = db.Column(
        db.String(30), nullable=False,
        default=KIND_INSPECTION, server_default=db.text("'inspektion'"),
    )
    result = db.Column(db.String(20), nullable=True)           # ok | mangel
    next_due = db.Column(db.Date, nullable=True)
    interval_months = db.Column(db.Integer, nullable=True)
    performed_by = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    def __repr__(self):
        return f"<MaintenanceLog feature={self.feature_id} {self.date} {self.kind}>"


class FeaturePhoto(db.Model):
    """Foto-Dokumentation zu einer NetworkFeature. Die Datei liegt im
    tenant-spezifischen Upload-Ordner (siehe ``technik.services.technik_upload_dir``);
    die DB haelt nur Metadaten — analog dazu, dass Rechnungs-PDFs ausserhalb der
    DB im instance-Volume liegen.
    """
    __tablename__ = "feature_photos"

    id = db.Column(db.Integer, primary_key=True)
    feature_id = db.Column(
        db.Integer, db.ForeignKey("network_features.id"),
        nullable=False, index=True,
    )
    filename = db.Column(db.String(255), nullable=False)        # gespeicherter Dateiname (UUID)
    original_name = db.Column(db.String(255), nullable=True)
    content_type = db.Column(db.String(80), nullable=True)
    caption = db.Column(db.String(255), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )

    def __repr__(self):
        return f"<FeaturePhoto feature={self.feature_id} {self.filename}>"


# ---------------------------------------------------------------------------
# Schriftführung (Mandant-Typ Wassergenossenschaft — Sitzungen, Einladungen,
# Protokolle, Beschlüsse, Schriftverkehr)
# ---------------------------------------------------------------------------

class Meeting(db.Model):
    """Vorstandssitzung (board) oder Hauptversammlung (assembly).

    Beide Sitzungsarten teilen sich diese Tabelle; ``meeting_type`` ist der
    Diskriminator (Unterschied nur in der Empfänger-Vorauswahl beim Versand).
    Lebenszyklus: ``planning`` → ``invited`` (mind. einmal versendet) → ``held``.
    """
    __tablename__ = "meetings"

    TYPE_BOARD = "board"          # Vorstandssitzung
    TYPE_ASSEMBLY = "assembly"    # Hauptversammlung
    TYPES = (TYPE_BOARD, TYPE_ASSEMBLY)

    STATUS_PLANNING = "planning"  # Planung
    STATUS_INVITED = "invited"    # Eingeladen
    STATUS_HELD = "held"          # Abgehalten
    STATUSES = (STATUS_PLANNING, STATUS_INVITED, STATUS_HELD)

    id = db.Column(db.Integer, primary_key=True)
    meeting_type = db.Column(db.String(20), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    meeting_date = db.Column(db.Date, nullable=True)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)
    location = db.Column(db.String(200), nullable=True)
    intro_text = db.Column(db.Text, nullable=True)     # sanitisiertes Rich-Text-HTML
    closing_text = db.Column(db.Text, nullable=True)   # sanitisiertes Rich-Text-HTML
    status = db.Column(db.String(20), nullable=False, default=STATUS_PLANNING,
                       server_default=db.text("'planning'"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    agenda_items = db.relationship(
        "MeetingAgendaItem", backref="meeting", lazy="select",
        cascade="all, delete-orphan", order_by="MeetingAgendaItem.position",
    )
    invitations = db.relationship(
        "MeetingInvitation", backref="meeting", lazy="select",
        cascade="all, delete-orphan",
    )
    delivery_logs = db.relationship(
        "MeetingDeliveryLog", backref="meeting", lazy="select",
        cascade="all, delete-orphan", order_by="MeetingDeliveryLog.occurred_at.desc()",
    )
    attendances = db.relationship(
        "MeetingAttendance", backref="meeting", lazy="select",
        cascade="all, delete-orphan",
    )
    resolutions = db.relationship(
        "MeetingResolution", backref="meeting", lazy="select",
        cascade="all, delete-orphan", order_by="MeetingResolution.id",
    )
    protocol = db.relationship(
        "MeetingProtocol", backref="meeting", uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_assembly(self):
        return self.meeting_type == self.TYPE_ASSEMBLY

    @property
    def can_delete(self):
        """Vor dem ersten Versand löschbar; danach bleibt sie erhalten (History)."""
        return self.status == self.STATUS_PLANNING

    def __repr__(self):
        return f"<Meeting #{self.id} {self.meeting_type} {self.title!r}>"


class MeetingAgendaItem(db.Model):
    """Tagesordnungspunkt (TOP) einer Sitzung."""
    __tablename__ = "meeting_agenda_items"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # TOP, über den abgestimmt wird → belegt das Protokoll mit einem Beschluss vor.
    requires_vote = db.Column(db.Boolean, nullable=False, default=False,
                              server_default=sa.false())

    def __repr__(self):
        return f"<MeetingAgendaItem #{self.id} m={self.meeting_id} pos={self.position}>"


class MeetingInvitation(EmailTrackableMixin, db.Model):
    """Einladung je Empfänger zu einer Sitzung.

    Erbt die E-Mail-Tracking-Felder (Versand-/Zustellstatus, Postmark-Webhook) —
    analog zur Rechnung. ``delivery_method`` haelt fest, wie dieser Empfänger
    informiert werden soll; E-Mail nur, wenn ``Customer.wants_email``.
    """
    __tablename__ = "meeting_invitations"

    EMAIL_SUBJECT_TYPE = "meeting_invitation"

    METHOD_EMAIL = "email"
    METHOD_POST = "post"
    METHOD_NONE = "none"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    delivery_method = db.Column(db.String(10), nullable=True)   # email | post | none
    post_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # E-Mail-Tracking-Spalten kommen aus EmailTrackableMixin.

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    # Polymorphe E-Mail-Events (kein FK → viewonly), analog Invoice.email_events.
    email_events = db.relationship(
        "EmailEvent",
        primaryjoin=(
            "and_(foreign(EmailEvent.subject_id) == MeetingInvitation.id, "
            "EmailEvent.subject_type == 'meeting_invitation')"
        ),
        order_by="(EmailEvent.occurred_at.desc(), EmailEvent.id.desc())",
        viewonly=True,
        lazy="select",
    )

    __table_args__ = (
        db.UniqueConstraint("meeting_id", "customer_id", name="uq_meeting_invitation"),
    )

    def __repr__(self):
        return f"<MeetingInvitation m={self.meeting_id} c={self.customer_id}>"


class MeetingDeliveryLog(db.Model):
    """History, wer wie und wann zu einer Sitzung informiert wurde (inkl.
    erneuter Versände + Post). Ergänzt das Mail-Status-Tracking von
    ``MeetingInvitation`` um die Aktionen des Schriftführers und um Post."""
    __tablename__ = "meeting_delivery_logs"

    METHOD_EMAIL = "email"
    METHOD_POST = "post"

    ACTION_SENT = "sent"
    ACTION_RESENT = "resent"
    ACTION_PRINTED = "printed"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"),
                            nullable=True)
    recipient_name = db.Column(db.String(200), nullable=True)    # Snapshot
    recipient_email = db.Column(db.String(255), nullable=True)   # Snapshot
    method = db.Column(db.String(10), nullable=False)            # email | post
    action = db.Column(db.String(20), nullable=False)            # sent | resent | printed
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    note = db.Column(db.String(255), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<MeetingDeliveryLog m={self.meeting_id} {self.method}/{self.action}>"


class MeetingAttendance(db.Model):
    """Anwesenheit je Person am Sitzungstag (für Teilnehmerliste + Quorum)."""
    __tablename__ = "meeting_attendances"

    STATUS_PRESENT = "present"     # anwesend
    STATUS_EXCUSED = "excused"     # entschuldigt
    STATUS_ABSENT = "absent"       # abwesend
    STATUSES = (STATUS_PRESENT, STATUS_EXCUSED, STATUS_ABSENT)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_PRESENT,
                       server_default=db.text("'present'"))
    is_member = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.false())
    # Stimmgewicht/Anteile (für anteilsgewichtetes Quorum; Default 1 = Kopfzählung).
    weight = db.Column(db.Integer, nullable=False, default=1, server_default=db.text("1"))
    note = db.Column(db.String(255), nullable=True)

    customer = db.relationship("Customer", foreign_keys=[customer_id])

    __table_args__ = (
        db.UniqueConstraint("meeting_id", "customer_id", name="uq_meeting_attendance"),
    )

    def __repr__(self):
        return f"<MeetingAttendance m={self.meeting_id} c={self.customer_id} {self.status}>"


class MeetingResolution(db.Model):
    """Beschluss/Abstimmung — eigenständig gespeichert und durchsuchbar
    (Beschluss-Register). Verknüpft mit der Sitzung (Vorstand oder HV; die Art
    ergibt sich aus ``meeting.meeting_type``) und optional dem Agendapunkt.
    Auch abgelehnte Beschlüsse bleiben mit eigenem Status gelistet."""
    __tablename__ = "meeting_resolutions"

    STATUS_ACCEPTED = "accepted"    # angenommen
    STATUS_REJECTED = "rejected"    # abgelehnt
    STATUS_POSTPONED = "postponed"  # vertagt
    STATUSES = (STATUS_ACCEPTED, STATUS_REJECTED, STATUS_POSTPONED)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    agenda_item_id = db.Column(
        db.Integer, db.ForeignKey("meeting_agenda_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    title = db.Column(db.String(300), nullable=False)        # Name (Vorschlag aus TOP)
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACCEPTED,
                       server_default=db.text("'accepted'"))
    votes_for = db.Column(db.Integer, nullable=False, default=0, server_default=db.text("0"))
    votes_against = db.Column(db.Integer, nullable=False, default=0, server_default=db.text("0"))
    votes_abstain = db.Column(db.Integer, nullable=False, default=0, server_default=db.text("0"))
    notes = db.Column(db.Text, nullable=True)
    decided_on = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)

    agenda_item = db.relationship("MeetingAgendaItem", foreign_keys=[agenda_item_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def is_unanimous(self):
        """Einstimmig angenommen: Ja-Stimmen vorhanden, keine Gegenstimmen/Enthaltungen."""
        return (self.status == self.STATUS_ACCEPTED
                and (self.votes_against or 0) == 0
                and (self.votes_abstain or 0) == 0
                and (self.votes_for or 0) > 0)

    def __repr__(self):
        return f"<MeetingResolution #{self.id} m={self.meeting_id} {self.status}>"


class MeetingProtocol(db.Model):
    """Protokoll zu einer Sitzung (1:1). Entweder als Rich-Text erfasst oder als
    Datei hochgeladen. ``draft`` ist editierbar, ``final`` gesperrt; ein Upload
    ist sofort ``final``. Die abgelegte Datei liegt im Schriftverkehr-Ordner."""
    __tablename__ = "meeting_protocols"

    SOURCE_RICHTEXT = "richtext"
    SOURCE_UPLOAD = "upload"

    STATUS_DRAFT = "draft"      # Entwurf
    STATUS_FINAL = "final"      # Abgeschlossen
    STATUSES = (STATUS_DRAFT, STATUS_FINAL)

    ATTENDANCE_LIST = "list"          # detaillierte Personenliste (Default)
    ATTENDANCE_FREETEXT = "freetext"  # Freitext statt Liste ("X Personen anwesend")
    ATTENDANCE_MODES = (ATTENDANCE_LIST, ATTENDANCE_FREETEXT)

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id", ondelete="CASCADE"),
                           nullable=False, unique=True, index=True)
    source_type = db.Column(db.String(10), nullable=False, default=SOURCE_RICHTEXT,
                            server_default=db.text("'richtext'"))
    content_html = db.Column(db.Text, nullable=True)        # narrativer Teil (sanitisiert)
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT,
                       server_default=db.text("'draft'"))

    # Quorum-Snapshot (Beschlussfähigkeit zum Zeitpunkt des Protokolls).
    quorum_present = db.Column(db.Integer, nullable=True)
    quorum_total = db.Column(db.Integer, nullable=True)
    is_quorate = db.Column(db.Boolean, nullable=True)

    # Anwesenheits-Erfassung: detaillierte Personenliste ('list') oder Freitext
    # ('freetext' — die Personenliste entfällt, z.B. "37 Personen anwesend").
    attendance_mode = db.Column(db.String(10), nullable=False, default=ATTENDANCE_LIST,
                                server_default=db.text("'list'"))
    attendance_freetext = db.Column(db.Text, nullable=True)
    # Manuell erfasste Kopfzahl Anwesender — genutzt im Freitext-Modus und wenn
    # die Versammlung nach erfolgloser Wartefrist erneut eröffnet wurde.
    present_headcount = db.Column(db.Integer, nullable=True)
    # Hauptversammlung: war zunächst nicht beschlussfähig und wurde nach einer
    # Wartefrist erneut eröffnet → mit den Anwesenden beschlussfähig.
    reconvened = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.false())
    reconvene_wait_minutes = db.Column(db.Integer, nullable=True)

    # Datei: generiertes PDF aus Rich-Text ODER hochgeladenes Dokument.
    file_path = db.Column(db.String(500), nullable=True)
    original_filename = db.Column(db.String(255), nullable=True)
    mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)

    finalized_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def is_locked(self):
        return self.status == self.STATUS_FINAL

    @property
    def is_freetext_attendance(self):
        return self.attendance_mode == self.ATTENDANCE_FREETEXT

    def __repr__(self):
        return f"<MeetingProtocol m={self.meeting_id} {self.status}>"


class SchriftverkehrDocument(db.Model):
    """Eigenständiges Schriftverkehr-Dokument (eingehende/ausgehende
    Korrespondenz) im Archiv. Die Datei liegt im Schriftverkehr-Ordner
    (Jahr-Unterordner); die DB hält nur Metadaten."""
    __tablename__ = "schriftverkehr_documents"

    TYPE_INCOMING = "incoming"   # eingehend
    TYPE_OUTGOING = "outgoing"   # ausgehend
    TYPE_OTHER = "other"         # sonstiges
    TYPES = (TYPE_INCOMING, TYPE_OUTGOING, TYPE_OTHER)

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    title = db.Column(db.String(300), nullable=False)
    doc_type = db.Column(db.String(20), nullable=False, default=TYPE_OUTGOING,
                         server_default=db.text("'outgoing'"))
    document_date = db.Column(db.Date, nullable=True)
    file_path = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    mime_type = db.Column(db.String(120), nullable=True)
    file_size = db.Column(db.Integer, nullable=True)
    note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<SchriftverkehrDocument #{self.id} {self.year} {self.title!r}>"
