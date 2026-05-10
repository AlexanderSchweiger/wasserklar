from flask import Blueprint

bp = Blueprint(
    "data_transfer",
    __name__,
    url_prefix="/data-transfer",
    template_folder="../templates",
)

from app.data_transfer import routes  # noqa: E402,F401
