"""Faelligkeiten der Teilbetraege (rein, ohne DB).

Die Einstellung ist eine Liste „TT.MM.“ (Standard „15.02.;15.05.;15.08.;15.11.“).
Sie wird auf die konkrete Abrechnungsperiode abgebildet: jedes Datum liegt
innerhalb ``[start, end]`` — auch bei Perioden, die nicht im Jänner beginnen
(Juni–Juni: 15.08., 15.11., 15.02., 15.05.).
"""
import calendar
import re
from datetime import date, timedelta

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


def _add_months(d, months):
    month = d.month - 1 + months
    return _on(d.year + month // 12, month % 12 + 1, d.day)


def part_of_period(start, end, count, seq):
    """Teilzeitraum ``seq`` (1-basiert) von ``count`` gleich langen Abschnitten
    der Periode ``[start, end]`` als ``(von, bis)`` — der Leistungszeitraum einer
    Teilrechnung (vier Teilbetraege → Quartale, zwei → Halbjahre).

    Ganze Monate, wenn die Periode am Monatsersten beginnt, am Monatsletzten
    endet und sich ihre Monate glatt teilen; sonst nach Tagen. ``None`` bei
    ungueltigem ``seq``/``count``."""
    if not count or not 1 <= seq <= count or end < start:
        return None
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    month_end = end.day == calendar.monthrange(end.year, end.month)[1]
    if start.day == 1 and month_end and months % count == 0:
        step = months // count
        part_start = _add_months(start, step * (seq - 1))
        part_end = _add_months(start, step * seq) - timedelta(days=1)
        return part_start, part_end
    days = (end - start).days + 1
    part_start = start + timedelta(days=days * (seq - 1) // count)
    part_end = start + timedelta(days=days * seq // count - 1)
    return part_start, part_end


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
