"""Routen des Notiz-Moduls.

Interaktionsmodell (HTMX, ohne eigene Karten-/Modal-Libs):
- Jede Ebene (Tenant am Dashboard, Entität auf Detailseite, Entität im Zeilen-
  Modal) rendert das gemeinsame ``_panel.html`` (Liste der Notizzettel + inline
  Anlage-/Bearbeiten-Formular). Alle Mutationen geben das neu gerenderte Panel
  zurück (``hx-target`` = Panel-Container, ``hx-swap=outerHTML``) — wie das
  Feature-Panel im Leitungsnetz-Modul.
- Mutationen setzen zusätzlich ``HX-Trigger: notes:changed`` mit
  ``{entity_type, entity_id}``, damit der kleine Zeilen-Pin außerhalb des Panels
  seinen Zähl-Badge aktualisiert (siehe ``notes/_modal.html``).
- Die Übersichtsseite ``/notes/`` nutzt für Löschen/Pin denselben Endpoint mit
  ``ctx=overview`` und bekommt dann das Übersichts-Tabellen-Fragment zurück.

Kein Bereichsrecht — nur ``@login_required`` (jeder eingeloggte User).
"""
import json

from flask import render_template, request, make_response, abort
from flask_login import login_required, current_user

from app.notes import bp
from app.notes import services as svc
from app.extensions import db
from app.models import Note


# ---------------------------------------------------------------------------
# Render-Helfer
# ---------------------------------------------------------------------------

def _render_panel(entity_type, entity_id, adding=False, edit_id=None, error=None,
                  draft=None):
    """``_panel.html``-Fragment für eine Ebene rendern. ``adding``/``edit_id``
    steuern das inline-Formular; ``draft`` hält Form-Werte bei Validierungsfehler."""
    return render_template(
        "notes/_panel.html",
        entity_type=entity_type,
        entity_id=entity_id,
        notes=svc.notes_for(entity_type, entity_id),
        adding=adding,
        edit_id=edit_id,
        error=error,
        draft=draft or {},
        colors=svc.NOTE_COLORS,
        default_color=svc.DEFAULT_COLOR,
        scope_labels=svc.SCOPE_LABELS,
    )


def _panel_response(entity_type, entity_id):
    """Panel-Fragment + ``notes:changed``-Trigger (für den Zeilen-Pin-Refresh)."""
    resp = make_response(_render_panel(entity_type, entity_id))
    resp.headers["HX-Trigger"] = json.dumps(
        {"notes:changed": {"entity_type": entity_type, "entity_id": entity_id}}
    )
    return resp


def _render_overview_table(scope=None):
    notes = svc.all_notes(scope)
    return render_template(
        "notes/_overview_table.html",
        notes=notes,
        displays={n.id: svc.entity_display(n) for n in notes},
        scope=scope,
        scope_labels=svc.SCOPE_LABELS,
        colors=svc.NOTE_COLORS,
    )


def _scope_from_request():
    """(entity_type, entity_id) aus Form/Query lesen + validieren. Tenant-Scope ⇒
    entity_id=None. Bricht mit 400 ab, wenn der Scope ungültig oder die
    Zielentität nicht existiert."""
    entity_type = (request.values.get("entity_type") or "").strip()
    if not svc.is_valid_scope(entity_type):
        abort(400)
    if entity_type == Note.SCOPE_TENANT:
        return entity_type, None
    entity_id = request.values.get("entity_id", type=int)
    if not svc.entity_exists(entity_type, entity_id):
        abort(400)
    return entity_type, entity_id


# ---------------------------------------------------------------------------
# Panel / Pin (Lesen + inline-Formular-Zustände)
# ---------------------------------------------------------------------------

@bp.route("/panel")
@login_required
def panel():
    """Panel-Fragment einer Ebene — initial via ``hx-trigger=load`` (Detailseite/
    Dashboard) oder ins Zeilen-Modal geladen. ``adding``/``edit_id`` öffnen das
    inline-Formular."""
    entity_type, entity_id = _scope_from_request()
    return _render_panel(
        entity_type, entity_id,
        adding=bool(request.args.get("adding")),
        edit_id=request.args.get("edit_id", type=int),
    )


@bp.route("/pin")
@login_required
def pin():
    """Einzelner Zeilen-Pin (Badge) — für den Live-Refresh nach einer Mutation im
    Zeilen-Modal (``notes:changed`` → htmx ``refresh`` auf diesen Pin)."""
    entity_type, entity_id = _scope_from_request()
    return render_template(
        "notes/_pin.html",
        entity_type=entity_type, entity_id=entity_id,
        notes=svc.notes_for(entity_type, entity_id),
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@bp.route("/", methods=["POST"])
@login_required
def create():
    entity_type, entity_id = _scope_from_request()
    body = (request.form.get("body") or "").strip()
    color = svc.normalize_color(request.form.get("color"))
    if not body:
        return _render_panel(entity_type, entity_id, adding=True,
                             error="Bitte einen Notiztext eingeben.",
                             draft={"body": body, "color": color})
    note = Note(entity_type=entity_type, entity_id=entity_id, body=body,
                color=color, pinned=True, created_by_id=current_user.id)
    db.session.add(note)
    db.session.commit()
    return _panel_response(entity_type, entity_id)


@bp.route("/<int:note_id>", methods=["POST"])
@login_required
def update(note_id):
    note = db.get_or_404(Note, note_id)
    body = (request.form.get("body") or "").strip()
    color = svc.normalize_color(request.form.get("color"))
    if not body:
        return _render_panel(note.entity_type, note.entity_id, edit_id=note.id,
                             error="Bitte einen Notiztext eingeben.",
                             draft={"body": body, "color": color})
    note.body = body
    note.color = color
    db.session.commit()
    return _panel_response(note.entity_type, note.entity_id)


@bp.route("/<int:note_id>/delete", methods=["POST"])
@login_required
def delete(note_id):
    note = db.get_or_404(Note, note_id)
    entity_type, entity_id = note.entity_type, note.entity_id
    db.session.delete(note)
    db.session.commit()
    if request.values.get("ctx") == "overview":
        return _render_overview_table(scope=request.values.get("scope") or None)
    return _panel_response(entity_type, entity_id)


@bp.route("/<int:note_id>/pin", methods=["POST"])
@login_required
def toggle_pin(note_id):
    note = db.get_or_404(Note, note_id)
    note.pinned = not note.pinned
    db.session.commit()
    if request.values.get("ctx") == "overview":
        return _render_overview_table(scope=request.values.get("scope") or None)
    return _panel_response(note.entity_type, note.entity_id)


# ---------------------------------------------------------------------------
# Übersicht
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    scope = request.args.get("scope") or None
    if scope and not svc.is_valid_scope(scope):
        scope = None
    notes = svc.all_notes(scope)
    ctx = dict(
        notes=notes,
        displays={n.id: svc.entity_display(n) for n in notes},
        scope=scope,
        scope_labels=svc.SCOPE_LABELS,
        colors=svc.NOTE_COLORS,
    )
    if request.headers.get("HX-Request"):
        return render_template("notes/_overview_table.html", **ctx)
    return render_template("notes/index.html", **ctx)
