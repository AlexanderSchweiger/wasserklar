from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.auth import bp
from app.extensions import db
from app.models import User


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            login_user(user)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("main.dashboard"))
        flash("Benutzername oder Passwort falsch.", "danger")
    return render_template("auth/login.html")


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Benutzerverwaltung (nur Admin)
# ---------------------------------------------------------------------------

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Kein Zugriff.", "danger")
            return redirect(url_for("main.dashboard"))
        return f(*args, **kwargs)
    return decorated


@bp.route("/users")
@login_required
@admin_required
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("auth/users.html", users=all_users)


@bp.route("/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def user_new():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")
        if User.query.filter_by(username=username).first():
            flash("Benutzername bereits vergeben.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("E-Mail bereits vergeben.", "danger")
        else:
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Benutzer '{username}' angelegt.", "success")
            return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", user=None)


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def user_edit(user_id):
    user = db.get_or_404(User, user_id)
    if request.method == "POST":
        user.username = request.form.get("username", "").strip()
        user.email = request.form.get("email", "").strip()
        user.role = request.form.get("role", "user")
        user.active = "active" in request.form
        new_pw = request.form.get("password", "")
        if new_pw:
            user.set_password(new_pw)
        db.session.commit()
        flash("Benutzer aktualisiert.", "success")
        return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", user=user)
