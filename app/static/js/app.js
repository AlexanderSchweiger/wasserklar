/* WG Verwaltung — zentrale Client-Skripte.
   Wird im <head> mit defer geladen, laeuft also GENAU EINMAL pro Browser-Tab:
   durch hx-boost wird nur der <body> getauscht, dieser Script-Tag im <head>
   bleibt bestehen und wird nicht neu ausgewertet. Dadurch koennen Listener
   hier einmalig registriert werden, ohne doppelte Einhaengungen bei
   Seitenwechseln. */

(function () {
  // --- Widget-Initialisierer ------------------------------------------------

  function tomSelectScore(query) {
    var q = query.toLowerCase();
    return function (item) {
      var text = (item.text || '').toLowerCase();
      if (text.startsWith(q)) return 2;
      if (text.indexOf(q) >= 0) return 1;
      return 0;
    };
  }

  // Optionale Farb-Swatch-Darstellung: Selects, deren <option>s ein
  // data-color tragen (z.B. das Projekt-Feld der Buchung), bekommen einen
  // farbigen Punkt vor dem Text. Die Farbzuordnung wird einmalig aus dem DOM
  // gelesen, damit der Renderer nicht von TomSelect-Interna abhaengt.
  function colorSwatchRender(el) {
    var colorByValue = {};
    var hasColor = false;
    el.querySelectorAll('option[data-color]').forEach(function (o) {
      if (o.value) { colorByValue[o.value] = o.getAttribute('data-color'); hasColor = true; }
    });
    if (!hasColor) return null;
    function dot(color) {
      return '<span style="display:inline-block;width:.8rem;height:.8rem;border-radius:50%;'
        + 'flex:0 0 auto;background:' + color + ';margin-right:.45rem;vertical-align:middle;'
        + 'border:1px solid rgba(0,0,0,.15)"></span>';
    }
    return {
      option: function (data, escape) {
        var color = colorByValue[data.value];
        return '<div class="d-flex align-items-center py-1">'
          + (color ? dot(escape(color)) : '')
          + '<span>' + escape(data.text) + '</span></div>';
      },
      item: function (data, escape) {
        var color = colorByValue[data.value];
        return '<div class="d-flex align-items-center">'
          + (color ? dot(escape(color)) : '')
          + '<span>' + escape(data.text) + '</span></div>';
      }
    };
  }

  // In einem Modal die Dropdown-Liste an <body> haengen, sonst schneidet der
  // Modal-Overflow (besonders modal-dialog-scrollable) sie ab. App-weit gueltig.
  function dropdownParentFor(el) {
    return el.closest('.modal') ? 'body' : null;
  }

  // Async-Create fuer TomSelects mit data-create-url: tippt der Nutzer einen
  // neuen Namen, wird der Kontakt per POST im Hintergrund (mit leeren
  // Adressdaten) angelegt und sofort ausgewaehlt — ohne separates Modal.
  // data-create-type steuert is_customer/is_supplier (default: supplier).
  function makeCreateHandler(el) {
    var url = el.dataset.createUrl;
    var type = el.dataset.createType || 'supplier';
    return function (input, callback) {
      var name = (input || '').trim();
      if (!name) { callback(false); return; }
      var fd = new FormData();
      fd.append('name', name);
      fd.append('force', '1');   // Dubletten-Dialog ueberspringen (Inline-Flow)
      if (type === 'customer' || type === 'both') fd.append('is_customer', '1');
      if (type === 'supplier' || type === 'both') fd.append('is_supplier', '1');
      var form = el.closest('form');
      var csrf = form && form.querySelector('input[name="csrf_token"]');
      if (csrf) fd.append('csrf_token', csrf.value);
      fetch(url, {
        method: 'POST', body: fd, headers: {'X-Requested-With': 'XMLHttpRequest'}
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data && data.ok) callback({value: String(data.id), text: data.name});
          else callback(false);
        })
        .catch(function () { callback(false); });
    };
  }

  // Aus einem gecachten/serialisierten DOM-Snapshot (htmx-History-Restore,
  // View-Transition, POST→Redirect auf dieselbe URL) kann ein bereits
  // gerendertes .ts-wrapper-DOM neben dem <select> zurueckbleiben, waehrend die
  // lebende .tomselect-Referenz am Element fehlt (frischer, aus einem HTML-
  // String deserialisierter Knoten). Der Guard `if (el.tomselect) return`
  // greift dann NICHT und es wird ein ZWEITES Widget daneben gebaut — das
  // beobachtete „doppelte Status-Dropdown". Vor dem Neuaufbau daher etwaige
  // Waisen-Wrapper entfernen. Idempotent: ohne Waisen passiert nichts.
  function removeOrphanTomSelect(el) {
    var sib;
    while ((sib = el.previousElementSibling) && sib.classList.contains('ts-wrapper')) sib.remove();
    while ((sib = el.nextElementSibling) && sib.classList.contains('ts-wrapper')) sib.remove();
    el.classList.remove('tomselected', 'ts-hidden-accessible');
  }

  function initTomSelects(root) {
    (root || document).querySelectorAll('select.tom-select').forEach(function (el) {
      if (el.tomselect) return;
      removeOrphanTomSelect(el);
      var cfg = {
        allowEmptyOption: true,
        selectOnTab: true,            // Tab waehlt die markierte Option + springt weiter
        score: tomSelectScore,
        dropdownParent: dropdownParentFor(el)
      };
      var render = colorSwatchRender(el) || {};
      if (el.dataset.createUrl) {
        cfg.create = makeCreateHandler(el);
        cfg.createOnBlur = false;   // nur bewusst per Enter/Tab/Klick, nicht beim Wegklicken
        render.option_create = function (data, escape) {
          return '<div class="create"><i class="fas fa-user-plus me-1"></i>'
            + '„' + escape(data.input) + '“ als neuen Kontakt anlegen</div>';
        };
      }
      if (Object.keys(render).length) cfg.render = render;
      new TomSelect(el, cfg);
    });
  }

  // TomSelect-Instanzen vor dem htmx-History-Snapshot zerstoeren. Sonst wird das
  // bereits gerenderte .ts-wrapper-DOM mitgecacht; beim Restore fehlt dem
  // Original-<select> die .tomselect-Referenz, der Guard greift nicht, und es
  // entsteht ein ZWEITES Widget daneben (das beobachtete „Doppelt-Anzeigen").
  function destroyTomSelects(root) {
    (root || document).querySelectorAll('select.tom-select, select.color-select').forEach(function (el) {
      if (el.tomselect) { try { el.tomselect.destroy(); } catch (e) {} }
    });
  }

  function initColorSelects(root) {
    (root || document).querySelectorAll('select.color-select').forEach(function (el) {
      if (el.tomselect) return;
      removeOrphanTomSelect(el);
      new TomSelect(el, {
        allowEmptyOption: true,
        selectOnTab: true,
        controlInput: null,
        dropdownParent: el.closest('.modal') ? 'body' : null,
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
  function spinnerReset() { pending = 0; spinnerHide(); }

  // --- Event-Listener (einmalig im head, bleiben ueber hx-boost-Swaps) -----

  // VOR jedem Swap die TomSelects im Ziel zerstoeren. Sonst bleiben — speziell
  // bei dropdownParent:'body' (Modal) — die ans <body> gehaengten Dropdown-
  // Listen als Waisen zurueck, wenn htmx den alten Formularinhalt ersetzt
  // (z.B. Fehler-Redisplay). Folge sonst: das neu gebaute Feld oeffnet eine
  // leere/kaputte Liste (nur Scrollbalken). Danach baut afterSwap frisch auf.
  document.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail && e.detail.target) destroyTomSelects(e.detail.target);
  });

  // Nach jedem HTMX-Swap (Boost oder Inline) Widgets neu initialisieren.
  document.addEventListener('htmx:afterSwap', function (e) {
    initTomSelects(e.detail.target);
    initColorSelects(e.detail.target);
  });

  // Selbstheilung des Lade-Spinners nach einer geboosteten Voll-Navigation.
  // Der pending-Counter (s.u.) ueberlebt hx-boost-Swaps, das #global-spinner-
  // Element NICHT — es liegt im getauschten <body>. Faellt waehrend einer
  // (langsamen) Navigation ein Hintergrund-Request mit dem Body-Swap zusammen,
  // wird sein ausloesendes Element abgekoppelt; dessen htmx:afterRequest feuert
  // dann auf dem detachten Element und erreicht document nie -> spinnerEnd
  // bleibt aus -> pending haengt > 0 -> der 300ms-Timer setzt .active auf den
  // frischen Spinner, der nie wieder entfernt wird (Vollbild-Overlay haengt).
  // Da nach einem Body-Swap ohnehin die fertige Seite steht, ist ein Nav-
  // Spinner hier immer obsolet: Counter + Timer hart zuruecksetzen, Overlay weg.
  // NUR fuer den Body-Swap (geboostete Navigation) — Fragment-/Badge-Swaps
  // duerfen einen WAEHREND einer langsamen Navigation laufenden Spinner nicht
  // vorzeitig abraeumen.
  document.addEventListener('htmx:afterSwap', function (e) {
    var t = e && e.detail && e.detail.target;
    if (t && t.tagName === 'BODY') spinnerReset();
  });

  // Vor dem History-Snapshot: TomSelects abbauen, damit kein gerendertes
  // Widget-DOM in den Cache wandert (sonst Doppel-Init beim Zurueck/Vorwaerts).
  document.addEventListener('htmx:beforeHistorySave', function () {
    destroyTomSelects(document);
  });

  // Nach einem History-Restore (Cache-Hit ODER Server-Reload) die Widgets auf
  // dem wiederhergestellten, sauberen DOM frisch aufbauen.
  document.addEventListener('htmx:historyRestore', function () {
    initTomSelects(); initColorSelects();
  });
  document.addEventListener('htmx:historyCacheMissLoad', function () {
    initTomSelects(); initColorSelects();
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

  // Zurueck/Vorwaerts-Navigation: Spinner ist nach einem pageshow nie
  // legitim aktiv — Seite wurde gerade frisch (oder aus bfcache) angezeigt.
  // Immer hart resetten, nicht nur bei e.persisted: bei einem hx-boost
  // "Zurueck" mit history-cache-miss feuert HTMX einen internen Fetch ueber
  // loadHistoryFromServer, der den htmx:beforeRequest/afterRequest-Lifecycle
  // NICHT durchlaeuft → unser `pending` wuerde nie dekrementiert, der
  // Spinner haengt.
  window.addEventListener('pageshow', spinnerReset);

  // HTMX-History-Restore (hx-boost Zurueck/Vorwaerts, sowohl Cache-Hit als
  // auch Cache-Miss). Setzt den Spinner zurueck, falls in dem Loch zwischen
  // popstate und Body-Swap noch eine alte Request-Spur haengt.
  document.addEventListener('htmx:historyRestore', spinnerReset);
  document.addEventListener('htmx:historyCacheMissLoad', spinnerReset);

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
