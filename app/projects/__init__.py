from flask import Blueprint

bp = Blueprint("projects", __name__, url_prefix="/projekte")

from app.projects import routes  # noqa
