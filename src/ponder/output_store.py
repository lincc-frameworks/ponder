"""Disk-backed output assembly and exact multiplicity audits.

DuckDB spills sorting/grouping to a per-operation temporary directory. The
memory limit is deliberately below a typical Slurm allocation; input rows never
pass through a full pandas DataFrame.
"""
import csv
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow.parquet as pq

ID_COLUMNS = ('ObjID', 'objectId', 'objectID', 'ssObjectId', 'ssObjectID',
              'Principal_desig', 'Provisional_packed_desig', 'Designation_and_name')


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def columns(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    if path.suffix == '.parquet':
        return pq.ParquetFile(path).schema_arrow.names
    with path.open(newline='') as handle:
        return next(csv.reader(handle), [])


@contextmanager
def connection(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    spill_root = Path(os.environ.get('PONDER_SPILL_DIR', output.parent))
    spill_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ponder-output-', dir=spill_root) as scratch:
        con = duckdb.connect(str(Path(scratch) / 'output.duckdb'))
        try:
            con.execute('SET memory_limit = ' + literal(os.environ.get('PONDER_OUTPUT_MEMORY_LIMIT', '8GB')))
            con.execute('SET threads = ' + str(int(os.environ.get('PONDER_OUTPUT_THREADS', '2'))))
            con.execute('SET preserve_insertion_order = false')
            con.execute('SET temp_directory = ' + literal(Path(scratch) / 'spill'))
            yield con
        finally:
            con.close()


def relation(paths):
    """Keep IDs lexical (including leading zeros); infer all numeric CSV rows."""
    groups = {}
    for path in paths:
        path = Path(path)
        names = columns(path)
        if names:
            groups.setdefault((path.suffix == '.parquet', tuple(names)), []).append(str(path))
    sources = []
    for (parquet, names), files in groups.items():
        paths_sql = '[' + ','.join(literal(p) for p in files) + ']'
        if parquet:
            sources.append(f'SELECT * FROM read_parquet({paths_sql}, union_by_name=true)')
        else:
            types = '{' + ','.join(literal(n) + ":'VARCHAR'" for n in names if n in ID_COLUMNS) + '}'
            sources.append(f'SELECT * FROM read_csv({paths_sql}, header=true, union_by_name=true, '
                           f'sample_size=-1, types={types}, auto_type_candidates=[\'BIGINT\',\'DOUBLE\',\'VARCHAR\'])')
    return ' UNION ALL BY NAME '.join(sources) if sources else None


def combine(paths, output, object_mode=None):
    output = Path(output)
    print(f"Combining into {output} (disk-backed)", flush=True)
    query = relation(paths)
    with connection(output) as con:
        temporary = output.with_name(output.name + f'.{os.getpid()}.tmp')
        try:
            if query is None:
                frame = pd.DataFrame({'object_mode': pd.Series(dtype='string')}) if object_mode else pd.DataFrame()
                frame.to_parquet(temporary, index=False)
            else:
                con.execute('CREATE VIEW inputs AS ' + query)
                names = [row[0] for row in con.execute('DESCRIBE inputs').fetchall()]
                projection = '*'
                if object_mode:
                    if 'object_mode' in names:
                        invalid = con.execute('SELECT DISTINCT object_mode FROM inputs WHERE object_mode IS NOT NULL '
                                              'AND object_mode != ?', [object_mode]).fetchall()
                        if invalid:
                            raise ValueError(f'Conflicting object modes: {invalid}')
                        projection = '* EXCLUDE (object_mode)'
                    projection += ', ' + literal(object_mode) + ' AS object_mode'
                order = ' ORDER BY fieldMJD_TAI NULLS LAST' if 'fieldMJD_TAI' in names else ''
                con.execute(f'COPY (SELECT {projection} FROM inputs{order}) TO {literal(temporary)} '
                            "(FORMAT PARQUET, COMPRESSION SNAPPY, ROW_GROUP_SIZE 131072)")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)


def audit_counts(source_paths, combined_path, key_columns, timestamp_column):
    """Return totals and a bounded diagnostic sample; retain full differences on disk."""
    combined_path = Path(combined_path)
    print(f"Auditing {combined_path} (disk-backed)", flush=True)
    report = combined_path.with_name(combined_path.stem + '_pair_differences.parquet')
    with connection(combined_path) as con:
        expressions = []
        for name in key_columns:
            quoted = identifier(name)
            # CSV spelling 1, 1.0, and scientific notation denote the same time.
            value = f'CAST({quoted} AS DOUBLE)' if name == timestamp_column else f'CAST({quoted} AS VARCHAR)'
            expressions.append(f'{value} AS {quoted}')
        keys = ','.join(identifier(n) for n in key_columns)
        for label, paths in [('source', source_paths), ('combined', [combined_path])]:
            query = relation(paths)
            if query is None:
                empty = ','.join(f'CAST(NULL AS {"DOUBLE" if n == timestamp_column else "VARCHAR"}) AS {identifier(n)}' for n in key_columns)
                query = f'SELECT {empty} WHERE false'
            con.execute(f'CREATE TABLE {label}_counts AS SELECT {keys}, count(*) AS row_count FROM '
                        f'(SELECT {",".join(expressions)} FROM ({query})) GROUP BY {keys}')
        totals = {}
        for label in ('source', 'combined'):
            pairs, rows = con.execute(f'SELECT count(*), coalesce(sum(row_count),0) FROM {label}_counts').fetchone()
            totals[label + '_pairs'] = pairs
            totals[label + '_rows'] = int(rows)
        join = ' AND '.join(f's.{identifier(n)} IS NOT DISTINCT FROM c.{identifier(n)}' for n in key_columns)
        projection = ','.join(f'coalesce(s.{identifier(n)}, c.{identifier(n)}) AS {identifier(n)}' for n in key_columns)
        con.execute(f'CREATE TABLE differences AS SELECT {projection}, coalesce(s.row_count,0)::BIGINT AS source_count, '
                    f'coalesce(c.row_count,0)::BIGINT AS combined_count FROM source_counts s FULL OUTER JOIN combined_counts c '
                    f'ON {join} WHERE coalesce(s.row_count,0) != coalesce(c.row_count,0)')
        for label, predicate, delta in [('missing', 'source_count > combined_count', 'source_count-combined_count'),
                                        ('extra', 'combined_count > source_count', 'combined_count-source_count')]:
            pairs, rows = con.execute(f'SELECT count(*), coalesce(sum({delta}),0) FROM differences WHERE {predicate}').fetchone()
            totals[label + '_pairs'] = pairs
            totals[label + '_rows'] = int(rows)
        if totals['missing_pairs'] or totals['extra_pairs']:
            con.execute(f'COPY differences TO {literal(report)} (FORMAT PARQUET)')
            totals['difference_report'] = str(report)
        else:
            report.unlink(missing_ok=True)
        sample = con.execute('SELECT *, source_count-combined_count AS missing_count FROM differences '
                             'WHERE source_count > combined_count LIMIT 10000').fetchdf()
        totals['missing_sample_truncated'] = totals['missing_pairs'] > len(sample)
        return totals, sample
