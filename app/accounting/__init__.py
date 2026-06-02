from flask import Blueprint, flash, redirect, request, url_for
from flask_login import current_user

from app.auth.permissions import (
    PERM_AUSWERTUNGEN,
    PERM_BUCHHALTUNG,
    PERM_RECHNUNGEN,
    PERM_STAMMDATEN,
)

bp = Blueprint("accounting", __name__, url_prefix="/accounting")


# accounting hat Routen aus vier Rechtsgruppen. Mapping per Endpoint-Namen
# (ohne Blueprint-Praefix), Fallback Buchhaltung.
_ENDPOINT_PERMS = {
    # Stammdaten: Buchungsjahre
    "fiscal_years": PERM_STAMMDATEN,
    "fiscal_year_new": PERM_STAMMDATEN,
    "fiscal_year_edit": PERM_STAMMDATEN,
    "fiscal_year_close": PERM_STAMMDATEN,
    "fiscal_year_reopen": PERM_STAMMDATEN,
    # Rechnungen / OP: Offene Posten
    "open_items": PERM_RECHNUNGEN,
    "open_items_set_account": PERM_RECHNUNGEN,
    "open_item_new": PERM_RECHNUNGEN,
    "open_item_pay": PERM_RECHNUNGEN,
    "open_item_invoice": PERM_RECHNUNGEN,
    # Auswertungen: Jahresbericht, USt-Voranmeldung, Kundenauswertung
    "report": PERM_AUSWERTUNGEN,
    "report_export_excel": PERM_AUSWERTUNGEN,
    "ust": PERM_AUSWERTUNGEN,
    "export_ust_csv": PERM_AUSWERTUNGEN,
    "kundenauswertung": PERM_AUSWERTUNGEN,
    "kundenauswertung_export": PERM_AUSWERTUNGEN,
}


@bp.before_request
def _check_accounting_permission():
    if not current_user.is_authenticated:
        return None
    endpoint = request.endpoint or ""
    # "accounting.bookings" -> "bookings"
    short = endpoint.split(".", 1)[-1]
    needed = _ENDPOINT_PERMS.get(short, PERM_BUCHHALTUNG)
    if not current_user.has_permission(needed):
        flash("Kein Zugriff für diesen Bereich.", "danger")
        return redirect(url_for("main.dashboard"))
    return None


from app.accounting import routes  # noqa
