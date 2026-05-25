from flask import Blueprint

bp = Blueprint("bank_import", __name__, url_prefix="/bank-import")

from app.bank_import import routes  # noqa: E402, F401
