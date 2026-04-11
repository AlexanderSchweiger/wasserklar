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
