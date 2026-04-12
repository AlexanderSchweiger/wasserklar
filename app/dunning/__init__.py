from flask import Blueprint

bp = Blueprint("dunning", __name__, url_prefix="/dunning")

from app.dunning import routes  # noqa
