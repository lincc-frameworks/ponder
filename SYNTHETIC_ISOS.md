# Synthetic ISO injections from the Ōtautahi–Oxford model

This opt-in Ponder extension generates synthetic orbit/photometry inputs and
unthresholded per-visit truth using Sorcha. It does not paint images or run KBMOD.
Existing MPC catalog ingestion, known-object filters, and incremental state stay
on their existing path.

The first implementation is a **Ponder fork using unmodified Sorcha**. Sorcha
already accepts hyperbolic barycentric Cartesian states. A Sorcha source fork is
not needed to bypass its detection filters: this command calls the dynamics and
photometry APIs before those filters.

## Data flow

```
external OO-model + seed + time window + encounter radius
    -> unrotated kinematic draws + explicit fixed-H scenario
    -> Sorcha BCART + physical parameters + model provenance
    -> optional, explicitly described object subset
    -> all object/visit pairs through Sorcha ASSIST/light time + HG photometry
    -> truth.parquet (including off-field and arbitrarily faint objects)
    -> injection_candidates.parquet (field-circle selection only)
    -> downstream detector/WCS/mask join -> painting -> KBMOD recovery
```

## Install and commands

Use a dedicated experiment environment or a workspace wheel target. Do not
replace a shared production Ponder installation for this pilot. Python 3.12,
Sorcha 1.2.1, ASSIST 1.2.3, and REBOUND 4.6.0 were exercised locally. The shared
Shire Sorcha 1.1.1 path has not been qualified for this extension.

The external sampler remains a separate checkout, not a vendored dependency:

- Model: https://github.com/Otautahi-Oxford/OO-model
- Exercised revision: `688f5548682ab1ef46b8c515a00294c3e31c2c0d`
- Velocity distribution: Hopkins et al. (2025), https://ui.adsabs.harvard.edu/abs/2025AJ....169...78H/abstract
- Trajectory sampler: Dorsey et al. (2025), Appendix A, https://arxiv.org/abs/2502.16741
- Sorcha input convention: https://sorcha.readthedocs.io/en/stable/inputs.html

After installing this fork, `ponder-synthetic` and `python -m ponder.synthetic`
are equivalent. Every output directory must be new; failed attempts retain a
failed receipt and are never silently resumed or overwritten.

```bash
ponder-synthetic prepare \
  --model-repo /path/to/OO-model \
  --count 30000 --radius-au 10 \
  --start-utc 2025-05-23 --end-utc 2025-05-31 \
  --seed 20261104 --h-r 16.857608 \
  --output /path/to/new-population

# Optional bounded pilot: CSV with a unique ObjID column; ordering is preserved.
ponder-synthetic select \
  --bundle /path/to/new-population --ids /path/to/pilot_ids.csv \
  --reason 'Explicit geometric examples and off-field controls; not a complete survey screen' \
  --output /path/to/new-pilot-bundle

ponder-synthetic propagate \
  --bundle /path/to/new-pilot-bundle \
  --pointings /path/to/selected_visits.csv \
  --planets /path/to/linux_p1550p2650.440 \
  --asteroids /path/to/massive_asteroid_kernel \
  --output /path/to/new-truth
```

Without an asteroid kernel, replace `--asteroids ...` with the explicit
`--allow-planetary-only` pilot option. The receipt records this reduced force
model. The all-pairs backend defaults to a one-million-pair ceiling; increase
`--max-pairs` deliberately or select a bounded sample. It does not silently use
an unvalidated fast sky screen.

Required pointing CSV columns:

| Column | Contract |
|---|---|
| `observationId` | Unique exact visit ID, preserved as a string |
| `exp_midpt_mjd` | Measured exposure midpoint, **MJD TAI** |
| `fieldRA`, `fieldDec` | Boresight degrees |
| `band` | Rubin u/g/r/i/z/y |
| `visitExposureTime` | Positive exposure duration in seconds |

Additional visit metadata are retained. Nominal observation start is never used
to reconstruct a supplied measured midpoint. The initial site adapter uses Rubin
longitude −70.74941°, latitude −30.244628°, height 2650 m.

## Scientific contracts

**Kinematics and population identity.** Preserve the model's sampled directions,
perihelion times, and encounter distribution. Model Gaia `source_id` is a
velocity-table row, not a claimed parent star. The adapter temporarily sets the
legacy NumPy RNG and working directory, restoring both on failure; run sampling
in a single process, not concurrent threads. Object IDs derive from model content,
settings and seed rather than checkout location.

**Explicit sampler overrides.** The pinned sampler's import-time
`ISO_weights /= ISO_weights` both flattens weights and can fail on read-only pandas
arrays. In memory only, that exact legacy line is replaced by a copy before
initialization finishes. Then the selected table column (`ISO_weights` by default,
or `oneOverESF`) is normalized. The original `Bcdf` is inverted by bounded bisection
using the full uniform [0,1) interval, with a residual check. No model file is
modified. Input hashes, revision, weighting choice, import override and CDF
residual are recorded. These implementation findings do not establish that the
published paper used the faulty normalization.

**Frames and dynamics.** Evaluate the authors' central-SSB hyperbola with the
model's own gravitational parameter. Convert Astropy mean-ecliptic J2000 through
ICRS to Sorcha's ecliptic convention, then export BCART (AU, AU/day, MJD TDB).
The chosen epoch defaults to the survey start converted from UTC. Sorcha then
integrates this finite-epoch state with ASSIST; it is not an asymptotic-boundary
matching solution. BCART avoids negative-semimajor-axis Keplerian conversion
assumptions. The model-state solver is checked against Sorcha's independent
universal-variable converter, including nearly parabolic and outbound cases.

**Brightness.** `--h-r` is required. It supplies a fixed-H inactive-nucleus scenario,
with solar colors and HG GS=0.15 by default. It is not Matthew's luminosity function,
a coma model, a size distribution, or a discovery forecast. Use separate prepared
bundles for different H scenarios; do not edit a bundle in place. A successful
preparation exports the effective encounter volume and per-draw volume weights.
An actual expected count also needs a physical number density for that H cohort
and a complete selection function. A `select` subset preserves its parent draw
count and explicitly disclaims selection completeness.

**Truth before detection.** The backend directly calls Sorcha's
`generate_simulations`, `integrate_light_time`, `calculate_rates_and_geometry`,
and `PPCalculateApparentMagnitude`. It applies no faint-object cut, SNR cut,
fading, photometric scatter, linking requirement, or random footprint losses.
There is one truth row per supplied ObjID/visit, including off-field controls.

**Painting.** `ra`/`dec` in candidates are observed topocentric light-time sky
coordinates, never reflex-corrected coordinates. `mag` and `flux_nJy` are noiseless
**total flux**, even for a trailed source. Trailing must not be charged twice by
both reducing painted flux and spreading it spatially. Trail length and sky PA are
included; conversion to detector pixels and the painter's trail convention remain
downstream. `requires_detector_join=True` explicitly marks these files as
candidates, not detector-ready `source_injection` catalogs.

## Validation boundary and deployment

The local integration tests exercise the actual model and actual Sorcha dynamics
with DE440, not only mocks. Set these variables to include those tests:

```bash
PONDER_OO_MODEL=/path/to/OO-model \
PONDER_PLANETS_KERNEL=/path/to/linux_p1550p2650.440 \
python -m pytest tests/ponder/test_synthetic.py
```

The observer offset is presently Astropy GCRS plus ASSIST Earth. Before subpixel
production work, compare it against the standard Sorcha/SPICE X05 observer and
an independent ephemeris control. Pin the full planetary/asteroid kernel set and
qualify the exact Sorcha version used on Shire. A plotting-level smoke test does
not establish production astrometric accuracy.

Join candidate visits to actual detector polygons, WCS, PSFs, variance and masks.
Recompute SNR and expected recovery for each exact patch/window rather than using
field-wide counts. Keep all faint eligible observations in the injection catalog.
For difference images, late-stage painting does not test template self-subtraction;
use upstream image injection if that loss is part of the experiment.

Compare exact-trajectory controls, ordinary lines, distance-corrected lines, and
linked short blocks at controlled false-positive rates. Report separate input,
field-circle, detector-covered, usable-pixel, searched and recovered denominators.

## Reviewable local pilot

The accompanying task artifact sampled 30,000 unrotated encounters within 10 au
for 2025-05-23 through 2025-05-31. An explicit, incomplete midpoint selection chose
two examples near COSMOS and two off-field controls. Propagation over 113 visits
produced 452 truth rows and 226 circle-eligible candidate rows. Those are validation
sample counts, not a forecast of two real ISOs. No shared installation was changed,
no pixel images were painted, and no PR or public fork was published.
