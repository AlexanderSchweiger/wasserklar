"""Berechtigungs-Konstanten, Decorator und Helfer.

Jeder Hauptmenuepunkt entspricht einem Recht. Rechte sind feste Code-Konstanten
(keine eigene DB-Tabelle) und werden Rollen ueber `role_permissions` zugeordnet.
Die Admin-Rolle (`Role.name == "Admin"`) hat implizit alle Rechte — auch
solche, die spaeter neu hinzukommen.
"""
from functools import wraps
from flask import flash, redirect, url_for
from flask_login import current_user, login_required

PERM_STAMMDATEN = "stammdaten"
PERM_ZAEHLER = "zaehler"
PERM_BUCHHALTUNG = "buchhaltung"
PERM_RECHNUNGEN = "rechnungen_op"
PERM_MAHNWESEN = "mahnwesen"
PERM_AUSWERTUNGEN = "auswertungen"
PERM_NETWORK = "network"
PERM_INCIDENTS = "incidents"
PERM_SCHRIFTFUEHRUNG = "schriftfuehrung"
PERM_CIRCULARS = "circulars"
PERM_VERWALTUNG = "verwaltung"

ALL_PERMISSIONS = [
    (PERM_STAMMDATEN, "Stammdaten", "Kontakte, Objekte, Buchungsjahre, Perioden, Import"),
    (PERM_ZAEHLER, "Zähler", "Zähler, Ablesungen, Zählertausch"),
    (PERM_BUCHHALTUNG, "Buchhaltung", "Buchungen, Umbuchungen, Bankkonten, Kontenplan, Projekte"),
    (PERM_RECHNUNGEN, "Rechnungen / OP", "Rechnungen, Tarife, Rechnungsläufe, Offene Posten"),
    (PERM_MAHNWESEN, "Mahnwesen", "Mahnungen, Mahnlauf, Mahnvorlagen"),
    (PERM_AUSWERTUNGEN, "Auswertungen", "Jahresbericht, USt-Voranmeldung"),
    (PERM_NETWORK, "Leitungsnetz", "Wasserleitungsplan, Anlagen, Wartung & Prüfung"),
    (PERM_INCIDENTS, "Störungsjournal", "Störungen, Rohrbrüche, Reparaturen & Jahresbericht"),
    (PERM_SCHRIFTFUEHRUNG, "Schriftführung", "Vorstandssitzungen, Hauptversammlungen, Beschlüsse, Schriftverkehr"),
    (PERM_CIRCULARS, "Rundschreiben", "Rundschreiben & Notfall-Kommunikation, Abkochempfehlung, Abschaltungs-Infos"),
    (PERM_VERWALTUNG, "Verwaltung", "Benutzer, Rollen, Einstellungen, Daten-Export/Import"),
]

PERMISSION_KEYS = [p[0] for p in ALL_PERMISSIONS]


def require_blueprint_permission(permission_key):
    """Hilfs-Hook fuer Blueprints, deren komplette Routen-Menge unter genau einem
    Recht steht. Wird via ``bp.before_request`` registriert.

    Unauthentifizierte Requests laesst das Hook durch — die ``@login_required``-
    Dekoratoren der einzelnen Routen kuemmern sich darum (Redirect nach
    ``/auth/login`` mit ``?next=``).
    """
    def hook():
        if not current_user.is_authenticated:
            return None
        if not current_user.has_permission(permission_key):
            flash("Kein Zugriff für diesen Bereich.", "danger")
            return redirect(url_for("main.dashboard"))
        return None
    return hook


def permission_required(permission_key):
    """Route-Decorator: prueft, ob current_user `permission_key` hat.

    Admin (Role.name == "Admin") hat implizit jedes Recht. Bei Fehlschlag:
    Flash + Redirect zum Dashboard (kein Hard-403, weil nicht alle Routen einen
    Error-Handler haben und der UX-Pfad konsistent sein soll).
    """
    def decorator(f):
        @wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            if not current_user.has_permission(permission_key):
                flash("Kein Zugriff für diesen Bereich.", "danger")
                return redirect(url_for("main.dashboard"))
            return f(*args, **kwargs)
        return wrapper
    return decorator
