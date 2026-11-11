import json
from datetime import date
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
                                  meter_replacement_interval, org_type)
from app.invoices import render_cache
from app.settings import invoice_layout
from app.wg import ORG_TYPES


# Render-Cache gesperrter Rechnungen: Regeln in app/invoices/render_cache.py.
# Die alten Namen bleiben fuer bestehende Aufrufer erhalten.
_INVOICE_RENDER_KEYS = render_cache.RENDER_KEYS
_INVOICE_RENDER_DEFAULTS = render_cache.RENDER_DEFAULTS
_invoice_render_signature = render_cache.signature
_invalidate_cached_invoice_documents = render_cache.invalidate_cached_documents


def _save_sepa_settings(form):
    """SEPA-Lastschrift: Glaeubiger-ID, Mandats-Praefix und Hinweistext (alles optional).

    Ungueltiges wird nicht uebernommen (Warnung), der Rest der Seite schon.
    """
    from app.banking import normalize_creditor_id, sepa_id_valid
    from app.direct_debit import notice

    cid = normalize_creditor_id(form.get('sepa_creditor_id'))
    issue = notice.creditor_id_issue(cid)
    if issue:
        flash(issue + ' Nicht übernommen.', 'warning')
    else:
        AppSetting.set(notice.CREDITOR_ID_KEY, cid or None)

    prefix = (form.get('sepa_mandate_prefix') or '').strip()
    if prefix and (len(prefix) > 20 or not sepa_id_valid(prefix)):
        flash('Das Präfix der Mandatsreferenz darf nur Buchstaben, Ziffern und - / . enthalten '
              '(höchstens 20 Zeichen, keine Leerzeichen) — nicht übernommen.', 'warning')
    else:
        AppSetting.set(notice.MANDATE_PREFIX_KEY, prefix or None)

    _save_mandate_form_settings(form)

    # Titel + Text des Lastschrift-Hinweises (gleiche Regeln wie die SaaS-Seite
    # „Rechnungsdesign“).
    values, errors = invoice_layout.sepa_notice_values(form)
    for msg in errors:
        flash(msg, 'warning')
    for key, value in values.items():
        AppSetting.set(key, value)


def _save_mandate_form_settings(form):
    """Mandatsformular: Umfang, Einleitungstext, Beilage im Rechnungsdruck."""
    from app.direct_debit import mandate_form

    scope = ' '.join((form.get('sepa_mandate_scope') or '').split())[:300]
    AppSetting.set(mandate_form.SCOPE_KEY, scope or None)

    enclosure = form.get('sepa_mandate_form_enclosure') == 'on'
    AppSetting.set(mandate_form.ENCLOSURE_KEY, 'true' if enclosure else None)

    intro = (form.get('sepa_mandate_form_intro') or '').strip()[:1000]
    if intro:
        sample = {key: f'[{key}]' for key, _label in mandate_form.INTRO_PLACEHOLDERS}
        try:
            mandate_form.render_intro(intro, sample)
        except Exception:
            flash('Der Einleitungstext des Mandatsformulars enthält einen Platzhalter-Fehler '
                  '— nicht übernommen.', 'warning')
            return
    AppSetting.set(mandate_form.INTRO_KEY, intro or None)


def _sepa_context():
    from app.direct_debit import mandate_form, notice
    return {
        'mandate_scope': AppSetting.get(mandate_form.SCOPE_KEY) or '',
        'default_scope': mandate_form.default_scope(),
        'mandate_form_intro': AppSetting.get(mandate_form.INTRO_KEY) or '',
        'default_intro': mandate_form.default_intro(),
        'intro_placeholders': mandate_form.INTRO_PLACEHOLDERS,
        'mandate_form_enclosure': AppSetting.get(mandate_form.ENCLOSURE_KEY) == 'true',
        'creditor_id': AppSetting.get(notice.CREDITOR_ID_KEY) or '',
        'mandate_prefix': AppSetting.get(notice.MANDATE_PREFIX_KEY) or '',
        'notice_title': AppSetting.get(notice.NOTICE_TITLE_KEY) or '',
        'notice_text': AppSetting.get(notice.NOTICE_TEXT_KEY) or '',
        'default_title': notice.default_title(),
        'default_text': notice.default_text(),
        'placeholders': notice.NOTICE_PLACEHOLDERS,
    }


def _save_share_area_rule(form):
    """Speichert die Regel "Flaeche je Anteil" (leer = keine Regel). Liefert
    eine Fehlermeldung oder None."""
    from decimal import Decimal, InvalidOperation
    from app import wg
    raw = (form.get('share_area_per_share') or '').strip().replace(',', '.')
    if raw:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return 'Fläche je Anteil: bitte eine Zahl in m² angeben.'
        if value <= 0:
            return 'Fläche je Anteil muss größer als 0 sein.'
        AppSetting.set(wg.SHARE_AREA_PER_SHARE_KEY, format(value.normalize(), 'f'))
    else:
        AppSetting.set(wg.SHARE_AREA_PER_SHARE_KEY, None)
    rounding = form.get('share_area_rounding', wg.ROUND_UP)
    AppSetting.set(wg.SHARE_AREA_ROUNDING_KEY,
                   rounding if rounding in wg.SHARE_ROUNDINGS else wg.ROUND_UP)
    return None


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


# Reiter der Einstellungsseite (``?tab=<name>``, Pane-ID ``pane-<name>``).
# Der aktive Reiter reist als Query-Parameter, nicht als ``#anker``: hx-boost
# folgt dem Redirect nach dem Speichern per XHR, und dabei geht ein Fragment
# verloren — die Seite sprang immer auf den ersten Reiter zurueck.
SETTINGS_TABS = ('mandant', 'tax', 'kontakt', 'email', 'rechnung', 'zaehler', 'db', 'danger')
DEFAULT_TAB = 'mandant'


def _settings_tab(raw):
    """Gueltiger Reiter-Name oder der Standard (unbekannte Werte verworfen)."""
    raw = (raw or '').strip()
    if raw.startswith('pane-'):
        raw = raw[len('pane-'):]
    if raw == 'danger' and not current_user.is_admin:
        return DEFAULT_TAB
    return raw if raw in SETTINGS_TABS else DEFAULT_TAB


def settings_url(tab=None):
    """URL der Einstellungsseite mit geoeffnetem Reiter."""
    tab = _settings_tab(tab)
    if tab == DEFAULT_TAB:
        return url_for('settings.index')
    return url_for('settings.index', tab=tab)


def _form_flag(form, name):
    """Checkbox/Hidden-Wert als bool. ``"false"``/``"0"``/``"off"`` sind aus —
    ein verstecktes Feld mit ``value="false"`` ist sonst ein nicht leerer String
    und damit „an“ (so verschwanden die SMTP-Daten der SaaS-Basis-Mandanten)."""
    return (form.get(name) or '').strip().lower() in ('true', '1', 'on', 'yes')


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
            # Nur bei echter Aenderung schreiben (die Auswahl liefert „7.00“,
            # gespeichert ist oft „7“ — sonst aendert jedes Speichern den Wert).
            if (water_rate is not None
                    and water_rate in set(tax_service.known_rate_values())
                    and water_rate != tax_service.water_tax_rate()):
                AppSetting.set(tax_service.WATER_RATE_KEY, str(water_rate))

        # Voranmeldungszeitraum der USt (nur bekannte Werte; fehlt das Feld, bleibt er).
        vat_period = request.form.get('tax_vat_return_period')
        if vat_period in tax_service.VAT_RETURN_PERIODS:
            AppSetting.set(tax_service.VAT_RETURN_PERIOD_KEY, vat_period)

        # WG-Kontaktdaten — nur gesendete Felder (ein ausgeblendetes Feld leert nichts).
        for attr in _WG_MAP:
            if f'wg_{attr}' not in request.form:
                continue
            val = request.form.get(f'wg_{attr}', '').strip()
            AppSetting.set(f'wg.{attr}', val if val else None)

        # SEPA-Lastschrift (Karte unter „Bankdaten“) — nur, wenn sie im Formular steht.
        if request.form.get('sepa_fields') == '1':
            _save_sepa_settings(request.form)

        # Darstellung der Rechnung (Logo, Kontaktinfo, Absenderzeile, Dokumentformat,
        # Design, Schalter) — nur mit Marker im Formular. Eine Erweiterung kann die
        # Karten von dieser Seite nehmen (SaaS: eigene Seite „Rechnungsdesign“),
        # ohne dass ein Speichern hier die Werte leert.
        if invoice_layout.form_has_fields(request.form):
            for msg in invoice_layout.save_invoice_layout(request.form):
                flash(msg, 'danger')

        # Regel "Flaeche je Anteil" (Karte „Genossenschaftsanteile“) — nur mit Marker.
        if request.form.get('share_rule_fields') == '1':
            error = _save_share_area_rule(request.form)
            if error:
                flash(error, 'danger')

        # Mail-Versandmodus (Checkbox bzw. verstecktes Feld der SaaS). Bei
        # aktivem Plattform-Relay sind die SMTP-Felder im UI disabled und werden
        # nicht mitgesendet — der _MAIL_MAP-Loop würde die gespeicherten
        # mail.*-Werte sonst leeren.
        # Geschrieben wird die Wahl nur, wenn der Schalter im Formular steht
        # (Feld selbst oder Marker mail_relay_field=1 fuer die nicht angehakte
        # Checkbox). Ohne Schalter (OSS-Standalone, SaaS ohne Relay-Recht) bleibt
        # die gespeicherte Wahl bzw. der Config-Default MAIL_PLATFORM_RELAY.
        relay = None
        if ('mail_use_platform_relay' in request.form
                or request.form.get('mail_relay_field') == '1'):
            relay = 'true' if _form_flag(request.form, 'mail_use_platform_relay') else 'false'
            AppSetting.set('mail.use_platform_relay', relay)

        # Mail-Server (nur wenn kein Plattform-Relay gewaehlt ist und die Felder
        # im Formular stehen — ein Formular ohne SMTP-Felder leert nichts).
        if relay != 'true' and 'mail_server' in request.form:
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
        # Hat sich das Rechnungs-Layout geaendert (z.B. GiroCode aktiviert,
        # Design/IBAN gewechselt), den Cache gesperrter Rechnungen verwerfen.
        render_cache.refresh()
        apply_mail_settings()
        flash('Einstellungen gespeichert.', 'success')
        if country_changed:
            name = country_mod.PROFILES[new_country].name
            msg = f'Land auf {name} umgestellt'
            msg += f' – {country_summary}.' if country_summary else '.'
            flash(msg + ' Nicht benötigte Steuersätze lassen sich unter '
                        '„Steuern" deaktivieren.', 'info')
        return redirect(settings_url(request.form.get('active_tab')))

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
    # Ohne gespeicherten Wert ist TLS an (Port 587 = STARTTLS, der Normalfall) —
    # sonst speicherte die Ersteinrichtung stillschweigend „TLS aus“.
    stored_tls = AppSetting.get('mail.use_tls')
    mail_raw['use_tls_bool'] = (stored_tls is None
                                or str(stored_tls).lower() in ('true', '1', 'yes'))
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

    from app.einvoice.service import settings_context as einvoice_settings_context
    return render_template('settings/index.html', wg=wg, mail=mail_cfg, mail_raw=mail_raw,
                           active_tab=_settings_tab(request.args.get('tab')),
                           einvoice=einvoice_settings_context(),
                           sepa=_sepa_context(),
                           db_info=db_info,
                           org_type=org_type(),
                           share_binding=_share_binding_context(),
                           number_ranges=_number_ranges(),
                           org_country=country_mod.current_code(),
                           country_choices=country_mod.COUNTRY_CHOICES,
                           map_ortho={
                               'url': AppSetting.get('map.ortho_wms_url') or '',
                               'layers': AppSetting.get('map.ortho_wms_layers') or '',
                               'attribution': AppSetting.get('map.ortho_attribution') or '',
                           },
                           **_tax_card_context(),
                           **invoice_layout.form_context(),
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
        return redirect(settings_url('danger'))

    from app.settings.reset import reset_tenant_data
    try:
        result = reset_tenant_data()
    except Exception as exc:  # noqa: BLE001 — Fehler dem Admin sichtbar machen
        db.session.rollback()
        current_app.logger.exception('Mandant-Reset fehlgeschlagen')
        flash(f'Zurücksetzen fehlgeschlagen: {exc}', 'danger')
        return redirect(settings_url('danger'))

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
    return render_template('settings/_tax_rates_card.html', tax_card_fragment=True,
                           **_tax_card_context())


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
    return redirect(settings_url('tax'))



def _share_binding_context():
    """Anzeige der Anteils-Bindung + Regel "Flaeche je Anteil" (Einstellungen → Mandant)."""
    from app import wg
    from app.settings_service import share_area_rule, share_binding as current_binding
    current = current_binding()
    rule = share_area_rule()
    return {"current": current, "label": wg.SHARE_BINDING_LABELS[current],
            "per_share": format(rule["per_share"].normalize(), 'f').replace('.', ',')
                         if rule else '',
            "rounding": rule["rounding"] if rule else wg.ROUND_UP,
            "rounding_labels": wg.SHARE_ROUNDING_LABELS}


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


# ---------------------------------------------------------------------------
# Bindung der Genossenschaftsanteile (Liegenschaft ↔ Mitglied)
# ---------------------------------------------------------------------------
@bp.route('/share-binding', methods=['GET', 'POST'])
@login_required
def share_binding():
    """Assistent zum Umstellen der Anteils-Bindung. GET ist rein lesend
    (Vorschau); POST bucht bei Liegenschaft → Mitglied die Anfangsbestaende
    und stellt um. In keiner Richtung wird etwas geloescht."""
    from app import wg, wg_shares
    from app.settings_service import is_wassergenossenschaft, share_binding as current_binding
    if not is_wassergenossenschaft():
        flash('Anteile gibt es nur im Mandant-Typ Wassergenossenschaft.', 'warning')
        return redirect(url_for('settings.index'))
    current = current_binding()
    back = settings_url('mandant')

    if request.method == 'POST':
        target = request.form.get('target')
        if target not in wg.SHARE_BINDINGS or target == current:
            flash('Die Anteile werden bereits so geführt — nichts geändert.', 'info')
            return redirect(back)
        if target == wg.SHARES_ON_PROPERTY:
            wg_shares.apply_switch_to_property()
            db.session.commit()
            flash('Anteile werden wieder an der Liegenschaft geführt. Das Anteilsbuch '
                  'bleibt gespeichert und ist ausgeblendet.', 'success')
            return redirect(back)
        try:
            on = date.fromisoformat(request.form.get('on', ''))
        except ValueError:
            flash('Bitte ein gültiges Übernahmedatum angeben.', 'danger')
            return redirect(url_for('settings.share_binding'))
        allocations = {}
        for key, raw in request.form.items():
            if not key.startswith('alloc_'):
                continue
            try:
                _, prop_id, cust_id = key.split('_')
                allocations[(int(prop_id), int(cust_id))] = int(raw or 0)
            except ValueError:
                flash('Ungültige Anzahl in der Übernahme-Tabelle.', 'danger')
                return redirect(url_for('settings.share_binding', on=on.isoformat()))
        try:
            contacts, shares = wg_shares.apply_switch_to_person(
                allocations, on, use_member_since=request.form.get('use_member_since') == '1',
                user_id=current_user.id)
            db.session.commit()
        except wg_shares.ShareError as exc:
            db.session.rollback()
            flash(f'Nicht umgestellt: {exc}', 'danger')
            return redirect(url_for('settings.share_binding', on=on.isoformat()))
        flash(f'Anteile werden jetzt am Mitglied geführt. Übernommen: {shares} Anteile '
              f'bei {contacts} Kontakt(en).', 'success')
        return redirect(back)

    ctx = dict(current=current, binding_labels=wg.SHARE_BINDING_LABELS)
    if current == wg.SHARES_ON_PROPERTY:
        try:
            on = date.fromisoformat(request.args.get('on', ''))
        except ValueError:
            on = date.today()
        ctx['preview'] = wg_shares.switch_preview(on)
    else:
        ctx['register_entries'] = db.session.query(wg_shares.WgShareEntry.id).count()
        ctx['register_total'] = wg_shares.total_holdings()
    return render_template('settings/share_binding.html', **ctx)
