from flask import Blueprint

bp = Blueprint("periods", __name__, url_prefix="/perioden")

from app.periods import routes  # noqa
