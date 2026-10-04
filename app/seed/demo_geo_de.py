"""Reale Geometrie-Stützpunkte für den **deutschen** Demo-Leitungsplan.

Gegenstück zu ``demo_geo.py`` (Hagenberg, OÖ) mit denselben Namen — ``demo.py``
wählt das Modul über das Land des Demo-Datensatzes. Der deutsche Datensatz spielt
im Altort von **Habach (Lkr. Weilheim-Schongau, Oberbayern)**: die
Versorgungsleitungen folgen echten Straßenachsen, die Hausanschlüsse sitzen auf
echten Gebäuden, die Hauptleitung läuft über die Höhlmühler Straße vom
Hochbehälter am Hang südlich des Orts herein. Der Plan liegt damit deckungsgleich
auf dem basemap.de-Hintergrund.

**Datenherkunft**

* Straßenachsen und Gebäude-Mittelpunkte: © OpenStreetMap-Mitwirkende, Daten
  verfügbar unter der Open Database License (ODbL 1.0),
  https://www.openstreetmap.org/copyright — Auszug vom 2026-10, auf den
  Demo-Ausschnitt beschnitten und per Douglas-Peucker (~2,5 m) vereinfacht.
* Geländehöhen (``*_ELEVATION``): EU-DEM 25 m via opentopodata.org.

Quellen, Sammelschacht, Pumpwerk und Hochbehälter sind **erfunden**; ihre
Koordinaten zeigen auf unbebautes Gelände südlich des Orts und gehören zu keiner
realen Anlage. Ebenso sind sämtliche Kunden-, Objekt- und Verbrauchsdaten frei
erfunden: die Hausanschlüsse markieren nur Gebäude-Positionen, und die
**Hausnummern in** ``HOUSE_ADDRESSES`` **sind synthetisch** (je Straße entlang
der Leitung durchgezählt, links ungerade, rechts gerade) — sie sind bewusst nicht
die amtlichen Nummern der Gebäude.

**Netzlogik** (vom Generator abgeleitet, nicht von Hand gezeichnet):

* Jedes adressierte Gebäude im Altort, das höchstens 35 m von einer benannten
  Ortsstraße entfernt steht, bekommt einen Anschluss (Anbohrschelle = Lotfußpunkt
  auf der Straßenachse). Das Neubaugebiet an der Antdorfer Straße (Nordosten)
  versorgt im Demo-Szenario der Nachbarversorger — dort endet der Notverbund.
* Das Ortsnetz ist die Vereinigung der kürzesten Wege vom Ortsverteiler zu allen
  Anbohrschellen (keine Leitung ohne Abnehmer); Lücken innerhalb derselben
  Straße bis 140 m sind geschlossen (Ringschluss), Sackgassen enden 12 m hinter
  dem letzten Haus. An Abzweigen wird die geradeste Fortsetzung zu einer Leitung
  verbunden, jeder Abzweig beginnt mit einem Schieber.
* Hydranten etwa alle 140 m (DVGW W 400-1), Endhydranten an Sackgassen.
* Hochbehälter ~702 m, Ortsnetz 639–659 m: ~4,3 bar am Hochpunkt
  (Steinberg, Be-/Entlüfter), ~6,3 bar im tiefen Ostteil — daher der
  Druckminderschacht an der Höhlmühler Straße. Entleerung am Tiefpunkt in den
  Heubach.

**Reproduktion.** Overpass-Abfragen ``way["highway"]`` und ``way["building"]``
(``out geom`` bzw. ``out center tags``) über (47.7180,11.2620,47.7420,11.3000);
abgeleitet mit einem einmaligen Generator-Skript nach den Regeln oben. Wer den
Ausschnitt austauschen will, ersetzt dieses Modul komplett — ``demo.py`` liest
ausschließlich die hier definierten Namen.

Alle Koordinaten sind ``(lng, lat)`` in WGS84 (GeoJSON-Reihenfolge).
"""

# Bezugspunkt für die lokal-planare Meter-Näherung in ``demo.py``.
CENTER = (11.279982, 47.728073)

# Ort des Demo-Datensatzes (Adressen der Liegenschaften).
POSTCODE = "82392"
TOWN = "Habach"


# Versorgungsleitungen: (Straßenname, Abschnitt-Nr | 0, [(lng, lat), ...]).
# Die Anbohrschellen der Hausanschlüsse liegen als Vertices in der Geometrie.
SUPPLY_LINES = [
    ("Eichbichlstraße", 0, [
        (11.283251, 47.726325), (11.282807, 47.726357), (11.282512, 47.726392),
        (11.282261, 47.726475), (11.282104, 47.726588), (11.281744, 47.726848),
        (11.281643, 47.726903), (11.281435, 47.726931), (11.28118, 47.726905),
        (11.280759, 47.726839), (11.280427, 47.726856), (11.28027, 47.726897),
        (11.280028, 47.727051), (11.279996, 47.727108), (11.279913, 47.72738),
    ]),
    ("Höhlmühler Straße", 0, [
        (11.283251, 47.726325), (11.283574, 47.72696), (11.283616, 47.727141),
        (11.283569, 47.727612), (11.283303, 47.728499), (11.283231, 47.728675),
        (11.283216, 47.728706), (11.283109, 47.729007), (11.283104, 47.729106),
        (11.283136, 47.729293), (11.283194, 47.729607),
    ]),
    ("Ährenanger", 0, [
        (11.282512, 47.726392), (11.282559, 47.726621), (11.28256, 47.726628),
        (11.28264, 47.727019), (11.282688, 47.727148), (11.282784, 47.727344),
        (11.282809, 47.72742), (11.282754, 47.727513), (11.282708, 47.72754),
        (11.282083, 47.727778), (11.281732, 47.72777), (11.281595, 47.727751),
        (11.281415, 47.727726), (11.281228, 47.727694), (11.281414, 47.727411),
        (11.281551, 47.727188), (11.281614, 47.727071), (11.281643, 47.726903),
    ]),
    ("Am Berggraben", 0, [
        (11.282104, 47.726588), (11.282049, 47.726529), (11.28206, 47.72641),
        (11.281951, 47.726231), (11.281792, 47.726162), (11.281676, 47.726149),
        (11.281497, 47.726138), (11.281284, 47.726118), (11.281139, 47.726118),
        (11.280913, 47.72614), (11.280648, 47.726253), (11.280598, 47.726431),
        (11.280474, 47.726754), (11.280427, 47.726856),
    ]),
    ("Auf der Leiten", 1, [
        (11.28027, 47.726897), (11.279906, 47.726844), (11.279628, 47.726792),
        (11.279618, 47.726697), (11.279605, 47.726511), (11.279528, 47.726413),
        (11.279231, 47.726333), (11.27896, 47.726275), (11.278712, 47.726242),
        (11.27846, 47.726226), (11.278018, 47.726199), (11.277733, 47.726135),
        (11.2776, 47.726101), (11.2775, 47.726076),
    ]),
    ("Heubachweg", 0, [
        (11.283109, 47.729007), (11.282554, 47.728729), (11.282538, 47.728721),
        (11.282261, 47.728582), (11.282225, 47.728565), (11.282102, 47.728503),
    ]),
    ("Schmiedgasse", 0, [
        (11.279988, 47.727538), (11.28025, 47.727508), (11.280451, 47.727509),
        (11.280553, 47.727556), (11.280814, 47.727668), (11.280834, 47.727674),
        (11.280933, 47.727701),
    ]),
    ("Auf der Leiten", 2, [
        (11.279605, 47.726511), (11.279188, 47.726461), (11.27894, 47.726438),
        (11.278666, 47.726411), (11.27842, 47.726386), (11.278032, 47.726318),
        (11.278009, 47.726354), (11.277866, 47.726508), (11.277656, 47.726582),
        (11.277549, 47.726582),
    ]),
    ("Stiftsweg", 0, [
        (11.280003, 47.727679), (11.279715, 47.727681), (11.279102, 47.727708),
        (11.279037, 47.727705), (11.278877, 47.727674), (11.278687, 47.727547),
        (11.278323, 47.727504), (11.278198, 47.72742), (11.278134, 47.72728),
    ]),
    ("Dürnhauser Straße", 0, [
        (11.283194, 47.729607), (11.282852, 47.729652), (11.282767, 47.729661),
        (11.282406, 47.729692), (11.282198, 47.729661), (11.282124, 47.729635),
        (11.28185, 47.729524), (11.281811, 47.729504), (11.281565, 47.729376),
        (11.281467, 47.729338), (11.281248, 47.729343),
    ]),
    ("St.-Ulrich-Straße", 1, [
        (11.279621, 47.728384), (11.279366, 47.728419), (11.279239, 47.728436),
    ]),
    ("St.-Ulrich-Straße", 2, [
        (11.279665, 47.728542), (11.279621, 47.728384), (11.279788, 47.728185),
        (11.27988, 47.728055), (11.279959, 47.727882), (11.280003, 47.727679),
        (11.279988, 47.727538), (11.279913, 47.72738), (11.279365, 47.727284),
        (11.279104, 47.727229), (11.278837, 47.727233), (11.278819, 47.727234),
        (11.278591, 47.72726), (11.27849, 47.727267), (11.278134, 47.72728),
        (11.27771, 47.727226), (11.277616, 47.727239), (11.277457, 47.727296),
    ]),
    ("Hofmark", 0, [
        (11.279482, 47.728487), (11.279375, 47.728681), (11.279342, 47.728742),
        (11.279279, 47.729017), (11.279167, 47.729181), (11.279088, 47.729283),
        (11.27904, 47.729347),
    ]),
    ("Hofheimer Straße", 0, [
        (11.277457, 47.727296), (11.277545, 47.727549), (11.277639, 47.72772),
        (11.277661, 47.727749), (11.277773, 47.727854), (11.278003, 47.728046),
        (11.278065, 47.7281), (11.278224, 47.728238),
    ]),
    ("Antdorfer Straße", 0, [
        (11.281248, 47.729343), (11.281311, 47.730124),
    ]),
    ("Hauptstraße", 0, [
        (11.281248, 47.729343), (11.281182, 47.729213), (11.280959, 47.729062),
        (11.280721, 47.728935), (11.280603, 47.728877), (11.280464, 47.728809),
        (11.280269, 47.72873), (11.280183, 47.728701), (11.279846, 47.728594),
        (11.279665, 47.728542), (11.279482, 47.728487), (11.279239, 47.728436),
        (11.279105, 47.728415), (11.278667, 47.728353), (11.278224, 47.728238),
    ]),
    ("Obersöcheringer Straße", 0, [
        (11.278224, 47.728238), (11.278041, 47.728394), (11.277973, 47.728461),
        (11.277753, 47.728717), (11.277647, 47.728828), (11.277439, 47.729006),
        (11.2772, 47.729034), (11.276811, 47.728991), (11.276655, 47.728967),
    ]),
    ("Schulweg", 1, [
        (11.281311, 47.730124), (11.281156, 47.730127), (11.280851, 47.730023),
        (11.280702, 47.729949), (11.279826, 47.729677), (11.279678, 47.729636),
    ]),
    ("Steinberg", 1, [
        (11.277439, 47.729006), (11.277536, 47.729112), (11.277537, 47.72922),
        (11.277527, 47.729268), (11.277365, 47.729813), (11.277321, 47.729912),
        (11.277195, 47.730266), (11.277507, 47.730321), (11.277975, 47.730404),
        (11.278204, 47.730447), (11.278482, 47.730494), (11.278638, 47.730519),
    ]),
    ("Schulweg", 2, [
        (11.280702, 47.729949), (11.280489, 47.730254), (11.280487, 47.730257),
        (11.280374, 47.730419), (11.280295, 47.730532), (11.28025, 47.730597),
    ]),
    ("Steinberg", 2, [
        (11.277321, 47.729912), (11.277654, 47.729991), (11.277675, 47.729995),
        (11.277871, 47.730033), (11.278066, 47.730066), (11.2781, 47.730072),
        (11.278272, 47.730103), (11.278373, 47.730121), (11.278493, 47.730142),
        (11.278598, 47.730157),
    ]),
]

# Straßen des Ortsnetzes (Adressen für Liegenschaften ohne Lage).
STREETS = [
    "Eichbichlstraße", "Höhlmühler Straße", "Ährenanger", "Am Berggraben",
    "Auf der Leiten", "Heubachweg", "Schmiedgasse", "Stiftsweg",
    "Dürnhauser Straße", "St.-Ulrich-Straße", "Hofmark", "Hofheimer Straße",
    "Antdorfer Straße", "Hauptstraße", "Obersöcheringer Straße", "Schulweg",
    "Steinberg",
]

# Hauptleitung Hochbehälter → Ortsverteiler über die Höhlmühler Straße.
MAIN_LINE = [
    (11.2826, 47.72225), (11.283904, 47.722913), (11.283706, 47.723201),
    (11.283181, 47.725499), (11.28318, 47.725993), (11.283251, 47.726325),
]

# Hausanschlüsse: (Gebäude-lng, Gebäude-lat, Anbohrschelle-lng, Anbohrschelle-lat).
HOUSE_CONNECTIONS = [
    (11.282786, 47.726242, 11.282807, 47.726357),
    (11.281907, 47.72695, 11.281744, 47.726848),
    (11.281432, 47.726759, 11.281435, 47.726931),
    (11.281099, 47.727095, 11.28118, 47.726905),
    (11.280751, 47.727045, 11.280759, 47.726839),
    (11.280153, 47.727084, 11.280028, 47.727051),
    (11.279783, 47.727053, 11.279996, 47.727108),
    (11.283532, 47.728529, 11.283303, 47.728499),
    (11.282976, 47.72862, 11.283231, 47.728675),
    (11.283627, 47.728787, 11.283216, 47.728706),
    (11.283475, 47.729097, 11.283104, 47.729106),
    (11.283575, 47.729257, 11.283136, 47.729293),
    (11.282805, 47.726603, 11.282559, 47.726621),
    (11.282372, 47.726642, 11.28256, 47.726628),
    (11.282415, 47.727049, 11.28264, 47.727019),
    (11.282875, 47.727116, 11.282688, 47.727148),
    (11.28294, 47.72731, 11.282784, 47.727344),
    (11.282886, 47.727612, 11.282754, 47.727513),
    (11.282529, 47.727405, 11.282708, 47.72754),
    (11.282158, 47.727936, 11.282083, 47.727778),
    (11.281652, 47.728034, 11.281732, 47.72777),
    (11.281647, 47.727583, 11.281595, 47.727751),
    (11.281359, 47.727875, 11.281415, 47.727726),
    (11.281262, 47.727365, 11.281414, 47.727411),
    (11.281391, 47.727149, 11.281551, 47.727188),
    (11.281484, 47.727039, 11.281614, 47.727071),
    (11.281866, 47.726402, 11.28206, 47.72641),
    (11.281875, 47.726023, 11.281792, 47.726162),
    (11.281692, 47.726005, 11.281676, 47.726149),
    (11.281518, 47.726001, 11.281497, 47.726138),
    (11.281314, 47.725986, 11.281284, 47.726118),
    (11.281115, 47.725989, 11.281139, 47.726118),
    (11.280875, 47.725982, 11.280913, 47.72614),
    (11.280328, 47.726389, 11.280598, 47.726431),
    (11.280263, 47.726711, 11.280474, 47.726754),
    (11.27993, 47.726655, 11.279906, 47.726844),
    (11.279411, 47.726691, 11.279618, 47.726697),
    (11.279647, 47.726319, 11.279528, 47.726413),
    (11.279323, 47.726154, 11.279231, 47.726333),
    (11.279024, 47.72614, 11.27896, 47.726275),
    (11.278731, 47.726112, 11.278712, 47.726242),
    (11.278479, 47.726063, 11.27846, 47.726226),
    (11.27811, 47.72601, 11.278018, 47.726199),
    (11.277826, 47.725969, 11.277733, 47.726135),
    (11.277525, 47.725912, 11.2776, 47.726101),
    (11.282403, 47.728865, 11.282554, 47.728729),
    (11.282689, 47.728584, 11.282538, 47.728721),
    (11.282377, 47.728477, 11.282261, 47.728582),
    (11.282065, 47.728709, 11.282225, 47.728565),
    (11.280207, 47.727354, 11.28025, 47.727508),
    (11.280542, 47.727413, 11.280451, 47.727509),
    (11.280437, 47.727647, 11.280553, 47.727556),
    (11.280783, 47.727719, 11.280814, 47.727668),
    (11.280916, 47.727538, 11.280834, 47.727674),
    (11.279138, 47.726658, 11.279188, 47.726461),
    (11.27889, 47.726645, 11.27894, 47.726438),
    (11.278625, 47.726627, 11.278666, 47.726411),
    (11.278366, 47.726605, 11.27842, 47.726386),
    (11.277833, 47.726302, 11.278009, 47.726354),
    (11.277652, 47.726456, 11.277656, 47.726582),
    (11.279708, 47.727549, 11.279715, 47.727681),
    (11.27908, 47.727988, 11.279102, 47.727708),
    (11.279044, 47.72761, 11.279037, 47.727705),
    (11.278224, 47.727717, 11.278323, 47.727504),
    (11.278028, 47.727504, 11.278198, 47.72742),
    (11.282804, 47.729452, 11.282852, 47.729652),
    (11.282735, 47.729526, 11.282767, 47.729661),
    (11.282125, 47.729755, 11.282198, 47.729661),
    (11.282246, 47.729476, 11.282124, 47.729635),
    (11.281995, 47.729397, 11.28185, 47.729524),
    (11.281731, 47.729574, 11.281811, 47.729504),
    (11.281467, 47.729461, 11.281565, 47.729376),
    (11.279332, 47.728304, 11.279366, 47.728419),
    (11.279542, 47.728099, 11.279788, 47.728185),
    (11.280134, 47.728126, 11.27988, 47.728055),
    (11.27982, 47.727854, 11.279959, 47.727882),
    (11.279289, 47.727421, 11.279365, 47.727284),
    (11.279153, 47.727031, 11.279104, 47.727229),
    (11.278815, 47.727109, 11.278837, 47.727233),
    (11.278848, 47.727394, 11.278819, 47.727234),
    (11.278605, 47.727296, 11.278591, 47.72726),
    (11.278465, 47.727096, 11.27849, 47.727267),
    (11.277712, 47.727128, 11.27771, 47.727226),
    (11.277635, 47.727296, 11.277616, 47.727239),
    (11.279526, 47.728719, 11.279375, 47.728681),
    (11.279212, 47.72871, 11.279342, 47.728742),
    (11.278945, 47.729103, 11.279167, 47.729181),
    (11.279211, 47.729356, 11.279088, 47.729283),
    (11.277755, 47.727513, 11.277545, 47.727549),
    (11.277345, 47.727824, 11.277639, 47.72772),
    (11.277797, 47.727702, 11.277661, 47.727749),
    (11.277658, 47.727918, 11.277773, 47.727854),
    (11.277882, 47.728109, 11.278003, 47.728046),
    (11.278234, 47.728012, 11.278065, 47.7281),
    (11.281474, 47.7291, 11.281182, 47.729213),
    (11.281101, 47.728946, 11.280959, 47.729062),
    (11.280543, 47.729099, 11.280721, 47.728935),
    (11.280778, 47.728716, 11.280603, 47.728877),
    (11.280286, 47.728973, 11.280464, 47.728809),
    (11.280435, 47.728512, 11.280269, 47.72873),
    (11.280036, 47.728895, 11.280183, 47.728701),
    (11.279733, 47.728774, 11.279846, 47.728594),
    (11.279064, 47.728541, 11.279105, 47.728415),
    (11.278586, 47.728605, 11.278667, 47.728353),
    (11.277901, 47.72832, 11.278041, 47.728394),
    (11.278323, 47.728606, 11.277973, 47.728461),
    (11.277618, 47.728659, 11.277753, 47.728717),
    (11.277862, 47.728922, 11.277647, 47.728828),
    (11.276742, 47.729196, 11.276811, 47.728991),
    (11.281059, 47.730295, 11.281156, 47.730127),
    (11.280773, 47.73011, 11.280851, 47.730023),
    (11.279902, 47.729553, 11.279826, 47.729677),
    (11.277677, 47.729083, 11.277536, 47.729112),
    (11.277315, 47.7292, 11.277537, 47.72922),
    (11.277754, 47.729289, 11.277527, 47.729268),
    (11.277507, 47.729841, 11.277365, 47.729813),
    (11.277461, 47.730441, 11.277507, 47.730321),
    (11.277902, 47.730581, 11.277975, 47.730404),
    (11.27814, 47.730601, 11.278204, 47.730447),
    (11.278539, 47.730336, 11.278482, 47.730494),
    (11.28031, 47.730198, 11.280489, 47.730254),
    (11.280653, 47.730309, 11.280487, 47.730257),
    (11.280736, 47.730533, 11.280374, 47.730419),
    (11.28003, 47.730448, 11.280295, 47.730532),
    (11.277591, 47.730135, 11.277654, 47.729991),
    (11.277719, 47.729894, 11.277675, 47.729995),
    (11.277914, 47.72992, 11.277871, 47.730033),
    (11.278104, 47.729966, 11.278066, 47.730066),
    (11.278043, 47.730221, 11.2781, 47.730072),
    (11.278306, 47.730016, 11.278272, 47.730103),
    (11.278319, 47.730258, 11.278373, 47.730121),
    (11.27864, 47.730033, 11.278493, 47.730142),
]

# Adresse je Hausanschluss (gleiche Reihenfolge, synthetische Hausnummern).
HOUSE_ADDRESSES = [
    ("Eichbichlstraße", "1"), ("Eichbichlstraße", "2"), ("Eichbichlstraße", "3"),
    ("Eichbichlstraße", "4"), ("Eichbichlstraße", "6"), ("Eichbichlstraße", "8"),
    ("Eichbichlstraße", "5"), ("Höhlmühler Straße", "2"), ("Höhlmühler Straße", "1"),
    ("Höhlmühler Straße", "4"), ("Höhlmühler Straße", "6"), ("Höhlmühler Straße", "8"),
    ("Ährenanger", "2"), ("Ährenanger", "1"), ("Ährenanger", "3"),
    ("Ährenanger", "4"), ("Ährenanger", "6"), ("Ährenanger", "8"),
    ("Ährenanger", "5"), ("Ährenanger", "10"), ("Ährenanger", "12"),
    ("Ährenanger", "7"), ("Ährenanger", "14"), ("Ährenanger", "16"),
    ("Ährenanger", "18"), ("Ährenanger", "20"), ("Am Berggraben", "2"),
    ("Am Berggraben", "1"), ("Am Berggraben", "3"), ("Am Berggraben", "5"),
    ("Am Berggraben", "7"), ("Am Berggraben", "9"), ("Am Berggraben", "11"),
    ("Am Berggraben", "13"), ("Am Berggraben", "15"), ("Auf der Leiten", "1"),
    ("Auf der Leiten", "2"), ("Auf der Leiten", "3"), ("Auf der Leiten", "5"),
    ("Auf der Leiten", "7"), ("Auf der Leiten", "9"), ("Auf der Leiten", "11"),
    ("Auf der Leiten", "13"), ("Auf der Leiten", "15"), ("Auf der Leiten", "17"),
    ("Heubachweg", "2"), ("Heubachweg", "1"), ("Heubachweg", "3"),
    ("Heubachweg", "4"), ("Schmiedgasse", "2"), ("Schmiedgasse", "4"),
    ("Schmiedgasse", "1"), ("Schmiedgasse", "3"), ("Schmiedgasse", "6"),
    ("Auf der Leiten", "4"), ("Auf der Leiten", "6"), ("Auf der Leiten", "8"),
    ("Auf der Leiten", "10"), ("Auf der Leiten", "19"), ("Auf der Leiten", "21"),
    ("Stiftsweg", "1"), ("Stiftsweg", "2"), ("Stiftsweg", "3"),
    ("Stiftsweg", "4"), ("Stiftsweg", "6"), ("Dürnhauser Straße", "1"),
    ("Dürnhauser Straße", "3"), ("Dürnhauser Straße", "2"), ("Dürnhauser Straße", "5"),
    ("Dürnhauser Straße", "7"), ("Dürnhauser Straße", "4"), ("Dürnhauser Straße", "6"),
    ("St.-Ulrich-Straße", "1"), ("St.-Ulrich-Straße", "2"), ("St.-Ulrich-Straße", "3"),
    ("St.-Ulrich-Straße", "4"), ("St.-Ulrich-Straße", "6"), ("St.-Ulrich-Straße", "5"),
    ("St.-Ulrich-Straße", "7"), ("St.-Ulrich-Straße", "8"), ("St.-Ulrich-Straße", "10"),
    ("St.-Ulrich-Straße", "9"), ("St.-Ulrich-Straße", "11"), ("St.-Ulrich-Straße", "12"),
    ("Hofmark", "2"), ("Hofmark", "1"), ("Hofmark", "3"),
    ("Hofmark", "4"), ("Hofheimer Straße", "2"), ("Hofheimer Straße", "1"),
    ("Hofheimer Straße", "4"), ("Hofheimer Straße", "3"), ("Hofheimer Straße", "5"),
    ("Hofheimer Straße", "6"), ("Hauptstraße", "1"), ("Hauptstraße", "3"),
    ("Hauptstraße", "2"), ("Hauptstraße", "5"), ("Hauptstraße", "4"),
    ("Hauptstraße", "7"), ("Hauptstraße", "6"), ("Hauptstraße", "8"),
    ("Hauptstraße", "10"), ("Hauptstraße", "12"), ("Obersöcheringer Straße", "1"),
    ("Obersöcheringer Straße", "2"), ("Obersöcheringer Straße", "3"), ("Obersöcheringer Straße", "4"),
    ("Obersöcheringer Straße", "6"), ("Schulweg", "2"), ("Schulweg", "4"),
    ("Schulweg", "1"), ("Steinberg", "2"), ("Steinberg", "1"),
    ("Steinberg", "4"), ("Steinberg", "6"), ("Steinberg", "3"),
    ("Steinberg", "5"), ("Steinberg", "7"), ("Steinberg", "8"),
    ("Schulweg", "3"), ("Schulweg", "6"), ("Schulweg", "8"),
    ("Schulweg", "5"), ("Steinberg", "9"), ("Steinberg", "10"),
    ("Steinberg", "12"), ("Steinberg", "14"), ("Steinberg", "11"),
    ("Steinberg", "16"), ("Steinberg", "13"), ("Steinberg", "18"),
]

# Hausanschlüsse ohne Liegenschafts-Zuordnung, aber mit einer geocodierten
# Liegenschaft in Reichweite — per „Zuordnen“-Lauf lösbar.
UNASSIGNED_NEAR = [
    (11.276958, 47.728843, 11.276919, 47.729003),
    (11.281702, 47.729278, 11.28158, 47.729384),
    (11.279428, 47.729004, 11.279285, 47.728989),
]
UNASSIGNED_NEAR_ADDRESSES = [
    ("Obersöcheringer Straße", "7"), ("Dürnhauser Straße", "8"), ("Hofmark", "5"),
]

# Hausanschlüsse ohne Liegenschaft im Umkreis — bleiben auch nach dem
# Zuordnen-Lauf grell markiert.
UNASSIGNED_FAR = [
    (11.282258, 47.724494), (11.282392, 47.73064), (11.275681, 47.730553),
]

# Hydranten: (lng, lat, Standort) — ~140 m entlang der Versorgungsleitungen,
# Endhydranten an Sackgassen, einer auf der Hauptleitung.
HYDRANTS = [
    (11.283446, 47.726707, "Höhlmühler Straße"),
    (11.28347, 47.727941, "Höhlmühler Straße"),
    (11.283115, 47.72917, "Höhlmühler Straße"),
    (11.281981, 47.727776, "Ährenanger"),
    (11.281626, 47.727004, "Ährenanger"),
    (11.279832, 47.728123, "St.-Ulrich-Straße"),
    (11.279288, 47.727268, "St.-Ulrich-Straße"),
    (11.280896, 47.729028, "Hauptstraße"),
    (11.277491, 47.729389, "Steinberg"),
    (11.277709, 47.730357, "Steinberg"),
    (11.278344, 47.726219, "Auf der Leiten"),
    (11.281924, 47.726219, "Am Berggraben"),
    (11.280537, 47.72659, "Am Berggraben"),
    (11.27788, 47.728569, "Obersöcheringer Straße"),
    (11.280767, 47.729981, "Schulweg"),
    (11.277717, 47.727801, "Hofheimer Straße"),
    (11.279312, 47.728873, "Hofmark"),
    (11.280629, 47.727589, "Schmiedgasse"),
    (11.2775, 47.726076, "Auf der Leiten"),
    (11.277549, 47.726582, "Auf der Leiten"),
    (11.27904, 47.729347, "Hofmark"),
    (11.276655, 47.728967, "Obersöcheringer Straße"),
    (11.279678, 47.729636, "Schulweg"),
    (11.28025, 47.730597, "Schulweg"),
    (11.278598, 47.730157, "Steinberg"),
    (11.28351, 47.72406, "Hauptleitung"),
]

# Schieber: (lng, lat, Standort) — am Anfang jedes Abzweigs und am Behälterabgang.
VALVES = [
    (11.283251, 47.726325, "Eichbichlstraße"),
    (11.282512, 47.726392, "Ährenanger"),
    (11.282104, 47.726588, "Am Berggraben"),
    (11.28027, 47.726897, "Auf der Leiten"),
    (11.283109, 47.729007, "Heubachweg"),
    (11.279988, 47.727538, "Schmiedgasse"),
    (11.279605, 47.726511, "Auf der Leiten"),
    (11.280003, 47.727679, "Stiftsweg"),
    (11.283194, 47.729607, "Dürnhauser Straße"),
    (11.279621, 47.728384, "St.-Ulrich-Straße"),
    (11.279665, 47.728542, "St.-Ulrich-Straße"),
    (11.279482, 47.728487, "Hofmark"),
    (11.277457, 47.727296, "Hofheimer Straße"),
    (11.281248, 47.729343, "Antdorfer Straße"),
    (11.278224, 47.728238, "Obersöcheringer Straße"),
    (11.281311, 47.730124, "Schulweg"),
    (11.277439, 47.729006, "Steinberg"),
    (11.280702, 47.729949, "Schulweg"),
    (11.277321, 47.729912, "Steinberg"),
    (11.283904, 47.722913, "Behälterabgang"),
]

# --- Erfundene Anlagen (unbebautes Gelände südlich des Orts) ---------------
# Hochbehälter am Hang, Quellen im Höhlmühler Graben, Sammelschacht im
# Talgrund — die Pumpe hebt von dort aufs Behälterniveau.
RESERVOIR = (11.2826, 47.72225)
RESERVOIR_ELEVATION = 701.9
COLLECTOR = (11.2872, 47.7208)
COLLECTOR_ELEVATION = 676.5
SPRINGS = [
    (11.2876, 47.717), (11.2896, 47.7182), (11.2852, 47.7176),
]
SPRING_ELEVATIONS = (695.2, 706.6, 704.0)

# --- Einbauten auf der Haupt- bzw. Versorgungsleitung -----------------------
# Ortsverteiler: hier übergibt die Hauptleitung ans Ortsnetz.
DISTRIBUTOR = (11.283251, 47.726325, "Höhlmühler Straße")
DISTRIBUTOR_ELEVATION = 648.9
# Hochpunkt des Ortsnetzes (659 m, Steinberg) — Be-/Entlüfter.
AIR_VALVE = (11.278638, 47.730519)
# Materialwechsel auf der Hauptleitung; teilt MAIN_LINE in zwei Abschnitte.
MATERIAL_CHANGE = (11.283706, 47.723201)
MATERIAL_CHANGE_INDEX = 2
# Eintritt in die tiefer liegende Ostzone (Heubachweg, Dürnhauser Straße).
PRESSURE_REDUCER = (11.283569, 47.727612, "Höhlmühler Straße")
# Zapfstelle im Ortsnetz (Probenahme).
SAMPLING_NETWORK = (11.279482, 47.728487)

# --- Strangenden: (lng, lat, Straße) ----------------------------------------
# Blindes Leitungsende (Spülpunkt).
END_CAP = (11.280933, 47.727701, "Schmiedgasse")
# Entleerung am Tiefpunkt in den Heubach (642 m).
DRAIN = (11.282102, 47.728503, "Heubachweg")
# Übergabe an den Nachbarversorger (normal geschlossen).
TIE_IN = (11.281311, 47.730124, "Antdorfer Straße")
TIE_IN_END = (11.281915, 47.731118)
