"""Generische Pagination fuer Listenseiten.

- ``per_page`` pro Listenseite wird als ``UserPreference`` gespeichert
  (Key: ``per_page:<page_key>``) und als Default beim naechsten Besuch
  uebernommen.
- ``per_page == 0`` bedeutet "alle Datensaetze" (keine Pagination).
- ``paginate_query`` fuer SQLAlchemy-Queries (LIMIT/OFFSET, COUNT separat).
- ``paginate_list`` fuer bereits materialisierte Listen (z.B. nach
  Post-Processing wie bei der Buchungsseite).

Die Helper greifen NICHT auf ``request`` oder ``current_user`` zu, wenn
keine App-Context anliegt — Aufrufer sind ausschliesslich Routen.
"""

from dataclasses import dataclass, field
from typing import List, Sequence

from flask import request
from flask_login import current_user

from app.extensions import db


PER_PAGE_OPTIONS: List[int] = [10, 25, 50, 100]
PER_PAGE_ALL: int = 0
DEFAULT_PER_PAGE: int = 25
PREF_KEY_PREFIX = "per_page:"


@dataclass
class Pagination:
    items: list
    page: int
    per_page: int
    total: int
    page_key: str
    options: List[int] = field(default_factory=lambda: PER_PAGE_OPTIONS + [PER_PAGE_ALL])

    @property
    def show_all(self) -> bool:
        return self.per_page == PER_PAGE_ALL

    @property
    def pages(self) -> int:
        if self.show_all or self.per_page <= 0 or self.total == 0:
            return 1
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.page > 1 and not self.show_all

    @property
    def has_next(self) -> bool:
        return not self.show_all and self.page < self.pages

    @property
    def prev_page(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_page(self) -> int:
        return min(self.pages, self.page + 1)

    @property
    def first_index(self) -> int:
        if self.total == 0:
            return 0
        if self.show_all:
            return 1
        return (self.page - 1) * self.per_page + 1

    @property
    def last_index(self) -> int:
        if self.show_all:
            return self.total
        return min(self.page * self.per_page, self.total)

    def iter_pages(self, left_edge: int = 1, around: int = 2, right_edge: int = 1):
        """Liefert Seitennummern fuer die Pagination, mit ``None`` als Ellipsis."""
        last = 0
        for num in range(1, self.pages + 1):
            if (
                num <= left_edge
                or (self.page - around <= num <= self.page + around)
                or num > self.pages - right_edge
            ):
                if last and num - last > 1:
                    yield None
                yield num
                last = num


def _coerce_per_page(raw) -> int | None:
    """Wandelt einen rohen Wert in eine zulaessige per_page-Zahl um.

    Erlaubt: alle Eintraege aus PER_PAGE_OPTIONS, sowie 0 / "all" fuer "alle".
    Liefert ``None`` bei ungueltigem Input.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s == "":
            return None
        if s == "all":
            return PER_PAGE_ALL
        try:
            val = int(s)
        except ValueError:
            return None
    else:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
    if val == PER_PAGE_ALL or val in PER_PAGE_OPTIONS:
        return val
    return None


def get_user_per_page(page_key: str, default: int = DEFAULT_PER_PAGE) -> int:
    """Liest die gespeicherte per_page-Vorgabe des aktuellen Benutzers."""
    from app.models import UserPreference

    if not getattr(current_user, "is_authenticated", False):
        return default
    pref = (
        UserPreference.query
        .filter_by(user_id=current_user.id, key=PREF_KEY_PREFIX + page_key)
        .first()
    )
    if pref is None:
        return default
    coerced = _coerce_per_page(pref.value)
    return coerced if coerced is not None else default


def set_user_per_page(page_key: str, value: int) -> None:
    """Speichert die per_page-Vorgabe des aktuellen Benutzers persistent."""
    from app.models import UserPreference

    if not getattr(current_user, "is_authenticated", False):
        return
    coerced = _coerce_per_page(value)
    if coerced is None:
        return
    pref = (
        UserPreference.query
        .filter_by(user_id=current_user.id, key=PREF_KEY_PREFIX + page_key)
        .first()
    )
    if pref is None:
        pref = UserPreference(
            user_id=current_user.id,
            key=PREF_KEY_PREFIX + page_key,
            value=str(coerced),
        )
        db.session.add(pref)
    else:
        pref.value = str(coerced)
    db.session.commit()


def resolve_per_page(page_key: str) -> int:
    """per_page aus URL lesen (und persistieren), sonst gespeicherten Wert nehmen."""
    raw = request.args.get("per_page")
    coerced = _coerce_per_page(raw)
    if coerced is not None:
        # User hat in URL einen expliziten Wert mitgegeben -> als neuen Default
        # speichern (idempotent: nur schreiben, wenn er sich geaendert hat).
        if coerced != get_user_per_page(page_key, default=-1):
            set_user_per_page(page_key, coerced)
        return coerced
    return get_user_per_page(page_key)


def resolve_page() -> int:
    raw = request.args.get("page", "1")
    try:
        page = int(raw)
    except (TypeError, ValueError):
        page = 1
    return max(1, page)


def paginate_query(query, page_key: str) -> Pagination:
    """SQLAlchemy-Query mit LIMIT/OFFSET paginieren."""
    page = resolve_page()
    per_page = resolve_per_page(page_key)
    # ``.count()`` packt die Query in eine COUNT-Subquery; das ORDER BY der
    # Originalquery wird intern weggekapselt und stoert nicht.
    total = query.count()
    if per_page == PER_PAGE_ALL:
        items = query.all()
    else:
        items = query.limit(per_page).offset((page - 1) * per_page).all()
        # Wenn der User auf einer Seite steht, die nach einer Loeschung leer
        # waere, faellt er hier sanft auf die letzte tatsaechlich existierende
        # Seite zurueck.
        if not items and total > 0 and page > 1:
            page = max(1, (total + per_page - 1) // per_page)
            items = query.limit(per_page).offset((page - 1) * per_page).all()
    return Pagination(
        items=items, page=page, per_page=per_page, total=total, page_key=page_key
    )


def paginate_list(seq: Sequence, page_key: str) -> Pagination:
    """Eine bereits materialisierte Liste paginieren (Post-Processing-Faelle)."""
    page = resolve_page()
    per_page = resolve_per_page(page_key)
    total = len(seq)
    if per_page == PER_PAGE_ALL:
        items = list(seq)
    else:
        # Sanft zurueckfallen, wenn ueber das Ende hinaus paginiert wurde.
        if total > 0 and (page - 1) * per_page >= total:
            page = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        items = list(seq[start : start + per_page])
    return Pagination(
        items=items, page=page, per_page=per_page, total=total, page_key=page_key
    )


def page_url(**overrides) -> str:
    """Erzeugt eine URL fuer die aktuelle Route mit ueberlagerten Query-Args.

    - ``request.args`` wird komplett uebernommen
    - Schluessel mit Wert ``None`` werden entfernt
    - Schluessel mit Wert werden gesetzt/ersetzt

    Wird in app/__init__.py als Jinja-Global registriert.
    """
    merged: dict[str, str] = {}
    for k, v in request.args.items():
        merged[k] = v
    for k, v in overrides.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = str(v)
    from urllib.parse import urlencode
    qs = urlencode(merged)
    return request.path + (("?" + qs) if qs else "")
