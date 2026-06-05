"""Schriftführungs-Domäne: deutsche Labels, Badges und Upload-Regeln.

Englische Enum-Keys (Naming-Konvention), deutsche Anzeige-Labels. Die
Label-/Badge-Dicts werden über einen Blueprint-Context-Processor in die
Schriftführungs-Templates injiziert (siehe ``__init__.py``). Badges folgen der
Tabler-Konvention (dezente Soft-Badges ``bg-{color}-lt``, nie ``text-white-lt``).
"""

from app.models import (
    Meeting, MeetingAttendance, MeetingResolution, MeetingProtocol,
    MeetingDeliveryLog, SchriftverkehrDocument,
)

MEETING_TYPE_LABELS = {
    Meeting.TYPE_BOARD: "Vorstandssitzung",
    Meeting.TYPE_ASSEMBLY: "Hauptversammlung",
}
MEETING_TYPE_LABELS_PLURAL = {
    Meeting.TYPE_BOARD: "Vorstandssitzungen",
    Meeting.TYPE_ASSEMBLY: "Hauptversammlungen",
}

MEETING_STATUS_LABELS = {
    Meeting.STATUS_PLANNING: "Planung",
    Meeting.STATUS_INVITED: "Eingeladen",
    Meeting.STATUS_HELD: "Abgehalten",
}
MEETING_STATUS_BADGE = {
    Meeting.STATUS_PLANNING: "bg-secondary-lt",
    Meeting.STATUS_INVITED: "bg-azure-lt",
    Meeting.STATUS_HELD: "bg-success-lt",
}

ATTENDANCE_STATUS_LABELS = {
    MeetingAttendance.STATUS_PRESENT: "Anwesend",
    MeetingAttendance.STATUS_EXCUSED: "Entschuldigt",
    MeetingAttendance.STATUS_ABSENT: "Abwesend",
}
ATTENDANCE_STATUS_BADGE = {
    MeetingAttendance.STATUS_PRESENT: "bg-success-lt",
    MeetingAttendance.STATUS_EXCUSED: "bg-yellow-lt",
    MeetingAttendance.STATUS_ABSENT: "bg-secondary-lt",
}

RESOLUTION_STATUS_LABELS = {
    MeetingResolution.STATUS_ACCEPTED: "Angenommen",
    MeetingResolution.STATUS_REJECTED: "Abgelehnt",
    MeetingResolution.STATUS_POSTPONED: "Vertagt",
}
RESOLUTION_STATUS_BADGE = {
    MeetingResolution.STATUS_ACCEPTED: "bg-success-lt",
    MeetingResolution.STATUS_REJECTED: "bg-danger-lt",
    MeetingResolution.STATUS_POSTPONED: "bg-yellow-lt",
}

PROTOCOL_STATUS_LABELS = {
    MeetingProtocol.STATUS_DRAFT: "Entwurf",
    MeetingProtocol.STATUS_FINAL: "Abgeschlossen",
}
PROTOCOL_STATUS_BADGE = {
    MeetingProtocol.STATUS_DRAFT: "bg-yellow-lt",
    MeetingProtocol.STATUS_FINAL: "bg-success-lt",
}

DELIVERY_METHOD_LABELS = {
    MeetingDeliveryLog.METHOD_EMAIL: "E-Mail",
    MeetingDeliveryLog.METHOD_POST: "Post",
}
DELIVERY_ACTION_LABELS = {
    MeetingDeliveryLog.ACTION_SENT: "Versendet",
    MeetingDeliveryLog.ACTION_RESENT: "Erneut versendet",
    MeetingDeliveryLog.ACTION_PRINTED: "Gedruckt",
}

DOC_TYPE_LABELS = {
    SchriftverkehrDocument.TYPE_INCOMING: "Eingehend",
    SchriftverkehrDocument.TYPE_OUTGOING: "Ausgehend",
    SchriftverkehrDocument.TYPE_OTHER: "Sonstiges",
}

# Upload: erlaubte Dateitypen + Größenlimit (Protokolle + Schriftverkehr).
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".odt", ".ods", ".md", ".txt",
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_UPLOAD_HINT = "PDF, Word, Excel, OpenOffice/LibreOffice, Markdown oder Text — max. 5 MB"
