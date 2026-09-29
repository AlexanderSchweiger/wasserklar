/* Basiskarten je Land — geteilt von allen Leaflet-Karten (Leitungsnetz,
 * Störungen, Touren, Rundschreiben, Liegenschafts-Minikarte, Feuerwehr-Freigabe).
 *
 * Die Konfiguration kommt als nicht-ausfuehrbares JSON aus _map_config.html
 * (<script type="application/json" id="wk-map-config">) — CSP-tauglich und
 * hx-boost-sicher (das JSON liegt im <body> und wird bei jeder Navigation mit
 * getauscht). Quelle: app/country.py (map_config). Fehlt die Konfiguration,
 * gilt der fruehere Stand (basemap.at, Startausschnitt Oesterreich).
 *
 * API:
 *   wkBasemaps.layers()  -> { "<Label>": L.TileLayer, …, _default, _grau?, _ortho? }
 *   wkBasemaps.view()    -> { center: [lat, lng], zoom }
 */
(function () {
  "use strict";

  var BM_AT_ATTR = 'Datenquelle: <a href="https://www.basemap.at" target="_blank" rel="noopener">basemap.at</a>';
  var FALLBACK = {
    center: [47.59, 14.14],
    zoom: 7,
    layers: [
      { label: "Karte (basemap.at)", role: "default", maxNativeZoom: 19, attribution: BM_AT_ATTR,
        url: "https://mapsneu.wien.gv.at/basemap/geolandbasemap/normal/google3857/{z}/{y}/{x}.png" },
      { label: "Karte grau (basemap.at)", role: "grau", maxNativeZoom: 19, attribution: BM_AT_ATTR,
        url: "https://mapsneu.wien.gv.at/basemap/bmapgrau/normal/google3857/{z}/{y}/{x}.png" },
      { label: "Orthofoto (basemap.at)", role: "ortho", maxNativeZoom: 19, attribution: BM_AT_ATTR,
        url: "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg" },
    ],
    ortho_wms: null,
  };

  function config() {
    var el = document.getElementById("wk-map-config");
    if (el) {
      try { return JSON.parse(el.textContent); } catch (e) { /* Fallback */ }
    }
    return FALLBACK;
  }

  function layers() {
    var cfg = config();
    var out = {};
    (cfg.layers || []).forEach(function (l) {
      var layer = L.tileLayer(l.url, {
        maxZoom: 20,
        maxNativeZoom: l.maxNativeZoom || 19,
        attribution: l.attribution || "",
      });
      out[l.label] = layer;
      if (l.role && !out["_" + l.role]) out["_" + l.role] = layer;
    });
    // Optionales Luftbild des Mandanten (WMS, z.B. Orthophotos eines Bundeslandes).
    if (cfg.ortho_wms && cfg.ortho_wms.url && L.tileLayer.wms) {
      var wms = L.tileLayer.wms(cfg.ortho_wms.url, {
        layers: cfg.ortho_wms.layers,
        format: "image/jpeg",
        transparent: false,
        maxZoom: 20,
        attribution: cfg.ortho_wms.attribution || "",
      });
      out["Luftbild"] = wms;
      if (!out._ortho) out._ortho = wms;
    }
    var osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende',
    });
    out["OpenStreetMap"] = osm;
    if (!out._default) out._default = osm;
    return out;
  }

  function view() {
    var cfg = config();
    return { center: cfg.center || FALLBACK.center, zoom: cfg.zoom || FALLBACK.zoom };
  }

  window.wkBasemaps = { layers: layers, view: view, config: config };
})();
