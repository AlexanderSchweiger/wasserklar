"""Vollersatz-Import: Rollen duerfen nicht geloescht werden, solange users sie referenzieren."""
from tests.integration.test_document_transfer import (  # noqa: F401
    books, do_import, export, tenant,
)
from app.extensions import db
from app.models import Role, User


def test_replace_keeps_users_on_their_roles(app, tenant, books):
    _account, admin = books
    data = export(einstellungen=True)

    extra = Role(name="Extra", is_system=False)
    db.session.add(extra)
    db.session.flush()
    other = User(username="extra", email="e@test.test", role_id=extra.id)
    other.set_password("secret")
    db.session.add(other)
    db.session.commit()

    do_import(data, tenant, mode="replace")

    roles = {r.name: r.id for r in Role.query.all()}
    assert "Admin" in roles and "Extra" in roles          # keine Rolle verloren
    assert not any(n.startswith("__imp_") for n in roles)
    assert User.query.filter_by(username="kassier").one().role_id == roles["Admin"]
    assert User.query.filter_by(username="extra").one().role_id == roles["Extra"]
