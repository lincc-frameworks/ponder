"""Run Sorcha with a per-object fallback for its linker night-boundary check."""

from __future__ import annotations

import sys

import numpy as np


def adjusted_night_start(times, configured_start):
    """Return a valid boundary for one object, or ``None`` if none exists."""
    values = np.asarray(times, dtype=float)
    phased = (values - configured_start) % 1.0
    if np.all((0.1 < phased) & (phased < 0.9)):
        return configured_start

    fractions = np.sort(values % 1.0)
    if len(fractions) == 0:
        return configured_start
    gaps = np.diff(np.r_[fractions, fractions[0] + 1.0])
    gap_index = int(np.argmax(gaps))
    if gaps[gap_index] <= 0.2:
        return None
    return float((fractions[gap_index] + gaps[gap_index] / 2.0) % 1.0)


def install_linker_boundary_fallback():
    """Patch only objects that violate Sorcha's configured boundary assertion."""
    from sorcha.modules import PPMiniDifi

    original = PPMiniDifi.linkObject

    def link_object(obsv, seed, **config):
        configured = config["night_start_utc_days"]
        adjusted = adjusted_night_start(obsv["midPointTai"], configured)
        if adjusted is None:
            return original(obsv, seed, **config)
        if adjusted != configured:
            config = dict(config)
            config["night_start_utc_days"] = adjusted
            print(
                "Ponder Sorcha wrapper adjusted linker night boundary for "
                f"one object: {configured * 24.0:.3f} -> {adjusted * 24.0:.3f} UTC",
                file=sys.stderr,
                flush=True,
            )
        return original(obsv, seed, **config)

    PPMiniDifi.linkObject = link_object


def main():
    install_linker_boundary_fallback()
    from sorcha_cmdline.run import main as sorcha_main

    return sorcha_main()


if __name__ == "__main__":
    raise SystemExit(main())
