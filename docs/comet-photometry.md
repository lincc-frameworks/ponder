# Comet magnitudes: validation and selection guidance

Ponder's default comet photometry uses MPC H/G and the Schleicher–Marcus
(Halley–Marcus, HM) dust phase function through sorcha-addons. The numerical
implementation has passed integration and independent model checks, but its
accuracy against observed comet photometry has not yet been established.
Predicted magnitudes should be treated as uncertain estimates.

## Consider retaining all comets regardless of predicted magnitude

For completeness-focused searches, archive searches, and cutout generation,
consider retaining **all geometrically eligible comets without any exclusion
based on predicted magnitude**, including missing/unknown magnitudes. Use the
predictions as annotations for inspection rather than as a guarantee that a
comet is too faint to be worth searching for. Retaining candidates increases
processing and storage costs and does not mean that they will be detectable.

MPC H/G values may be uncertain, missing, or inappropriate for an epoch or
heliocentric distance. Catalog fits may absorb phase and aperture effects.
Activity changes, outbursts, gas emission, and an inactive nucleus are not
modeled. Ponder currently treats catalog H as zero-phase H_r and applies fixed
Rubin colors; these are assumptions, not measured comet-specific band
transformations. A total coma magnitude also does not directly predict a
point-source detection or the surface brightness in a particular aperture.

There is no validated universal magnitude margin that makes a hard cutoff safe.
If a detection simulation needs selection cuts, retain a separate unfiltered
candidate baseline, document the cuts, and measure how the selected sample
changes under plausible photometric assumptions. Do not treat a model-based
rejection as an observational nondetection.

### Configure selection explicitly

For `mpc-hm`, Ponder disables the early faint-object cull with
`brute_force = True`; comet mode also bypasses the saturation cut. **Other Sorcha
detection filters still follow the supplied configuration.** `--comet` alone
does not retain every candidate, and `--no-filter-orbits` does not disable
photometric selection.

For a baseline without photometric rejection, the following settings mirror the
Sorcha 1.2.0 external-ephemeris integration check:

```ini
[EXPERT]
default_snr_cut = False
randomization_on = False
trailing_losses_on = False
vignetting_on = False
```

Edit the existing section rather than adding a duplicate. Remove `mag_limit`
and `snr_limit` from `[EXPERT]`; a large finite limit can still reject unknown
magnitudes. Omit the `[FADINGFUNCTION]` and `[LINKINGFILTER]` sections to disable
those filters in the tested version. The four settings above by themselves are
not a complete configuration or a switch that disables every selection effect.
Keep the input, simulation, and field-of-view configuration appropriate to the
search; disabling a camera footprint changes which geometrical candidates are
included.

Inspect the effective configuration recorded by `photometry.json`, and audit
object/epoch retention on a small run containing faint and missing-H cases.
Repeat this check after configuration or Sorcha upgrades. The integration test
in [test_comet_photometry.py](../tests/ponder/test_comet_photometry.py) exercises
retention of unknown H with selection disabled and rejection after a magnitude
cut is enabled. These settings do not prevent unrelated orbit, geometry, or
execution failures from removing rows.

## Initial model spot check: 2026-11-08

We checked 12 comet/epoch cases: eight saved Sorcha rows and four selected epochs
covering near-perihelion and strong forward-scattering geometries. This was a
small diagnostic sample, not a population-wide validation. The
[comparison CSV](validation/comet-photometry-20261108/comparison.csv),
[version and residual summary](validation/comet-photometry-20261108/summary.json),
and [selected original MPC records](validation/comet-photometry-20261108/catalog_records.json)
preserve the numerical findings and catalog provenance. Paths inside these
files identify the original analysis inputs; they are not portable dependencies.

The check used the pinned sorcha-addons commit
`7cfc0e9f173cc194379991f75d468c4cc482cd13`, Sorcha 1.2.0, sbpy 0.6.0, and Kete
1.1.0. Horizons queries used Rubin observer center `X05@399`, UTC converted from
saved TAI timestamps, and quantities 9, 19, 20, and 43 (magnitudes, distances,
and true phase angle).

| Check | Result |
|---|---|
| Same JPL parameters and geometry, phase correction disabled | All 12 match Horizons within its printed rounding; the 11 three-decimal outputs differ by at most 0.000429 mag. |
| Kete comet law with the same inputs | Agreement within 3.6e-15 mag for both JPL and MPC parameter sets. |
| Replay of eight saved Sorcha magnitudes | Agreement within 7.2e-15 mag. |
| Substitute Horizons geometry into those eight predictions | Magnitudes change by less than 0.001 mag. |
| HM correction at all 181 integer-degree phase angles | Agreement with Lowell's table within 0.000037 mag, explained by sbpy's five-significant-digit storage of phase factors. |

The high-phase C/2023 A3 Horizons output was rounded to an integer (`6.`); the
unrounded phase-free calculation is 5.742284. Kete received vectors constructed
from Horizons geometry: this checked its independent photometry implementation,
not its orbit propagation. Numerical precision in these comparisons is **not**
an estimate of physical magnitude accuracy.

### Catalog disagreements dominate some predictions

These examples use identical Horizons geometry and omit HM to isolate catalog
parameter differences. They retain each catalog's H normalization and are not
calibrated band-matched comparisons: Horizons T-mag is visual, whereas Ponder
treats MPC H as H_r.

| Comet | UTC date | MPC H / G | JPL M1 / K1 | MPC law, no HM (mag) | Horizons T-mag |
|---|---|---|---|---:|---:|
| P/2009 WX51 | 2026-05-25 | 1 / 4 (G assumed) | 20.6 / 10 | -0.043 | 19.557 |
| 38P | 2026-07-12 | 3.5 / 12 | 11.6 / 9.5 | 45.211 | 28.828 |
| C/2014 Q3 | 2025-06-01 | -0.5 / 20 | 8.8 / 22 | 73.980 | 45.317 |

The source record for P/2009 WX51 contains H = 1 and no G; Ponder flags its G = 4
fallback with `comet_G_assumed`. The 19.6-mag offset from JPL is already present
before the phase correction. C/2014 Q3's steep MPC G extrapolates to extremely
faint magnitudes at large heliocentric distance. The activity model has no
separate nucleus contribution to dominate when activity fades. Neither
catalog's prediction in this table is an observed measurement.

The forward-scattering term also behaves as intended numerically: C/2023 A3 on
2024-10-09, at phase 172.088 degrees, receives a -6.100-mag HM correction. This
does not establish that every comet at that angle brightens by that amount.
[Lowell describes HM](https://asteroid.lowell.edu/comet/dustphase/) as a
first-order dust correction; applying it to total broadband light assumes that
dust dominates.

## Repeat model and observational spot checks

Repeat spot checks before relying on a new catalog, photometry implementation,
dependency version, or selection configuration. Include a mix of well-observed
periodic and long-period comets, near and far from perihelion, moderate and high
phase angles, extreme H/G values, missing parameters, and candidates near any
proposed selection boundary.

1. **Match geometry and parameter conventions.** Use the same comet/apparition,
   observer location, epoch/time scale, heliocentric and observer distances, and
   phase definition. Record catalog versions and fitted parameters.
   [Horizons](https://ssd.jpl.nasa.gov/horizons/manual.html) and
   [Kete](https://caltech-ipac.github.io/kete/api/flux.html) use the standard total
   law `M1 + 5 log10(delta) + K1 log10(r_h)`. To compare with Ponder's
   `H + 5 log10(delta) + 2.5 G log10(r_h)`, use `M1 = H` and `K1 = 2.5 G`.
   First compare identical inputs with the phase term off, then separate the
   effects of differing catalogs, geometry, bands, and phase treatment.
2. **Check the phase correction separately.** Verify zero-phase normalization
   and the sign of `-2.5 log10(Phi_HM)` against the
   [published table](https://asteroid.lowell.edu/static/comet/dustphaseHM_table.txt).
   The standard Horizons total law lacks HM; raw T-mag need not equal a
   phase-corrected Ponder prediction. Record any special object-specific law.
3. **Compare with actual observations.** Use calibrated photometry with known
   times, filters, magnitude systems, apertures, uncertainties, and quality
   flags. Match total-coma, aperture, or nucleus measurements to the quantity
   being predicted; do not silently compare integrated coma flux with a PSF
   magnitude. Account for band transformations, seeing, trailing, saturation,
   outbursts, and gas contamination where relevant. Reserve independent epochs
   or objects for validation if observations are also used to fit H/G.
4. **Evaluate residuals and selection losses.** Examine predicted minus observed
   magnitudes against distance, phase angle, epoch, and filter. Include measured
   upper limits and inspect candidates that a proposed cutoff would reject,
   rather than checking only surviving detections. Quantify the effect of
   missing/assumed parameters and plausible alternative fits. Until justified
   by these checks, consider retaining the full geometrical candidate sample.
5. **Preserve the evidence.** Save source records, queries/responses, software
   versions, effective configuration, flags, observations and their provenance,
   comparison tables, and the adopted selection policy. A successful
   model-to-model comparison establishes numerical consistency; observational
   checks are needed to assess predictive accuracy.
