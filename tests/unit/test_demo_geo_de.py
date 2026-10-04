"""Geometrie des deutschen Demo-Leitungsplans (``app/seed/demo_geo_de.py``).

Der Plan ist generiert, nicht gezeichnet — diese Tests halten die Netzlogik fest,
damit ein neu erzeugter Ausschnitt nicht still „unsinnig" wird: ein zusammen-
hängendes Netz, Anschlüsse auf den Leitungen, Armaturen auf den Achsen und nie
auf einer Anbohrschelle, gleiche Namen wie das österreichische Modul.
"""
import math

from app.seed import demo_geo, demo_geo_de as geo

M = 111320.0
COS = math.cos(math.radians(geo.CENTER[1]))


def _dist(a, b):
    return math.hypot((a[0] - b[0]) * M * COS, (a[1] - b[1]) * M)


def _dist_to_segment(p, a, b):
    ax, ay = (a[0] - p[0]) * M * COS, (a[1] - p[1]) * M
    bx, by = (b[0] - p[0]) * M * COS, (b[1] - p[1]) * M
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def _all_lines():
    return [coords for _street, _section, coords in geo.SUPPLY_LINES] + [geo.MAIN_LINE]


def _dist_to_network(p):
    return min(_dist_to_segment(p, a, b) for line in _all_lines() for a, b in zip(line, line[1:]))


def _key(p):
    return (round(p[0], 6), round(p[1], 6))


def test_same_names_as_austrian_module():
    public = {name for name in dir(demo_geo) if name.isupper()}
    assert public <= {name for name in dir(geo) if name.isupper()}


def test_network_is_connected():
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for line in _all_lines():
        for a, b in zip(line, line[1:]):
            parent[find(_key(a))] = find(_key(b))
    assert len({find(x) for x in parent}) == 1


def test_house_connections_tap_supply_line_vertices():
    vertices = {_key(p) for _s, _n, coords in geo.SUPPLY_LINES for p in coords}
    assert len(geo.HOUSE_CONNECTIONS) == len(geo.HOUSE_ADDRESSES) >= 100
    for h_lng, h_lat, t_lng, t_lat in geo.HOUSE_CONNECTIONS:
        assert _key((t_lng, t_lat)) in vertices
        assert _dist((h_lng, h_lat), (t_lng, t_lat)) <= 36.0      # Stichleitung höchstens ~35 m


def test_addresses_are_unique_and_on_supplied_streets():
    streets = {street for street, _section, _coords in geo.SUPPLY_LINES}
    addresses = list(geo.HOUSE_ADDRESSES) + list(geo.UNASSIGNED_NEAR_ADDRESSES)
    assert len(set(addresses)) == len(addresses)
    assert {street for street, _no in addresses} <= streets


def test_fittings_sit_on_the_network_but_never_on_a_tap():
    taps = [(t_lng, t_lat) for _h1, _h2, t_lng, t_lat in geo.HOUSE_CONNECTIONS]
    fittings = [p[:2] for p in geo.HYDRANTS + geo.VALVES] + [
        geo.END_CAP[:2], geo.DRAIN[:2], geo.AIR_VALVE, geo.PRESSURE_REDUCER[:2],
        geo.MATERIAL_CHANGE, geo.SAMPLING_NETWORK, geo.TIE_IN[:2], geo.DISTRIBUTOR[:2]]
    for p in fittings:
        assert _dist_to_network(p) < 0.5, p
        assert min(_dist(p, t) for t in taps) > 4.0, p


def test_main_line_runs_from_reservoir_to_distributor():
    assert _key(geo.MAIN_LINE[0]) == _key(geo.RESERVOIR)
    assert _key(geo.MAIN_LINE[-1]) == _key(geo.DISTRIBUTOR[:2])
    assert 0 < geo.MATERIAL_CHANGE_INDEX < len(geo.MAIN_LINE) - 1
    assert _key(geo.MAIN_LINE[geo.MATERIAL_CHANGE_INDEX]) == _key(geo.MATERIAL_CHANGE)


def test_hydraulics_are_plausible():
    # Quellen über dem Sammelschacht (Freispiegel), Pumpe hebt auf den Hochbehälter,
    # Hochbehälter über dem Ortsverteiler.
    assert min(geo.SPRING_ELEVATIONS) > geo.COLLECTOR_ELEVATION
    assert geo.RESERVOIR_ELEVATION > geo.COLLECTOR_ELEVATION
    assert 30 <= geo.RESERVOIR_ELEVATION - geo.DISTRIBUTOR_ELEVATION <= 70


def test_unassigned_connections():
    houses = [(h[0], h[1]) for h in geo.HOUSE_CONNECTIONS] + [(h[0], h[1]) for h in geo.UNASSIGNED_NEAR]
    # „fern": außerhalb des Zuordnungsradius (60 m) jeder geocodeten Liegenschaft
    for p in geo.UNASSIGNED_FAR:
        assert min(_dist(p, h) for h in houses) > 60.0
    assert len(geo.UNASSIGNED_NEAR) == len(geo.UNASSIGNED_NEAR_ADDRESSES) == 3
