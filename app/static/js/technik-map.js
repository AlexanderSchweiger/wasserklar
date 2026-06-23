/* Technik / Wasserleitungsplan — Leaflet-Karte.
 *
 * Laeuft auf der Editor-Seite (#technik-map) und der Druckseite
 * (#technik-print-map). Die Editor-Seite wird per hx-boost="false" immer als
 * Vollseite geladen, daher liegt Leaflet/Geoman im <head> (block head_extra)
 * und ist bei DOMContentLoaded fertig.
 *
 * Konfig kommt aus window.TECHNIK (von der Seite gesetzt): base, featuresUrl,
 * createUrl, csrfToken, vocab.
 */
(function () {
  "use strict";

  var T = window.TECHNIK || {};
  var V = T.vocab || {};

  // --- Basiskarten (basemap.at + OSM) --------------------------------------

  function baseLayers() {
    var bmAttr = 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>';
    // Einzelhost mapsneu.wien.gv.at: die alten Lastverteilungs-Subdomains
    // maps1..maps4.wien.gv.at antworten nicht mehr (→ ~80 % graue Kacheln).
    var standard = L.tileLayer(
      "https://mapsneu.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png",
      { maxZoom: 20, maxNativeZoom: 19, attribution: bmAttr }
    );
    // Graue basemap.at-Ebene — gedaempfter Hintergrund, auf dem sich die farbigen
    // Marker/Leitungen klar abheben. Default fuer die Druckkarte (renderPrint).
    var grau = L.tileLayer(
      "https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png",
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
      "Karte grau (basemap.at)": grau,
      "Orthofoto (basemap.at)": ortho,
      "OpenStreetMap": osm,
      _default: standard,
      _grau: grau,
    };
  }

  // opts.startLayer: interner Layer-Handle aus baseLayers() (z. B. "_grau"),
  // mit dem die Karte startet; ungesetzt -> Standardkarte.
  function createMap(elId, opts) {
    opts = opts || {};
    var layers = baseLayers();
    var start = (opts.startLayer && layers[opts.startLayer]) || layers._default;
    var map = L.map(elId, { center: [47.59, 14.14], zoom: 7, layers: [start] });
    var bases = {};
    Object.keys(layers).forEach(function (k) { if (k.charAt(0) !== "_") bases[k] = layers[k]; });
    L.control.layers(bases, {}, { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    return map;
  }

  // --- Styling -------------------------------------------------------------

  function typeColor(props) {
    if (props.color) return props.color;
    var t = (V.pointTypes && V.pointTypes[props.feature_type]) ||
            (V.lineTypes && V.lineTypes[props.feature_type]);
    return t ? t.color : "#868e96";
  }

  // scale = 1 -> 26 px (Editor-Default). Kleinere Werte (Druck) schrumpfen den
  // Marker proportional; unter ~0.6 entfaellt der unleserliche Glyph (nur noch
  // farbiger Punkt) und der weisse Ring wird duenner (technik-marker-tight).
  function pointIcon(props, scale) {
    scale = scale || 1;
    var size = Math.max(7, Math.round(26 * scale));
    var anchor = size / 2;
    var color = typeColor(props);
    var pt = V.pointTypes && V.pointTypes[props.feature_type];
    var fa = pt ? pt.icon : "fa-map-marker-alt";
    var tight = scale < 0.85 ? " technik-marker-tight" : "";
    // Hausanschluss ohne zugeordnete Liegenschaft -> grell (siehe app.css).
    var unassigned = props.unassigned ? " technik-marker-unassigned" : "";
    var glyph = scale >= 0.6
      ? '<i class="fas ' + fa + '" style="font-size:' + Math.round(13 * scale) + 'px"></i>'
      : "";
    return L.divIcon({
      className: "technik-marker-wrap",
      html: '<span class="technik-marker' + tight + unassigned + '" style="background:' + color +
        ';width:' + size + 'px;height:' + size + 'px">' + glyph + "</span>",
      iconSize: [size, size],
      iconAnchor: [anchor, anchor],
      popupAnchor: [0, -anchor + 1],
    });
  }

  function lineDash(accuracy) {
    var a = V.accuracies && V.accuracies[accuracy];
    return a && a.dash ? a.dash : null;
  }

  // Globale Strichelung (Darstellungs-Regler). "auto" -> pro Linie nach
  // Lagegenauigkeit (Default, bisheriges Verhalten); sonst fuer ALLE Linien das
  // gewaehlte Muster (durchgezogen .. grob). SVG-dashArray "Strich Luecke".
  var DASH_PATTERNS = {
    solid: null,
    dotted: "1 6",
    "dash-sm": "5 6",
    "dash-md": "10 8",
    "dash-lg": "18 10",
  };
  function resolveDash(accuracy, dashMode) {
    if (dashMode && dashMode !== "auto") return DASH_PATTERNS[dashMode] || null;
    return lineDash(accuracy);
  }

  function popupHtml(props) {
    var html = '<strong>' + escapeHtml(props.name || props.type_label) + '</strong>';
    html += '<br><span class="text-secondary">' + escapeHtml(props.type_label) + '</span>';
    if (props.length_m != null) html += '<br>Länge: ' + props.length_m + ' m';
    if (props.dimension_dn != null) html += ' · DN ' + props.dimension_dn;
    if (props.pressure_rating) html += '<br>' + escapeHtml(props.pressure_rating);
    if (props.manufacturer) html += '<br>Fabrikat: ' + escapeHtml(props.manufacturer);
    // Verknuepfte Liegenschaft: aktueller Besitzer + Adresse (Strasse + HNR).
    // Die Adresse ist ein Link auf die Liegenschaft, wenn das Feature verknuepft
    // ist; hx-boost="false", damit htmx den Klick nicht boostet (Map-Container).
    var owners = props.owner_names || [];
    if (props.property_address || owners.length) {
      html += '<hr class="my-1">';
      if (props.property_address) {
        var addr = escapeHtml(props.property_address);
        if (props.property_id != null && T.propertyUrl) {
          addr = '<a href="' + T.propertyUrl + props.property_id + '" hx-boost="false">' + addr + '</a>';
        }
        html += '<div><i class="fas fa-map-marker-alt text-secondary"></i> ' + addr + '</div>';
      }
      if (owners.length) {
        html += '<div><i class="fas fa-user text-secondary"></i> ' + escapeHtml(owners.join(", ")) + '</div>';
      }
    }
    return html;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // opts (optional): {markerScale, lineWeight} — steuert die Symbolgroesse fuer
  // den Druck. Ungesetzt -> Editor-Defaults (26 px Marker, Linie weight 4).
  function buildLayer(feature, opts) {
    opts = opts || {};
    var props = feature.properties || {};
    var layer;
    if (feature.geometry.type === "Point") {
      layer = L.marker(toLatLng(feature.geometry.coordinates), { icon: pointIcon(props, opts.markerScale) });
    } else {
      var latlngs = feature.geometry.coordinates.map(toLatLng);
      layer = L.polyline(latlngs, {
        color: typeColor(props), weight: opts.lineWeight || 4, opacity: 0.9,
        dashArray: resolveDash(props.accuracy, opts.dashMode),
      });
    }
    layer._technikId = feature.id;
    layer._technikType = props.feature_type;
    layer.bindPopup(popupHtml(props));
    return layer;
  }

  function toLatLng(coord) { return [coord[1], coord[0]]; } // GeoJSON [lng,lat] -> Leaflet [lat,lng]

  // --- Vermessung (Laenge + Richtungswinkel) -------------------------------
  // Spiegelt die Backend-Haversine (services.py) fuer die Live-Anzeige beim
  // Zeichnen. a/b sind L.LatLng-Objekte.

  function segLengthM(a, b) {
    var R = 6371000.0;
    var p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
    var dp = (b.lat - a.lat) * Math.PI / 180, dl = (b.lng - a.lng) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) +
            Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function pathLengthM(latlngs) {
    var total = 0;
    for (var i = 1; i < latlngs.length; i++) total += segLengthM(latlngs[i - 1], latlngs[i]);
    return total;
  }
  // Richtungswinkel (Azimut) von a nach b, 0–360° im Uhrzeigersinn ab Nord.
  function bearingDeg(a, b) {
    var p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
    var dl = (b.lng - a.lng) * Math.PI / 180;
    var y = Math.sin(dl) * Math.cos(p2);
    var x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }
  function compassDir(deg) {
    return ["N", "NO", "O", "SO", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8];
  }
  function fmtLength(m) {
    if (m >= 1000) return (m / 1000).toFixed(2).replace(".", ",") + " km";
    return m.toFixed(1).replace(".", ",") + " m";
  }

  // --- Feature-Verwaltung --------------------------------------------------

  function FeatureStore(map) {
    this.map = map;
    this.group = L.featureGroup().addTo(map);
    this.byId = {};
    this.featById = {};   // rohes GeoJSON-Feature je id — fuer Liste/Suche
    this.hiddenTypes = {};
    this.renderOpts = {}; // {markerScale, lineWeight} — Symbolgroesse (Druck)
  }
  FeatureStore.prototype.add = function (feature, opts) {
    var layer = buildLayer(feature, this.renderOpts);
    this.byId[feature.id] = layer;
    this.featById[feature.id] = feature;
    if (!this.hiddenTypes[layer._technikType]) this.group.addLayer(layer);
    if (opts && opts.onSelect) layer.on("click", function () { opts.onSelect(feature.id); });
    if (opts && opts.onGeometry) layer.on("pm:update", function () { opts.onGeometry(feature.id, layer); });
    return layer;
  };
  FeatureStore.prototype.remove = function (id) {
    var layer = this.byId[id];
    if (layer) { this.group.removeLayer(layer); delete this.byId[id]; }
    delete this.featById[id];
  };
  FeatureStore.prototype.fit = function () {
    if (this.group.getLayers().length) {
      try { this.map.fitBounds(this.group.getBounds().pad(0.2)); } catch (e) {}
    }
  };
  FeatureStore.prototype.toggleType = function (type, visible) {
    this.hiddenTypes[type] = !visible;
    var self = this;
    Object.keys(this.byId).forEach(function (id) {
      var layer = self.byId[id];
      if (layer._technikType !== type) return;
      if (visible) self.group.addLayer(layer); else self.group.removeLayer(layer);
    });
  };
  // Symbolgroesse live aendern (Druck-Regler), ohne die Features neu zu laden:
  // Marker bekommen ein neu skaliertes Icon, Linien eine neue Staerke.
  FeatureStore.prototype.restyle = function (renderOpts) {
    this.renderOpts = renderOpts || {};
    var self = this;
    Object.keys(this.byId).forEach(function (id) {
      var layer = self.byId[id];
      var feat = self.featById[id];
      if (layer.setIcon && feat && feat.geometry.type === "Point") {
        layer.setIcon(pointIcon(feat.properties || {}, self.renderOpts.markerScale));
      } else if (layer.setStyle) {
        var acc = feat && feat.properties ? feat.properties.accuracy : null;
        layer.setStyle({
          weight: self.renderOpts.lineWeight || 4,
          dashArray: resolveDash(acc, self.renderOpts.dashMode),
        });
      }
    });
  };

  function fetchFeatures(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); });
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": T.csrfToken },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error(j.error || r.status); });
      return r.json();
    });
  }

  // --- Legende -------------------------------------------------------------

  // Legende einklappbar: Zustand in localStorage merken, auf schmalen Screens
  // (Mobile) per Default zugeklappt, damit die Karte mehr Platz bekommt.
  var LEGEND_KEY = "technik.legend.collapsed";
  function legendCollapsedDefault() {
    try {
      var stored = window.localStorage.getItem(LEGEND_KEY);
      if (stored !== null) return stored === "1";
    } catch (e) { /* localStorage gesperrt -> Fallback unten */ }
    return window.matchMedia && window.matchMedia("(max-width: 768px)").matches;
  }

  // --- Darstellung (Symbolgroesse + Linienstaerke) -------------------------
  // Auf der Editor-Karte einstellbar (in der Legende) und in localStorage
  // gemerkt, damit der Plan beim naechsten Aufruf gleich aussieht. Die
  // Druckseite hat eigene, druckoptimierte Defaults (renderPrint).
  var DISPLAY_PREF = { markerScale: "technik.markerScale", lineWeight: "technik.lineWeight", dashMode: "technik.dashMode" };
  function readDisplayPref(key, fallback) {
    try {
      var raw = window.localStorage.getItem(DISPLAY_PREF[key]);
      if (raw !== null) { var n = parseFloat(raw); if (!isNaN(n)) return n; }
    } catch (e) { /* localStorage gesperrt -> Fallback */ }
    return fallback;
  }
  // String-Variante (z. B. dashMode = "auto"/"solid"/...).
  function readDisplayPrefStr(key, fallback) {
    try {
      var raw = window.localStorage.getItem(DISPLAY_PREF[key]);
      if (raw !== null) return raw;
    } catch (e) { /* localStorage gesperrt -> Fallback */ }
    return fallback;
  }
  function writeDisplayPref(key, value) {
    try { window.localStorage.setItem(DISPLAY_PREF[key], String(value)); } catch (e) { /* ignore */ }
  }
  function markerSizeLabel(scale) { return Math.round(26 * scale) + " px"; }

  // Auswahl fuer die globale Strichelung (Editor-Legende + Druck halten dieselbe
  // Reihenfolge: Auto -> durchgezogen -> immer groebere Striche).
  var DASH_CHOICES = [
    ["auto", "Automatisch (Genauigkeit)"],
    ["solid", "Durchgezogen"],
    ["dotted", "Gepunktet"],
    ["dash-sm", "Fein gestrichelt"],
    ["dash-md", "Mittel gestrichelt"],
    ["dash-lg", "Grob gestrichelt"],
  ];
  function dashOptionsHtml(current) {
    return DASH_CHOICES.map(function (o) {
      return '<option value="' + o[0] + '"' + (o[0] === current ? " selected" : "") + ">" + escapeHtml(o[1]) + "</option>";
    }).join("");
  }

  // HTML der beiden Regler (Initialwerte aus store.renderOpts).
  function displayControlsHtml(store) {
    var ms = store.renderOpts.markerScale != null ? store.renderOpts.markerScale : 1;
    var lw = store.renderOpts.lineWeight != null ? store.renderOpts.lineWeight : 4;
    var dm = store.renderOpts.dashMode || "auto";
    return '<div class="technik-legend-sub">Darstellung</div>' +
      '<div class="technik-legend-display">' +
        '<label class="technik-legend-disp-row"><span>Symbolgröße</span>' +
          '<output data-disp="marker">' + markerSizeLabel(ms) + '</output></label>' +
        '<input type="range" class="technik-legend-range" data-pref="markerScale" min="0.3" max="1.5" step="0.05" value="' + ms + '">' +
        '<label class="technik-legend-disp-row"><span>Linienstärke</span>' +
          '<output data-disp="line">' + lw + ' px</output></label>' +
        '<input type="range" class="technik-legend-range" data-pref="lineWeight" min="1" max="8" step="0.5" value="' + lw + '">' +
        '<div class="technik-legend-disp-row"><span>Strichelung</span></div>' +
        '<select class="technik-legend-dash form-select form-select-sm" data-pref="dashMode">' + dashOptionsHtml(dm) + '</select>' +
      '</div>';
  }

  // Regler verdrahten: live restylen (rAF-gebuendelt), Wert merken, Label updaten.
  function wireDisplayControls(div, store) {
    var raf = false;
    div.addEventListener("input", function (e) {
      var rng = e.target.closest(".technik-legend-range");
      if (!rng) return;
      var pref = rng.getAttribute("data-pref");
      var val = parseFloat(rng.value);
      if (isNaN(val)) return;
      store.renderOpts[pref] = val;
      writeDisplayPref(pref, val);
      var out = div.querySelector('output[data-disp="' + (pref === "markerScale" ? "marker" : "line") + '"]');
      if (out) out.textContent = pref === "markerScale" ? markerSizeLabel(val) : val + " px";
      if (!raf) {
        raf = true;
        window.requestAnimationFrame(function () { raf = false; store.restyle(store.renderOpts); });
      }
    });
    div.addEventListener("change", function (e) {
      var sel = e.target.closest(".technik-legend-dash");
      if (!sel) return;
      store.renderOpts.dashMode = sel.value;
      writeDisplayPref("dashMode", sel.value);
      store.restyle(store.renderOpts);
    });
  }

  function legendControl(store) {
    var ctrl = L.control({ position: "bottomleft" });
    ctrl.onAdd = function () {
      var div = L.DomUtil.create("div", "technik-legend card");
      var collapsed = legendCollapsedDefault();
      var html = '<button type="button" class="technik-legend-head" aria-expanded="' +
        (collapsed ? "false" : "true") + '" title="Legende ein-/ausblenden">' +
        '<i class="fas fa-layer-group me-1 text-secondary"></i><strong>Legende</strong>' +
        '<i class="fas fa-chevron-down technik-legend-caret ms-auto"></i></button>' +
        '<div class="technik-legend-body">' + displayControlsHtml(store) +
        '<div class="technik-legend-sub">Sichtbarkeit</div>';
      function row(type, label, color, isLine) {
        var swatch = isLine
          ? '<span class="technik-legend-line" style="border-color:' + color + '"></span>'
          : '<span class="technik-legend-swatch" style="background:' + color + '"></span>';
        return '<label class="technik-legend-row"><input type="checkbox" checked data-type="' + type + '">' +
          swatch + escapeHtml(label) + "</label>";
      }
      Object.keys(V.pointTypes || {}).forEach(function (k) { html += row(k, V.pointTypes[k].label, V.pointTypes[k].color, false); });
      Object.keys(V.lineTypes || {}).forEach(function (k) { html += row(k, V.lineTypes[k].label, V.lineTypes[k].color, true); });
      html += "</div>";
      div.innerHTML = html;
      if (collapsed) div.classList.add("is-collapsed");
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      var head = div.querySelector(".technik-legend-head");
      head.addEventListener("click", function () {
        var nowCollapsed = div.classList.toggle("is-collapsed");
        head.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
        try { window.localStorage.setItem(LEGEND_KEY, nowCollapsed ? "1" : "0"); } catch (e) { /* ignore */ }
      });
      div.addEventListener("change", function (e) {
        var cb = e.target.closest("input[type=checkbox]");
        if (cb) store.toggleType(cb.getAttribute("data-type"), cb.checked);
      });
      wireDisplayControls(div, store);
      return div;
    };
    return ctrl;
  }

  // --- Panel ---------------------------------------------------------------

  function panelEl() { return document.getElementById("technik-panel"); }

  function resetPanel() {
    var tpl = document.getElementById("technik-panel-empty");
    var el = panelEl();
    if (el && tpl) el.innerHTML = tpl.innerHTML;
  }

  function openPanel(id) {
    if (window.htmx) {
      window.htmx.ajax("GET", T.base + "features/" + id, { target: "#technik-panel", swap: "innerHTML" });
    }
  }

  // Auf ein Feature fokussieren: Panel laden, hinzoomen, Popup als Hervorhebung
  // oeffnen. Genutzt vom Deep-Link (?feature=<id>) und vom Listen-/Marker-Klick.
  function focusFeature(map, store, id) {
    var layer = store.byId[id];
    if (!layer) return;
    openPanel(id);
    if (layer.getLatLng) map.setView(layer.getLatLng(), 18);
    else if (layer.getBounds) map.fitBounds(layer.getBounds().pad(0.5));
    if (layer.openPopup) layer.openPopup();
  }

  // --- Editor-Init ---------------------------------------------------------

  function initEditor() {
    var el = document.getElementById("technik-map");
    if (!el || typeof L === "undefined") return;

    var map = createMap("technik-map");
    var store = new FeatureStore(map);
    // Gemerkte Symbolgroesse/Linienstaerke (Default = bisheriges Aussehen) —
    // gilt fuer alle danach hinzugefuegten Features (add liest store.renderOpts).
    store.renderOpts = {
      markerScale: readDisplayPref("markerScale", 1),
      lineWeight: readDisplayPref("lineWeight", 4),
      dashMode: readDisplayPrefStr("dashMode", "auto"),
    };
    legendControl(store).addTo(map);
    resetPanel();

    // --- Mobile-Bottom-Sheet ---
    // Auf Mobile (< lg) liegt die rechte Spalte als Bottom-Sheet ueber der Karte
    // (CSS). setSheet(true) schiebt es hoch, setSheet(false) wieder runter; auf
    // Desktop ist die Klasse wirkungslos (Media-Query). So bleibt die Karte mobil
    // gross, das Detail/die Liste kommt nur bei Bedarf rein.
    var cardEl = document.getElementById("technik-card");
    function setSheet(open) {
      if (cardEl) cardEl.classList.toggle("technik-sheet-open", open);
    }
    var sheetHandle = document.querySelector(".technik-sheet-handle");
    if (sheetHandle) sheetHandle.addEventListener("click", function () { setSheet(false); });

    // --- Elementliste + Suche (rechte Spalte) ---
    var listEl = document.getElementById("technik-list");
    var listEmptyEl = document.getElementById("technik-list-empty");
    var listViewEl = document.getElementById("technik-list-view");
    var searchInput = document.getElementById("technik-list-search");
    var listToggleBtn = document.getElementById("technik-list-toggle");
    var currentFilter = "";
    // Ob die Liste die aktive Basisansicht ist (vom "Elementliste"-Button bzw. der
    // Suche geoeffnet). Bleibt true, waehrend ein Detail darueber liegt — beim
    // Schliessen des Details kehren wir dann zur Liste statt zum leeren Panel zurueck.
    var listMode = false;

    // akzent-/case-insensitiv (z. B. "uberlauf" findet "Überlauf"). Combining-
    // Marks-Range U+0300–U+036F als String-RegExp, damit der Quelltext ASCII bleibt.
    var COMBINING_MARKS = new RegExp("[\\u0300-\\u036f]", "g");
    function norm(s) {
      return String(s == null ? "" : s).normalize("NFD").replace(COMBINING_MARKS, "").toLowerCase();
    }
    function featLabel(f) {
      var p = f.properties || {};
      return p.name || p.type_label || ("#" + f.id);
    }
    function matchFeature(f, q) {
      var p = f.properties || {};
      var owners = (p.owner_names || []).join(" ");
      var hay = norm([p.name, p.type_label, p.notes, p.manufacturer,
                      p.pressure_rating, p.material,
                      p.property_label, p.property_address, owners].join(" "));
      return norm(q).split(/\s+/).every(function (t) { return !t || hay.indexOf(t) >= 0; });
    }
    // Deutsche Zahl (Punkt -> Komma) fuer die kompakte Feldanzeige.
    function fmtNum(n) {
      return n == null ? "" : String(n).replace(".", ",");
    }
    // Strukturierte Fachfelder als abgekuerzte „Name Wert"-Kette (nur befuellte).
    // Ersetzt die fruehere reine Notiz-Anzeige; die Notiz wandert ans Ende.
    function fieldsHtml(p) {
      var bits = [];
      if (p.pressure_rating) bits.push(escapeHtml(p.pressure_rating));
      if (p.manufacturer) bits.push("Fabr. " + escapeHtml(p.manufacturer));
      if (p.material) bits.push("Mat. " + escapeHtml(p.material));
      if (p.dimension_dn != null) bits.push("DN " + p.dimension_dn);
      if (p.installation_depth_m != null) bits.push("Tiefe " + fmtNum(p.installation_depth_m) + " m");
      if (p.ground_level_m != null) bits.push("GOK " + fmtNum(p.ground_level_m) + " m");
      if (!bits.length) return "";
      return '<span class="technik-list-fields text-secondary small">' + bits.join(" · ") + "</span>";
    }
    function liHtml(f) {
      var p = f.properties || {};
      var isLine = p.geometry_kind === "line";
      var pt = V.pointTypes && V.pointTypes[p.feature_type];
      var icon = isLine ? "fa-route" : (pt ? pt.icon : "fa-map-marker-alt");
      var fields = fieldsHtml(p);
      // Notiz nur noch als Anhang ganz am Ende (falls noch vorhanden).
      var note = p.notes
        ? '<span class="technik-list-note text-secondary text-truncate small">' + escapeHtml(p.notes) + "</span>"
        : "";
      return '<li class="list-group-item list-group-item-action technik-list-item" data-feature-id="' + p.id + '">' +
        '<i class="fas ' + icon + ' me-2" style="color:' + typeColor(p) + '"></i>' +
        '<span class="fw-medium">' + escapeHtml(featLabel(f)) + "</span> " +
        '<span class="text-secondary small">' + escapeHtml(p.type_label || "") + "</span>" +
        fields + note + "</li>";
    }
    function renderList() {
      if (!listEl) return;
      var items = [];
      Object.keys(store.featById).forEach(function (id) {
        var f = store.featById[id];
        if (!currentFilter || matchFeature(f, currentFilter)) items.push(f);
      });
      items.sort(function (a, b) { return featLabel(a).localeCompare(featLabel(b), "de"); });
      listEl.innerHTML = items.map(liHtml).join("");
      if (listEmptyEl) listEmptyEl.style.display = items.length ? "none" : "";
    }
    function showList() {
      listMode = true;
      if (listToggleBtn) listToggleBtn.classList.add("active");
      if (listViewEl) listViewEl.style.display = "";
      var p = panelEl(); if (p) p.style.display = "none";
      setSheet(true);
    }
    function showPanel() {   // nur DOM umschalten; listMode bleibt (Detail liegt ueber der Liste)
      if (listViewEl) listViewEl.style.display = "none";
      var p = panelEl(); if (p) p.style.display = "";
    }
    // Default-Ansicht: Liste aus, leeres Detail-Panel. Genutzt von "Zurücksetzen",
    // dem Listen-X, beim Leeren der Suche und nach Schliessen/Loeschen ohne Liste.
    function showEmpty() {
      listMode = false;
      if (listToggleBtn) listToggleBtn.classList.remove("active");
      resetPanel();
      showPanel();
      setSheet(false);   // Mobile: Sheet runter, Karte wieder voll sichtbar
    }
    function showDetail(id) {     // Panel einblenden + laden, Karte NICHT bewegen (Marker-Klick)
      showPanel();
      openPanel(id);
      setSheet(true);
    }
    function selectFeature(id) {  // wie showDetail, zusaetzlich auf das Element zentrieren (Liste/Deep-Link)
      showPanel();
      focusFeature(map, store, id);
      setSheet(true);
    }

    if (searchInput) {
      // Tippen blendet die gefilterte Liste ein; leeres Feld faellt auf das
      // Default-Panel zurueck (wie "Zurücksetzen").
      searchInput.addEventListener("input", function () {
        currentFilter = this.value || "";
        if (currentFilter) { renderList(); showList(); }
        else { showEmpty(); }
      });
    }
    function clearAndClose() {   // Suche leeren + Liste schliessen (Zurücksetzen / Listen-X)
      if (searchInput) searchInput.value = "";
      currentFilter = "";
      showEmpty();
    }
    var listResetBtn = document.getElementById("technik-list-reset");
    if (listResetBtn) listResetBtn.addEventListener("click", clearAndClose);
    var listCloseBtn = document.getElementById("technik-list-close");
    if (listCloseBtn) listCloseBtn.addEventListener("click", clearAndClose);
    // "Elementliste"-Button: blendet die vollstaendige Liste ein/aus (Toggle).
    if (listToggleBtn) {
      listToggleBtn.addEventListener("click", function () {
        if (listMode) { clearAndClose(); }
        else {
          if (searchInput) searchInput.value = "";
          currentFilter = "";
          renderList();
          showList();
        }
      });
    }
    // Layer auf der Karte hervorheben (Linie verstaerken / Marker vergroessern).
    function highlightLayer(id, on) {
      var layer = store.byId[id];
      if (!layer) return;
      if (layer.setStyle) {                  // Linie
        var baseW = store.renderOpts.lineWeight || 4;   // eingestellte Staerke respektieren
        layer.setStyle(on ? { weight: baseW + 3, opacity: 1 } : { weight: baseW, opacity: 0.9 });
        if (on && layer.bringToFront) layer.bringToFront();
      } else if (layer.getElement) {         // Punkt-Marker (divIcon)
        var el = layer.getElement();
        if (el) el.classList.toggle("technik-marker-hl", on);
      }
    }
    var hoveredId = null;
    function setHover(id) {                   // flackerfrei: nur bei echtem Wechsel toggeln
      if (hoveredId === id) return;
      if (hoveredId != null) highlightLayer(hoveredId, false);
      hoveredId = id;
      if (hoveredId != null) highlightLayer(hoveredId, true);
    }

    if (listEl) {
      listEl.addEventListener("click", function (e) {
        var li = e.target.closest(".technik-list-item");
        if (li) selectFeature(li.getAttribute("data-feature-id"));
      });
      listEl.addEventListener("mouseover", function (e) {
        var li = e.target.closest(".technik-list-item");
        setHover(li ? li.getAttribute("data-feature-id") : null);
      });
      listEl.addEventListener("mouseleave", function () { setHover(null); });
    }

    var pending = null; // {feature_type, geometry}
    var editMode = false;

    function persistGeometry(id, layer) {
      var geom = layer.toGeoJSON().geometry;
      postJson(T.base + "features/" + id + "/geometry", { geometry: geom })
        .then(function (feat) { layer.bindPopup(popupHtml(feat.properties)); })
        .catch(function (err) { console.error("Geometrie speichern fehlgeschlagen", err); });
    }

    var storeOpts = { onSelect: showDetail, onGeometry: persistGeometry };

    // Bestehende Features laden
    fetchFeatures(T.featuresUrl).then(function (fc) {
      (fc.features || []).forEach(function (f) { store.add(f, storeOpts); });
      store.fit();
      renderList();
      // Deep-Link (Dashboard / Elementliste): ?feature=<id> -> auswaehlen + zentrieren.
      var deepId = new URLSearchParams(window.location.search).get("feature");
      if (deepId && store.byId[deepId]) selectFeature(deepId);
    });

    // --- Zeichnen ueber die Typ-Palette ---
    function cancelDraw() {
      pending = null;
      if (map.pm) map.pm.disableDraw();
      document.querySelectorAll(".technik-add.active").forEach(function (b) { b.classList.remove("active"); });
      var cancelBtn = document.getElementById("technik-cancel-draw");
      if (cancelBtn) cancelBtn.style.display = "none";
    }

    document.querySelectorAll(".technik-add").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!map.pm) { alert("Zeichen-Bibliothek nicht geladen."); return; }
        cancelDraw();
        pending = { feature_type: btn.getAttribute("data-feature-type"), geometry: btn.getAttribute("data-geometry") };
        btn.classList.add("active");
        var cancelBtn = document.getElementById("technik-cancel-draw");
        if (cancelBtn) cancelBtn.style.display = "";
        map.pm.enableDraw(pending.geometry === "line" ? "Line" : "Marker", { continueDrawing: false });
      });
    });

    var cancelBtn = document.getElementById("technik-cancel-draw");
    if (cancelBtn) cancelBtn.addEventListener("click", cancelDraw);

    // Bootstrap-Tooltips fuer die Typ-Palette (Beschreibungen je Feature-Typ).
    // Das title-Attribut bleibt als Fallback, falls Bootstrap nicht geladen ist.
    if (window.bootstrap && window.bootstrap.Tooltip) {
      document.querySelectorAll('.technik-add[data-bs-toggle="tooltip"]').forEach(function (el) {
        new window.bootstrap.Tooltip(el);
      });
    }

    // --- Live-Vermessung beim Linien-Zeichnen (Laenge + Richtungswinkel) ---
    // Ein kleines Overlay oben mittig auf der Karte zeigt fuer das gerade
    // gezogene Segment Laenge + Azimut sowie die Gesamtlaenge. pointer-events:none
    // (CSS), damit Klicks zum Setzen der Stuetzpunkte durchgehen.
    var measureBox = null;
    var drawingLine = false;
    var placedPts = [];   // tatsaechlich gesetzte Stuetzpunkte (via pm:vertexadded)
    function ensureMeasureBox() {
      if (!measureBox) {
        measureBox = L.DomUtil.create("div", "technik-measure", map.getContainer());
      }
      return measureBox;
    }
    function hideMeasure() { if (measureBox) measureBox.style.display = "none"; }
    function measureHint() {
      ensureMeasureBox().innerHTML =
        '<span class="text-secondary">Klicken, um den ersten Stützpunkt zu setzen…</span>';
    }
    function updateMeasure(cursor) {
      var box = ensureMeasureBox();
      if (!placedPts.length) { measureHint(); return; }
      var last = placedPts[placedPts.length - 1];
      var segM = segLengthM(last, cursor);
      var brg = bearingDeg(last, cursor);
      var html = '<span class="technik-measure-main">' +
        '<i class="fas fa-ruler-horizontal me-1"></i>' + fmtLength(segM) +
        ' &nbsp;·&nbsp; <i class="fas fa-drafting-compass me-1"></i>' +
        Math.round(brg) + '° ' + compassDir(brg) + '</span>';
      if (placedPts.length > 1) {
        var totalM = pathLengthM(placedPts) + segM;
        html += '<span class="technik-measure-sub text-secondary">Gesamt: ' +
          fmtLength(totalM) + ' · ' + (placedPts.length + 1) + ' Stützpunkte</span>';
      }
      box.innerHTML = html;
    }

    if (map.pm) {
      map.on("pm:drawstart", function (e) {
        if (e.shape !== "Line") return;
        drawingLine = true;
        placedPts = [];
        ensureMeasureBox().style.display = "";
        measureHint();
        e.workingLayer.on("pm:vertexadded", function (ev) {
          if (ev.latlng) { placedPts.push(ev.latlng); updateMeasure(ev.latlng); }
        });
      });
      map.on("pm:drawend", function (e) {
        if (e.shape && e.shape !== "Line") return;
        drawingLine = false;
        placedPts = [];
        hideMeasure();
      });
      map.on("mousemove", function (e) {
        if (drawingLine) updateMeasure(e.latlng);
      });
    }

    if (map.pm) {
      map.on("pm:create", function (e) {
        var geometry = e.layer.toGeoJSON().geometry;
        map.removeLayer(e.layer);          // Roh-Layer entfernen, Server-Version kommt zurueck
        var ftype = pending ? pending.feature_type : null;
        cancelDraw();
        postJson(T.createUrl, { geometry: geometry, feature_type: ftype, plan_id: T.planId })
          .then(function (feat) {
            store.add(feat, storeOpts);
            renderList();
            selectFeature(feat.id);
          })
          .catch(function (err) { console.error("Anlegen fehlgeschlagen", err); alert("Element konnte nicht angelegt werden."); });
      });
    }

    // --- Bearbeiten-Umschalter (Zeichnen-Leiste + Geometrie verschieben) ---
    // Blendet die Zeichnen-Palette ein/aus und schaltet den Geoman-Edit-Modus.
    var editBtn = document.getElementById("technik-edit-toggle");
    var drawBar = document.getElementById("technik-draw-bar");
    if (editBtn && map.pm) {
      editBtn.addEventListener("click", function () {
        editMode = !editMode;
        editBtn.classList.toggle("active", editMode);
        if (drawBar) drawBar.classList.toggle("is-open", editMode);
        if (editMode) {
          map.pm.enableGlobalEditMode();
        } else {
          cancelDraw();                 // laufendes Zeichnen abbrechen
          map.pm.disableGlobalEditMode();
        }
      });
    }

    // --- Position ---
    var locateBtn = document.getElementById("technik-locate");
    if (locateBtn) {
      locateBtn.addEventListener("click", function () {
        map.locate({ setView: true, maxZoom: 17 });
      });
      map.on("locationfound", function (e) {
        L.circleMarker(e.latlng, { radius: 8, color: "#206bc4", fillColor: "#4dabf7", fillOpacity: 0.7 })
          .addTo(map).bindPopup("Ihr Standort").openPopup();
      });
      map.on("locationerror", function () { alert("Standort konnte nicht ermittelt werden."); });
    }

    // --- Panel schliessen ---
    var panel = panelEl();
    if (panel) {
      panel.addEventListener("click", function (e) {
        if (e.target.closest("#technik-panel-close")) {
          resetPanel();
          if (listMode) showList(); else showEmpty();
        }
      });
    }

    // --- Sync via HX-Trigger (Panel-Aktionen) ---
    document.body.addEventListener("technik:featureSaved", function (e) {
      var feat = e.detail;
      if (!feat || feat.id == null) return;
      store.remove(feat.id);
      store.add(feat, storeOpts);
      renderList();
    });
    document.body.addEventListener("technik:featureDeleted", function (e) {
      if (e.detail && e.detail.id != null) store.remove(e.detail.id);
      resetPanel();
      renderList();
      if (listMode) showList(); else showEmpty();
    });
  }

  // --- Druckkarte (Druckseite) ---------------------------------------------
  // Statische Karte mit grauem Hintergrund + Steuerleiste (d-print-none):
  // Symbolgroesse, Linienstaerke, Typ-Filter und Elementlisten-Toggle. Beim
  // Drucken wird die Karte neu vermessen (invalidateSize) und auf die sichtbaren
  // Features eingepasst, damit der Ausdruck die ganze Seite fuellt.

  function num(el, fallback) {
    var v = el ? parseFloat(el.value) : NaN;
    return isNaN(v) ? fallback : v;
  }

  function wirePrintControls(map, store) {
    var scaleEl = document.getElementById("technik-print-marker-scale");
    var weightEl = document.getElementById("technik-print-line-weight");
    var dashEl = document.getElementById("technik-print-dash");
    function currentRenderOpts() {
      return {
        markerScale: num(scaleEl, 0.65),
        lineWeight: num(weightEl, 3),
        dashMode: dashEl ? dashEl.value : "auto",
      };
    }

    // Regler-Aenderungen rAF-gebuendelt anwenden (1194 Features nicht je
    // Pixel-Schritt neu bauen — sonst ruckelt das Ziehen).
    var pending = false;
    function scheduleRestyle() {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () {
        pending = false;
        store.restyle(currentRenderOpts());
      });
    }
    if (scaleEl) scaleEl.addEventListener("input", scheduleRestyle);
    if (weightEl) weightEl.addEventListener("input", scheduleRestyle);
    if (dashEl) dashEl.addEventListener("change", function () { store.restyle(currentRenderOpts()); });

    // Typ-Filter (Checkboxen aus dem Vokabular): Feature-Typ ein-/ausblenden und
    // auf die verbleibenden Features einpassen (z. B. Hausanschluesse weg ->
    // Trunk-Netz wird sichtbar).
    document.querySelectorAll(".technik-print-type").forEach(function (cb) {
      cb.addEventListener("change", function () {
        store.toggleType(cb.getAttribute("data-type"), cb.checked);
        store.fit();
      });
    });
    var allBtn = document.getElementById("technik-print-types-all");
    var noneBtn = document.getElementById("technik-print-types-none");
    function setAllTypes(on) {
      document.querySelectorAll(".technik-print-type").forEach(function (cb) {
        cb.checked = on;
        store.toggleType(cb.getAttribute("data-type"), on);
      });
      store.fit();
    }
    if (allBtn) allBtn.addEventListener("click", function () { setAllTypes(true); });
    if (noneBtn) noneBtn.addEventListener("click", function () { setAllTypes(false); });

    // Elementliste nur am Bildschirm zeigen, im Ausdruck optional (Default: an).
    var listCb = document.getElementById("technik-print-show-list");
    var listEl = document.getElementById("technik-print-list");
    if (listCb && listEl) {
      listCb.addEventListener("change", function () {
        listEl.classList.toggle("technik-print-list-off", !listCb.checked);
      });
    }

    // Seitenformat (Quer-/Hochformat): die @page-Regel + passende Kartenhoehe in
    // <style id="technik-page-style"> umschreiben. Hochformat bekommt eine hoehere
    // Karte (mehr vertikaler Platz auf A4 portrait).
    var pageStyle = document.getElementById("technik-page-style");
    var orientEl = document.getElementById("technik-print-orientation");
    function applyOrientation() {
      if (!pageStyle) return;
      var portrait = orientEl && orientEl.value === "portrait";
      // Karte EXAKT im A4-Seitenverhaeltnis (Breite = Hoehe x Verhaeltnis),
      // zentriert: Hochformat 18.38 x 26 cm (210:297), Querformat 24.75 x 17.5 cm
      // (297:210). Hoehe bewusst < druckbare Seitenhoehe, damit der kompakte Titel
      // drueber passt und die Karte trotzdem Seite 1 fuellt (Legende -> Seite 2).
      var dims = portrait
        ? "width:18.38cm!important;height:26cm!important"
        : "width:24.75cm!important;height:17.5cm!important";
      pageStyle.textContent =
        "@media print{@page{size:A4 " + (portrait ? "portrait" : "landscape") + ";margin:10mm}" +
        "#technik-print-map{" + dims + ";margin-left:auto!important;margin-right:auto!important}}";
    }
    if (orientEl) orientEl.addEventListener("change", applyOrientation);
    applyOrientation();

    // Beim Drucken NUR neu vermessen (Print-CSS aendert die Karten-Hoehe), aber
    // den vom Nutzer gewaehlten Ausschnitt behalten: Mittelpunkt + Zoom vorher
    // sichern, nach invalidateSize per setView exakt wiederherstellen. KEIN
    // store.fit() hier — das wuerde auf das ganze Netz zurueckzoomen und genau
    // die Position verwerfen, die gedruckt werden soll.
    function resizeForPrint() {
      var center = map.getCenter(), zoom = map.getZoom();
      map.invalidateSize({ animate: false, pan: false });
      map.setView(center, zoom, { animate: false });
    }
    window.addEventListener("beforeprint", resizeForPrint);
    window.addEventListener("afterprint", resizeForPrint);
    if (window.matchMedia) {
      try {
        window.matchMedia("print").addEventListener("change", function () { resizeForPrint(); });
      } catch (e) { /* aeltere Browser ohne addEventListener auf MediaQueryList */ }
    }

    // Drucken-Button: kurz warten, damit die Kacheln des AKTUELLEN Ausschnitts
    // fertig laden, dann den Dialog oeffnen (Mittelpunkt/Zoom bleiben).
    var printBtn = document.getElementById("technik-print-btn");
    if (printBtn) {
      printBtn.addEventListener("click", function () {
        window.setTimeout(function () { window.print(); }, 300);
      });
    }
  }

  function renderPrint(elId) {
    if (typeof L === "undefined" || !document.getElementById(elId)) return;
    var map = createMap(elId, { startLayer: "_grau" });
    var store = new FeatureStore(map);
    store.renderOpts = {
      markerScale: num(document.getElementById("technik-print-marker-scale"), 0.65),
      lineWeight: num(document.getElementById("technik-print-line-weight"), 3),
      dashMode: (document.getElementById("technik-print-dash") || {}).value || "auto",
    };
    fetchFeatures(T.featuresUrl).then(function (fc) {
      (fc.features || []).forEach(function (f) { store.add(f, {}); });
      store.fit();
    });
    wirePrintControls(map, store);
  }

  window.TECHNIK_MAP = {
    initEditor: initEditor,
    renderPrint: renderPrint,
    renderStatic: renderPrint, // Rueckwaerts-Kompat (alter Aufrufname)
    createMap: createMap,
  };

  // Editor automatisch starten, sobald das DOM steht (Vollseiten-Load).
  if (document.getElementById("technik-map")) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initEditor, { once: true });
    } else {
      initEditor();
    }
  }
})();
