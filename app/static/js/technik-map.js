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
    var ortho = L.tileLayer(
      "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
      { maxZoom: 20, maxNativeZoom: 19, attribution: bmAttr }
    );
    var osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende',
    });
    return { "Karte (basemap.at)": standard, "Orthofoto (basemap.at)": ortho, "OpenStreetMap": osm, _default: standard };
  }

  function createMap(elId) {
    var layers = baseLayers();
    var map = L.map(elId, { center: [47.59, 14.14], zoom: 7, layers: [layers._default] });
    var overlays = {};
    var bases = {};
    Object.keys(layers).forEach(function (k) { if (k !== "_default") bases[k] = layers[k]; });
    L.control.layers(bases, overlays, { position: "topright" }).addTo(map);
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

  function pointIcon(props) {
    var color = typeColor(props);
    var pt = V.pointTypes && V.pointTypes[props.feature_type];
    var fa = pt ? pt.icon : "fa-map-marker-alt";
    return L.divIcon({
      className: "technik-marker-wrap",
      html: '<span class="technik-marker" style="background:' + color + '"><i class="fas ' + fa + '"></i></span>',
      iconSize: [26, 26],
      iconAnchor: [13, 13],
      popupAnchor: [0, -14],
    });
  }

  function lineDash(accuracy) {
    var a = V.accuracies && V.accuracies[accuracy];
    return a && a.dash ? a.dash : null;
  }

  function popupHtml(props) {
    var html = '<strong>' + escapeHtml(props.name || props.type_label) + '</strong>';
    html += '<br><span class="text-secondary">' + escapeHtml(props.type_label) + '</span>';
    if (props.length_m != null) html += '<br>Länge: ' + props.length_m + ' m';
    if (props.dimension_dn != null) html += ' · DN ' + props.dimension_dn;
    return html;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function buildLayer(feature) {
    var props = feature.properties || {};
    var layer;
    if (feature.geometry.type === "Point") {
      layer = L.marker(toLatLng(feature.geometry.coordinates), { icon: pointIcon(props) });
    } else {
      var latlngs = feature.geometry.coordinates.map(toLatLng);
      layer = L.polyline(latlngs, {
        color: typeColor(props), weight: 4, opacity: 0.9, dashArray: lineDash(props.accuracy),
      });
    }
    layer._technikId = feature.id;
    layer._technikType = props.feature_type;
    layer.bindPopup(popupHtml(props));
    return layer;
  }

  function toLatLng(coord) { return [coord[1], coord[0]]; } // GeoJSON [lng,lat] -> Leaflet [lat,lng]

  // --- Feature-Verwaltung --------------------------------------------------

  function FeatureStore(map) {
    this.map = map;
    this.group = L.featureGroup().addTo(map);
    this.byId = {};
    this.hiddenTypes = {};
  }
  FeatureStore.prototype.add = function (feature, opts) {
    var layer = buildLayer(feature);
    this.byId[feature.id] = layer;
    if (!this.hiddenTypes[layer._technikType]) this.group.addLayer(layer);
    if (opts && opts.onSelect) layer.on("click", function () { opts.onSelect(feature.id); });
    if (opts && opts.onGeometry) layer.on("pm:update", function () { opts.onGeometry(feature.id, layer); });
    return layer;
  };
  FeatureStore.prototype.remove = function (id) {
    var layer = this.byId[id];
    if (layer) { this.group.removeLayer(layer); delete this.byId[id]; }
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

  function legendControl(store) {
    var ctrl = L.control({ position: "bottomleft" });
    ctrl.onAdd = function () {
      var div = L.DomUtil.create("div", "technik-legend card");
      var html = '<div class="technik-legend-head"><strong>Legende</strong></div><div class="technik-legend-body">';
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
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      div.addEventListener("change", function (e) {
        var cb = e.target.closest("input[type=checkbox]");
        if (cb) store.toggleType(cb.getAttribute("data-type"), cb.checked);
      });
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

  // Deep-Link vom Dashboard: ?feature=<id> -> Panel oeffnen und hinzoomen.
  function focusFromUrl(map, store) {
    var id = new URLSearchParams(window.location.search).get("feature");
    if (!id) return;
    var layer = store.byId[id];
    if (!layer) return;
    openPanel(id);
    if (layer.getLatLng) map.setView(layer.getLatLng(), 18);
    else if (layer.getBounds) map.fitBounds(layer.getBounds().pad(0.5));
  }

  // --- Editor-Init ---------------------------------------------------------

  function initEditor() {
    var el = document.getElementById("technik-map");
    if (!el || typeof L === "undefined") return;

    var map = createMap("technik-map");
    var store = new FeatureStore(map);
    legendControl(store).addTo(map);
    resetPanel();

    var pending = null; // {feature_type, geometry}
    var editMode = false;

    function persistGeometry(id, layer) {
      var geom = layer.toGeoJSON().geometry;
      postJson(T.base + "features/" + id + "/geometry", { geometry: geom })
        .then(function (feat) { layer.bindPopup(popupHtml(feat.properties)); })
        .catch(function (err) { console.error("Geometrie speichern fehlgeschlagen", err); });
    }

    var storeOpts = { onSelect: openPanel, onGeometry: persistGeometry };

    // Bestehende Features laden
    fetchFeatures(T.featuresUrl).then(function (fc) {
      (fc.features || []).forEach(function (f) { store.add(f, storeOpts); });
      store.fit();
      focusFromUrl(map, store);
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

    if (map.pm) {
      map.on("pm:create", function (e) {
        var geometry = e.layer.toGeoJSON().geometry;
        map.removeLayer(e.layer);          // Roh-Layer entfernen, Server-Version kommt zurueck
        var ftype = pending ? pending.feature_type : null;
        cancelDraw();
        postJson(T.createUrl, { geometry: geometry, feature_type: ftype, plan_id: T.planId })
          .then(function (feat) {
            store.add(feat, storeOpts);
            openPanel(feat.id);
          })
          .catch(function (err) { console.error("Anlegen fehlgeschlagen", err); alert("Objekt konnte nicht angelegt werden."); });
      });
    }

    // --- Bearbeiten-Umschalter (Geometrie verschieben/Stuetzpunkte) ---
    var editBtn = document.getElementById("technik-edit-toggle");
    if (editBtn && map.pm) {
      editBtn.addEventListener("click", function () {
        editMode = !editMode;
        editBtn.classList.toggle("active", editMode);
        if (editMode) { map.pm.enableGlobalEditMode(); } else { map.pm.disableGlobalEditMode(); }
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
        if (e.target.closest("#technik-panel-close")) resetPanel();
      });
    }

    // --- Sync via HX-Trigger (Panel-Aktionen) ---
    document.body.addEventListener("technik:featureSaved", function (e) {
      var feat = e.detail;
      if (!feat || feat.id == null) return;
      store.remove(feat.id);
      store.add(feat, storeOpts);
    });
    document.body.addEventListener("technik:featureDeleted", function (e) {
      if (e.detail && e.detail.id != null) store.remove(e.detail.id);
      resetPanel();
    });
  }

  // --- Statische Karte (Druckseite) ----------------------------------------

  function renderStatic(elId) {
    if (typeof L === "undefined" || !document.getElementById(elId)) return;
    var map = createMap(elId);
    var store = new FeatureStore(map);
    fetchFeatures(T.featuresUrl).then(function (fc) {
      (fc.features || []).forEach(function (f) { store.add(f, {}); });
      store.fit();
    });
  }

  window.TECHNIK_MAP = { initEditor: initEditor, renderStatic: renderStatic, createMap: createMap };

  // Editor automatisch starten, sobald das DOM steht (Vollseiten-Load).
  if (document.getElementById("technik-map")) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initEditor, { once: true });
    } else {
      initEditor();
    }
  }
})();
