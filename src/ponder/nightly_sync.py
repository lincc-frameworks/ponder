"""Transfer verified nightly Parquets through remote staging and SHA-256 checks."""

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def remote_action(config):
    """Also executed on the remote host using only the standard library."""
    target, stage = Path(config["target"]), Path(config["stage"])
    expected, identity = config["expected"], config["identity"]
    dates = {name for name in expected if "/" not in name and name.endswith(".parquet")}
    for name in expected:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("Unsafe relative transfer filename")

    def verify(root, partial=False):
        actual = {p.name for p in root.glob("*.parquet")}
        if not actual <= dates:
            raise RuntimeError("Unexpected nightly files at destination")
        complete = True
        for i, (name, wanted) in enumerate(expected.items()):
            path = root / name
            if partial and not path.exists():
                complete = False
                continue
            if not path.is_file() or checksum(path) != wanted:
                raise RuntimeError("Remote checksum conflict: " + str(path))
            if config["action"] == "finish" and (i + 1) % 500 == 0:
                print("Verified remote files:", i + 1, "/", len(expected), flush=True)
        return complete

    if config["action"] == "prepare":
        if target.exists() and verify(target, partial=True):
            return {"already_complete": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < config["bytes"] * 1.1:
            raise RuntimeError("Insufficient remote free space")
        stage.mkdir(exist_ok=True)
        marker = stage / "_transfer_identity.json"
        if marker.exists() and json.loads(marker.read_text()) != identity:
            raise RuntimeError("Staging directory belongs to a different publication")
        marker.write_text(json.dumps(identity, indent=2) + "\n")
        return {"already_complete": False}
    if config["action"] != "finish":
        raise RuntimeError("Unknown transfer action")
    if config.get("already_complete"):
        verify(target)
    elif stage.exists():
        verify(stage)
        if target.exists():
            verify(target, partial=True)
            # Existing identical files are reused; no existing content is overwritten.
            for name, wanted in expected.items():
                dest = target / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(stage / name, dest)
                except FileExistsError:
                    if checksum(dest) != wanted:
                        raise RuntimeError("Concurrent destination change: " + str(dest))
        else:
            stage.rename(target)
    else:
        verify(target)
    report = {
        **identity,
        "passed": True,
        "destination": str(target),
        "verified_files": len(expected),
        "verified_utc": datetime.now(timezone.utc).isoformat(),
    }
    temporary = target / "sync_verification.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(target / "sync_verification.json")
    return report


def remote(host, config):
    program = (
        "import hashlib,json,os,shutil\nfrom pathlib import Path\n"
        "from datetime import datetime,timezone\n"
        + inspect.getsource(checksum)
        + "\n"
        + inspect.getsource(remote_action)
        + "\n"
        + "print(json.dumps(remote_action(json.loads("
        + repr(json.dumps(config))
        + "))))\n"
    )
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, "python3 -"],
        input=program,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    lines = completed.stdout.strip().splitlines()
    for line in lines[:-1]:
        print(line, flush=True)
    return json.loads(lines[-1])


def sync(run, destination):
    run = Path(run).resolve()
    host, separator, path = destination.partition(":")
    if not separator or not re.fullmatch(r"[\w.-]+@[\w.-]+", host) or not path.startswith("/"):
        raise ValueError("Destination must be user@host:/absolute/path")
    verified = json.loads((run / "verification.json").read_text())
    publication = json.loads((run / "publication.json").read_text())
    if not verified.get("passed") or verified.get("pilot") or publication.get("pilot"):
        raise RuntimeError("A verified full publication is required")
    if (verified["rows"], verified["nights"], verified["manifest_sha256"]) != (
        publication["rows"],
        publication["nights"],
        checksum(run / "manifest.json"),
    ):
        raise RuntimeError("Publication provenance mismatch")
    transfer = run / "publication_v2/transfer"
    bundle = transfer / "bundle"
    provenance = bundle / "_provenance"
    provenance.mkdir(parents=True, exist_ok=True)
    expected = {}
    for line in (run / "nightly/SHA256SUMS").read_text().splitlines():
        wanted, name = line.split("  ", 1)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.parquet", name) or name in expected:
            raise RuntimeError("Invalid nightly checksum manifest")
        if checksum(run / "nightly" / name) != wanted:
            raise RuntimeError("Local nightly checksum mismatch: " + name)
        expected[name] = wanted
    if len(expected) != verified["nights"] or set(expected) != {
        p.name for p in (run / "nightly").glob("*.parquet")
    }:
        raise RuntimeError("Nightly file count mismatch")
    files = list(expected)
    (transfer / "nightly.files").write_text("".join(name + "\n" for name in files))
    for name in [
        "manifest.json",
        "publication.json",
        "verification.json",
        "nightly_coverage.parquet",
        "excluded_exposures.parquet",
        "failed_orbits.json",
        "environment.txt",
    ]:
        shutil.copy2(run / name, provenance / name)
    for population in ["comet", "non_comet"]:
        shutil.copy2(
            run / "inputs" / population / "excluded_orbits.jsonl",
            provenance / f"{population}_excluded_orbits.jsonl",
        )
    for file in sorted(provenance.iterdir()):
        expected["_provenance/" + file.name] = checksum(file)
    (bundle / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in expected.items()))
    expected["SHA256SUMS"] = checksum(bundle / "SHA256SUMS")
    identity = {key: verified[key] for key in ["manifest_sha256", "publisher_sha256", "rows", "nights"]}
    identity["run_id"] = run.name
    target = Path(path)
    stage = target.with_name("." + target.name + "." + run.name + ".incoming")
    config = {
        "target": str(target),
        "stage": str(stage),
        "expected": expected,
        "identity": identity,
        "bytes": sum((run / "nightly" / name).stat().st_size for name in files),
        "action": "prepare",
    }
    result = remote(host, config)
    if not result["already_complete"]:
        options = [
            "rsync",
            "-rt",
            "--checksum",
            "--partial",
            "--partial-dir=.rsync-partial",
            "--protect-args",
            "--info=stats2",
            "-e",
            "ssh -o BatchMode=yes -o ConnectTimeout=20",
        ]
        subprocess.run(
            options
            + [
                "--files-from=" + str(transfer / "nightly.files"),
                str(run / "nightly") + "/",
                host + ":" + str(stage) + "/",
            ],
            check=True,
        )
        subprocess.run(options + [str(bundle) + "/", host + ":" + str(stage) + "/"], check=True)
    config["action"] = "finish"
    config["already_complete"] = result["already_complete"]
    report = remote(host, config)
    report["remote_host"] = host
    temporary = run / "sync.json.tmp"
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(run / "sync.json")
    print(json.dumps(report, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    sync(args.run_dir, args.destination)


if __name__ == "__main__":
    main()
