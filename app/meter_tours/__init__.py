from flask import Blueprint, abort, current_app

from app.auth.permissions import require_blueprint_permission, PERM_ZAEHLER

bp = Blueprint("meter_tours", __name__, url_prefix="/meters/tours")


# Feature-Gate VOR dem Permission-Hook: ist FEATURE_METER_TOURS aus (OSS-
# Standalone-Default), existieren die Routen nach aussen nicht (404) — gleiches
# Muster wie network.assign_hausanschluss. Der SaaS-Layer schaltet das Flag
# fuer alle Tenants an (Basis + Pro, kein Plan-Gate).
@bp.before_request
def _feature_gate():
    if not current_app.config.get("FEATURE_METER_TOURS"):
        abort(404)


bp.before_request(require_blueprint_permission(PERM_ZAEHLER))

from app.meter_tours import routes  # noqa
