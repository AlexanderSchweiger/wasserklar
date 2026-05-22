from flask import render_template, redirect, url_for, flash, request, current_app
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from app.auth import bp
from app.auth.password_policy import validate_password
from app.auth.permissions import (
    ALL_PERMISSIONS,
    PERMISSION_KEYS,
    PERM_VERWALTUNG,
    permission_required,
)
from app.extensions import db
from app.models import Role, RolePermission, User
from app.settings_service import send_mail


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
# Passwort: vergessen / zuruecksetzen / aendern
# ---------------------------------------------------------------------------

_RESET_SALT = "wk-pw-reset"
_RESET_MAX_AGE = 3600  # 1 Stunde


def _reset_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_RESET_SALT)


def _make_reset_token(user):
    """Signiertes, zustandsloses Token. Der Hash-Anhang (pwf) bindet das Token
    an das aktuelle Passwort — sobald es geaendert wird, wird das Token tot."""
    return _reset_serializer().dumps(
        {"uid": user.id, "pwf": user.password_hash[-12:]}
    )


def _load_reset_token(token):
    """User zum Token oder None bei ungueltigem/abgelaufenem/verbrauchtem Token."""
    try:
        data = _reset_serializer().loads(token, max_age=_RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user = db.session.get(User, data.get("uid"))
    if user is None or not user.active:
        return None
    if user.password_hash[-12:] != data.get("pwf"):
        return None
    return user


def _send_reset_mail(user):
    token = _make_reset_token(user)
    reset_url = url_for("auth.reset_password", token=token, _external=True)
    msg = Message(subject="Passwort zurücksetzen", recipients=[user.email])
    msg.body = render_template(
        "auth/email/reset_password.txt", user=user, reset_url=reset_url
    )
    msg.html = render_template(
        "auth/email/reset_password.html", user=user, reset_url=reset_url
    )
    send_mail(msg)
    if current_app.debug:
        current_app.logger.info("Passwort-Reset-Link (%s): %s", user.email, reset_url)


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if email:
            user = User.query.filter(db.func.lower(User.email) == email).first()
            if user is not None and user.active:
                try:
                    _send_reset_mail(user)
                except Exception:
                    current_app.logger.exception(
                        "Versand der Passwort-Reset-Mail fehlgeschlagen"
                    )
        # Immer dieselbe Antwort — verraet nicht, ob die Adresse existiert.
        return render_template("auth/forgot_password.html", sent=True)
    return render_template("auth/forgot_password.html", sent=False)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    user = _load_reset_token(token)
    if user is None:
        return render_template("auth/reset_password.html", invalid=True), 410
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        errors = validate_password(
            password, username=user.username, email=user.email
        )
        if password != confirm:
            errors.append("Die beiden Passwörter stimmen nicht überein.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template(
                "auth/reset_password.html", invalid=False, user=user, token=token
            )
        user.set_password(password)
        db.session.commit()
        flash("Dein Passwort wurde geändert. Bitte melde dich neu an.", "success")
        return redirect(url_for("auth.login"))
    return render_template(
        "auth/reset_password.html", invalid=False, user=user, token=token
    )


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        if not current_user.check_password(current):
            flash("Das aktuelle Passwort ist falsch.", "danger")
        else:
            errors = validate_password(
                password, username=current_user.username, email=current_user.email
            )
            if password != confirm:
                errors.append("Die beiden neuen Passwörter stimmen nicht überein.")
            if current == password:
                errors.append(
                    "Das neue Passwort muss sich vom aktuellen unterscheiden."
                )
            if errors:
                for e in errors:
                    flash(e, "danger")
            else:
                current_user.set_password(password)
                db.session.commit()
                flash("Dein Passwort wurde geändert.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template("auth/change_password.html")


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


def _all_roles():
    return Role.query.order_by(Role.name).all()


def _resolve_role_id(raw):
    """POST-Param 'role_id' -> Role oder None bei ungueltigem/fehlendem Wert."""
    try:
        return db.session.get(Role, int(raw))
    except (TypeError, ValueError):
        return None


@bp.route("/users")
@permission_required(PERM_VERWALTUNG)
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("auth/users.html", users=all_users)


@bp.route("/users/new", methods=["GET", "POST"])
@permission_required(PERM_VERWALTUNG)
def user_new():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = _resolve_role_id(request.form.get("role_id"))
        pw_errors = validate_password(password, username=username, email=email)
        if User.query.filter_by(username=username).first():
            flash("Benutzername bereits vergeben.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("E-Mail bereits vergeben.", "danger")
        elif role is None:
            flash("Bitte eine gültige Rolle auswählen.", "danger")
        elif pw_errors:
            for e in pw_errors:
                flash(e, "danger")
        else:
            user = User(username=username, email=email, role_id=role.id)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Benutzer '{username}' angelegt.", "success")
            return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", user=None, roles=_all_roles())


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@permission_required(PERM_VERWALTUNG)
def user_edit(user_id):
    user = db.get_or_404(User, user_id)
    if request.method == "POST":
        role = _resolve_role_id(request.form.get("role_id"))
        if role is None:
            flash("Bitte eine gültige Rolle auswählen.", "danger")
        else:
            user.username = request.form.get("username", "").strip()
            user.email = request.form.get("email", "").strip()
            user.role_id = role.id
            user.active = "active" in request.form
            db.session.commit()
            flash("Benutzer aktualisiert.", "success")
            return redirect(url_for("auth.users"))
    return render_template("auth/user_form.html", user=user, roles=_all_roles())


@bp.route("/users/<int:user_id>/password", methods=["POST"])
@permission_required(PERM_VERWALTUNG)
def user_set_password(user_id):
    user = db.get_or_404(User, user_id)
    password = request.form.get("password", "")
    confirm = request.form.get("password_confirm", "")
    errors = validate_password(password, username=user.username, email=user.email)
    if password != confirm:
        errors.append("Die beiden Passwörter stimmen nicht überein.")
    if errors:
        for e in errors:
            flash(e, "danger")
    else:
        user.set_password(password)
        db.session.commit()
        flash(f"Passwort für '{user.username}' geändert.", "success")
    return redirect(url_for("auth.user_edit", user_id=user.id))


# ---------------------------------------------------------------------------
# Rollen- und Berechtigungsverwaltung
# ---------------------------------------------------------------------------

def _selected_permissions_from_form():
    """Aus dem Form gewaehlte Permission-Keys extrahieren (Checkbox-Liste)."""
    raw = request.form.getlist("permissions")
    return [p for p in raw if p in PERMISSION_KEYS]


@bp.route("/roles")
@permission_required(PERM_VERWALTUNG)
def roles():
    all_roles = Role.query.order_by(Role.is_system.desc(), Role.name).all()
    user_counts = {
        rid: cnt
        for rid, cnt in db.session.query(
            User.role_id, db.func.count(User.id)
        ).group_by(User.role_id).all()
    }
    return render_template(
        "auth/roles.html", roles=all_roles, user_counts=user_counts
    )


@bp.route("/roles/new", methods=["GET", "POST"])
@permission_required(PERM_VERWALTUNG)
def role_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        perms = _selected_permissions_from_form()
        if not name:
            flash("Bitte einen Namen für die Rolle angeben.", "danger")
        elif Role.query.filter(db.func.lower(Role.name) == name.lower()).first():
            flash("Rollenname bereits vergeben.", "danger")
        else:
            role = Role(name=name, description=description, is_system=False)
            for key in perms:
                role.permissions.append(RolePermission(permission_key=key))
            db.session.add(role)
            db.session.commit()
            flash(f"Rolle '{name}' angelegt.", "success")
            return redirect(url_for("auth.roles"))
    return render_template(
        "auth/role_form.html",
        role=None,
        selected_perms=set(_selected_permissions_from_form()),
    )


@bp.route("/roles/<int:role_id>/edit", methods=["GET", "POST"])
@permission_required(PERM_VERWALTUNG)
def role_edit(role_id):
    role = db.get_or_404(Role, role_id)
    if request.method == "POST":
        if role.is_system:
            flash("System-Rollen können nicht verändert werden.", "danger")
            return redirect(url_for("auth.roles"))
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip() or None
        perms = _selected_permissions_from_form()
        clash = Role.query.filter(
            db.func.lower(Role.name) == name.lower(), Role.id != role.id
        ).first()
        if not name:
            flash("Bitte einen Namen für die Rolle angeben.", "danger")
        elif clash is not None:
            flash("Rollenname bereits vergeben.", "danger")
        else:
            role.name = name
            role.description = description
            # Permissions neu setzen (cascade=delete-orphan raeumt die alten weg)
            role.permissions.clear()
            for key in perms:
                role.permissions.append(RolePermission(permission_key=key))
            db.session.commit()
            flash(f"Rolle '{name}' aktualisiert.", "success")
            return redirect(url_for("auth.roles"))
    return render_template(
        "auth/role_form.html",
        role=role,
        selected_perms=role.permission_keys,
    )


@bp.route("/roles/<int:role_id>/delete", methods=["POST"])
@permission_required(PERM_VERWALTUNG)
def role_delete(role_id):
    role = db.get_or_404(Role, role_id)
    if role.is_system:
        flash("System-Rollen können nicht gelöscht werden.", "danger")
        return redirect(url_for("auth.roles"))
    user_count = User.query.filter_by(role_id=role.id).count()
    if user_count:
        flash(
            f"Rolle '{role.name}' ist noch {user_count} Benutzer(n) zugeordnet "
            "und kann daher nicht gelöscht werden.",
            "danger",
        )
        return redirect(url_for("auth.roles"))
    db.session.delete(role)
    db.session.commit()
    flash(f"Rolle '{role.name}' gelöscht.", "success")
    return redirect(url_for("auth.roles"))
