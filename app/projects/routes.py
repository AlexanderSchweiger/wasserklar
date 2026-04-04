from flask import render_template, redirect, url_for, flash, request, abort
from flask_login import login_required
from sqlalchemy import exists

from app.projects import bp
from app.extensions import db
from app.models import Project, Booking, OpenItem


@bp.route("/")
@login_required
def index():
    show_closed = request.args.get("show_closed", "0") == "1"
    query = Project.query.order_by(Project.name)
    if not show_closed:
        query = query.filter_by(closed=False)
    projects = query.all()
    return render_template(
        "projects/index.html",
        projects=projects,
        show_closed=show_closed,
    )


@bp.route("/neu", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        if not name:
            flash("Projektname ist erforderlich.", "danger")
            return render_template("projects/form.html", project=None)
        if Project.query.filter_by(name=name).first():
            flash("Ein Projekt mit diesem Namen existiert bereits.", "danger")
            return render_template("projects/form.html", project=None)
        color = request.form.get("color", "#3498db").strip() or "#3498db"
        project = Project(name=name, description=description, color=color)
        db.session.add(project)
        db.session.commit()
        flash(f'Projekt "{project.name}" wurde angelegt.', "success")
        return redirect(url_for("projects.index"))
    return render_template("projects/form.html", project=None)


@bp.route("/<int:project_id>/bearbeiten", methods=["GET", "POST"])
@login_required
def edit(project_id):
    project = db.session.get(Project, project_id) or abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        if not name:
            flash("Projektname ist erforderlich.", "danger")
            return render_template("projects/form.html", project=project)
        existing = Project.query.filter_by(name=name).first()
        if existing and existing.id != project.id:
            flash("Ein Projekt mit diesem Namen existiert bereits.", "danger")
            return render_template("projects/form.html", project=project)
        project.name = name
        project.description = description
        project.color = request.form.get("color", "#3498db").strip() or "#3498db"
        db.session.commit()
        flash(f'Projekt "{project.name}" wurde gespeichert.', "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/form.html", project=project)


@bp.route("/<int:project_id>")
@login_required
def detail(project_id):
    project = db.session.get(Project, project_id) or abort(404)

    bookings = (
        Booking.query
        .filter_by(project_id=project.id)
        .order_by(Booking.date.desc())
        .all()
    )

    open_items = (
        OpenItem.query
        .filter(
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
            exists().where(
                (Booking.open_item_id == OpenItem.id) &
                (Booking.project_id == project.id)
            ),
        )
        .order_by(OpenItem.due_date)
        .all()
    )

    return render_template(
        "projects/detail.html",
        project=project,
        bookings=bookings,
        open_items=open_items,
    )


@bp.route("/<int:project_id>/abschliessen", methods=["POST"])
@login_required
def toggle_closed(project_id):
    project = db.session.get(Project, project_id) or abort(404)
    project.closed = not project.closed
    db.session.commit()
    status = "abgeschlossen" if project.closed else "wieder geöffnet"
    flash(f'Projekt "{project.name}" wurde {status}.', "success")
    return redirect(url_for("projects.detail", project_id=project.id))


@bp.route("/<int:project_id>/loeschen", methods=["POST"])
@login_required
def delete(project_id):
    project = db.session.get(Project, project_id) or abort(404)
    if Booking.query.filter_by(project_id=project.id).count() > 0:
        flash(
            f'Projekt "{project.name}" kann nicht geloescht werden, '
            "da noch Buchungen zugeordnet sind.",
            "danger",
        )
        return redirect(url_for("projects.detail", project_id=project.id))
    name = project.name
    db.session.delete(project)
    db.session.commit()
    flash(f'Projekt "{name}" wurde geloescht.', "success")
    return redirect(url_for("projects.index"))
