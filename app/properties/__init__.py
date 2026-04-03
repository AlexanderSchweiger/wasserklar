from flask import Blueprint

bp = Blueprint("properties", __name__, url_prefix="/properties")

from app.properties import routes  # noqa: E402, F401
