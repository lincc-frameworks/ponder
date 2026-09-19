"""Select the supplied DEEP VI designations from a frozen MPC JSON catalogue."""

import argparse
import csv
import hashlib
import json
from pathlib import Path


def objects(stream):
    """Incremental JSON array decoding, bounded by one object plus one read block."""
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    ended = False
    while True:
        data = stream.read(1024 * 1024)
        buffer += data
        while True:
            buffer = buffer.lstrip()
            if not started:
                if not buffer:
                    break
                if buffer[0] != "[":
                    raise ValueError("Expected a JSON array")
                started = True
                buffer = buffer[1:]
            buffer = buffer.lstrip(" \n\r\t,")
            if buffer.startswith("]"):
                ended = True
                break
            if not buffer:
                break
            try:
                value, offset = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if not data:
                    raise
                break
            buffer = buffer[offset:]
            yield value
        if ended:
            return
        if not data:
            raise ValueError("Truncated JSON array")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mpcorb", type=Path)
    p.add_argument("deep_catalog", type=Path)
    p.add_argument("output_dir", type=Path)
    a = p.parse_args()
    targets = {r["MPC"]: r for r in csv.DictReader(a.deep_catalog.open())}
    selected, mapping = [], {}
    total = 0
    with a.mpcorb.open() as f:
        for row in objects(f):
            total += 1
            names = {row.get("Principal_desig"), *row.get("Other_desigs", [])}
            matches = names & targets.keys()
            if matches:
                if len(matches) != 1:
                    raise ValueError(f"Multiple target names for one orbit: {matches}")
                name = matches.pop()
                if name in mapping:
                    raise ValueError(f"Multiple MPC rows for {name}")
                selected.append(row)
                mapping[name] = row["Principal_desig"]
    a.output_dir.mkdir(parents=True, exist_ok=True)
    (a.output_dir / "deep_mpcorb.json").write_text(json.dumps(selected, indent=2) + "\n")
    with (a.output_dir / "deep_physical.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=["ObjID", "H_VR"])
        writer.writeheader()
        for name, ident in mapping.items():
            writer.writerow(dict(ObjID=ident, H_VR=float(targets[name]["VRMAG"])))
    with a.mpcorb.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    manifest = dict(
        source=str(a.mpcorb.resolve()),
        source_sha256=digest,
        source_rows=total,
        selected_rows=len(selected),
        designation_mapping=mapping,
        missing=sorted(targets.keys() - mapping.keys()),
        photometry_source=str(a.deep_catalog.resolve()),
        photometry_sha256=hashlib.sha256(a.deep_catalog.read_bytes()).hexdigest(),
        photometry="DEEP VI supplied VRMAG column -> measured H_VR; no Rubin colour conversion",
    )
    (a.output_dir / "catalog_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: v for k, v in manifest.items() if k != "designation_mapping"}, indent=2))


if __name__ == "__main__":
    main()
