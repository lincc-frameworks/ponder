# DEEP geometry validation

This branch extends `codex/new-objects-only-update-mode` at
`7fbe9c6b30142ba38893872e37e96d87028b292c`. It retains the existing incremental
rules: `--update-mode new-objects` runs only IDs missing from the baseline;
updated orbits and new pointings alone do not trigger another propagation.

DEEP's lost Butler registry is not required. `ponder-deep-exporter` reads the
packed `joined.collection`, writes one SQLite pointing per visit, and preserves
the individual detector polygons and duplicate dataset aliases in separate tables.
It rejects inconsistent geometry, timing, or conflicting duplicate metadata.

## Timing and coverage

The audited dinob collections store FITS `DATE-AVG` as `mjd_start`, although
DATE-AVG is a **TAI midpoint**. Their `mjd_mid` adds `(exposureTime+1)/2` seconds.
The exporter explicitly requires `--time-semantics date-avg-tai`, verifies that
legacy relationship, and computes the true exposure start in TAI as
`mjd_start - exposureTime/172800`. It does not reinterpret TAI as UTC or use a
fixed UTC correction across the archive. This convention must be re-audited for
other collection producers. Recheck FITS headers across dates and exposure-duration variants before adopting this convention.

The 1.1-degree circle is only a candidate field cone. The `detector_footprints`
table must be checked before claiming detector coverage; masks are a further,
separate gate. Diagnostic visit-median seeing and depth do not establish sensitivity.
Rotation is unused for the circle; detector polygons retain the actual orientations.

## Install and export

The live integration test used Python 3.12 and Sorcha 1.1.1.

```bash
python -m pip install -e '.[deep]'
ponder-deep-exporter /path/to/joined.collection /path/to/deep_pointings.sqlite \
  --band VR --time-semantics date-avg-tai
python scripts/select_deep_catalog.py /path/to/frozen_mpcorb.json \
  /path/to/deepvi_110_tnos.csv /path/to/inputs
```

The selector retains source MPC rows unchanged and records aliases and hashes.
It maps the supplied DEEP VI `VRMAG` absolute-magnitude column to `H_VR`, keyed by
the MPC principal designation. Published barycentric elements must not be passed
as heliocentric `KEP` elements: this workflow uses the separate MPC orbit input.

## Run Ponder

Sorcha 1.1.1's ordinary CLI dispatch and pointing reader only accept Rubin surveys.
`configs/deep_geometry.ini` selects the explicit Ponder `deep_geometry` backend,
which calls Sorcha's `precompute_pointing_information` and `create_ephemeris`
ASSIST/REBOUND APIs at **W84** with **VR** labels. No third-party source is patched,
and VR is not relabeled as a Rubin filter. This backend is version guarded.

This is an **ephemeris-only geometry path**, not the ordinary Sorcha Rubin
postprocessing pipeline. It applies a deterministic sky circle to the generated
positions. It does not simulate photometric detections, fading, linking efficiency,
or an uncertainty cloud. Columns/files called "detections" by Ponder's existing
combiner are predicted opportunities in this mode. Measured H_VR is preserved in
the inputs but does not gate the geometry output. The `_ew` product retains the
generator's wider buffered cone; the normal product uses the 1.1-degree circle.

Prepare a **private** Sorcha kernel cache, with a meta-kernel that references that
cache. The example configuration explicitly pins `earth_620120_240827.bpc` and
`earth_200101_990827_predict.bpc`; both are in the
[NAIF historical archive](https://naif.jpl.nasa.gov/pub/naif/generic_kernels/pck/a_old_versions/).
The validation reused copies of existing Sorcha assets and records SHA-256 hashes.
Use a fresh run directory when changing physical inputs, configuration, or the
pointing catalogue if already-baselined objects must be recomputed. This is
especially important in new-objects-only mode, by design.

```bash
export PONDER_SORCHA_CACHE=/absolute/path/to/private/sorcha-cache
mkdir -p /path/to/new/run
cd /path/to/new/run
ponder --db /path/to/deep_pointings.sqlite \
  --orbits /path/to/inputs/deep_mpcorb.json \
  --physical-parameters /path/to/inputs/deep_physical.csv \
  --config /absolute/path/to/ponder/configs/deep_geometry.ini \
  --update-mode new-objects --no-filter-orbits \
  --chunk-size 200 --sorcha-workers 1 --sorcha-timeout 1800
```

Ponder's existing runner stores `work/`, `results/`, and incremental state relative
to the working directory; use a separate directory per experiment. The additional
`--physical-parameters` CSV replaces default Rubin H/colour inputs, aligns by ObjID,
rejects missing/duplicate IDs, records a snapshot, and enters the chunk-resume hash.
Omitting this option preserves the previous conversion and resume hash behavior.

## Validation boundary

Unit tests cover backend dispatch, physical-parameter alignment, timing, quoted
ECSV parsing, duplicate aliases, and export geometry. Live scientific validation
requires frozen orbit/pointing inputs, the pinned Sorcha runtime and kernels,
and an explicit comparison to an independent ephemeris source. Test an unchanged
second run to verify that the new-objects baseline processes zero new objects.

KnownObjsMatcher associations are a separate stage. Use image-validity masks and
an explicit positional tolerance; a fixed 1-arcsecond gate can reject displaced
but corroborated tracks. The KBMOD revision used in the prior investigation has
an inverted `match_on_obs_ratio` comparison, so use its minimum-observation API
and compute any fraction thresholds explicitly until that revision is corrected.
