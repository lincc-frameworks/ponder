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


def diff_objects(objects, prev_hashes):
    """
    Given a list of objects and a dict of previous comet hashes, return lists of new, updated, and unchanged comets.

    Parameters:
    - objects: TODO: figure out what this is
    - prev_hashes: dict of previous comet hashes, keyed by comet ID
    """
    # TODO: is there a way to numpyify this to make it faster?
    new, updated, unchanged = [], [], []
    for c in objects:
        oid = obj_id(c)
        h = hash_orbit(c)
        if oid not in prev_hashes:
            new.append(oid)
        elif prev_hashes[oid] != h:
            updated.append(oid)
        else:
            unchanged.append(oid)
    return new, updated, unchanged


def mpcorb_to_sorcha_inputs(mpcorb_json, ids):
    df = pd.DataFrame(mpcorb_json)

    id_col = df["Principal_desig"]

    df = df[id_col.isin(ids)].copy()

    orbs = df[
        [
            "Principal_desig",
            "a",
            "e",
            "i",
            "Node",
            "Peri",
            "M",
            "Epoch",
        ]
    ]

    phys = df[
        [
            "Principal_desig",
            "H",
        ]
    ]

    orbs["Epoch"] = orbs["Epoch"] - 2400000.5

    orbs.rename(
        columns={
            "Principal_desig": "ObjID",
            "i": "inc",
            "Node": "node",
            "Peri": "argPeri",
            "M": "ma",
            "Epoch": "epochMJD_TDB",
        },
        inplace=True,
    )

    phys.rename(
        columns={
            "Principal_desig": "ObjID",
            "H": "H_r",
        },
        inplace=True,
    )

    return orbs, phys


# -- comet -> sorcha format converters --


def comets_to_sorcha_inputs(comets_json, ids):
    # TODO: potentially refactor so that it only does the conversion once
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
            # print(f"  [warn] dropping bad epoch: {row.get('Designation_and_name')}")
            bad.append(r)
            continue

        epoch_strings.append(f"{int(row['Epoch_year'])}-{int(row['Epoch_month'])}-{int(row['Epoch_day'])}")

        # preserve fractional day for perihelion time precision
        tp_day_int = int(row["Day_of_perihelion"])
        tp_day_frac = row["Day_of_perihelion"] - tp_day_int
        year_str = f"{int(row['Year_of_perihelion']):04d}"
        tp_strings.append((f"{year_str}-{int(row['Month_of_perihelion'])}-{tp_day_int}", tp_day_frac))

    df = df.drop(bad).reset_index(drop=True)

    df["epochMJD_TDB"] = Time(epoch_strings).mjd
    df["t_p_MJD_TDB"] = Time([s for s, _ in tp_strings], format="iso").mjd + [f for _, f in tp_strings]
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


# -- DB helpers --


def extract_new_pointings(db_path, last_mjd, out_path):
    if out_path.exists():
        out_path.unlink()
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(out_path)
    # sql = src.execute(
    #     "SELECT sql FROM sqlite_master WHERE type='table' AND name='observations'"
    # )
    # print(sql.fetchone())
    # dst.execute(sql.fetchone[0])
    df = pd.read_sql_query(f"SELECT * FROM observations WHERE observationStartMJD > {last_mjd}", src)
    num_rows = len(df)
    print(num_rows, " new pointings")
    if num_rows > 0:
        df.to_sql("observations", dst, index=False)
    src.close()
    dst.close()
    return num_rows


def db_max_mjd(db_path):
    con = sqlite3.connect(db_path)
    val = con.execute("SELECT MAX(observationStartMJD) FROM observations").fetchone()[0]
    con.close()
    return val or 0.0


def db_count(db_path):
    con = sqlite3.connect(db_path)
    n = con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    con.close()
    return n
