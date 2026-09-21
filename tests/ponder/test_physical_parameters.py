import pandas as pd
import pytest
from ponder import runner
from ponder.utils import select_physical_parameters


def test_survey_photometry_aligns_by_id_not_row_position():
    physical = pd.DataFrame({"ObjID": ["B", "A", "unused"], "H_VR": [8.0, 9.0, 10.0]})
    out = select_physical_parameters(physical, ["A", "B"])
    assert out.ObjID.tolist() == ["A", "B"]
    assert out.H_VR.tolist() == [9.0, 8.0]
    assert "H_r" not in out


@pytest.mark.parametrize("ids", [["A", "A"], ["A", None]])
def test_ambiguous_ids_rejected(ids):
    with pytest.raises(ValueError):
        select_physical_parameters(pd.DataFrame({"ObjID": ids, "H_VR": [8.0, 9.0]}), ["A"])


def test_missing_object_rejected():
    with pytest.raises(ValueError, match="Missing physical"):
        select_physical_parameters(pd.DataFrame({"ObjID": ["A"], "H_VR": [8.0]}), ["B"])


def test_override_reaches_chunk_runner(monkeypatch):
    orbs = pd.DataFrame({"ObjID": ["A", "B"]})
    defaults = pd.DataFrame({"ObjID": ["A", "B"], "H_r": [1.0, 2.0]})
    monkeypatch.setattr(runner, "build_id_set_inputs", lambda *a: (orbs, defaults, orbs))
    seen = []
    monkeypatch.setattr(runner, "run_sorcha_chunks", lambda *args, **kwargs: seen.append(args[1]))
    physical = pd.DataFrame({"ObjID": ["B", "A"], "H_VR": [8.0, 9.0]})
    runner.run_id_set(
        [], ["A", "B"], "new", "now", False, "db", "config", 100, 1, physical_parameters=physical
    )
    assert seen[0].H_VR.tolist() == [9.0, 8.0]
