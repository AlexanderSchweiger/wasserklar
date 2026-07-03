/* Störungs-/Rohrbruch-Journal — Leaflet-Karte.
 *
 * Zwei Modi (window.INCIDENT.mode), beide auf einer per hx-boost="false" voll
 * geladenen Seite (Leaflet liegt im <head>, block head_extra):
 *   - "collection" (#incidents-map): alle Störungen als Pins + optionaler
 *     Leitungsplan-Kontext-Layer (Plan im Dropdown wählbar).
 *   - "single" (#incident-detail-map): genau eine Störung; Pin setzen/verschieben
 *     (Klick/Drag) → POST an INCIDENT.geometryUrl. Plan als Kontext + Fokus.
 *
 * Konfig aus window.INCIDENT: mode, dataUrl, geometryUrl, feature, vocab,
 * csrfToken, plans, defaultPlanId, networkBase.
 */
(function () {
  "use strict";

  var I = window.INCIDENT || {};
  var V = I.vocab || {};

  var STATUS_COLOR = { offen: "#c92a2a", in_bearbeitung: "#f59f00", behoben: "#2b8a3e" };

  // --- Basiskarten (basemap.at + OSM) --------------------------------------

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
    return { "Karte (basemap.at)": standard, "Orthofoto (basemap.at)": ortho, "OpenStreetMap": osm, _default: standard };
  }

  function createMap(elId) {
    var layers = baseLayers();
    var map = L.map(elId, { center: [47.59, 14.14], zoom: 7, layers: [layers._default] });
    var bases = {};
    Object.keys(layers).forEach(function (k) { if (k !== "_default") bases[k] = layers[k]; });
    L.control.layers(bases, {}, { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);
    return map;
  }

  // --- Marker / Popup ------------------------------------------------------

  function statusColor(props) { return STATUS_COLOR[props.status] || "#868e96"; }

  function incidentIcon(props) {
    var fa = props.type_icon || "fa-exclamation-triangle";
    return L.divIcon({
      className: "incident-marker-wrap",
      html: '<span class="incident-marker" style="background:' + statusColor(props) + ";border-color:" +
            (props.severity_color || "#fff") + '"><i class="fas ' + fa + '"></i></span>',
      iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -14],
    });
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function popupHtml(props) {
    var html = '<strong>' + escapeHtml(props.title || props.type_label) + '</strong>';
    html += '<br><span class="text-secondary">' + escapeHtml(props.type_label) + '</span>';
    html += '<br>Status: ' + escapeHtml(props.status_label) + ' · ' + escapeHtml(props.severity_label);
    if (props.detected_at) html += '<br>Erkannt: ' + escapeHtml(props.detected_at);
    if (props.detail_url) html += '<br><a href="' + props.detail_url + '" target="_top">Details öffnen</a>';
    return html;
  }

  function toLatLng(coord) { return [coord[1], coord[0]]; } // GeoJSON [lng,lat] -> [lat,lng]

  // --- Leitungsplan-Kontext-Layer (geteilt von beiden Modi) ----------------

  function attachPlanLayer(map) {
    var planLayer = L.layerGroup().addTo(map);

    // load(planId, focus): Plan-Features (read-only, grau-blau) rendern.
    // ``focus`` = true -> nach dem Laden auf den Plan zoomen.
    function load(planId, focus) {
      planLayer.clearLayers();
      if (!planId || !I.networkBase) return;
      fetch(I.networkBase + "?plan=" + encodeURIComponent(planId), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (fc) {
          if (!fc || !fc.features) return;
          var bounds = [];
          fc.features.forEach(function (f) {
            var g = f.geometry || {};
            if (g.type === "Point") {
              var ll = toLatLng(g.coordinates);
              L.circleMarker(ll, { radius: 4, color: "#1971c2", weight: 1, fillColor: "#1971c2", fillOpacity: 0.6 }).addTo(planLayer);
              bounds.push(ll);
            } else if (g.type === "LineString") {
              var lls = g.coordinates.map(toLatLng);
              L.polyline(lls, { color: "#1971c2", weight: 2, opacity: 0.6 }).addTo(planLayer);
              lls.forEach(function (x) { bounds.push(x); });
            }
          });
          if (focus && bounds.length) map.fitBounds(bounds, { padding: [30, 30], maxZoom: 18 });
        })
        .catch(function () {});
    }

    return { layer: planLayer, load: load };
  }

  // --- Collection-Modus (Sammelkarte) --------------------------------------

  function initCollection() {
    var map = createMap("incidents-map");
    var plan = attachPlanLayer(map);
    var planSel = document.getElementById("incident-plan-select");
    var incidentBounds = [];

    // Plan-Umschalter: beim manuellen Wechsel auf den Plan fokussieren, solange
    // keine verorteten Störungen den Kartenausschnitt bestimmen.
    if (planSel) {
      planSel.addEventListener("change", function () {
        plan.load(this.value, !incidentBounds.length);
      });
    }

    fetch(I.dataUrl, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        (fc.features || []).forEach(function (f) {
          var props = f.properties || {};
          var ll = toLatLng(f.geometry.coordinates);
          L.marker(ll, { icon: incidentIcon(props) }).bindPopup(popupHtml(props)).addTo(map);
          incidentBounds.push(ll);
        });
        if (incidentBounds.length) map.fitBounds(incidentBounds, { padding: [40, 40], maxZoom: 17 });
        // Default-Plan NACH den Störungen laden, damit der Fokus-Check stimmt
        // (Fokus auf den Plan nur, wenn es keine verorteten Störungen gibt).
        if (I.defaultPlanId) {
          if (planSel) planSel.value = String(I.defaultPlanId);
          plan.load(I.defaultPlanId, !incidentBounds.length);
        }
      })
      .catch(function () {});
  }

  // --- Single-Modus (Detailseite) ------------------------------------------

  function initSingle() {
    var map = createMap("incident-detail-map");
    var feature = I.feature;
    var hasPos = !!(feature && feature.geometry);
    var marker = null;
    var editMode = false;
    var plan = attachPlanLayer(map);
    var editBtn = document.getElementById("incident-map-edit");
    var clearBtn = document.getElementById("incident-map-clear");
    var hint = document.getElementById("incident-map-hint");
    var planSel = document.getElementById("incident-plan-select");

    // Leaflet rendert grau, wenn der Container beim Init noch nicht final
    // ausgelegt ist (Card im Flex-Grid) — nach dem Layout-Tick neu vermessen.
    setTimeout(function () { map.invalidateSize(); }, 200);

    function applyDraggable() {
      if (!marker || !marker.dragging) return;
      if (editMode) marker.dragging.enable(); else marker.dragging.disable();
    }

    function setCursor() {
      map.getContainer().style.cursor = editMode ? "crosshair" : "";
    }

    function place(latlng) {
      if (marker) {
        marker.setLatLng(latlng);
      } else {
        marker = L.marker(latlng, { draggable: true });
        marker.addTo(map);
        marker.on("dragend", function () { save(marker.getLatLng()); });
        applyDraggable();
      }
    }

    function save(latlng) {
      var geometry = latlng ? { type: "Point", coordinates: [latlng.lng, latlng.lat] } : null;
      fetch(I.geometryUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": I.csrfToken },
        body: JSON.stringify({ geometry: geometry }),
      }).then(function (r) {
        if (!r.ok) return;
        hasPos = !!latlng;
        if (clearBtn) clearBtn.style.display = latlng ? "" : "none";
        if (hint) hint.textContent = latlng
          ? 'Position gespeichert. „Position setzen" klicken, um den Pin zu verschieben.'
          : 'Position entfernt.';
      });
    }

    // Vorhandene Position anzeigen + drauf zentrieren.
    if (hasPos) {
      var ll = toLatLng(feature.geometry.coordinates);
      place(ll);
      map.setView(ll, 18);
    }

    // Initialer Plan (Default = erster aktiver) + Umschalter. Fokus auf den Plan
    // nur, wenn die Störung noch keine eigene Position hat.
    if (planSel) {
      planSel.addEventListener("change", function () { plan.load(this.value, !hasPos); });
    }
    if (I.defaultPlanId) {
      if (planSel) planSel.value = String(I.defaultPlanId);
      plan.load(I.defaultPlanId, !hasPos);
    }

    map.on("click", function (e) {
      if (!editMode) return;
      place(e.latlng);
      save(e.latlng);
    });

    if (editBtn) {
      editBtn.addEventListener("click", function () {
        editMode = !editMode;
        editBtn.classList.toggle("btn-primary", editMode);
        editBtn.innerHTML = editMode
          ? '<i class="fas fa-check me-1"></i> Fertig'
          : '<i class="fas fa-pen me-1"></i> Position setzen';
        applyDraggable();
        setCursor();
        if (hint) hint.textContent = editMode
          ? "Auf die Karte tippen, um die Position zu setzen (oder den Pin ziehen)."
          : (hasPos ? '„Position setzen" klicken, um den Pin zu verschieben.' : 'Noch keine Position hinterlegt.');
      });
    }

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        if (marker) { map.removeLayer(marker); marker = null; }
        save(null);
      });
    }
  }

  // --- Boot ----------------------------------------------------------------

  function boot() {
    if (typeof L === "undefined") { return; }
    if (I.mode === "collection" && document.getElementById("incidents-map")) {
      initCollection();
    } else if (I.mode === "single" && document.getElementById("incident-detail-map")) {
      initSingle();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
