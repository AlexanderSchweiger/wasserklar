/* Passwort-Stärkeanzeige + Anzeigen-Button.
 *
 * Reine UX-Hilfe — die verbindliche Prüfung passiert serverseitig in
 * app/auth/password_policy.py. Initialisiert automatisch jedes Element
 * mit [data-pw-field].
 */
(function () {
  "use strict";

  var MIN_LENGTH = 12;

  // Knappe Blockliste — Spiegel der serverseitigen COMMON_PASSWORDS.
  // Maßgeblich bleibt der Server.
  var COMMON = [
    "password", "passwort", "12345678", "123456789", "1234567890",
    "qwertzuiop", "qwertyuiop", "password123", "passwort123", "willkommen",
    "wasserklar", "test1234", "changeme123", "geheim123", "hallo12345"
  ];

  function isCommon(pw) {
    return COMMON.indexOf(pw.toLowerCase().trim()) !== -1;
  }

  function containsIdentity(pw, tokens) {
    var low = pw.toLowerCase();
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (t && t.length >= 3 && low.indexOf(t) !== -1) {
        return true;
      }
    }
    return false;
  }

  function score(pw, tokens) {
    if (!pw) return 0;
    if (isCommon(pw) || containsIdentity(pw, tokens)) return 0;
    var s = 0;
    if (pw.length >= 8) s++;
    if (pw.length >= MIN_LENGTH) s++;
    if (pw.length >= 16) s++;
    var classes = 0;
    if (/[a-z]/.test(pw)) classes++;
    if (/[A-Z]/.test(pw)) classes++;
    if (/[0-9]/.test(pw)) classes++;
    if (/[^A-Za-z0-9]/.test(pw)) classes++;
    if (classes >= 3) s++;
    return Math.min(s, 4);
  }

  var LABELS = ["Sehr schwach", "Schwach", "Mittel", "Gut", "Stark"];
  var COLORS = ["bg-danger", "bg-danger", "bg-warning", "bg-success", "bg-success"];

  function setCheck(li, ok) {
    if (!li) return;
    li.classList.toggle("text-success", ok);
    li.classList.toggle("text-secondary", !ok);
    var icon = li.querySelector("[data-pw-icon]");
    if (icon) {
      icon.className = "fas me-1 " + (ok ? "fa-check-circle" : "fa-circle");
    }
  }

  function expandTokens(raw) {
    var tokens = [];
    raw.toLowerCase().split(",").forEach(function (t) {
      t = t.trim();
      if (!t) return;
      tokens.push(t);
      if (t.indexOf("@") !== -1) tokens.push(t.split("@")[0]);
    });
    return tokens;
  }

  function initField(field) {
    var input = field.querySelector("[data-pw-input]");
    if (!input) return;

    var toggle = field.querySelector("[data-pw-toggle]");
    if (toggle) {
      toggle.addEventListener("click", function (e) {
        e.preventDefault();
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        var icon = toggle.querySelector("i");
        if (icon) icon.className = show ? "fas fa-eye-slash" : "fas fa-eye";
        toggle.setAttribute(
          "aria-label", show ? "Passwort verbergen" : "Passwort anzeigen"
        );
      });
    }

    var bar = field.querySelector("[data-pw-bar]");
    if (!bar) return; // Feld ohne Meter (z.B. "aktuelles Passwort")

    var tokens = expandTokens(field.getAttribute("data-pw-identity") || "");
    var checks = {
      length: field.querySelector('[data-pw-check="length"]'),
      common: field.querySelector('[data-pw-check="common"]'),
      identity: field.querySelector('[data-pw-check="identity"]')
    };
    var labelEl = field.querySelector("[data-pw-label]");

    function update() {
      var pw = input.value;
      var sc = score(pw, tokens);
      bar.style.width = (pw ? (sc + 1) * 20 : 0) + "%";
      bar.className = "progress-bar " + COLORS[sc];
      if (labelEl) labelEl.textContent = pw ? LABELS[sc] : "";

      setCheck(checks.length, pw.length >= MIN_LENGTH);
      setCheck(checks.common, pw.length > 0 && !isCommon(pw));
      setCheck(checks.identity, pw.length > 0 && !containsIdentity(pw, tokens));
    }

    input.addEventListener("input", update);
    update();
  }

  function initAll() {
    var fields = document.querySelectorAll("[data-pw-field]");
    for (var i = 0; i < fields.length; i++) {
      initField(fields[i]);
    }
  }

  // Bei hx-boost-Navigation feuert DOMContentLoaded nicht erneut — dieses
  // Skript laeuft dann zwar (block scripts), aber das Event kaeme nie. Daher
  // direkt initialisieren, wenn das DOM schon geparst ist.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll, { once: true });
  } else {
    initAll();
  }
})();
