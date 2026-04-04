from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from app.extensions import db, login_manager


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default="user")  # admin / user
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == "admin"

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Kunden
# ---------------------------------------------------------------------------

class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    customer_number = db.Column(db.Integer, unique=True, nullable=True)    # fortlaufende Kundennummer
    externe_kennung = db.Column(db.String(100), nullable=True)             # optionale externe Kennung
    name = db.Column(db.String(200), nullable=False)
    strasse = db.Column(db.String(200))
    hausnummer = db.Column(db.String(20))
    plz = db.Column(db.String(10))
    ort = db.Column(db.String(100))
    land = db.Column(db.String(100), default="Österreich")
    email = db.Column(db.String(120))
    phone = db.Column(db.String(50))
    member_since = db.Column(db.Date)
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    base_fee_override = db.Column(db.Numeric(10, 2), nullable=True)       # überschreibt Tarif-Grundgebühr
    additional_fee_override = db.Column(db.Numeric(10, 2), nullable=True)  # überschreibt Tarif-Zusatzgebühr

    invoices = db.relationship("Invoice", backref="customer", lazy="dynamic")
    ownerships = db.relationship("PropertyOwnership", backref="customer", lazy="dynamic")

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    readings = db.relationship("MeterReading", backref="meter", lazy="dynamic",
                               cascade="all, delete-orphan",
                               order_by="MeterReading.year.desc()")

    def last_reading(self):
        return self.readings.order_by(MeterReading.year.desc()).first()

    def reading_for_year(self, year):
        return self.readings.filter_by(year=year).first()

    def __repr__(self):
        return f"<WaterMeter {self.meter_number}>"


class MeterReading(db.Model):
    __tablename__ = "meter_readings"

    id = db.Column(db.Integer, primary_key=True)
    meter_id = db.Column(db.Integer, db.ForeignKey("water_meters.id"), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    reading_date = db.Column(db.Date, default=date.today)
    value = db.Column(db.Numeric(12, 3), nullable=False)  # m³
    consumption = db.Column(db.Numeric(12, 3))             # m³ Verbrauch (berechnet)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        db.UniqueConstraint("meter_id", "year", name="uq_meter_year"),
    )

    def __repr__(self):
        return f"<MeterReading {self.meter.meter_number} {self.year}: {self.value}>"


# ---------------------------------------------------------------------------
# Tarife
# ---------------------------------------------------------------------------

class WaterTariff(db.Model):
    __tablename__ = "water_tariffs"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    valid_from = db.Column(db.Integer, nullable=False)   # Jahr
    valid_to = db.Column(db.Integer)                      # Jahr (None = aktuell gültig)
    base_fee = db.Column(db.Numeric(10, 2), default=0)   # Grundgebühr €
    base_fee_label = db.Column(db.String(100), default="Grundgebühr")
    additional_fee = db.Column(db.Numeric(10, 2), default=0)  # Zusatzgebühr €
    additional_fee_label = db.Column(db.String(100), default="Zusatzgebühr")
    price_per_m3 = db.Column(db.Numeric(10, 2), nullable=False)  # Preis pro m³
    notes = db.Column(db.Text)

    def __repr__(self):
        return f"<WaterTariff {self.name} {self.valid_from}>"


# ---------------------------------------------------------------------------
# Rechnungen
# ---------------------------------------------------------------------------

class Invoice(db.Model):
    __tablename__ = "invoices"

    STATUS_DRAFT = "Entwurf"
    STATUS_SENT = "Versendet"
    STATUS_PAID = "Bezahlt"
    STATUS_CANCELLED = "Storniert"
    STATUS_CREDIT = "Guthaben"
    ALL_STATUSES = [STATUS_DRAFT, STATUS_SENT, STATUS_PAID, STATUS_CANCELLED, STATUS_CREDIT]

    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id"), nullable=True)
    period_year = db.Column(db.Integer)
    date = db.Column(db.Date, default=date.today)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(20), default=STATUS_DRAFT)
    total_amount = db.Column(db.Numeric(10, 2), default=0)
    pdf_path = db.Column(db.String(500))
    notes = db.Column(db.Text)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("InvoiceItem", backref="invoice", lazy="select",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    bookings = db.relationship("Booking", backref="invoice", lazy="dynamic")

    def recalculate_total(self):
        self.total_amount = sum(item.amount for item in self.items)

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

    def __repr__(self):
        return f"<InvoiceItem {self.description}>"


# ---------------------------------------------------------------------------
# Projekte
# ---------------------------------------------------------------------------

class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    closed = db.Column(db.Boolean, default=False, nullable=False)
    color = db.Column(db.String(20), nullable=True, default="#3498db")

    bookings = db.relationship("Booking", backref="project", lazy="dynamic")

    def __repr__(self):
        return f"<Project {self.name}>"


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

    bookings = db.relationship("Booking", backref="real_account", lazy="dynamic")

    def __repr__(self):
        return f"<RealAccount {self.name}>"


class Account(db.Model):
    __tablename__ = "accounts"

    TYPE_INCOME = "Einnahme"
    TYPE_EXPENSE = "Ausgabe"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    type = db.Column(db.String(20), nullable=False)   # Einnahme / Ausgabe
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


# ---------------------------------------------------------------------------
# Offene Posten (manuell angelegt)
# ---------------------------------------------------------------------------

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
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", backref=db.backref("open_items", lazy="dynamic"))
    invoice = db.relationship("Invoice", backref=db.backref("open_item", uselist=False))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
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
