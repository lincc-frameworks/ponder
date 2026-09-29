# LSSTCam comet comparison: SkyBot, JPL, and Ponder

Generated 2026-08-24 from 306 LSSTCam nightly directories spanning 2025-04-15 through 2026-07-14. DP1/ComCam was checked separately.

## Answer

Ponder does find comets. The fresh full LSSTCam comet run produced 24,116 detections for 967 comet identities across the mixed pointing database; 21,736 detections and 932 identities fall in the 306-night LSSTCam comparison window. The historical zero-comet result was a publication/consolidation failure: asteroid and comet runs shared promoted filenames, so only the asteroid product reached RCC.

After normalizing comet designations (including `3I` versus `3I/ATLAS`), the LSSTCam object-level service counts are:

- SkyBot: 1,332 comet identities
- Ponder: 932 comet identities
- JPL: 480 comet identities
- union: 1,494 comet identities
- all three: 333 identities
- Ponder and at least one external service: 814 identities (87.3% of Ponder)
- Ponder only: 118 identities

The mutually exclusive object membership counts are 433 SkyBot only, 26 JPL only, 118 Ponder only, 103 SkyBot+JPL, 463 SkyBot+Ponder, 18 JPL+Ponder, and 333 in all three.

## Cases that motivated the audit

- `C/2020 U4` is present in all three services. Ponder has a detection at visit `2025072300330`; the old RCC Ponder result omitted it because the comet-mode output was never consolidated into the promoted product.
- `99P` is present in SkyBot and JPL but not in Ponder detections. Ponder did propagate it (36 ephemeris rows), but its simplified comet model predicted approximately `g=12.49` at the representative visit, brighter than the configured saturation limit of magnitude 16. This is a photometry/selection difference, not a consolidation loss.

## Comparison grains

The analysis compares presence at three grains and does not compare raw row counts as if they were equivalent:

- object: unique canonical comet identity
- object-night: identity and local observing night
- object-visit: identity and matched Rubin visit

Ponder visit matching uses the exposure midpoint (`observationStartMJD + visitExposureTime / 2 / 86400`) and matched all 21,736 selected detections. Detailed tables are in `object_membership.csv`, `object_night_membership.csv`, `object_visit_membership.csv`, and `membership_counts.csv`.

## Ponder-only validation cutouts

One highest-margin representative was selected for each of the 118 globally Ponder-only identities. RCC rendered 83 identities as PNG cutouts and retained 91 compressed FITS files; eight successful cutouts include two detector FITS files behind one rendered PNG. Thirty-five representatives could not be rendered: 17 lacked a usable requested dataset during lookup and 18 encountered later dataset, WCS, or overlap failures. RCC retained failure/skip artifacts for those cases.

Cutouts: `/sdf/scratch/rubin/kbmod/users/colinc/rcc/ponder_only_comets_20260824`

## DP1/ComCam

The fresh DP1/ComCam comet run produced zero detections. It produced 16 buffered ephemeris rows for two comets, but all were outside the 0.50-degree science footprint. This agrees with the earlier DP1 result.

## Publication audit

Asteroid and comet runs now use separate result roots. Their audited combined
LSSTCam product contains 56,992,854 asteroid detections and 24,116 comet
detections, with `object_mode` set on every row. All 257 Ponder nightly source
parquets and all 257 Ponder-bearing unified parquets retain `object_mode`; all
302 service-bearing LSSTCam nightly unified products are fresh. The other 45
unified nights have no Ponder source, so no Ponder run mode applies. The final
audit found no stale nightly outputs or lock files.

## Interpretation limits

SkyBot and JPL are field-query services, while Ponder is a simulated-detection pipeline. Ponder currently uses `r` and `g`, a magnitude-16 bright limit, no comet activity model, and no phase function. Service catalog refresh times can also differ, especially for newly designated objects. Therefore, the membership tables measure agreement in reported presence; they are not completeness or false-positive truth labels. The cutouts are the empirical follow-up for Ponder-only candidates.

## Source products

- Ponder comet detections: `/sdf/scratch/rubin/kbmod/ponder/comets/results/2026-08-24_job_new.parquet`
- Audited combined detections: `/sdf/scratch/rubin/kbmod/ponder/combined/results/2026-08-24_job_new.parquet`
- LSSTCam nightly source root: `/sdf/scratch/rubin/kbmod/users/colinc/rcc/data/nightly/lsstcam`
- Ponder pointing database: `/sdf/scratch/rubin/kbmod/ponder/pointing_dbs/pointings.sqlite`
- Ponder Sorcha configuration: `/sdf/scratch/rubin/kbmod/ponder/sorcha_ponder_no_linking_config.ini`
