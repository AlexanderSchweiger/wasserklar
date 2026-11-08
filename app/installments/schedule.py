"""Faelligkeiten der Teilbetraege (rein, ohne DB).

Die Einstellung ist eine Liste „TT.MM.“ (Standard „15.02.;15.05.;15.08.;15.11.“).
Sie wird auf die konkrete Abrechnungsperiode abgebildet: jedes Datum liegt
innerhalb ``[start, end]`` — auch bei Perioden, die nicht im Jänner beginnen
(Juni–Juni: 15.08., 15.11., 15.02., 15.05.).
"""
import calendar
import re
from datetime import date

MAX_INSTALLMENTS = 12

_TOKEN = re.compile(r"^(\d{1,2})\.(\d{1,2})\.?$")


class ScheduleError(ValueError):
    """Ungueltige Faelligkeiten (deutsche Meldung)."""


def parse_due_dates(text):
    """„15.02.;15.05.“ → ``[(15, 2), (15, 5)]``. Trenner: ``;``, ``,``, Leerraum.

    Wirft ``ScheduleError`` bei Tippfehlern, doppelten Daten oder mehr als
    zwoelf Faelligkeiten."""
    raw = [t for t in re.split(r"[;,\s]+", (text or "").strip()) if t]
    if not raw:
        raise ScheduleError("Bitte mindestens eine Fälligkeit angeben (z. B. 15.02.).")
    out = []
    for token in raw:
        m = _TOKEN.match(token)
        if not m:
            raise ScheduleError(f"„{token}“ ist keine Fälligkeit im Format TT.MM.")
        day, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            raise ScheduleError(f"„{token}“ ist kein gültiges Datum.")
        if day > calendar.monthrange(2024, month)[1]:   # 2024: Schaltjahr erlaubt 29.02.
            raise ScheduleError(f"„{token}“ ist kein gültiges Datum.")
        if (day, month) in out:
            raise ScheduleError(f"Die Fälligkeit {token} ist doppelt.")
        out.append((day, month))
    if len(out) > MAX_INSTALLMENTS:
        raise ScheduleError(f"Höchstens {MAX_INSTALLMENTS} Fälligkeiten je Periode.")
    return out


def format_due_dates(spec):
    return ";".join(f"{d:02d}.{m:02d}." for d, m in spec)


def _on(year, month, day):
    """Datum, der Tag auf das Monatsende gekappt (29.02. in Nicht-Schaltjahren)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def dates_for_period(spec, start, end):
    """Faelligkeiten ``spec`` (aus ``parse_due_dates``) als aufsteigende Daten
    innerhalb der Periode ``[start, end]``. Ein Tag/Monat, der in der Periode
    gar nicht vorkommt (Periode kuerzer als ein Jahr), entfaellt."""
    result = []
    for day, month in spec:
        for year in range(start.year, end.year + 1):
            d = _on(year, month, day)
            if start <= d <= end:
                result.append(d)
                break
    return sorted(set(result))
