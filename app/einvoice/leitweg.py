"""Leitweg-ID (XRechnung BT-10): Format und Pruefziffer.

Aufbau: ``Grobadressierung[-Feinadressierung]-Pruefziffer``, z.B.
``04011000-1234512345-06`` oder ``992-90009-96``.

* Grobadressierung: 2-12 Ziffern, beginnt mit dem Laenderkennzeichen
  (01-16 = Bundesland, 99 = Bund).
* Feinadressierung: optional, bis 30 alphanumerische Zeichen.
* Pruefziffer: zwei Ziffern nach ISO 7064 MOD 97-10 ueber Grob- und
  Feinadressierung ohne Bindestriche (wie bei der IBAN).

Die Pruefziffer wird nur bei rein numerischer Adressierung nachgerechnet —
fuer Buchstaben in der Feinadressierung ist die Umrechnung nicht an einer
echten ID belegt, dort lieber nichts beanstanden als eine gueltige ID
abzulehnen. Das Portal der Behoerde prueft selbst.
"""
import re

_PATTERN = re.compile(r"^(?P<grob>(?:0[1-9]|1[0-6]|99)\d{0,10})(?:-(?P<fein>[0-9A-Za-z]{1,30}))?-(?P<check>\d{2})$")


def looks_like(value):
    """True, wenn ``value`` wie eine Leitweg-ID aufgebaut ist (Format, ohne Pruefziffer)."""
    return bool(_PATTERN.match((value or "").strip()))


def check_digit(base):
    """Pruefziffer (zweistellig, als String) zu ``Grob[-Fein]`` — nur fuer Ziffern."""
    digits = re.sub(r"-", "", base)
    return f"{98 - (int(digits + '00') % 97):02d}"


def is_valid(value):
    """True, wenn ``value`` Format und (bei rein numerischer ID) Pruefziffer erfuellt."""
    match = _PATTERN.match((value or "").strip())
    if not match:
        return False
    base = (value or "").strip().rsplit("-", 1)[0]
    if not re.fullmatch(r"[0-9-]+", base):
        return True
    return check_digit(base) == match.group("check")
