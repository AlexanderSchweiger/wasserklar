import hashlib
from decimal import Decimal, ROUND_HALF_UP

from flask import (
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.extensions import db
from app.models import (
    Account,
    BankStatement,
    BankStatementLine,
    BankStatementLineAllocation,
    OpenItem,
    RealAccount,
)

from app.bank_import import bp
from app.bank_import import matching, parsers, services


_CENT = Decimal("0.01")


def _round2(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _line_render_context(lines):
    """Gemeinsamer Render-Kontext fuer die Buchungs-Zeilen (preview + Row-Swap).

    Liefert die Dropdowns/Statusdaten, die ``bank_import/_row.html`` braucht —
    einmal fuer alle Zeilen (Vollseite) und fuer genau eine Zeile (HTMX-Swap
    nach einer Aktion), damit beide Pfade dieselbe Logik teilen.
    """
    accounts = Account.query.filter_by(active=True).order_by(Account.name).all()

    customer_ids = {l.matched_customer_id for l in lines if l.matched_customer_id}
    open_items_by_customer = {}
    if customer_ids:
        ops = OpenItem.query.filter(
            OpenItem.customer_id.in_(customer_ids),
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        ).all()
        for op in ops:
            open_items_by_customer.setdefault(op.customer_id, []).append(op)

    # Komplette Liste aller offenen Posten (fuer das Tom-Select-Dropdown in
    # Zeilen, bei denen kein Kunde automatisch erkannt wurde — der Nutzer
    # kann dann manuell durchsuchen statt einen Workaround zu basteln).
    all_open_items = (
        OpenItem.query.filter(
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        )
        .join(OpenItem.customer)
        .order_by(OpenItem.date.desc())
        .all()
    )

    # Pro Zeile: Beziehung Bankbetrag <-> offener Posten (auf Cent gerundet,
    # sonst loest ein Decimal('100.00') vs. Decimal('100.000') faelschlich
    # eine Ueberzahlungs-Warnung aus). Wert ist eine Tuple
    # (kind, diff, op_open_balance) mit kind in {'match','over','under'}.
    payment_status = {}
    for l in lines:
        if l.line_status != BankStatementLine.STATUS_PENDING:
            # Nach dem Verbuchen ist der OP auf 0 — eine Diff-Warnung waere
            # irrefuehrend ("Ueberzahlung um den vollen Eingangsbetrag").
            continue
        if l.matched_open_item_id and l.matched_open_item is not None:
            amt = _round2(l.amount)
            bal = _round2(l.matched_open_item.open_balance)
            diff = amt - bal
            if diff == 0:
                kind = "match"
            elif diff > 0:
                kind = "over"
            else:
                kind = "under"
            payment_status[l.id] = (kind, abs(diff), bal)

    return {
        "accounts": accounts,
        "open_items_by_customer": open_items_by_customer,
        "all_open_items": all_open_items,
        "payment_status": payment_status,
    }


@bp.route("/")
@login_required
def index():
    statements = (
        BankStatement.query.order_by(BankStatement.uploaded_at.desc()).limit(100).all()
    )
    return render_template("bank_import/index.html", statements=statements)


@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    real_accounts = (
        RealAccount.query.filter_by(active=True).order_by(RealAccount.name).all()
    )
    if not real_accounts:
        flash(
            "Bitte legen Sie zuerst ein Bankkonto unter Buchhaltung › Bankkonten an.",
            "warning",
        )
        return redirect(url_for("bank_import.index"))

    if request.method == "GET":
        default_ra = next((ra for ra in real_accounts if ra.is_default), real_accounts[0])
        return render_template(
            "bank_import/upload.html",
            real_accounts=real_accounts,
            default_ra=default_ra,
        )

    # POST
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Bitte eine Datei auswählen.", "danger")
        return redirect(url_for("bank_import.upload"))

    content = file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        flash("Die Datei ist zu groß (Maximum: 10 MB).", "danger")
        return redirect(url_for("bank_import.upload"))

    try:
        fmt = parsers.detect_format(content)
        parsed = parsers.parse(content, fmt)
    except Exception as e:  # noqa: BLE001
        flash(f"Datei konnte nicht gelesen werden: {e}", "danger")
        return redirect(url_for("bank_import.upload"))

    if not parsed.lines:
        flash("Die Datei enthält keine Buchungen.", "warning")
        return redirect(url_for("bank_import.upload"))

    # RealAccount bestimmen: erst IBAN-Match, sonst Form-Auswahl
    real_account = None
    if parsed.account_iban:
        real_account = RealAccount.query.filter_by(
            iban=parsed.account_iban, active=True
        ).first()
    if real_account is None:
        ra_id = request.form.get("real_account_id", type=int)
        if not ra_id:
            flash(
                "Die Konto-IBAN in der Datei konnte keinem Bankkonto zugeordnet werden. "
                "Bitte wählen Sie das Ziel-Bankkonto aus.",
                "warning",
            )
            return render_template(
                "bank_import/upload.html",
                real_accounts=real_accounts,
                default_ra=real_accounts[0],
                require_manual_account=True,
            )
        real_account = RealAccount.query.get(ra_id)
        if real_account is None or not real_account.active:
            flash("Ungültiges Bankkonto.", "danger")
            return redirect(url_for("bank_import.upload"))

    file_hash = hashlib.sha256(content).hexdigest()
    existing = BankStatement.query.filter_by(
        real_account_id=real_account.id, file_hash=file_hash
    ).first()
    if existing:
        flash(
            f"Dieser Auszug wurde bereits am {existing.uploaded_at:%d.%m.%Y %H:%M} importiert.",
            "warning",
        )
        return redirect(url_for("bank_import.preview", statement_id=existing.id))

    stmt = BankStatement(
        format=parsed.format,
        filename=file.filename[:255],
        file_hash=file_hash,
        real_account_id=real_account.id,
        statement_reference=parsed.statement_reference,
        booking_date_from=parsed.booking_date_from,
        booking_date_to=parsed.booking_date_to,
        opening_balance=parsed.opening_balance,
        closing_balance=parsed.closing_balance,
        currency=parsed.currency or "EUR",
        uploaded_by_id=current_user.id,
    )
    db.session.add(stmt)
    db.session.flush()

    for idx, pl in enumerate(parsed.lines):
        line = BankStatementLine(
            statement_id=stmt.id,
            line_index=idx,
            booking_date=pl.booking_date,
            value_date=pl.value_date,
            amount=pl.amount,
            currency=pl.currency,
            counterparty_name=(pl.counterparty_name or "")[:200] or None,
            counterparty_iban=(pl.counterparty_iban or "")[:34] or None,
            purpose=pl.purpose,
            end_to_end_id=(pl.end_to_end_id or "")[:100] or None,
            tx_id=(pl.tx_id or "")[:100] or None,
        )
        matching.match_line(line)
        db.session.add(line)

    db.session.commit()
    flash(
        f"{len(parsed.lines)} Buchung(en) eingelesen. Bitte Zuordnung prüfen und committen.",
        "success",
    )
    return redirect(url_for("bank_import.preview", statement_id=stmt.id))


@bp.route("/statements/<int:statement_id>")
@login_required
def preview(statement_id):
    stmt = BankStatement.query.get_or_404(statement_id)
    lines = stmt.lines.all()
    total_in = sum((l.amount for l in lines if l.amount > 0), start=0)
    total_out = sum((l.amount for l in lines if l.amount < 0), start=0)

    return render_template(
        "bank_import/preview.html",
        stmt=stmt,
        lines=lines,
        total_in=total_in,
        total_out=total_out,
        **_line_render_context(lines),
    )


@bp.route("/statements/<int:statement_id>/lines/<int:line_id>", methods=["POST"])
@login_required
def update_line(statement_id, line_id):
    line = BankStatementLine.query.get_or_404(line_id)
    if line.statement_id != statement_id:
        abort(404)
    if line.line_status != BankStatementLine.STATUS_PENDING:
        abort(400, "Zeile bereits verbucht oder übersprungen.")

    action = request.form.get("action")

    if action == "toggle_selected":
        line.selected = request.form.get("selected") == "1"

    elif action == "clear_match":
        # Nur OP/Invoice loesen — der erkannte Kunde bleibt, damit der Nutzer
        # einen anderen OP desselben Kunden auswaehlen kann.
        line.matched_invoice_id = None
        line.matched_open_item_id = None
        line.match_type = BankStatementLine.MATCH_MANUAL

    elif action == "clear_customer":
        # Kompletter Reset der automatischen Zuordnung. Danach erscheint die
        # Zeile als "keine Zuordnung" mit dem grossen OP-TomSelect ueber alle
        # offenen Posten.
        line.matched_invoice_id = None
        line.matched_open_item_id = None
        line.matched_customer_id = None
        line.match_type = BankStatementLine.MATCH_MANUAL

    elif action == "set_open_item":
        op_id = request.form.get("open_item_id", type=int)
        if op_id:
            op = OpenItem.query.get(op_id)
            if op:
                line.matched_open_item_id = op.id
                line.matched_invoice_id = op.invoice_id
                line.matched_customer_id = op.customer_id
                line.match_type = BankStatementLine.MATCH_MANUAL
                line.selected = True
                line.override_account_id = None
        else:
            line.matched_open_item_id = None
            line.matched_invoice_id = None
            line.match_type = BankStatementLine.MATCH_MANUAL

    elif action == "set_account":
        acc_id = request.form.get("account_id", type=int)
        line.override_account_id = acc_id or None

    elif action == "set_split":
        _apply_split(line)

    elif action == "clear_split":
        line.allocations.clear()
        line.match_type = None

    else:
        abort(400, "Unbekannte Aktion.")

    db.session.commit()

    # HTMX: nur die betroffene Zeile zuruecktauschen (kein Full-Reload), sonst
    # klassischer Redirect als No-JS-Fallback.
    if request.headers.get("HX-Request"):
        stmt = BankStatement.query.get_or_404(statement_id)
        resp = make_response(render_template(
            "bank_import/_row.html",
            stmt=stmt,
            line=line,
            **_line_render_context([line]),
        ))
        if action == "set_split":
            # Modal nach erfolgreichem Speichern schliessen.
            resp.headers["HX-Trigger"] = "bankSplitSaved"
        return resp
    return redirect(url_for("bank_import.preview", statement_id=statement_id))


def _apply_split(line):
    """Allocations aus dem Split-Formular uebernehmen (ersetzt bestehende).

    Form-Felder: wiederholte ``alloc_op_id`` + ``alloc_amount`` (paarweise).
    Validiert Summe == Buchungsbetrag und setzt die Zeile in den Split-Modus
    (einfache 1:1-Felder werden geleert).
    """
    op_ids = request.form.getlist("alloc_op_id")
    amounts = request.form.getlist("alloc_amount")

    rows = []
    total = Decimal("0")
    for raw_op, raw_amt in zip(op_ids, amounts):
        raw_op = (raw_op or "").strip()
        raw_amt = (raw_amt or "").strip().replace(",", ".")
        if not raw_op or not raw_amt:
            continue
        try:
            op_id = int(raw_op)
            amt = Decimal(raw_amt).quantize(_CENT, rounding=ROUND_HALF_UP)
        except (ValueError, ArithmeticError):
            abort(400, "Ungültige Aufteilungs-Position.")
        if amt <= 0:
            abort(400, "Teilbeträge müssen größer als 0 sein.")
        op = OpenItem.query.get(op_id)
        if op is None:
            abort(400, f"Offener Posten #{op_id} nicht gefunden.")
        rows.append((op, amt))
        total += amt

    if len(rows) < 2:
        abort(400, "Eine Aufteilung braucht mindestens zwei Positionen.")
    if total != _round2(line.amount):
        abort(400, "Die Summe der Teilbeträge muss dem Buchungsbetrag entsprechen.")

    line.allocations.clear()
    db.session.flush()
    for op, amt in rows:
        line.allocations.append(
            BankStatementLineAllocation(
                open_item_id=op.id, amount=amt,
            )
        )
    # In Split-Modus wechseln: 1:1-Zuordnung aufloesen, Kunde als Info behalten.
    line.matched_open_item_id = None
    line.matched_invoice_id = None
    line.override_account_id = None
    line.match_type = BankStatementLine.MATCH_SPLIT
    line.selected = True


@bp.route("/statements/<int:statement_id>/lines/<int:line_id>/split")
@login_required
def split_form(statement_id, line_id):
    """Liefert das Aufteilen-Modal (Formular-Body) fuer eine Zeile per HTMX."""
    line = BankStatementLine.query.get_or_404(line_id)
    if line.statement_id != statement_id:
        abort(404)

    # Vorbelegung: bestehende Aufteilung, sonst die offenen Posten des erkannten
    # Kunden mit auto-verteiltem Betrag (Rest schrumpft je Zeile auf 0).
    prefill = []
    if line.allocations:
        for a in line.allocations:
            prefill.append((a.open_item, _round2(a.amount)))
    elif line.matched_customer_id:
        remaining = _round2(line.amount)
        cust_ops = (
            OpenItem.query.filter(
                OpenItem.customer_id == line.matched_customer_id,
                OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
            )
            .order_by(OpenItem.date.asc())
            .all()
        )
        for op in cust_ops:
            if remaining <= 0:
                break
            take = min(_round2(op.open_balance), remaining)
            if take > 0:
                prefill.append((op, take))
                remaining -= take

    all_open_items = (
        OpenItem.query.filter(
            OpenItem.status.in_([OpenItem.STATUS_OPEN, OpenItem.STATUS_PARTIAL]),
        )
        .join(OpenItem.customer)
        .order_by(OpenItem.date.desc())
        .all()
    )

    return render_template(
        "bank_import/_split_modal.html",
        line=line,
        statement_id=statement_id,
        prefill=prefill,
        all_open_items=all_open_items,
    )


@bp.route("/statements/<int:statement_id>/commit", methods=["POST"])
@login_required
def commit(statement_id):
    stmt = BankStatement.query.get_or_404(statement_id)
    if stmt.status == BankStatement.STATUS_COMMITTED:
        flash("Dieser Auszug wurde bereits vollständig verbucht.", "info")
        return redirect(url_for("bank_import.preview", statement_id=statement_id))

    stats = services.commit_statement(statement_id, current_user.id)

    if stats["committed"]:
        flash(
            f"{stats['committed']} Buchung(en) verbucht, "
            f"{stats['skipped']} übersprungen.",
            "success",
        )
    if stats["errors"]:
        for err in stats["errors"]:
            flash(err, "danger")

    return redirect(url_for("bank_import.preview", statement_id=statement_id))


@bp.route("/statements/<int:statement_id>/delete", methods=["POST"])
@login_required
def delete(statement_id):
    stmt = BankStatement.query.get_or_404(statement_id)
    has_committed = stmt.lines.filter_by(
        line_status=BankStatementLine.STATUS_COMMITTED
    ).first()
    if has_committed:
        flash(
            "Auszug kann nicht gelöscht werden — es existieren bereits verbuchte Zeilen.",
            "danger",
        )
        return redirect(url_for("bank_import.preview", statement_id=statement_id))

    db.session.delete(stmt)
    db.session.commit()
    flash("Bankauszug gelöscht.", "success")
    return redirect(url_for("bank_import.index"))
