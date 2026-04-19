from flask import render_template, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required, current_user

from app.settings import bp
from app.extensions import db
from app.models import AppSetting
from app.settings_service import _WG_MAP, _MAIL_MAP, send_mail, encrypt_password, apply_mail_settings
from app.invoices.design import INVOICE_DESIGNS, available_designs


@bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    """Einstellungsseite für WG-Kontaktdaten und E-Mail-Server (nur Admin)."""
    if not current_user.is_admin:
        flash('Kein Zugriff.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method == 'POST':
        # WG-Kontaktdaten
        for attr in _WG_MAP:
            val = request.form.get(f'wg_{attr}', '').strip()
            AppSetting.set(f'wg.{attr}', val if val else None)

        # Mail-Server
        for attr, db_key, _ in _MAIL_MAP:
            if attr == 'password':
                # Leerstring = unverändert lassen
                val = request.form.get('mail_password', '').strip()
                if val:
                    AppSetting.set('mail.password', encrypt_password(val))
            elif attr == 'use_tls':
                # Checkbox: nicht vorhanden = false (muss explizit gespeichert werden)
                val = 'true' if request.form.get('mail_use_tls') else 'false'
                AppSetting.set(db_key, val)
            else:
                val = request.form.get(f'mail_{attr}', '').strip()
                AppSetting.set(db_key, val if val else None)

        # Rechnungsformat
        fmt = request.form.get('invoice_document_format', 'pdf')
        AppSetting.set('invoice.document_format', fmt if fmt in ('pdf', 'docx', 'both') else 'pdf')

        # Rechnungsdesign (nur gültige Keys akzeptieren)
        design_key = request.form.get('invoice_design', 'classic')
        if design_key not in INVOICE_DESIGNS:
            design_key = 'classic'
        AppSetting.set('invoice.design', design_key)

        db.session.commit()
        apply_mail_settings()
        flash('Einstellungen gespeichert.', 'success')
        return redirect(url_for('settings.index'))

    # Aktuelle Werte für das Formular zusammenstellen (DB > .env-Fallback)
    def _get(db_key, config_key, default=''):
        val = AppSetting.get(db_key)
        if val is not None:
            return val
        return current_app.config.get(config_key, default)

    wg = {attr: _get(f'wg.{attr}', cfg_key) for attr, cfg_key in _WG_MAP.items()}

    mail_cfg = {}
    mail_defaults = {
        'server':         ('MAIL_SERVER', ''),
        'port':           ('MAIL_PORT', '587'),
        'use_tls':        ('MAIL_USE_TLS', 'true'),
        'username':       ('MAIL_USERNAME', ''),
        'default_sender': ('MAIL_DEFAULT_SENDER', ''),
    }
    for attr, (cfg_key, default) in mail_defaults.items():
        db_key = f'mail.{attr}'
        mail_cfg[attr] = _get(db_key, cfg_key, default)
    # use_tls als bool für Checkbox
    mail_cfg['use_tls_bool'] = str(mail_cfg['use_tls']).lower() in ('true', '1', 'yes')
    # Passwort-Platzhalter: zeige ob bereits gesetzt
    mail_cfg['password_set'] = bool(AppSetting.get('mail.password')
                                    or current_app.config.get('MAIL_PASSWORD'))

    # Datenbankverbindungsinfo (kein Passwort)
    from sqlalchemy.engine import make_url
    raw_url = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    try:
        u = make_url(raw_url)
        db_info = {
            'engine':   u.get_backend_name(),
            'driver':   u.drivername,
            'host':     u.host or '–',
            'port':     u.port or '–',
            'database': u.database or '–',
            'username': u.username or '–',
            'url_masked': (
                f"{u.drivername}://"
                + (f"{u.username}:***@" if u.username else '')
                + (f"{u.host}" if u.host else '')
                + (f":{u.port}" if u.port else '')
                + (f"/{u.database}" if u.database else u.database or '')
            ),
        }
    except Exception:
        db_info = {'engine': '–', 'driver': '–', 'host': '–', 'port': '–',
                   'database': raw_url or '–', 'username': '–', 'url_masked': raw_url}

    doc_format = AppSetting.get('invoice.document_format', 'pdf')
    invoice_design = AppSetting.get('invoice.design', 'classic')
    if invoice_design not in INVOICE_DESIGNS:
        invoice_design = 'classic'
    return render_template('settings/index.html', wg=wg, mail=mail_cfg, db_info=db_info,
                           doc_format=doc_format,
                           invoice_design=invoice_design,
                           invoice_designs=available_designs())


@bp.route('/test-mail', methods=['POST'])
@login_required
def send_test_mail():
    """Sendet eine Test-Mail an die Admin-Adresse (JSON-Antwort)."""
    if not current_user.is_admin:
        return jsonify({'ok': False, 'error': 'Kein Zugriff'}), 403

    recipient = current_user.email
    if not recipient:
        return jsonify({'ok': False, 'error': 'Keine E-Mail-Adresse beim Admin-Benutzer hinterlegt'}), 400

    try:
        from flask_mail import Message
        msg = Message(
            subject='Test-Mail – Wassergenossenschaft Verwaltung',
            recipients=[recipient],
            body=(
                'Dies ist eine Test-Mail der Wassergenossenschaft Verwaltung.\n\n'
                'Die E-Mail-Einstellungen funktionieren korrekt.\n\n'
                f'Gesendet an: {recipient}'
            ),
        )
        send_mail(msg)
        return jsonify({'ok': True, 'recipient': recipient})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500
