# LSSTCam comet comparison without a bright limit

This comparison uses 306 LSSTCam observing nights from 2025-04-15 through
2026-07-14. Ponder detections come from the controlled full-catalog rerun with
the Sorcha `[SATURATION] bright_limit` omitted only for comet mode. SkyBot and
JPL records come from the RCC nightly products under the same observing-night
window.

## Service totals

| Service | Objects | Object-nights | Object-visits | Source comet rows |
| --- | ---: | ---: | ---: | ---: |
| SkyBot | 1,332 | 15,999 | 36,713 | 40,773 |
| JPL | 480 | 7,967 | 26,794 | 40,140 |
| Ponder | 200 | 2,530 | 13,016 | 13,016 |

Ponder, SkyBot, and JPL all contain both `99P/Kowal` and
`C/2020 U4 (PANSTARRS)`. In particular, the no-limit Ponder run retains all 36
predicted 99P detections, whose modeled magnitudes are about 11.99--12.63.

Twenty identities appear only in Ponder at object level. Their detailed counts
are in `ponder_only_comets.csv`, and one highest-margin position per object is
in `ponder_only_cutout_input.parquet`.

Representative cutouts were written to
`/sdf/scratch/rubin/kbmod/users/colinc/rcc/ponder_only_comets_no_bright_limit_20260824`.
The job reports 30 successful generation operations and 10 failed attempts;
the directory contains 15 PNG and 16 FITS-FZ image files for 15 of the 20
object identities. The failures comprise two missing-WCS cases, two missing
visit-image cases, four detector-match failures, and two known-bad inputs.

## Important linking caveat

The no-limit result is a controlled rerun against the same mixed pointing
database and the same comet catalog as the earlier bright-limited run. It has
14,202 final detections from 202 objects over the full database, versus 24,116
detections from 967 objects previously. Relative to that earlier final output,
37 objects are gained and 802 are lost.

This is not evidence that removing saturation made those 802 objects
undetectable. Sorcha applies stochastic fading and SSP discovery linking after
photometric filtering. Adding the saturated observations therefore changes the
input histories and deterministic random seeds used by those downstream stages,
so their final linked-object output is not monotonic. The ephemeris product is
unchanged at 39,584 rows. For a monotonic known-comet detectability comparison,
the next analysis should use a pre-link detection product or bypass discovery
linking for comet mode.

The no-limit result and its combined asteroid+comet parquet remain isolated
from the RCC published nightly tree pending that workflow decision.
