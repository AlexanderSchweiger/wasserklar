from flask import Blueprint

bp = Blueprint("accounting", __name__, url_prefix="/accounting")

from app.accounting import routes  # noqa
