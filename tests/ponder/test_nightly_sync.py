import json

import pytest

from ponder.nightly_sync import checksum, remote_action


@pytest.fixture
def config(tmp_path):
    file = tmp_path / "sample"
    file.write_bytes(b"verified nightly data")
    return {
        "target": str(tmp_path / "data/nightly"),
        "stage": str(tmp_path / "data/.incoming"),
        "expected": {"2024-01-01.parquet": checksum(file)},
        "identity": {"run_id": "test", "nights": 1, "rows": 2},
        "bytes": 20,
        "action": "prepare",
    }


def test_remote_staging_verification_promotion_and_repeat(config):
    from pathlib import Path

    assert not remote_action(config)["already_complete"]
    stage, target = Path(config["stage"]), Path(config["target"])
    (stage / "2024-01-01.parquet").write_bytes(b"verified nightly data")
    result = remote_action({**config, "action": "finish"})
    assert result["passed"] and result["verified_files"] == 1
    assert target.exists() and not stage.exists()
    assert json.loads((target / "sync_verification.json").read_text())["passed"]
    assert remote_action(config)["already_complete"]
    assert remote_action({**config, "action": "finish"})["passed"]


def test_bad_remote_checksum_never_promotes(config):
    from pathlib import Path

    remote_action(config)
    (Path(config["stage"]) / "2024-01-01.parquet").write_bytes(b"broken")
    with pytest.raises(RuntimeError, match="checksum"):
        remote_action({**config, "action": "finish"})
    assert not Path(config["target"]).exists()


def test_conflicting_existing_destination_is_preserved(config):
    from pathlib import Path

    target = Path(config["target"])
    target.mkdir(parents=True)
    file = target / "2024-01-01.parquet"
    file.write_bytes(b"other science run")
    with pytest.raises(RuntimeError, match="conflict"):
        remote_action(config)
    assert file.read_bytes() == b"other science run"


def test_partial_identical_destination_is_reused(config):
    from pathlib import Path

    target = Path(config["target"])
    target.mkdir(parents=True)
    unrelated = target / "notes.txt"
    unrelated.write_text("retain me")
    remote_action(config)
    (Path(config["stage"]) / "2024-01-01.parquet").write_bytes(b"verified nightly data")
    assert remote_action({**config, "action": "finish"})["passed"]
    assert unrelated.read_text() == "retain me"


def test_other_run_staging_cannot_be_reused(config):
    remote_action(config)
    with pytest.raises(RuntimeError, match="different publication"):
        remote_action({**config, "identity": {"run_id": "another"}})
