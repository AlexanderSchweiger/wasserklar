from flask import Blueprint

bp = Blueprint("customers", __name__, url_prefix="/customers")

from app.customers import routes  # noqa
