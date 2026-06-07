from flask import render_template, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required, current_user

from app.settings import bp
from app.extensions import db
from app.models import AppSetting
from app.settings_service import (_WG_MAP, _MAIL_MAP, send_mail, encrypt_password,
                                  apply_mail_settings, platform_relay_active, get_wg,
                                  sanitize_rich_text, meter_replacement_interval,
                                  validate_logo_data_uri, get_contact_info_font_size,
                                  CONTACT_INFO_FONT_MIN, CONTACT_INFO_FONT_MAX,
                                  CONTACT_INFO_FONT_DEFAULT,
                                  get_invoice_sender_address, org_type)
from app.invoices.design import INVOICE_DESIGNS, available_designs
from app.wg import ORG_TYPES


@bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    """Einstellungsseite für WG-Kontaktdaten und E-Mail-Server (Verwaltungs-Recht)."""
    if request.method == 'POST':
        # Mandant-Typ (Wassergenossenschaft vs. Versorger) — unbekannte Werte
        # ignorieren, damit der Default (cooperative) nicht versehentlich geleert wird.
        org_val = request.form.get('org_type', '')
        if org_val in ORG_TYPES:
            AppSetting.set('org.type', org_val)

        # WG-Kontaktdaten
        for attr in _WG_MAP:
            val = request.form.get(f'wg_{attr}', '').strip()
            AppSetting.set(f'wg.{attr}', val if val else None)

        # Logo-Text/-Untertitel (Wortmarke als Alternative zum Logo-Bild).
        for attr in ('logo_text', 'logo_subtitle'):
            val = request.form.get(f'wg_{attr}', '').strip()
            AppSetting.set(f'wg.{attr}', val if val else None)

        # WG-Logo (Data-URI aus dem Cropper). "Entfernen" hat Vorrang vor einem
        # neu hochgeladenen Bild.
        if request.form.get('wg_logo_remove'):
            AppSetting.set('wg.logo', None)
        else:
            data_uri, logo_err = validate_logo_data_uri(request.form.get('wg_logo_data', ''))
            if logo_err:
                flash(logo_err, 'danger')
            elif data_uri:
                AppSetting.set('wg.logo', data_uri)

        # Mail-Versandmodus (Checkbox). Bei aktivem Plattform-Relay sind die
        # SMTP-Felder im UI disabled und werden nicht mitgesendet — der
        # _MAIL_MAP-Loop würde die gespeicherten mail.*-Werte sonst leeren.
        relay = 'true' if request.form.get('mail_use_platform_relay') else 'false'
        AppSetting.set('mail.use_platform_relay', relay)

        # Mail-Server (nur wenn kein Plattform-Relay aktiv)
        if relay != 'true':
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

        # Zählerwechsel-Detail auf Rechnungen (Checkbox)
        print_swap = 'true' if request.form.get('invoice_print_meter_swap') else 'false'
        AppSetting.set('invoice.print_meter_swap', print_swap)

        # „Rechnung per E-Mail?"-Block auf gedruckten Rechnungen (Checkbox).
        # Nur relevant im SaaS-Kontext (Selbstregistrierung) + wenn das Design
        # diesen Block unterstützt; der Block erscheint nie auf per Mail
        # versendeten Rechnungen.
        show_email_signup = 'true' if request.form.get('invoice_show_email_signup') else 'false'
        AppSetting.set('invoice.show_email_signup', show_email_signup)

        # EPC-QR-Code (GiroCode) im Zahlungsblock (Checkbox).
        # Nur relevant im SaaS-Kontext mit wasserklar-Design.
        show_payment_qr = 'true' if request.form.get('invoice_show_payment_qr') else 'false'
        AppSetting.set('invoice.show_payment_qr', show_payment_qr)

        # Rechnungs-Kontakttext (Rich-Text, auf <b>/<i>/<u>/<br> normalisiert)
        contact_info = sanitize_rich_text(request.form.get('invoice_contact_info', ''))
        AppSetting.set('invoice.contact_info', contact_info if contact_info else None)

        # Schriftgroesse des Kontakttexts (Pt) — ausserhalb der Grenzen = Default
        try:
            font_size = int(request.form.get('invoice_contact_info_font_size',
                                             CONTACT_INFO_FONT_DEFAULT))
        except (TypeError, ValueError):
            font_size = CONTACT_INFO_FONT_DEFAULT
        if font_size < CONTACT_INFO_FONT_MIN or font_size > CONTACT_INFO_FONT_MAX:
            font_size = CONTACT_INFO_FONT_DEFAULT
        AppSetting.set('invoice.contact_info_font_size', str(font_size))

        # Absenderadresse (einzeilig, Klartext)
        sender_address = request.form.get('invoice_sender_address', '').strip()
        AppSetting.set('invoice.sender_address', sender_address if sender_address else None)

        # Zähler-Tauschintervall (Jahre)
        try:
            interval = int(request.form.get('meter_replacement_interval', '5'))
        except (TypeError, ValueError):
            interval = 5
        if interval < 1:
            interval = 5
        AppSetting.set('meters.replacement_interval_years', str(interval))

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
    wg['logo'] = AppSetting.get('wg.logo') or ''
    wg['logo_text'] = AppSetting.get('wg.logo_text') or ''
    wg['logo_subtitle'] = AppSetting.get('wg.logo_subtitle') or ''

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
    mail_cfg['use_platform_relay'] = platform_relay_active()

    # DB-only Werte (ohne .env-Fallback) — das SaaS-Template rendert damit die
    # SMTP-Felder, ohne die Plattform-Relay-Zugangsdaten aus der .env zu zeigen.
    mail_raw = {attr: (AppSetting.get(f'mail.{attr}') or '')
                for attr in ('server', 'port', 'username', 'default_sender')}
    mail_raw['use_tls_bool'] = str(AppSetting.get('mail.use_tls')).lower() in ('true', '1', 'yes')
    mail_raw['password_set'] = bool(AppSetting.get('mail.password'))

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
    contact_info = AppSetting.get('invoice.contact_info') or ''
    print_meter_swap = AppSetting.get('invoice.print_meter_swap') == 'true'
    show_email_signup = AppSetting.get('invoice.show_email_signup') == 'true'
    show_payment_qr = AppSetting.get('invoice.show_payment_qr') == 'true'
    return render_template('settings/index.html', wg=wg, mail=mail_cfg, mail_raw=mail_raw,
                           db_info=db_info,
                           org_type=org_type(),
                           doc_format=doc_format,
                           invoice_design=invoice_design,
                           invoice_designs=available_designs(),
                           invoice_contact_info=contact_info,
                           invoice_contact_info_font_size=get_contact_info_font_size(),
                           invoice_sender_address=get_invoice_sender_address(),
                           invoice_print_meter_swap=print_meter_swap,
                           invoice_show_email_signup=show_email_signup,
                           invoice_show_payment_qr=show_payment_qr,
                           meter_replacement_interval=meter_replacement_interval())


@bp.route('/reset', methods=['POST'])
@login_required
def reset_tenant():
    """Setzt den aktuellen Mandanten zurueck ("Danger Zone").

    Loescht alle Geschaefts-Daten, behaelt aber Einstellungen sowie Benutzer +
    Rollen und re-seedet die Defaults (siehe app.settings.reset). Doppelt
    abgesichert: nur die Admin-Rolle UND erneute Passworteingabe. Im SaaS wirkt
    die Loeschung dank Schema-per-Tenant ausschliesslich auf das eigene
    Tenant-Schema.
    """
    # Gate 1: nur Admin (das Settings-Blueprint ist bereits auf 'verwaltung'
    # gegated, der Reset ist aber strikter — Admin-Rolle Pflicht).
    if not current_user.is_admin:
        flash('Nur Administratoren dürfen den Mandanten zurücksetzen.', 'danger')
        return redirect(url_for('settings.index'))

    # Gate 2: erneute Passwortbestaetigung des ausfuehrenden Admins.
    password = request.form.get('confirm_password', '')
    if not password or not current_user.check_password(password):
        flash('Passwort falsch — der Mandant wurde NICHT zurückgesetzt.', 'danger')
        return redirect(url_for('settings.index', _anchor='pane-danger'))

    from app.settings.reset import reset_tenant_data
    try:
        result = reset_tenant_data()
    except Exception as exc:  # noqa: BLE001 — Fehler dem Admin sichtbar machen
        db.session.rollback()
        current_app.logger.exception('Mandant-Reset fehlgeschlagen')
        flash(f'Zurücksetzen fehlgeschlagen: {exc}', 'danger')
        return redirect(url_for('settings.index', _anchor='pane-danger'))

    flash('Mandant wurde zurückgesetzt: alle Daten gelöscht, Einstellungen erhalten. '
          f'({result["cleared_tables"]} Tabellen geleert)', 'success')
    return redirect(url_for('settings.index'))


@bp.route('/test-mail', methods=['POST'])
@login_required
def send_test_mail():
    """Sendet eine Test-Mail an die Admin-Adresse (JSON-Antwort)."""
    recipient = get_wg('email')
    if not recipient:
        return jsonify({'ok': False, 'error': 'Keine Kontakt-E-Mail-Adresse hinterlegt (Einstellungen → Kontaktdaten)'}), 400

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
