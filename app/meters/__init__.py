from flask import Blueprint

bp = Blueprint("meters", __name__, url_prefix="/meters")

from app.meters import routes  # noqa
