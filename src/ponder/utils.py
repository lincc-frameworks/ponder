import gzip
import hashlib
import re
import sqlite3

import pandas as pd
import requests
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

MPCORB_URL = "https://www.minorplanetcenter.net/Extended_Files/mpcorb_extended.json.gz"
MPCCOM_URL = "https://www.minorplanetcenter.net/Extended_Files/allcometels.json.gz"
MPCORB_SEMIMAJOR_AXIS_LIMIT_AU = 30.0
MPCORB_UNCERTAINTY_CODE_LIMIT = 6
MPCORB_OBSERVATION_ARC_LIMIT_DAYS = 3.0


# -- comet diff helpers --


def obj_id(comet):
    return (
        comet.get("Provisional_packed_desig")
        or comet.get("Designation_and_name")
        or comet.get("Principal_desig")
    )


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


# -- catalog filters --


def _is_missing(value):
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() == "nan"
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _float_or_none(value):
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_arc_years(obj):
    arc_years = obj.get("Arc_years")
    if not _is_missing(arc_years):
        return True

    arc_length = obj.get("Arc_length")
    if _is_missing(arc_length):
        return False

    # MPCORB sometimes records long arcs as a year range in Arc_length instead
    # of populating Arc_years; treat that as a long-enough observation history.
    return bool(re.fullmatch(r"\d{4}\s*-\s*\d{4}", str(arc_length).strip()))


def _arc_length_days(obj):
    arc_length = obj.get("Arc_length")
    if _is_missing(arc_length):
        return None

    if isinstance(arc_length, str):
        # Accept values like "2 days" while still rejecting year ranges, which
        # are handled by _has_arc_years before day-length filtering.
        match = re.search(r"\d+(?:\.\d+)?", arc_length)
        if not match:
            return None
        return float(match.group(0))

    return _float_or_none(arc_length)


def mpcorb_observation_arc_at_least(obj, minimum_days=MPCORB_OBSERVATION_ARC_LIMIT_DAYS):
    if _has_arc_years(obj):
        return True

    days = _arc_length_days(obj)
    return days is not None and days >= minimum_days


def keep_mpcorb_object(obj):
    # Keep objects that are either distant, well
    # constrained, or observed over enough time to be worth a Sorcha run.
    semimajor_axis = _float_or_none(obj.get("a"))
    if semimajor_axis is not None and semimajor_axis > MPCORB_SEMIMAJOR_AXIS_LIMIT_AU:
        return True

    uncertainty_code = _float_or_none(obj.get("U"))
    if uncertainty_code is not None and uncertainty_code <= MPCORB_UNCERTAINTY_CODE_LIMIT:
        return True

    return mpcorb_observation_arc_at_least(obj)


def filter_orbit_objects(objects, comet=False):
    if comet:
        return list(objects)
    return [obj for obj in objects if keep_mpcorb_object(obj)]


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
    ].copy()

    phys = df[
        [
            "Principal_desig",
            "H",
        ]
    ].copy()

    orbs["Epoch"] = orbs["Epoch"] - 2400000.5
    orbs["FORMAT"] = "KEP"
    phys["g-r"] = 0.5

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
    try:
        df = pd.read_sql_query(
            "SELECT * FROM observations WHERE observationStartMJD > ?",
            src,
            params=(last_mjd,),
        )
        num_rows = len(df)
        print(num_rows, " new pointings")
        df.to_sql("observations", dst, index=False, if_exists="replace")
    finally:
        src.close()
        dst.close()
    return num_rows


def invalid_observation_rows(db_path, sample_limit=5):
    con = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(observations)").fetchall()}
        required = {"observationId", "fieldRA", "fieldDec"}
        missing = sorted(required - columns)
        if missing:
            raise ValueError(f"observations table is missing required columns: {missing}")

        df = pd.read_sql_query("SELECT observationId, fieldRA, fieldDec FROM observations", con)
    finally:
        con.close()

    ra = pd.to_numeric(df["fieldRA"], errors="coerce")
    dec = pd.to_numeric(df["fieldDec"], errors="coerce")
    invalid = df.loc[
        ra.isna() | dec.isna() | (ra < 0.0) | (ra >= 360.0) | (dec < -90.0) | (dec > 90.0)
    ]
    return len(df), invalid.head(sample_limit)


def validate_observations_db(db_path):
    total_rows, invalid_sample = invalid_observation_rows(db_path)
    if len(invalid_sample) == 0:
        return total_rows

    sample = invalid_sample.to_dict(orient="records")
    raise ValueError(
        f"Invalid pointing DB {db_path}: found rows with missing or out-of-range fieldRA/fieldDec; "
        f"sample={sample}"
    )


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


def get_current_orbits(date_str, work_dir, comet=False):
    file_type = "comet" if comet else "asteroid"
    orbits_gz_path = work_dir / f"{file_type}_orbits_{date_str}.json.gz"
    orbits_uc_path = work_dir / f"{file_type}_orbits_{date_str}.json"
    fetch_url = MPCCOM_URL if comet else MPCORB_URL
    print(f"Fetching orbits from {fetch_url}...")
    r = requests.get(fetch_url)
    if r.status_code != 200:
        raise Exception(f"Failed to fetch orbits: {r.status_code}")
    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing orbits to {orbits_gz_path}...")
    with open(orbits_gz_path, "wb") as f:
        f.write(r.content)
    print(f"Orbits saved to {orbits_gz_path}")

    with gzip.open(orbits_gz_path, "rt") as f_in:
        with open(orbits_uc_path, "w") as f_out:
            f_out.write(f_in.read())
    print(f"Uncompressed orbits saved to {orbits_uc_path}")

    return orbits_uc_path
