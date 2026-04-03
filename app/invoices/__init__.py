from flask import Blueprint

bp = Blueprint("invoices", __name__, url_prefix="/invoices")

from app.invoices import routes  # noqa
