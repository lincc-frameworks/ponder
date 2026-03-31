import hashlib
import sqlite3

import pandas as pd
from astropy.time import Time

# orbital element fields - a change in any of these triggers a full-history rerun
ORBIT_FIELDS = [
    "e",
    "Peri",
    "Node",
    "i",
    "Perihelion_dist",
    "Year_of_perihelion",
    "Month_of_perihelion",
    "Day_of_perihelion",
    "Epoch_year",
    "Epoch_month",
    "Epoch_day",
]


# -- comet diff helpers --


def obj_id(comet):
    return comet.get("Provisional_packed_desig") or comet.get("Designation_and_name")


def hash_orbit(comet):
    key = "|".join(str(comet.get(f, "")) for f in ORBIT_FIELDS)
    return hashlib.md5(key.encode()).hexdigest()


def diff_comets(comets, prev_hashes):
    """
    Given a list of comets and a dict of previous comet hashes, return lists of new, updated, and unchanged comets.

    Parameters:
    - comets: TODO: figure out what this is
    - prev_hashes: dict of previous comet hashes, keyed by comet ID
    """
    # TODO: is there a way to numpyify this to make it faster?
    new, updated, unchanged = [], [], []
    for c in comets:
        oid = obj_id(c)
        h = hash_orbit(c)
        if oid not in prev_hashes:
            new.append(oid)
        elif prev_hashes[oid] != h:
            updated.append(oid)
        else:
            unchanged.append(oid)
    return new, updated, unchanged


# -- comet -> sorcha format converters --


def comets_to_sorcha_inputs(comets_json, ids, orbits_out, physparams_out):
    df = pd.DataFrame(comets_json)

    # filter to requested ids
    id_col = df["Provisional_packed_desig"].where(
        df["Provisional_packed_desig"].notna(), df["Designation_and_name"]
    )
    df = df[id_col.isin(ids)].copy()

    # build epoch and perihelion MJD strings, dropping rows with missing epochs
    epoch_strings, tp_strings, bad = [], [], []
    for r, row in df.iterrows():
        if any(
            pd.isna(row.get(c))
            for c in [
                "Epoch_year",
                "Epoch_month",
                "Epoch_day",
                "Year_of_perihelion",
                "Month_of_perihelion",
                "Day_of_perihelion",
            ]
        ):
            print(f"  [warn] dropping bad epoch: {row.get('Designation_and_name')}")
            bad.append(r)
            continue

        epoch_strings.append(f"{int(row['Epoch_year'])}-{int(row['Epoch_month'])}-{int(row['Epoch_day'])}")

        # preserve fractional day for perihelion time precision
        tp_day_int = int(row["Day_of_perihelion"])
        tp_day_frac = row["Day_of_perihelion"] - tp_day_int
        tp_strings.append(
            (f"{int(row['Year_of_perihelion'])}-{int(row['Month_of_perihelion'])}-{tp_day_int}", tp_day_frac)
        )

    df = df.drop(bad).reset_index(drop=True)

    df["epochMJD_TDB"] = Time(epoch_strings).mjd
    df["t_p_MJD_TDB"] = Time([s for s, _ in tp_strings]).mjd + [f for _, f in tp_strings]
    df["FORMAT"] = "COM"

    # -- orbits file --
    orbs = df[
        [
            "Designation_and_name",
            "FORMAT",
            "Perihelion_dist",
            "e",
            "i",
            "Peri",
            "Node",
            "t_p_MJD_TDB",
            "epochMJD_TDB",
        ]
    ].rename(
        columns={
            "Designation_and_name": "ObjID",
            "Perihelion_dist": "q",
            "i": "inc",
            "Peri": "argPeri",
            "Node": "node",
        }
    )
    # orbs.to_csv(orbits_out, index=False)

    # -- physical parameters file --
    phys = pd.DataFrame(
        {
            "ObjID": orbs["ObjID"],
            "H_r": df["H"].values,
            "g-r": 0.5,
        }
    )
    # phys.to_csv(physparams_out, index=False)

    return orbs, phys


def db_max_mjd(db_path):
    con = sqlite3.connect(db_path)
    val = con.execute("SELECT MAX(observationStartMJD) FROM observations").fetchone()[0]
    con.close()
    return val or 0.0
