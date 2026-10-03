"""Lieferantennummern im Daten-Export/-Import (Vollersatz und Zusammenfuehren)."""
from app.extensions import db
from app.models import Customer, SupplierCounter
from app.utils import SUPPLIER_START_DEFAULT, next_supplier_number
from tests.integration.test_document_transfer import do_import, export, tenant  # noqa: F401


def _supplier(name):
    c = Customer(name=name, is_customer=False, is_supplier=True, active=True,
                 creditor_number=next_supplier_number())
    db.session.add(c)
    db.session.commit()
    return c


def test_replace_keeps_numbers_and_counter(app, tenant):  # noqa: F811
    _supplier("Rohr AG")
    _supplier("Pumpen KG")
    data = export(include_pdfs=False, einstellungen=True)
    do_import(data, tenant, mode="replace")
    numbers = {c.name: c.creditor_number for c in Customer.query.all()}
    assert numbers == {"Rohr AG": SUPPLIER_START_DEFAULT, "Pumpen KG": SUPPLIER_START_DEFAULT + 1}
    assert next_supplier_number(peek=True) == SUPPLIER_START_DEFAULT + 2
    assert db.session.get(SupplierCounter, 1) is not None


def test_merge_renumbers_colliding_supplier(app, tenant):  # noqa: F811
    _supplier("Import Lieferant")
    data = export(include_pdfs=False)
    Customer.query.delete()
    db.session.commit()
    db.session.add(Customer(name="Bestand Lieferant", is_customer=False, is_supplier=True,
                            active=True, creditor_number=SUPPLIER_START_DEFAULT))  # Kollision
    db.session.commit()
    do_import(data, tenant, mode="merge")
    numbers = {c.name: c.creditor_number for c in Customer.query.all()}
    assert numbers["Bestand Lieferant"] == SUPPLIER_START_DEFAULT
    assert numbers["Import Lieferant"] not in (None, SUPPLIER_START_DEFAULT)
