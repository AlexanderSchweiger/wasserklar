import json
from decimal import Decimal, InvalidOperation

from flask import (render_template, redirect, url_for, flash, request, current_app,
                   jsonify, make_response)
from flask_login import login_required, current_user

from app import country as country_mod
from app import tax_service
from app.accounting import handover as handover_svc
from app.settings import bp
from app.extensions import db
from app.models import AppSetting, TaxRate
from app.settings_service import (_WG_MAP, _MAIL_MAP, send_mail, encrypt_password,
                                  apply_mail_settings, platform_relay_active, get_wg,
                                  sanitize_rich_text, meter_replacement_interval,
                                  validate_logo_data_uri, get_contact_info_font_size,
                                  CONTACT_INFO_FONT_MIN, CONTACT_INFO_FONT_MAX,
                                  CONTACT_INFO_FONT_DEFAULT,
                                  get_invoice_sender_address, org_type)
from app.invoices.design import INVOICE_DESIGNS, available_designs
from app.wg import ORG_TYPES


# AppSetting-Keys, die das gerenderte Rechnungs-Dokument optisch beeinflussen.
# Aendert sich einer davon, ist der gecachte PDF-/DOCX-Stand gesperrter
# Rechnungen veraltet (z.B. neu aktivierter GiroCode, Designwechsel, geaenderte
# IBAN/Kontaktdaten) und muss beim naechsten Abruf neu gerendert werden.
_INVOICE_RENDER_KEYS = (
    'invoice.design', 'invoice.show_payment_qr', 'invoice.show_email_signup',
    'invoice.print_meter_swap', 'invoice.contact_info',
    'invoice.contact_info_font_size', 'invoice.sender_address',
    'wg.name', 'wg.address', 'wg.email', 'wg.phone',
    'wg.iban', 'wg.bic', 'wg.account_holder', 'wg.vat_id', 'wg.tax_number',
    'invoice.small_business_note',
    'wg.logo', 'wg.logo_text', 'wg.logo_subtitle',
)


def _invoice_render_signature():
    """Stabiler Hash aller render-relevanten Einstellungen als Vergleichsschluessel."""
    import hashlib
    raw = "\x1f".join((AppSetting.get(k) or "") for k in _INVOICE_RENDER_KEYS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _invalidate_cached_invoice_documents():
    """Loescht den PDF-/DOCX-Cache-Zeiger aller gesperrten Rechnungen.

    Nur der DB-Zeiger (``pdf_path``/``doc_path``) wird geleert — die Dateien auf
    der Platte bleiben liegen (Versionspfade _V2/_V3 erhalten den Audit-Verlauf).
    Beim naechsten Abruf rendert die Rechnung mit dem aktuellen Design/den
    aktuellen Einstellungen neu und cached wieder. Entwuerfe cachen ohnehin nie.
    """
    from app.models import Invoice
    Invoice.query.filter(Invoice.status != Invoice.STATUS_DRAFT).update(
        {Invoice.pdf_path: None, Invoice.doc_path: None},
        synchronize_session=False,
    )


def _save_supplier_number_start(raw):
    """Startwert der Lieferantennummern setzen (nur ohne vergebene Nummer)."""
    from app.customers.numbers import supplier_numbers_in_use
    from app.models import SupplierCounter
    from app.utils import SUPPLIER_START_SETTING, supplier_number_start

    if supplier_numbers_in_use():
        return
    raw = (raw or '').strip()
    try:
        start = int(raw)
    except ValueError:
        flash('Der Startwert der Lieferantennummern muss eine Zahl sein — nicht übernommen.',
              'warning')
        return
    if start < 1:
        flash('Der Startwert der Lieferantennummern muss positiv sein — nicht übernommen.',
              'warning')
        return
    if start == supplier_number_start():
        return
    AppSetting.set(SUPPLIER_START_SETTING, str(start))
    counter = db.session.get(SupplierCounter, 1)
    if counter is not None:
        counter.next_seq = start


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

        # Land des Mandanten. Ein Wechsel zieht die Länder-Defaults nach (siehe
        # unten, nach den uebrigen Feldern), deshalb den alten Stand merken.
        previous_country = country_mod.current_code()
        new_country = country_mod.normalize_code(request.form.get('org_country'))
        country_changed = new_country is not None and new_country != previous_country
        if new_country is not None:
            AppSetting.set(country_mod.SETTING_KEY, new_country)

        # Startwert des Lieferanten-Nummernkreises — nur solange noch keine
        # Lieferantennummer vergeben ist (danach ist das Feld deaktiviert und fehlt).
        if 'supplier_number_start' in request.form:
            _save_supplier_number_start(request.form.get('supplier_number_start'))

        # Übergabe an die Steuerberatung (Format an/aus) — das Feld gibt es nur, wenn eine
        # Erweiterung ein Format angemeldet hat; unbekannte Werte werden verworfen.
        handover_svc.save_setting_from_form(request.form)

        # Optionaler Luftbild-WMS fuer alle Karten (nur https; ohne Layer
        # wirkungslos, siehe app.country.map_config).
        ortho_url = (request.form.get('map_ortho_wms_url') or '').strip()
        if ortho_url and not ortho_url.lower().startswith('https://'):
            flash('Die Luftbild-Adresse muss mit https:// beginnen — nicht übernommen.', 'warning')
        else:
            AppSetting.set('map.ortho_wms_url', ortho_url or None)
            for key in ('map_ortho_wms_layers', 'map_ortho_attribution'):
                val = (request.form.get(key) or '').strip()
                AppSetting.set(key.replace('map_', 'map.', 1), val or None)

        # Standard-USt fuer Wasserpositionen (nur bekannte Saetze). Bei einem
        # Landwechsel stammt der gepostete Wert noch aus dem alten Land — dann
        # entscheidet apply_country_defaults.
        water_raw = (request.form.get('tax_water_rate') or '').strip().replace(',', '.')
        if water_raw and not country_changed:
            try:
                water_rate = Decimal(water_raw)
            except (InvalidOperation, ValueError):
                water_rate = None
            if water_rate is not None and water_rate in set(tax_service.known_rate_values()):
                AppSetting.set(tax_service.WATER_RATE_KEY, str(water_rate))

        # Voranmeldungszeitraum der USt (nur bekannte Werte; fehlt das Feld, bleibt er).
        vat_period = request.form.get('tax_vat_return_period')
        if vat_period in tax_service.VAT_RETURN_PERIODS:
            AppSetting.set(tax_service.VAT_RETURN_PERIOD_KEY, vat_period)

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

        # Kleinunternehmer-Hinweis (opt-in; gedruckt nur in nicht
        # USt-pflichtigen Jahren, siehe invoices.services.invoice_legal_context)
        sb_note = (request.form.get('invoice_small_business_note') or '').strip()
        AppSetting.set('invoice.small_business_note', sb_note[:300] or None)

        # Absenderadresse (einzeilig, Klartext)
        sender_address = request.form.get('invoice_sender_address', '').strip()
        AppSetting.set('invoice.sender_address', sender_address if sender_address else None)

        # E-Rechnung (ZUGFeRD in Rechnungs-PDFs). Die strukturierten wg.*-Felder
        # speichert oben schon die _WG_MAP-Schleife.
        from app.einvoice.mapper import EXEMPT_REASON_KEY
        from app.einvoice.service import ENABLED_KEY
        AppSetting.set(ENABLED_KEY, 'true' if request.form.get('einvoice_enabled') else 'false')
        exempt = (request.form.get('einvoice_exempt_reason') or '').strip()
        AppSetting.set(EXEMPT_REASON_KEY, exempt[:300] or None)
        # Stichtag der B2B-Pflicht (nur deutsche Mandanten haben das Feld im Formular).
        if 'einvoice_mandate_from' in request.form:
            from app.einvoice.obligation import MANDATE_KEY, MANDATE_OPTIONS
            chosen = request.form.get('einvoice_mandate_from', '').strip()
            if chosen in {d.isoformat() for d in MANDATE_OPTIONS}:
                AppSetting.set(MANDATE_KEY, chosen)

        # Zähler-Tauschintervall (Jahre)
        default_interval = country_mod.profile(previous_country).calibration_years
        try:
            interval = int(request.form.get('meter_replacement_interval', default_interval))
        except (TypeError, ValueError):
            interval = default_interval
        if interval < 1:
            interval = default_interval
        AppSetting.set('meters.replacement_interval_years', str(interval))

        country_summary = None
        if country_changed:
            from app.settings.country_defaults import (apply_country_defaults,
                                                       summary_message)
            result = apply_country_defaults(new_country, previous_code=previous_country)
            country_summary = summary_message(result)

        db.session.commit()
        # Hat sich das Rechnungs-Layout gegenueber dem Stand geaendert, mit dem
        # die aktuellen Caches erzeugt wurden (z.B. GiroCode aktiviert,
        # Design/IBAN gewechselt), den Cache gesperrter Rechnungen verwerfen,
        # damit sie beim naechsten Abruf neu rendern statt den alten Stand
        # auszuliefern. Die Signatur wird mitgespeichert, sodass auch bereits
        # vor diesem Feature veraltete Caches beim naechsten Speichern einmalig
        # aufgefrischt werden (gespeicherte Signatur fehlt dann → Mismatch).
        current_sig = _invoice_render_signature()
        if AppSetting.get('invoice.render_cache_signature') != current_sig:
            _invalidate_cached_invoice_documents()
            AppSetting.set('invoice.render_cache_signature', current_sig)
            db.session.commit()
        apply_mail_settings()
        flash('Einstellungen gespeichert.', 'success')
        if country_changed:
            name = country_mod.PROFILES[new_country].name
            msg = f'Land auf {name} umgestellt'
            msg += f' – {country_summary}.' if country_summary else '.'
            flash(msg + ' Nicht benötigte Steuersätze lassen sich unter '
                        '„Steuern" deaktivieren.', 'info')
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
    from app.einvoice.service import settings_context as einvoice_settings_context
    return render_template('settings/index.html', wg=wg, mail=mail_cfg, mail_raw=mail_raw,
                           einvoice=einvoice_settings_context(),
                           db_info=db_info,
                           org_type=org_type(),
                           number_ranges=_number_ranges(),
                           org_country=country_mod.current_code(),
                           country_choices=country_mod.COUNTRY_CHOICES,
                           map_ortho={
                               'url': AppSetting.get('map.ortho_wms_url') or '',
                               'layers': AppSetting.get('map.ortho_wms_layers') or '',
                               'attribution': AppSetting.get('map.ortho_attribution') or '',
                           },
                           **_tax_card_context(),
                           doc_format=doc_format,
                           invoice_design=invoice_design,
                           invoice_designs=available_designs(),
                           invoice_contact_info=contact_info,
                           invoice_contact_info_font_size=get_contact_info_font_size(),
                           invoice_sender_address=get_invoice_sender_address(),
                           invoice_small_business_note=AppSetting.get('invoice.small_business_note') or '',
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


# ---------------------------------------------------------------------------
# Steuersaetze (Tab „Steuern")
# ---------------------------------------------------------------------------

def _tax_card_context():
    """Render-Kontext der Steuersatz-Karte (Vollseite + htmx-Neuladen)."""
    rows = TaxRate.query.order_by(TaxRate.rate).all()
    water_rate = tax_service.water_tax_rate()
    return dict(
        tax_rate_rows=rows,
        # Auswahl fuer die Standard-USt Wasser: aktive Saetze + der aktuell
        # gepflegte, falls er inzwischen deaktiviert wurde.
        water_rate_options=tax_service.tax_rates(include=water_rate),
        water_tax_rate=water_rate,
        vat_return_period=tax_service.vat_return_period(),
        country_profile=country_mod.current_profile(),
    )


def _parse_tax_rate_form(form, rate_obj=None):
    """Liest das Steuersatz-Formular. ``(data, error)``; der Satz selbst ist
    nur bei der Neuanlage editierbar (danach Schluessel der Historie)."""
    data = {
        'label': (form.get('label') or '').strip() or None,
        'active': bool(form.get('active')),
    }
    if rate_obj is None:
        raw = (form.get('rate') or '').strip().replace(',', '.')
        if not raw:
            return None, 'Bitte einen Steuersatz in Prozent angeben.'
        try:
            rate = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None, 'Ungültiger Steuersatz — bitte eine Zahl eingeben.'
        if rate < 0 or rate >= 100:
            return None, 'Der Steuersatz muss zwischen 0 und 100 % liegen.'
        if rate != rate.quantize(Decimal('0.01')):
            return None, 'Höchstens zwei Nachkommastellen.'
        if TaxRate.query.filter(TaxRate.rate == rate).first() is not None:
            return None, (f'Den Steuersatz {tax_service.default_label(rate)} gibt es bereits '
                          '— ggf. dort wieder aktivieren.')
        data['rate'] = rate
    elif not data['active']:
        # Die Standard-USt fuer Wasser darf nicht aus der Auswahl verschwinden.
        if Decimal(str(rate_obj.rate)) == tax_service.water_tax_rate():
            return None, ('Dieser Satz ist als Standard-USt für Wasser eingestellt. '
                          'Bitte zuerst einen anderen Standardsatz wählen.')
    return data, None


def _tax_rate_body(rate_obj, form=None):
    return render_template('settings/_tax_rate_form_body.html',
                           rate_obj=rate_obj, form=form)


def _tax_rate_saved(rate_id):
    """204 + HX-Trigger fuers Steuersatz-Modal (schliessen + Karte neu laden)."""
    resp = make_response('', 204)
    resp.headers['HX-Trigger'] = json.dumps({
        'closeTaxRateModal': True,
        'taxRateSaved': {'rate_id': rate_id},
    })
    return resp


@bp.route('/tax-rates/new', methods=['GET', 'POST'])
@login_required
def tax_rate_new():
    if request.method == 'POST':
        data, err = _parse_tax_rate_form(request.form)
        if err:
            flash(err, 'danger')
            return _tax_rate_body(None, request.form)
        row = TaxRate(rate=data['rate'], label=data['label'], active=data['active'])
        err = handover_svc.apply_tax_rate_fields(row, request.form)
        if err:
            flash(err, 'danger')
            return _tax_rate_body(None, request.form)
        db.session.add(row)
        db.session.commit()
        flash(f'Steuersatz {tax_service.default_label(row.rate)} angelegt.', 'success')
        return _tax_rate_saved(row.id)
    return _tax_rate_body(None)


@bp.route('/tax-rates/<int:rate_id>', methods=['GET', 'POST'])
@login_required
def tax_rate_edit(rate_id):
    row = db.get_or_404(TaxRate, rate_id)
    if request.method == 'POST':
        data, err = _parse_tax_rate_form(request.form, row)
        if err:
            flash(err, 'danger')
            return _tax_rate_body(row, request.form)
        err = handover_svc.apply_tax_rate_fields(row, request.form)
        if err:
            flash(err, 'danger')
            return _tax_rate_body(row, request.form)
        row.label = data['label']
        row.active = data['active']
        db.session.commit()
        flash(f'Steuersatz {tax_service.default_label(row.rate)} gespeichert.', 'success')
        return _tax_rate_saved(row.id)
    return _tax_rate_body(row)


@bp.route('/tax-rates/card')
@login_required
def tax_rates_card():
    """Steuersatz-Karte als Fragment — nach dem Speichern im Modal neu geladen,
    damit ungespeicherte Eingaben im Einstellungsformular erhalten bleiben."""
    return render_template('settings/_tax_rates_card.html', **_tax_card_context())


@bp.route('/country-defaults', methods=['POST'])
@login_required
def apply_country_defaults_route():
    """„Länder-Defaults übernehmen": Standardsaetze ergaenzen/aktivieren,
    Wasser-USt + Eichfrist auf die Länder-Defaults setzen, optional fremde
    Saetze deaktivieren (nie loeschen)."""
    from app.settings.country_defaults import apply_country_defaults, summary_message
    code = country_mod.current_code()
    result = apply_country_defaults(
        code, overwrite=True,
        deactivate_foreign=bool(request.form.get('deactivate_foreign')))
    db.session.commit()
    summary = summary_message(result)
    name = country_mod.PROFILES[code].name
    if summary:
        flash(f'Länder-Defaults ({name}) übernommen – {summary}.', 'success')
    else:
        flash(f'Länder-Defaults ({name}) waren bereits aktiv — nichts geändert.', 'info')
    return redirect(url_for('settings.index', _anchor='pane-tax'))



def _number_ranges():
    """Anzeige der Nummernkreise (Einstellungen → Mandant)."""
    from app.customers.numbers import supplier_numbers_in_use
    from app.utils import next_customer_number, next_supplier_number, supplier_number_start

    return {
        "next_customer": next_customer_number(peek=True),
        "next_supplier": next_supplier_number(peek=True),
        "supplier_start": supplier_number_start(),
        "supplier_locked": supplier_numbers_in_use(),
    }
