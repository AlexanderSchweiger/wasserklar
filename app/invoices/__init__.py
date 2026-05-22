from flask import Blueprint, flash, redirect, request, url_for
from flask_login import current_user

from app.auth.permissions import PERM_RECHNUNGEN, PERM_VERWALTUNG

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


# invoices.email_settings ist eine Admin/Verwaltungs-Konfiguration (SMTP) und
# gehoert nicht zum Rechnungs-Recht. Alles andere ist "Rechnungen / OP".
_VERWALTUNG_ENDPOINTS = {"email_settings"}


@bp.before_request
def _check_invoices_permission():
    if not current_user.is_authenticated:
        return None
    short = (request.endpoint or "").split(".", 1)[-1]
    needed = PERM_VERWALTUNG if short in _VERWALTUNG_ENDPOINTS else PERM_RECHNUNGEN
    if not current_user.has_permission(needed):
        flash("Kein Zugriff für diesen Bereich.", "danger")
        return redirect(url_for("main.dashboard"))
    return None


from app.invoices import routes  # noqa
