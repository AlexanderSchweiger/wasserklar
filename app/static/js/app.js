/* WG Verwaltung — zentrale Client-Skripte.
   Wird im <head> mit defer geladen, laeuft also GENAU EINMAL pro Browser-Tab:
   durch hx-boost wird nur der <body> getauscht, dieser Script-Tag im <head>
   bleibt bestehen und wird nicht neu ausgewertet. Dadurch koennen Listener
   hier einmalig registriert werden, ohne doppelte Einhaengungen bei
   Seitenwechseln. */

(function () {
  // --- Widget-Initialisierer ------------------------------------------------

  function initTomSelects(root) {
    (root || document).querySelectorAll('select.tom-select').forEach(function (el) {
      if (el.tomselect) return;
      new TomSelect(el, {
        allowEmptyOption: true,
        selectOnTab: true,
        score: function (query) {
          var q = query.toLowerCase();
          return function (item) {
            var text = (item.text || '').toLowerCase();
            if (text.startsWith(q)) return 2;
            if (text.indexOf(q) >= 0) return 1;
            return 0;
          };
        }
      });
    });
  }

  function initColorSelects(root) {
    (root || document).querySelectorAll('select.color-select').forEach(function (el) {
      if (el.tomselect) return;
      new TomSelect(el, {
        allowEmptyOption: true,
        selectOnTab: true,
        controlInput: null,
        render: {
          option: function (data, escape) {
            if (!data.value) return '<div class="py-1 px-2 text-muted">\u2013</div>';
            return '<div class="d-flex align-items-center py-1 px-2">'
              + '<span class="badge bg-' + escape(data.value) + ' text-' + escape(data.value) + '-fg" style="width:1.2rem;height:1.2rem;display:inline-block">&nbsp;</span>'
              + '<span class="ms-2">' + escape(data.text) + '</span></div>';
          },
          item: function (data, escape) {
            if (!data.value) return '<div class="text-muted">\u2013</div>';
            return '<div class="d-flex align-items-center">'
              + '<span class="badge bg-' + escape(data.value) + ' text-' + escape(data.value) + '-fg" style="width:1.2rem;height:1.2rem;display:inline-block">&nbsp;</span></div>';
          }
        }
      });
    });
  }

  // --- Boost-Ausschluss fuer Downloads / externe Links ----------------------

  // Pfad-/Query-Muster, bei denen der Server eine Datei ausliefert
  // (PDF/DOCX/ZIP/CSV/XLSX/Excel-Downloads, Backup-Export usw.).
  // Achtung: "/export/" oder "/backups/" alleine (SaaS-Index-Seiten) bleiben
  // geboostet — ausgeschlossen werden nur nicht-fuehrende Pfadsegmente wie
  // "/accounting/.../export".
  var DL_PATH_RX = new RegExp(
      '(?:^|\\/)(?:pdf|pdfs|docx|xlsx|csv|zip|excel)(?:[\\/?#]|$)' +
      '|\\/bulk[-_][^\\/?#]+(?:[\\/?#]|$)' +
      '|\\/download(?:[-_][^\\/?#]*)?(?:[\\/?#]|$)' +
      '|\\/[^\\/?#]+\\/(?:export|backup|backups)(?:[\\/?#]|$)',
      'i'
  );
  var DL_QUERY_RX = /[?&]fmt=(pdf|docx|csv|xlsx|excel)/i;

  function looksLikeDownload(href) {
    if (!href) return false;
    return DL_PATH_RX.test(href) || DL_QUERY_RX.test(href);
  }

  function isExternal(href) {
    if (!href) return false;
    if (/^https?:\/\//i.test(href)) {
      return href.indexOf(window.location.origin) !== 0;
    }
    return false;
  }

  function isBoosted(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      var v = cur.getAttribute && cur.getAttribute('hx-boost');
      if (v === 'false') return false;
      if (v === 'true') return true;
      cur = cur.parentElement;
    }
    return false;
  }

  // --- Globaler Lade-Spinner ------------------------------------------------

  var pending = 0;
  var timer = null;

  function spinnerShow() {
    if (timer) return;
    timer = setTimeout(function () {
      var el = document.getElementById('global-spinner');
      if (el) el.classList.add('active');
    }, 300);
  }
  function spinnerHide() {
    clearTimeout(timer);
    timer = null;
    var el = document.getElementById('global-spinner');
    if (el) el.classList.remove('active');
  }
  function spinnerStart() { if (++pending === 1) spinnerShow(); }
  function spinnerEnd()   { if (--pending <= 0) { pending = 0; spinnerHide(); } }

  // --- Event-Listener (einmalig im head, bleiben ueber hx-boost-Swaps) -----

  // Nach jedem HTMX-Swap (Boost oder Inline) Widgets neu initialisieren.
  document.addEventListener('htmx:afterSwap', function (e) {
    initTomSelects(e.detail.target);
    initColorSelects(e.detail.target);
  });

  // Spinner fuer HTMX-Requests (inkl. geboosteten Navigationen).
  document.addEventListener('htmx:beforeRequest', spinnerStart);
  document.addEventListener('htmx:afterRequest',  spinnerEnd);

  // fetch()-Monkey-Patch (fuer E-Mail-Versand, Test-Mail etc.)
  var _fetch = window.fetch;
  window.fetch = function () {
    spinnerStart();
    return _fetch.apply(this, arguments).finally(spinnerEnd);
  };

  // Capture-Phase: geboostete Klicks auf Download-/Externe-Links abfangen,
  // damit HTMX nicht versucht, eine Binary-Response in den Body zu swappen.
  // stopImmediatePropagation verhindert, dass HTMX's Element-Listener feuert;
  // die Browser-Default-Action (href folgen / Datei laden) bleibt erhalten.
  // Bewusst KEIN Spinner: bei Content-Disposition-Downloads erfolgt kein
  // Seitenwechsel, ein Spinner wuerde bis zum Fallback-Timer haengen;
  // fuer externe Links zeigt der Browser selbst den Ladezustand.
  document.addEventListener('click', function (e) {
    var link = e.target.closest && e.target.closest('a[href]');
    if (!link) return;
    if (!isBoosted(link)) return;
    var href = link.getAttribute('href') || '';
    var needsNative =
      href === '' || href.charAt(0) === '#' ||
      href.indexOf('javascript:') === 0 ||
      href.indexOf('mailto:') === 0 ||
      href.indexOf('tel:') === 0 ||
      link.target === '_blank' ||
      link.hasAttribute('download') ||
      isExternal(href) ||
      looksLikeDownload(href);
    if (!needsNative) return;
    e.stopImmediatePropagation();
  }, true);

  // Dasselbe fuer Formulare: Download-Submits (Bulk-PDF, Files-Download-Zip,
  // Report-Excel etc.) und externe Ziele muessen nativ durchgereicht werden.
  // File-Uploads brauchen aber den Spinner als User-Feedback waehrend der
  // Upload-Zeit.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    if (!isBoosted(form)) return;
    var action = form.getAttribute('action') || '';
    // formaction auf Submit-Buttons beruecksichtigen
    var submitter = e.submitter;
    if (submitter && submitter.getAttribute('formaction')) {
      action = submitter.getAttribute('formaction');
    }
    var hasFileUpload = !!form.querySelector('input[type="file"]');
    var isDownload = looksLikeDownload(action);
    var isExt = isExternal(action);
    if (!hasFileUpload && !isDownload && !isExt) return;
    e.stopImmediatePropagation();
    // Nur bei Upload Spinner zeigen — Download-Response und externe
    // Navigationen brauchen keinen Modal-Blocker.
    if (hasFileUpload && !isDownload) {
      spinnerStart();
      setTimeout(spinnerEnd, 30000);
    }
  }, true);

  // Zurueck/Vorwaerts-Navigation: Seite aus bfcache wiederhergestellt →
  // Spinner zuruecksetzen (Download-Fallback-Timer lief evtl. noch).
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) {
      pending = 0;
      clearTimeout(timer);
      timer = null;
      var el = document.getElementById('global-spinner');
      if (el) el.classList.remove('active');
    }
  });

  // Erst-Init, nachdem alle defer-Scripte (TomSelect, HTMX) geladen sind.
  // defer-Scripte laufen vor DOMContentLoaded; daher beim Erst-Lauf einmal
  // triggern und zusaetzlich auf das Event hoeren, falls HTML noch parst.
  function firstInit() {
    initTomSelects();
    initColorSelects();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', firstInit, { once: true });
  } else {
    firstInit();
  }
})();
