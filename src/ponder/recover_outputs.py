"""Finish assembly/audit of a saved run without re-running orbital predictions."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path

import pandas as pd

from . import runner


def completed_leaves(manifest_path):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    root = manifest_path.parent
    candidates = []
    for marker_path in root.rglob('*.done'):
        marker = json.loads(marker_path.read_text())
        if marker.get('status') != 'done':
            raise ValueError(f'Invalid completion marker: {marker_path}')
        output = Path(marker['output_path'])
        if output.resolve() != marker_path.with_suffix('.csv').resolve():
            raise ValueError(f'Completion marker path mismatch: {marker_path}')
        chunk = runner.SorchaChunk(index=marker['chunk'], row_start=marker['row_start'],
                                  row_end=marker['row_end'], output_path=output,
                                  orbits_path=Path(marker['orbits_path']),
                                  physparams_path=Path(marker['physparams_path']))
        if not runner.chunk_is_complete(chunk):
            raise ValueError(f'Completed chunk has missing output: {output}')
        # A zero-output marker must not silently admit stale files.
        for key, path in [('output_exists', chunk.output_path), ('ew_output_exists', chunk.ew_output_path)]:
            if not marker.get(key, True) and path.exists():
                raise ValueError(f'Unexpected output for zero-output marker: {path}')
        candidates.append(chunk)
    # Prefer a completed parent to any nested debugging artifacts. Crossing
    # ranges and incomplete coverage are errors, never silently dropped rows.
    selected = []
    end = 0
    for chunk in sorted(candidates, key=lambda c: (c.row_start, -c.row_end)):
        if selected and selected[-1].row_start <= chunk.row_start and chunk.row_end <= end:
            continue
        if chunk.row_start != end or chunk.row_end <= chunk.row_start:
            raise ValueError(f'Gap/overlap in catalog coverage at {end}: {chunk.output_path}')
        selected.append(chunk)
        end = chunk.row_end
    if end != manifest['row_count']:
        raise ValueError(f'Incomplete catalog coverage: {end}/{manifest["row_count"]}')
    return manifest, selected


def _recover(manifest_path, apply=False):
    manifest_path = Path(manifest_path).resolve()
    manifest, chunks = completed_leaves(manifest_path)
    final_output = manifest_path.parent / (manifest_path.parent.name.rsplit('_', 1)[0] + '.parquet')
    fingerprint = hashlib.sha256(manifest_path.read_bytes())
    for chunk in chunks:
        for path in (chunk.done_path, chunk.output_path, chunk.ew_output_path):
            fingerprint.update(str(path).encode())
            if path.exists():
                stat = path.stat()
                fingerprint.update(f'{stat.st_size}:{stat.st_mtime_ns}'.encode())
    source_fingerprint = fingerprint.hexdigest()
    report = dict(source_fingerprint=source_fingerprint, catalog_rows=manifest['row_count'], completed_leaves=len(chunks),
                  manifest=str(manifest_path), output=str(final_output), status='validated')
    print(json.dumps(report), flush=True)
    if not apply:
        return report
    # Clear a previous audit before replacing either output. Consumers must not
    # mistake an interrupted two-file rebuild for an audited pair.
    audit_file = manifest_path.parent / 'output_audit.csv'
    receipt = manifest_path.parent / 'recovery_receipt.json'
    if receipt.exists() and audit_file.exists():
        saved = json.loads(receipt.read_text())
        audits = pd.read_csv(audit_file)
        outputs = [final_output, final_output.with_name(final_output.stem + '_ew.parquet')]
        output_stats = {str(p): [p.stat().st_size, p.stat().st_mtime_ns] for p in outputs if p.exists()}
        if (saved.get('source_fingerprint') == source_fingerprint and saved.get('output_stats') == output_stats
                and len(audits) == 2 and audits.status.isin(['ok', 'skipped_no_key_columns']).all()):
            runner.promote_combined_outputs(final_output, results_dir=manifest_path.parent.parent.parent)
            return saved
    for path in (final_output, final_output.with_name(final_output.stem + '_ew.parquet')):
        visible = manifest_path.parent.parent.parent / path.name
        if visible.exists():
            raise ValueError(f'Visible output already exists; refusing replacement: {visible}')
    audit_file.unlink(missing_ok=True)
    runner.combine_chunk_outputs(chunks, final_output, manifest.get('object_mode'))
    audit_path, missing_path, discrepancies = runner.audit_combined_outputs(
        chunks, final_output, pd.DataFrame(), object_mode=manifest.get('object_mode'))
    if discrepancies:
        raise ValueError(f'Output audit failed: {audit_path}; differences: {missing_path}')
    report.update(status='audited', audit=str(audit_path), output_stats={str(p): [p.stat().st_size, p.stat().st_mtime_ns] for p in (final_output, final_output.with_name(final_output.stem + '_ew.parquet'))})
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(receipt)
    runner.promote_combined_outputs(final_output, results_dir=manifest_path.parent.parent.parent)
    return report


def recover(manifest_path, apply=False):
    if not apply:
        return _recover(manifest_path)
    with Path(manifest_path).with_name('recovery.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _recover(manifest_path, apply=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(recover(args.manifest, args.apply), indent=2))


if __name__ == '__main__':
    main()
