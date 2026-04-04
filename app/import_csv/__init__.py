from flask import Blueprint

bp = Blueprint("import_csv", __name__, url_prefix="/import")

from app.import_csv import routes  # noqa: E402, F401
