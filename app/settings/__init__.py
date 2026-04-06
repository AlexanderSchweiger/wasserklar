from flask import Blueprint

bp = Blueprint('settings', __name__, url_prefix='/einstellungen')

from app.settings import routes  # noqa: E402, F401
