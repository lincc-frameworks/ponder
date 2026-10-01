import json
from pathlib import Path

import pandas as pd
import pytest
import pyarrow.parquet as pq

from ponder import output_store, runner
from ponder.recover_outputs import completed_leaves, recover


def test_disk_combine_preserves_ids_states_order_and_nulls(tmp_path, monkeypatch):
    monkeypatch.setenv('PONDER_OUTPUT_MEMORY_LIMIT', '128MB')
    a, b, out = [tmp_path / n for n in ('a.csv', 'b.csv', 'out.parquet')]
    a.write_text('ObjID,fieldMJD_TAI,Obj_Sun_x_LTC_km,FieldID\n001,3,1.25,9007199254740993\nA,1,,2\n')
    b.write_text('ObjID,fieldMJD_TAI,Obj_Sun_x_LTC_km,FieldID\nC,2,-7.5,3\n')
    output_store.combine([a, b], out, 'asteroid')
    result = pd.read_parquet(out)
    assert result.ObjID.tolist() == ['A', 'C', '001']
    assert result.FieldID.tolist() == [2, 3, 9007199254740993]
    assert pd.isna(result.Obj_Sun_x_LTC_km.iloc[0])
    assert result.Obj_Sun_x_LTC_km.iloc[1:].tolist() == [-7.5, 1.25]
    totals, missing = output_store.audit_counts([a, b], out, ['ObjID', 'fieldMJD_TAI'], 'fieldMJD_TAI')
    assert totals['source_rows'] == totals['combined_rows'] == 3
    assert totals['missing_rows'] == totals['extra_rows'] == 0
    assert missing.empty


def test_atomic_combine_and_mode_conflict(tmp_path):
    source, out = tmp_path / 'a.csv', tmp_path / 'out.parquet'
    source.write_text('ObjID,object_mode\nA,comet\n')
    out.write_bytes(b'previous output')
    with pytest.raises(ValueError, match='Conflicting'):
        output_store.combine([source], out, 'asteroid')
    assert out.read_bytes() == b'previous output'
    assert not list(tmp_path.glob('*.tmp'))


def test_audit_detects_extra_rows_and_null_keys(tmp_path):
    a, out = tmp_path / 'a.csv', tmp_path / 'out.parquet'
    a.write_text('ObjID,fieldMJD_TAI\n001,1.0\nA,\n')
    pd.DataFrame({'ObjID':['001','A','001'], 'fieldMJD_TAI':[1.,None,1.]}).to_parquet(out)
    summary, missing = runner.audit_output_pairs([a], out, 'detections', pd.DataFrame())
    assert summary['status'] == 'extra'
    assert summary['extra_rows'] == 1
    assert summary['missing_rows'] == 0
    assert missing.empty
    assert pq.ParquetFile(summary['difference_report']).metadata.num_rows == 1


def test_empty_and_parquet_inputs(tmp_path):
    out = tmp_path / 'empty.parquet'
    output_store.combine([tmp_path / 'missing.csv'], out, 'comet')
    assert pd.read_parquet(out).empty
    source = tmp_path / 'input.parquet'
    pd.DataFrame({'ObjID':['01'], 'fieldMJD_TAI':[2.]}).to_parquet(source)
    output_store.combine([source, out], tmp_path / 'combined.parquet', 'comet')
    assert pd.read_parquet(tmp_path / 'combined.parquet').ObjID.tolist() == ['01']


def test_resume_uses_full_digest_and_prior_date(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'WORK_DIR', tmp_path / 'work')
    monkeypatch.setattr(runner, 'RESULTS_DIR', tmp_path / 'results')
    digest = 'abcdef1234567890'
    original = runner.plan_sorcha_chunks('new', '2026-09-27', 4, 2, digest)
    root = original[0].output_path.parent
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps(dict(job_name='new', row_count=4, chunk_size=2, digest=digest)))
    retry = runner.plan_sorcha_chunks('new', '2026-10-01', 4, 2, digest)
    assert retry == original
    changed = runner.plan_sorcha_chunks('new', '2026-10-01', 4, 2, 'abcdef123456different')
    assert changed[0].output_path.parent != root


def test_recovery_rejects_gaps_and_accepts_zero_output(tmp_path):
    root = tmp_path / 'results/chunk_runs/2026-09-27_job_new_abcdef123456'
    root.mkdir(parents=True)
    manifest = root / 'manifest.json'
    manifest.write_text(json.dumps(dict(row_count=2, object_mode='asteroid')))
    output = root / 'chunk.csv'
    marker = dict(status='done', chunk=0, row_start=0, row_end=1, output_path=str(output),
                  orbits_path='orbits.csv', physparams_path='phys.csv', output_exists=False, ew_output_exists=False)
    output.with_suffix('.done').write_text(json.dumps(marker))
    with pytest.raises(ValueError, match='Incomplete'):
        completed_leaves(manifest)
    marker['row_end'] = 2
    output.with_suffix('.done').write_text(json.dumps(marker))
    report = recover(manifest, apply=True)
    assert report['status'] == 'audited'
    assert pq.ParquetFile(report['output']).metadata.num_rows == 0
    assert recover(manifest, apply=True) == report


def test_audit_bounds_failure_sample_but_retains_full_report(tmp_path):
    source = tmp_path / 'source.parquet'
    out = tmp_path / 'out.parquet'
    pd.DataFrame({'ObjID':[str(i) for i in range(10002)]}).to_parquet(source)
    pd.DataFrame({'ObjID':pd.Series(dtype=str)}).to_parquet(out)
    summary, sample = output_store.audit_counts([source], out, ['ObjID'], '')
    assert summary['missing_rows'] == 10002
    assert summary['missing_sample_truncated']
    assert len(sample) == 10000
    assert pq.ParquetFile(summary['difference_report']).metadata.num_rows == 10002
