import pytest
from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope="session")
def app():
    """Erstellt die Flask-App einmalig für die gesamte Test-Session."""
    _app = create_app("testing")
    ctx = _app.app_context()
    ctx.push()
    _db.create_all()
    yield _app
    _db.session.remove()
    _db.drop_all()
    ctx.pop()


@pytest.fixture(scope="function", autouse=True)
def clean_db(app):
    """Leert nach jedem Test alle Tabellen (Schema bleibt erhalten)."""
    yield
    _db.session.rollback()
    for table in reversed(_db.metadata.sorted_tables):
        _db.session.execute(table.delete())
    _db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


def _ensure_role(name, perms=()):
    """Test-Helper: idempotent eine Rolle anlegen.

    Nach jedem Test laeuft ``clean_db`` und leert auch die ``roles``-Tabelle.
    Tests, die User anlegen, muessen daher die benoetigte Rolle vorher
    re-seeden. ``User.role_id`` ist NOT NULL, ein blosses ``role="admin"``-
    Anlegen kracht — daher diese Hilfsfunktion.
    """
    from app.models import Role, RolePermission
    role = Role.query.filter_by(name=name).first()
    if role is None:
        role = Role(name=name, is_system=(name == "Admin"))
        _db.session.add(role)
        _db.session.flush()
        for key in perms:
            _db.session.add(RolePermission(role_id=role.id, permission_key=key))
        _db.session.commit()
    return role


@pytest.fixture
def admin_role(app):
    return _ensure_role("Admin")
