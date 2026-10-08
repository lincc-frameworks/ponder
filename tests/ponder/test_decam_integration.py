"""Opt-in live Sorcha check; execute on a compute node with staged catalogs/kernels."""

import json
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ponder.decam_pointings import SCHEMA, normalize
from ponder.decam_run import prepare, geometry, checkpoint, finalize
from ponder.decam_pilot import pilot
from ponder.predictions import catalog_rows, convert_orbit


@pytest.mark.skipif(
    os.environ.get("PONDER_LIVE_SORCHA") != "1", reason="requires staged kernels and compute node"
)
def test_real_sorcha_direct_and_indexed(tmp_path):
    root = Path(os.environ["PONDER_DECAM_DATA"])
    inputs = root / "runs/2026-10-05-v1/inputs"
    catalogs = {}
    for mode, filename in (("comet", "comets.json.gz"), ("non_comet", "asteroids.json.gz")):
        rows = []
        for index, row in enumerate(catalog_rows(inputs / filename)):
            converted, _ = convert_orbit(row, index, comet=mode == "comet")
            if converted:
                rows.append(row)
            if len(rows) == 24:
                break
        assert len(rows) == 24
        path = tmp_path / (mode + ".json")
        path.write_text(json.dumps(rows))
        catalogs[mode] = path
    fixture = json.loads((Path(__file__).parent / "data/noirlab_decam.json").read_text())
    exposures = [normalize(row)[0] for row in fixture["rows"]]
    catalog = tmp_path / "catalog.parquet"
    pq.write_table(pa.Table.from_pylist(exposures, schema=SCHEMA), catalog)
    run = tmp_path / "run"
    manifest = prepare(run, catalog, root / "auxiliary", catalogs=catalogs)
    geometry(run)
    report = pilot(run, manifest)
    assert report["passed"]
    assert report["edge_matches"] > 0
    for chunk in manifest["chunks"]:
        result = checkpoint(run, chunk["index"])
        assert result["input_rows"] == chunk["rows"]
        assert checkpoint(run, chunk["index"]) == result
    published = finalize(run)
    assert published["nights"] == 1
    assert finalize(run) == published
    assert pq.read_schema(run / "nightly/2024-01-01.parquet").names[-1] == "field_separation_deg"
