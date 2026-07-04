/* Zaehlertausch-Touren — mobile-first Leaflet-Karte.
 *
 * Laeuft auf der Tour-Detailseite (#tour-map). Die Seite wird per
 * hx-boost="false" immer als Vollseite geladen, daher liegt Leaflet im <head>
 * (block head_extra) und ist bei DOMContentLoaded fertig.
 *
 * Konfig kommt aus window.TOUR (von der Seite gesetzt): das JSON aus
 * meter_tours.stops_json plus csrfToken. Die Route ist eine Luftlinien-
 * Heuristik (Server: Nearest-Neighbour + 2-Opt); echte Strassen-Navigation
 * uebernimmt das Navi des Geraets ueber den Google-Maps-Deep-Link pro Stopp.
 */
(function () {
  "use strict";

  // Leaflet liegt als defer-Skript im <head> (block head_extra) und ist erst
  // bei DOMContentLoaded verfuegbar — Init daher wie in technik-map.js
  // aufschieben (dieses Skript laedt non-defer im block scripts).
  function init() {
  var TOUR = window.TOUR || null;
  if (!TOUR || !window.L) { return; }
  var CSRF = TOUR.csrfToken || "";

  var STATUS_COLORS = {
    pending: "#206bc4",
    done: "#2b8a3e",
    skipped: "#868e96",
    not_home: "#f76707",
  };
  var STATUS_LABELS = {
    pending: "Offen",
    done: "Erledigt",
    skipped: "Übersprungen",
    not_home: "Nicht angetroffen",
  };

  // --- Basiskarten (basemap.at + OSM) — Muster aus technik-map.js -----------

  function baseLayers() {
    var bmAttr = 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>';
    var standard = L.tileLayer(
      "https://mapsneu.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png",
      { maxZoom: 20, maxNativeZoom: 19, attribution: bmAttr }
    );
    var ortho = L.tileLayer(
      "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
      { maxZoom: 20, maxNativeZoom: 19, attribution: bmAttr }
    );
    var osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende',
    });
    return {
      "Karte (basemap.at)": standard,
      "Orthofoto (basemap.at)": ortho,
      "OpenStreetMap": osm,
      _default: standard,
    };
  }

  function createMap(elId) {
    var layers = baseLayers();
    var map = L.map(elId, {
      preferCanvas: true, center: [47.59, 14.14], zoom: 7,
      layers: [layers._default],
    });
    var bases = {};
    Object.keys(layers).forEach(function (k) { if (k.charAt(0) !== "_") bases[k] = layers[k]; });
    L.control.layers(bases, {}, { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    return map;
  }

  // Haversine in Metern — Spiegel von technik-map.js segLengthM.
  function haversineM(a, b) {
    var R = 6371000.0;
    var p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
    var dp = (b.lat - a.lat) * Math.PI / 180, dl = (b.lng - a.lng) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function fmtDistance(m) {
    if (m >= 950) { return (m / 1000).toFixed(1).replace(".", ",") + " km"; }
    return Math.round(m) + " m";
  }

  function postForm(url, values) {
    var body = new URLSearchParams();
    body.set("csrf_token", CSRF);
    Object.keys(values || {}).forEach(function (k) {
      if (values[k] !== null && values[k] !== undefined) body.set(k, values[k]);
    });
    return fetch(url, {
      method: "POST",
      headers: { "X-CSRFToken": CSRF, "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
  }

  // --- Karte + State ---------------------------------------------------------

  var map = createMap("tour-map");
  var mapEl = document.getElementById("tour-map");

  // Karte auf die tatsaechlich verfuegbare Hoehe strecken: vom Oberkant der
  // Karte bis zum unteren Viewport-Rand. Robuster als eine feste vh-Angabe —
  // nutzt den Platz aus, egal wie hoch Browser-Chrome/Kopfleiste gerade sind.
  function fitMapHeight() {
    if (!mapEl) { return; }
    var top = mapEl.getBoundingClientRect().top;
    var h = Math.max(320, Math.round(window.innerHeight - top - 8));
    mapEl.style.height = h + "px";
    map.invalidateSize();
  }

  var stopLayer = L.featureGroup().addTo(map);
  var selfMarker = null, selfCircle = null, watchId = null, following = false;
  var lastPosition = null;
  var selectedStopId = null;

  var cardEl = document.getElementById("tour-card");
  var panelEl = document.getElementById("tour-panel");
  var listEl = document.getElementById("tour-stop-list");
  var bannerEl = document.getElementById("tour-next-banner");

  // History-Integration: beim Oeffnen des Stopp-Details wird EIN
  // History-Eintrag gepusht — der Zurueck-Button des Browsers/Handys
  // schliesst dann nur das Detail (popstate -> showList) statt die ganze
  // Seite zu verlassen. Beim Schliessen ueber das X wird der Eintrag per
  // history.back() wieder konsumiert, damit sich nichts aufstaut.
  var panelStatePushed = false;

  function consumePanelState() {
    // Eintrag still entfernen (Flag zuerst, damit der popstate-Handler
    // nichts mehr tut).
    if (panelStatePushed) {
      panelStatePushed = false;
      history.back();
    }
  }

  window.addEventListener("popstate", function () {
    if (!panelStatePushed) { return; }
    panelStatePushed = false;
    showList();
  });

  function stopsByStatus(status) {
    return TOUR.stops.filter(function (s) { return s.status === status; });
  }

  function stopByMeterId(meterId) {
    return TOUR.stops.find(function (s) { return s.meter_id === meterId; }) || null;
  }

  function stopById(id) {
    return TOUR.stops.find(function (s) { return s.id === id; }) || null;
  }

  function stopIcon(stop) {
    var color = STATUS_COLORS[stop.status] || STATUS_COLORS.pending;
    return L.divIcon({
      className: "tour-marker-wrap",
      html: '<span class="tour-marker" style="background:' + color + '">' +
            stop.position + "</span>",
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    });
  }

  function renderMap(fit) {
    stopLayer.clearLayers();

    // Bewusst KEINE Verbindungslinien zwischen den Stops: Luftlinien
    // suggerieren eine Fahrstrecke, die es ohne Strassen-Routing nicht gibt.
    // Die Reihenfolge steckt in der Marker-Nummerierung; navigiert wird pro
    // Stopp mit dem Navi des Geraets.
    if (TOUR.start_lat !== null && TOUR.start_lng !== null) {
      L.marker([TOUR.start_lat, TOUR.start_lng], {
        icon: L.divIcon({
          className: "tour-marker-wrap",
          html: '<span class="tour-marker tour-marker-start"><i class="fas fa-flag"></i></span>',
          iconSize: [30, 30], iconAnchor: [15, 15],
        }),
        title: "Startpunkt",
      }).addTo(stopLayer);
    }

    TOUR.stops.forEach(function (s) {
      if (s.lat === null || s.lng === null) { return; }
      var m = L.marker([s.lat, s.lng], { icon: stopIcon(s), title: "#" + s.position + " " + s.address });
      m.on("click", function () { selectStop(s.id); });
      m.addTo(stopLayer);
    });

    if (fit) {
      var b = stopLayer.getBounds();
      if (b.isValid()) { map.fitBounds(b.pad(0.15), { maxZoom: 16 }); }
    }
  }

  // --- Stop-Liste + Detail-Panel (Bottom-Sheet auf Mobile) -------------------

  function setSheet(open) {
    if (cardEl) { cardEl.classList.toggle("tour-sheet-open", open); }
  }

  function ownerLine(o) {
    var parts = ['<strong>' + escapeHtml(o.name || "") + "</strong>"];
    if (o.phone) {
      parts.push('<a href="tel:' + encodeURIComponent(o.phone) + '" hx-boost="false" class="ms-2">' +
        '<i class="fas fa-phone me-1"></i>' + escapeHtml(o.phone) + "</a>");
    }
    if (o.email) {
      parts.push('<a href="mailto:' + encodeURIComponent(o.email) + '" hx-boost="false" class="ms-2">' +
        '<i class="fas fa-envelope me-1"></i>' + escapeHtml(o.email) + "</a>");
    }
    return '<div class="tour-owner">' + parts.join("") + "</div>";
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function statusBadge(stop) {
    var color = STATUS_COLORS[stop.status] || "#206bc4";
    return '<span class="badge text-white" style="background:' + color + '">' +
      (STATUS_LABELS[stop.status] || stop.status) + "</span>";
  }

  function renderList() {
    if (!listEl) { return; }
    listEl.innerHTML = "";
    var reorderable = TOUR.status === "planned" || TOUR.status === "active";
    TOUR.stops.forEach(function (s, idx) {
      var li = document.createElement("li");
      li.className = "list-group-item tour-stop-item" +
        (s.id === selectedStopId ? " active-stop" : "");
      var distance = "";
      if (lastPosition && s.lat !== null && s.lng !== null) {
        distance = '<span class="text-secondary small">' +
          fmtDistance(haversineM(lastPosition, { lat: s.lat, lng: s.lng })) + "</span>";
      }
      var geoWarn = (s.lat === null || s.lng === null)
        ? ' <i class="fas fa-exclamation-triangle text-warning" title="Keine Koordinaten — Objekt nicht geocodet"></i>'
        : "";
      var ownerNames = s.owners.map(function (o) { return o.name; }).join(", ");
      // Manuelles Umsortieren: Pfeile tauschen mit dem Nachbarn — die
      // automatische Luftlinien-Reihenfolge ist nur ein Startvorschlag.
      var moveBtns = "";
      if (reorderable) {
        moveBtns =
          '<span class="tour-move-btns">' +
          '<button type="button" class="btn btn-sm btn-icon btn-ghost-secondary" data-move="up"' +
          (idx === 0 ? " disabled" : "") + ' title="Nach oben">' +
          '<i class="fas fa-chevron-up"></i></button>' +
          '<button type="button" class="btn btn-sm btn-icon btn-ghost-secondary" data-move="down"' +
          (idx === TOUR.stops.length - 1 ? " disabled" : "") + ' title="Nach unten">' +
          '<i class="fas fa-chevron-down"></i></button>' +
          "</span>";
      }
      li.innerHTML =
        '<div class="d-flex align-items-center" style="gap:.5rem">' +
        '<span class="tour-marker tour-marker-inline" style="background:' +
        (STATUS_COLORS[s.status] || "#206bc4") + '">' + s.position + "</span>" +
        '<div class="me-auto"><div>' + escapeHtml(s.address || s.property_label) + geoWarn + "</div>" +
        (ownerNames
          ? '<div class="small"><i class="fas fa-user me-1 text-secondary"></i>' +
            escapeHtml(ownerNames) + "</div>"
          : "") +
        '<div class="text-secondary small">Zähler ' + escapeHtml(s.meter_number || "?") +
        (s.notified ? ' · <i class="fas fa-envelope-open-text" title="Vorab informiert"></i>' : "") +
        "</div></div>" +
        '<div class="d-flex flex-column align-items-end" style="gap:.15rem">' +
        moveBtns + distance + "</div></div>";
      li.addEventListener("click", function () { selectStop(s.id); });
      li.querySelectorAll("[data-move]").forEach(function (btn) {
        btn.addEventListener("click", function (ev) {
          ev.stopPropagation();  // Klick soll nicht das Detail-Panel oeffnen
          if (btn.disabled) { return; }
          postForm(s.move_url, { direction: btn.dataset.move })
            .then(function () { return refresh(); });
        });
      });
      listEl.appendChild(li);
    });
  }

  function actionButtons(stop) {
    var html = '<div class="btn-list mt-3">';
    if (stop.gmaps_url) {
      html += '<a class="btn btn-primary" hx-boost="false" target="_blank" rel="noopener" href="' +
        stop.gmaps_url + '"><i class="fas fa-directions me-1"></i>Navigation</a>';
    }
    if (stop.status === "pending" || stop.status === "not_home") {
      html += '<button type="button" class="btn btn-danger" data-tour-action="replace">' +
        '<i class="fas fa-exchange-alt me-1"></i>Zähler tauschen</button>';
    }
    if (stop.status === "pending") {
      html += '<button type="button" class="btn btn-outline-secondary" data-tour-action="skip">' +
        '<i class="fas fa-forward me-1"></i>Überspringen</button>';
      html += '<button type="button" class="btn btn-outline-warning" data-tour-action="not_home">' +
        '<i class="fas fa-user-slash me-1"></i>Nicht angetroffen</button>';
    }
    if ((stop.status === "skipped" || stop.status === "not_home")) {
      html += '<button type="button" class="btn btn-outline-secondary" data-tour-action="reopen">' +
        '<i class="fas fa-undo me-1"></i>Wieder öffnen</button>';
    }
    if (stop.status === "done" && !stop.has_invoice && TOUR.can_invoice) {
      html += '<button type="button" class="btn btn-outline-success" data-tour-action="invoice">' +
        '<i class="fas fa-file-invoice me-1"></i>Rechnung erstellen</button>';
    }
    html += "</div>";
    return html;
  }

  function selectStop(stopId) {
    var stop = stopById(stopId);
    if (!stop || !panelEl) { return; }
    selectedStopId = stopId;
    var listView = document.getElementById("tour-list-view");
    if (listView) { listView.style.display = "none"; }
    panelEl.style.display = "";
    var html =
      '<div class="p-3">' +
      '<div class="d-flex align-items-center mb-2" style="gap:.5rem">' +
      '<span class="tour-marker tour-marker-inline" style="background:' +
      (STATUS_COLORS[stop.status] || "#206bc4") + '">' + stop.position + "</span>" +
      "<strong class=\"me-auto\">" + escapeHtml(stop.property_label) + "</strong>" +
      statusBadge(stop) +
      "</div>" +
      '<div class="text-secondary mb-1"><i class="fas fa-map-marker-alt me-1"></i>' +
      escapeHtml(stop.address) + "</div>" +
      '<div class="mb-2"><i class="fas fa-tachometer-alt me-1 text-secondary"></i>Zähler <strong>' +
      escapeHtml(stop.meter_number || "?") + "</strong></div>" +
      stop.owners.map(ownerLine).join("") +
      (stop.skip_reason
        ? '<div class="text-secondary small mt-1">Grund: ' + escapeHtml(stop.skip_reason) + "</div>"
        : "") +
      actionButtons(stop) +
      "</div>";
    panelEl.innerHTML = html;
    if (stop.lat !== null && stop.lng !== null) {
      // Reinzoomen statt nur zentrieren — aber nie rauszoomen, wenn der
      // Nutzer schon naeher dran ist.
      map.setView([stop.lat, stop.lng], Math.max(map.getZoom(), 17),
                  { animate: true });
    }
    setSheet(true);
    if (!panelStatePushed) {
      history.pushState({ tourStopPanel: true }, "");
      panelStatePushed = true;
    }

    panelEl.querySelectorAll("[data-tour-action]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        handleAction(btn.dataset.tourAction, stop);
      });
    });
  }

  function showList() {
    selectedStopId = null;
    var listView = document.getElementById("tour-list-view");
    if (listView) { listView.style.display = ""; }
    if (panelEl) { panelEl.style.display = "none"; panelEl.innerHTML = ""; }
    renderList();
  }

  function handleAction(action, stop) {
    if (action === "back") {
      if (panelStatePushed) {
        // popstate-Handler schliesst das Panel — konsumiert zugleich den
        // gepushten History-Eintrag.
        history.back();
      } else {
        showList();
      }
      return;
    }
    if (action === "replace") {
      if (window.openReplaceModal) {
        window.openReplaceModal({ dataset: { url: stop.replace_url } });
      }
      return;
    }
    if (action === "invoice") {
      if (window.openTourInvoiceModal) { window.openTourInvoiceModal(stop.invoice_url); }
      return;
    }
    var status = null, reason = null;
    if (action === "skip") {
      status = "skipped";
      reason = window.prompt("Grund fürs Überspringen (optional):", "") || "";
    } else if (action === "not_home") {
      status = "not_home";
    } else if (action === "reopen") {
      status = "pending";
    }
    if (!status) { return; }
    postForm(stop.status_url, { status: status, skip_reason: reason })
      .then(function () { return refresh(); })
      .then(function () {
        if (status === "pending") {
          // Wieder geoeffnet -> Detail mit aktualisierten Aktionen zeigen.
          selectStop(stop.id);
        } else {
          // Uebersprungen / nicht angetroffen: Stop faellt aus der Route —
          // Panel schliessen, damit er nicht weiter als aktuelles Ziel im
          // Fokus steht. Der "Naechster Halt"-Vorschlag (updateBanner) zaehlt
          // ohnehin nur pending-Stops, blendet ihn also ebenfalls aus.
          showList();
          consumePanelState();
        }
      });
  }

  // --- Refresh (nach Tausch / Statuswechsel) ---------------------------------

  function refresh() {
    return fetch(TOUR.stops_url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // csrfToken/URLs von der Seite behalten, Nutzdaten uebernehmen.
        data.csrfToken = CSRF;
        window.TOUR = TOUR = data;
        renderMap(false);
        renderList();
        updateBanner();
        updateProgress();
      });
  }
  window.tourRefresh = refresh;

  function updateProgress() {
    var done = stopsByStatus("done").length;
    var text = done + " / " + TOUR.stops.length + " erledigt";
    var el = document.getElementById("tour-progress");
    if (el) { el.textContent = text; }
    var elMobile = document.getElementById("tour-progress-mobile");
    if (elMobile) { elMobile.textContent = text; }
  }

  // --- Eigener Standort + Naechster-Halt-Vorschlag ---------------------------

  function updateBanner() {
    if (!bannerEl) { return; }
    if (!lastPosition) { bannerEl.style.display = "none"; return; }
    var best = null, bestDist = Infinity;
    stopsByStatus("pending").forEach(function (s) {
      if (s.lat === null || s.lng === null) { return; }
      var d = haversineM(lastPosition, { lat: s.lat, lng: s.lng });
      if (d < bestDist) { best = s; bestDist = d; }
    });
    if (!best) { bannerEl.style.display = "none"; return; }
    bannerEl.style.display = "";
    bannerEl.innerHTML =
      '<i class="fas fa-location-arrow me-1"></i>Nächster Halt: ' +
      "<strong>#" + best.position + " – " + escapeHtml(best.address || best.property_label) +
      "</strong> (" + fmtDistance(bestDist) + ")";
    bannerEl.onclick = function () { selectStop(best.id); };
  }

  function onPosition(pos) {
    lastPosition = { lat: pos.coords.latitude, lng: pos.coords.longitude };
    var acc = pos.coords.accuracy || 0;
    if (!selfMarker) {
      selfMarker = L.circleMarker(lastPosition, {
        radius: 7, color: "#fff", weight: 2, fillColor: "#206bc4", fillOpacity: 1,
      }).addTo(map).bindTooltip("Mein Standort");
      selfCircle = L.circle(lastPosition, {
        radius: acc, color: "#206bc4", weight: 1, opacity: 0.4, fillOpacity: 0.08,
      }).addTo(map);
    } else {
      selfMarker.setLatLng(lastPosition);
      selfCircle.setLatLng(lastPosition);
      selfCircle.setRadius(acc);
    }
    if (following) { map.panTo(lastPosition); }
    updateBanner();
    renderList();
  }

  function startWatch() {
    if (!navigator.geolocation) {
      window.alert("Standortermittlung wird von diesem Browser nicht unterstützt.");
      return;
    }
    if (watchId !== null) { return; }
    watchId = navigator.geolocation.watchPosition(onPosition, function (err) {
      // Haeufigster Fall: kein Secure Context (HTTP im LAN) oder abgelehnt.
      window.alert("Standort nicht verfügbar: " + err.message +
        "\nHinweis: Die Standortermittlung braucht HTTPS (oder localhost).");
      stopWatch();
    }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 });
  }

  function stopWatch() {
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    following = false;
    var btn = document.getElementById("tour-follow");
    if (btn) { btn.classList.remove("active"); }
  }

  var followBtn = document.getElementById("tour-follow");
  if (followBtn) {
    followBtn.addEventListener("click", function () {
      following = !following;
      followBtn.classList.toggle("active", following);
      if (following) { startWatch(); if (lastPosition) { map.panTo(lastPosition); } }
    });
  }

  // Sheet-Griff (Mobile) schliesst das Panel; ein evtl. offener Detail-
  // History-Eintrag wird dabei still konsumiert (sonst braeuchte es spaeter
  // zwei Zurueck-Klicks zum Verlassen der Seite).
  var handle = document.querySelector(".tour-sheet-handle");
  if (handle) {
    handle.addEventListener("click", function () {
      setSheet(false);
      consumePanelState();
    });
  }
  // Stoppliste ein-/ausblenden — Button existiert doppelt (Kopf + Mobilleiste).
  document.querySelectorAll("[data-tour-list-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showList();
      setSheet(!cardEl.classList.contains("tour-sheet-open"));
      consumePanelState();
    });
  });

  // X in der Stoppliste (Mobile): Sheet schliessen.
  var listClose = document.getElementById("tour-list-close");
  if (listClose) {
    listClose.addEventListener("click", function () {
      setSheet(false);
      consumePanelState();
    });
  }

  // Mobile-Kopf auf-/zuklappen (Tourname, Aktionen, Status).
  var headerToggle = document.getElementById("tour-header-toggle");
  if (headerToggle && cardEl) {
    headerToggle.addEventListener("click", function () {
      var open = cardEl.classList.toggle("tour-header-open");
      headerToggle.setAttribute("aria-expanded", open ? "true" : "false");
      // Der auf-/zugeklappte Kopf verschiebt die Karten-Oberkante -> Hoehe
      // neu strecken (invalidateSize inklusive).
      setTimeout(fitMapHeight, 60);
    });
  }

  // Umsortieren ab aktuellem Standort (offene Stops).
  var reorderBtn = document.getElementById("tour-reorder");
  if (reorderBtn) {
    reorderBtn.addEventListener("click", function () {
      if (!lastPosition) {
        window.alert("Zuerst den Standort aktivieren (Folgen-Button).");
        return;
      }
      postForm(TOUR.reorder_url, {
        start_lat: lastPosition.lat, start_lng: lastPosition.lng,
      }).then(function () { return refresh(); });
    });
  }

  // --- Zaehlertausch-Rueckmeldung (vom _modal_scripts.html-Hook) -------------
  // Der Server verifiziert den Tausch selbst (unique old_meter_id); das Event
  // liefert nur den Trigger + die meter_id.
  window.onMeterReplaced = function (e) {
    var meterId = e && e.detail ? e.detail.meter_id : null;
    var stop = meterId ? stopByMeterId(meterId) : null;
    if (!stop) { window.location.reload(); return; }
    postForm(stop.complete_url, {})
      .then(function (r) { return r.json(); })
      .then(function (d) {
        return refresh().then(function () {
          selectStop(stop.id);
          if (d && d.invoice_offer && window.openTourInvoiceModal) {
            window.openTourInvoiceModal(stop.invoice_url);
          }
        });
      });
  };

  // --- Init ------------------------------------------------------------------

  renderMap(true);
  showList();
  updateBanner();
  updateProgress();
  // Hoehe strecken + nachziehen, falls das Layout beim Init noch nicht final
  // war (Fonts/Chrome). Bei Groessen-/Ausrichtungswechsel neu berechnen.
  fitMapHeight();
  setTimeout(fitMapHeight, 250);
  window.addEventListener("resize", fitMapHeight);
  window.addEventListener("orientationchange", function () {
    setTimeout(fitMapHeight, 200);
  });
  // Standort direkt anwerfen (mobiler Hauptanwendungsfall); scheitert leise,
  // wenn der Nutzer ablehnt — der Folgen-Button fragt dann erneut.
  if (navigator.geolocation && window.isSecureContext) {
    watchId = navigator.geolocation.watchPosition(onPosition, function () {
      watchId = null;
    }, { enableHighAccuracy: true, maximumAge: 5000, timeout: 15000 });
  }
  }

  function boot() {
    if (window.L) { init(); return; }
    // Leaflet fehlt: die Seite kam ueber eine geboostete Navigation herein
    // (Body-Swap laedt head_extra nicht) — Karte bliebe sonst weiss.
    // Selbstheilung: einmalig hart neu laden; Zeitstempel-Guard verhindert
    // eine Reload-Schleife, falls das CDN wirklich nicht erreichbar ist.
    var key = "tourMapReloadedAt";
    var last = parseInt(sessionStorage.getItem(key) || "0", 10);
    if (Date.now() - last > 10000) {
      sessionStorage.setItem(key, String(Date.now()));
      window.location.reload();
    }
  }

  if (document.getElementById("tour-map")) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else {
      boot();
    }
  }
})();
