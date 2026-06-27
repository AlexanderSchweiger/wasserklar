"""iCal-(.ics)-Erzeugung für Sitzungs-Einladungen — als ``text/calendar``-Anhang
an die Einladungs-Mail. Bewusst handgebaut nach RFC 5545, ohne zusätzliche
Dependency. Zeiten als „floating" lokale Zeit (kein TZID/Z), das akzeptieren die
gängigen Kalender-Clients für Einladungen.
"""
from datetime import datetime, time, timedelta

from flask import current_app


def _brand_slug():
    """Markenname als iCal-taugliches Token (klein, nur alphanumerisch)."""
    name = current_app.config.get("APP_BRAND_NAME", "wasserklar")
    return "".join(c for c in name.lower() if c.isalnum()) or "app"


def _escape(text):
    if not text:
        return ""
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fmt(d, t):
    t = t or time(0, 0)
    return datetime(d.year, d.month, d.day, t.hour, t.minute, t.second).strftime("%Y%m%dT%H%M%S")


def build_meeting_ics(meeting, description=""):
    """Liefert den .ics-Inhalt (bytes) für eine Sitzung mit Datum/Uhrzeit, oder
    ``None``, wenn kein Datum gesetzt ist (dann kein Kalendereintrag möglich)."""
    if not meeting.meeting_date:
        return None

    dtstart = _fmt(meeting.meeting_date, meeting.start_time)
    if meeting.end_time:
        dtend = _fmt(meeting.meeting_date, meeting.end_time)
    elif meeting.start_time:
        end = datetime.combine(meeting.meeting_date, meeting.start_time) + timedelta(hours=2)
        dtend = end.strftime("%Y%m%dT%H%M%S")
    else:
        dtend = _fmt(meeting.meeting_date, time(23, 59))

    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{_brand_slug()}//Schriftfuehrung//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:meeting-{meeting.id}@{_brand_slug()}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_escape(meeting.title)}",
    ]
    if meeting.location:
        lines.append(f"LOCATION:{_escape(meeting.location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
