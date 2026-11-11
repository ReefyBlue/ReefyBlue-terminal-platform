from pathlib import Path

import pytest
from terminal_model.layout import load_terminal
from terminal_sim.stack import Stack, StackError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def blk():
    return load_terminal(ROOT / "terminal_mv_small.yaml").blocks[0]


@pytest.fixture
def stack(blk):
    return Stack([blk])


def test_put_stacks_on_top_and_take_requires_top(stack):
    assert stack.put("A", 3, 2, "c1") == ("A", 3, 2, 0)
    assert stack.put("A", 3, 2, "c2") == ("A", 3, 2, 1)
    assert stack.top("A", 3, 2) == "c2"
    assert stack.column_height("A", 3, 2) == 2
    assert stack.locate("c1") == ("A", 3, 2, 0)
    with pytest.raises(StackError, match="c1"):
        stack.take("c1")
    assert stack.take("c2") == ("A", 3, 2, 1)
    assert stack.take("c1") == ("A", 3, 2, 0)
    assert stack.top("A", 3, 2) is None
    assert stack.locate("c1") is None


def test_above_is_top_down(stack):
    for c in ("a", "b", "c"):
        stack.put("A", 0, 0, c)
    assert stack.above("a") == ["c", "b"]
    assert stack.above("c") == []
    with pytest.raises(StackError, match="zz"):
        stack.above("zz")


def test_full_column_and_bounds_and_duplicates(stack, blk):
    for i in range(blk.tiers):
        stack.put("A", 1, 1, f"x{i}")
    with pytest.raises(StackError, match="full"):
        stack.put("A", 1, 1, "extra")
    with pytest.raises(StackError):
        stack.put("A", blk.bays, 0, "oob")
    with pytest.raises(StackError):
        stack.put("A", 0, blk.rows, "oob")
    with pytest.raises(StackError):
        stack.put("Z", 0, 0, "nb")
    with pytest.raises(StackError, match="x0"):
        stack.put("A", 2, 2, "x0")
    with pytest.raises(StackError):
        stack.take("nobody")


def test_free_columns_sorted_and_excludes_full(stack, blk):
    stack.put("A", 0, 0, "a")
    cols = stack.free_columns("A")
    assert len(cols) == blk.bays * blk.rows
    assert cols[0] == (0, 1)  # height 0 first, then bay, row
    assert cols[-1] == (0, 0)  # the only height-1 column is last
    assert cols == sorted(cols, key=lambda c: (stack.column_height("A", *c), *c))
    for i in range(blk.tiers):
        stack.put("A", 5, 5, f"f{i}")
    assert (5, 5) not in stack.free_columns("A")


def test_exclude_rows_and_reefer_rule(stack, blk):
    assert blk.reefer_rows
    non = stack.free_columns_for("A", reefer=False)
    assert all(r not in blk.reefer_rows for _, r in non)
    ree = stack.free_columns_for("A", reefer=True)
    assert ree and all(r in blk.reefer_rows for _, r in ree)
    assert len(non) + len(ree) == blk.bays * blk.rows
    assert all(r != 3 for _, r in stack.free_columns("A", exclude_rows=[3]))


def test_occupancy(stack, blk):
    assert stack.occupancy("A") == 0.0
    stack.put("A", 0, 0, "a")
    stack.put("A", 0, 0, "b")
    assert stack.occupancy("A") == pytest.approx(2 / (blk.bays * blk.rows * blk.tiers))
