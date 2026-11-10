import pytest

from terminal_model.types import Position
from terminal_sim.grid import Grid


def make() -> Grid:
    return Grid(20, 6, 20.0)


def test_one_way_lanes():
    g = make()
    assert set(g.neighbors((3, 0))) == {(4, 0), (3, 1)}
    assert set(g.neighbors((3, 1))) == {(2, 1), (3, 0), (3, 2)}


def test_neighbors_stay_inside_grid():
    g = make()
    assert g.neighbors((19, 0)) == [(19, 1)]
    assert g.neighbors((0, 1)) == [(0, 0), (0, 2)]
    assert g.neighbors((0, 5)) == [(0, 4)]


def test_path_respects_direction():
    path = make().shortest_path((5, 0), (3, 0))
    assert path[0] == (5, 0) and path[-1] == (3, 0)
    assert any(y == 1 for _, y in path)
    for a, b in zip(path, path[1:]):
        assert b in make().neighbors(a)


def test_path_length_cells():
    g = make()
    assert g.shortest_path((2, 0), (6, 0)) == [(2, 0), (3, 0), (4, 0), (5, 0), (6, 0)]
    assert g.shortest_path((4, 4), (4, 4)) == [(4, 4)]
    assert g.shortest_path((5, 0), (3, 0)) == [(5, 0), (5, 1), (4, 1), (3, 1), (3, 0)]


def test_unreachable_returns_empty():
    assert make().shortest_path((19, 5), (0, 0)) != []
    assert Grid(1, 1, 20.0).shortest_path((0, 0), (0, 0)) == [(0, 0)]
    assert Grid(2, 1, 20.0).shortest_path((1, 0), (0, 0)) == []


def test_reserve_conflict():
    g = make()
    assert g.try_reserve((2, 2), "A") is True
    assert g.try_reserve((2, 2), "B") is False
    assert g.try_reserve((2, 2), "A") is True
    assert g.owner_of((2, 2)) == "A"
    g.release((2, 2), "B")
    assert g.owner_of((2, 2)) == "A"
    g.release((2, 2), "A")
    assert g.owner_of((2, 2)) is None
    assert g.try_reserve((2, 2), "B") is True


def test_center():
    assert make().center((2, 5)) == Position(50, 110)


def test_out_of_grid_raises():
    g = make()
    for bad in [(-1, 0), (20, 0), (0, 6), (0, -1)]:
        with pytest.raises(ValueError):
            g.try_reserve(bad, "A")
        with pytest.raises(ValueError):
            g.release(bad, "A")
        with pytest.raises(ValueError):
            g.center(bad)
        with pytest.raises(ValueError):
            g.shortest_path(bad, (0, 0))
        with pytest.raises(ValueError):
            g.shortest_path((0, 0), bad)


def test_shortest_path_respects_avoid():
    g = make()
    assert g.shortest_path((0, 2), (3, 2)) == [(0, 2), (1, 2), (2, 2), (3, 2)]
    path = g.shortest_path((0, 2), (3, 2), avoid={(1, 2)})
    assert path and (1, 2) not in path and path[0] == (0, 2) and path[-1] == (3, 2)
    # start and goal are always allowed
    assert g.shortest_path((0, 2), (1, 2), avoid={(0, 2), (1, 2)}) == [(0, 2), (1, 2)]


def test_reserved_by_others_excludes_own_cells():
    g = make()
    g.try_reserve((1, 1), "AGV-01")
    g.try_reserve((2, 2), "AGV-02")
    g.try_reserve((3, 3), "AGV-02")
    assert g.reserved_by_others("AGV-01") == {(2, 2), (3, 3)}
    assert g.reserved_by_others("AGV-03") == {(1, 1), (2, 2), (3, 3)}
