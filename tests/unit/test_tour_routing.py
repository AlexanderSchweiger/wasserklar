"""Unit-Tests fuer die Luftlinien-Routenplanung (NN + 2-Opt), ohne DB."""
from app.meter_tours.services import (
    nearest_neighbour_order, plan_route, route_length_m, two_opt,
)

# Punkte entlang eines Laengengrads — Distanzen verhalten sich linear.
START = (48.000, 16.000)
POINTS = {
    1: (48.001, 16.000),   # ~111 m vom Start
    2: (48.010, 16.000),   # ~1.1 km
    3: (48.005, 16.000),   # ~555 m
    4: (48.002, 16.000),   # ~222 m
    5: (48.020, 16.000),   # ~2.2 km
}


class TestNearestNeighbour:
    def test_orders_by_proximity_chain(self):
        order = nearest_neighbour_order(START, POINTS)
        assert order == [1, 4, 3, 2, 5]

    def test_single_point(self):
        assert nearest_neighbour_order(START, {7: (48.5, 16.5)}) == [7]

    def test_empty(self):
        assert nearest_neighbour_order(START, {}) == []


class TestTwoOpt:
    def test_untangles_crossing_route(self):
        # Absichtlich verdrehte Reihenfolge: 2-Opt muss die Gesamtlaenge
        # strikt verbessern und die lineare Kette wiederherstellen.
        bad = [5, 1, 3, 4, 2]
        bad_len = route_length_m(bad, START, POINTS)
        best = two_opt(bad, START, POINTS)
        best_len = route_length_m(best, START, POINTS)
        assert best_len < bad_len
        assert best == [1, 4, 3, 2, 5]

    def test_keeps_optimal_route(self):
        optimal = [1, 4, 3, 2, 5]
        assert two_opt(optimal, START, POINTS) == optimal

    def test_short_routes_unchanged(self):
        assert two_opt([1], START, POINTS) == [1]
        assert two_opt([1, 2], START, POINTS) == [1, 2]


class TestPlanRoute:
    def test_ungeocoded_appended_at_end(self):
        items = [
            (1, 48.001, 16.000),
            (9, None, None),
            (3, 48.005, 16.000),
            (8, 48.002, None),   # halbe Koordinate zaehlt als ungeocodet
        ]
        ordered, ungeocoded = plan_route(START[0], START[1], items)
        assert ordered == [1, 3]
        assert ungeocoded == [9, 8]

    def test_without_start_uses_first_point_as_anchor(self):
        items = [(1, 48.001, 16.000), (2, 48.010, 16.000), (3, 48.005, 16.000)]
        ordered, ungeocoded = plan_route(None, None, items)
        assert set(ordered) == {1, 2, 3}
        assert ungeocoded == []
        # Anker = erster Punkt -> Kette laeuft von 1 aus aufsteigend.
        assert ordered[0] == 1

    def test_all_ungeocoded(self):
        ordered, ungeocoded = plan_route(START[0], START[1], [(4, None, None)])
        assert ordered == []
        assert ungeocoded == [4]

    def test_empty(self):
        assert plan_route(START[0], START[1], []) == ([], [])
