from flask import render_template, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required, current_user

from app.settings import bp
from app.extensions import db
from app.models import AppSetting
from app.settings_service import _WG_MAP, _MAIL_MAP, send_mail


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
                    AppSetting.set('mail.password', val)
            else:
                val = request.form.get(f'mail_{attr}', '').strip()
                AppSetting.set(db_key, val if val else None)

        db.session.commit()
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

    return render_template('settings/index.html', wg=wg, mail=mail_cfg)


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
