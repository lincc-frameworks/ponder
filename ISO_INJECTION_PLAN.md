# Synthetic ISO input and truth-generation extension

Base: Ponder 7fbe9c6b30142ba38893872e37e96d87028b292c, local fork branch `iso-hopkins-injections`.

Implement a separate `ponder-synthetic` command, keeping MPC catalog/state behavior unchanged.

1. Sample the external, pinned Ōtautahi–Oxford model with explicit seed, survey interval,
   encounter radius and H_r. Record the supplied velocity-table weighting and bounded-CDF
   inversion overrides already used in our exploratory analysis. Preserve unrotated
   directions, encounter times, Gaia row identity and per-draw volume normalization.
2. Convert the authors' barycentric hyperbolic states to Sorcha BCART at a named TDB epoch,
   including the Astropy mean-ecliptic to Sorcha ecliptic convention conversion. Export
   separate orbit, physical-property, model-draw, and provenance files.
3. Evaluate all object/visit pairs through Sorcha's ASSIST setup and light-time functions.
   Preserve measured TAI midpoints, keep unfiltered truth, and mark field-circle eligibility.
   Use Sorcha's own apparent-magnitude calculation before noise/detection cuts. Output
   observed-coordinate injection candidates with exact IDs and flux, requiring a later
   detector/WCS/mask join. Keep this bounded brute-force validation backend separate from
   scalable HEALPix screening and the full Sorcha CLI.
4. Keep uncertainty explicit: inactive HG photometry only initially; no invented luminosity
   distribution or occurrence forecast. Require an explicit planetary-only option when
   no asteroid kernel is supplied. The observer adapter remains the documented Astropy
   Rubin site adapter until independently qualified against the production SPICE path.
5. Validate frame/unit conversion against Sorcha's independent universal solver, preserve
   faint objects and non-detections, test exact midpoint handling and object joins, and
   run a real seeded model-to-truth pilot against selected published DP2 visits.

No shared Shire installation changes, image painting, new PR, or public fork publication
is part of this first implementation. These are unnecessary for a locally reviewable fork.
