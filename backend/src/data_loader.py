"""Data loading and cleaning.

Pipeline stages 1 and 2:

    Data Loading  ->  Data Cleaning  ->  (graph.py) Graph Construction

The ``data/`` folder contains four CSV files (file names carry suffixes such as
``" (1)"``, so they are located with glob patterns rather than hard-coded):

* ``societies*.csv``            - housing societies (customers), with coordinates
* ``restaurants*.csv``          - restaurants, with coordinates and metadata
* ``society_connections*.csv``  - roads between societies (distance + time)
* ``society_restaurants*.csv``  - roads between a society and its nearest
                                  restaurants, with separate forward
                                  (society -> restaurant) and return
                                  (restaurant -> society) distance/time
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field

import pandas as pd


class DataError(Exception):
    """Raised when the dataset is missing, empty or malformed."""


# Logical table name -> (file glob pattern, required columns)
DATASET_SPEC: dict[str, tuple[str, list[str]]] = {
    "societies": (
        "societies*.csv",
        ["Society_ID", "Society_Name", "Latitude", "Longitude"],
    ),
    "restaurants": (
        "restaurants*.csv",
        ["Restaurant_ID", "Restaurant_Name", "Latitude", "Longitude"],
    ),
    "connections": (
        "society_connections*.csv",
        ["Society_A", "Society_B", "Distance_km", "Travel_Time_min"],
    ),
    "society_restaurants": (
        "society_restaurants*.csv",
        [
            "Society_ID", "Restaurant_ID",
            "Distance_km", "Travel_Time_min",
            "Return_Distance_km", "Return_Travel_Time_min",
        ],
    ),
}

# Optional descriptive columns: filled with "" when absent.
OPTIONAL_TEXT_COLUMNS = ["Address", "Area", "Cuisine", "Phone"]


@dataclass
class Dataset:
    """Cleaned tables, ready for graph construction."""

    societies: pd.DataFrame
    restaurants: pd.DataFrame
    connections: pd.DataFrame
    society_restaurants: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def _find_file(data_dir: str, pattern: str, warnings: list[str]) -> str:
    """Return the CSV in ``data_dir`` matching ``pattern``."""
    matches = sorted(glob.glob(os.path.join(data_dir, pattern)))
    if not matches:
        raise DataError(
            f"No file matching '{pattern}' found in '{data_dir}'."
        )
    if len(matches) > 1:
        raise DataError(f"Several files match '{pattern}' in '{data_dir}'; "
                        "remove the duplicate to avoid ambiguous imports.")
    return matches[0]


def _read_csv(path: str, required: list[str]) -> pd.DataFrame:
    """Read a CSV file and verify it is non-empty and has required columns."""
    name = os.path.basename(path)
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except pd.errors.EmptyDataError as exc:
        raise DataError(f"'{name}' is empty.") from exc
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise DataError(f"'{name}' could not be parsed: {exc}") from exc

    df.columns = [str(col).strip() for col in df.columns]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise DataError(
            f"'{name}' is missing required column(s): {', '.join(missing)}"
        )
    if df.empty:
        raise DataError(f"'{name}' has a header but no data rows.")
    return df


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

def _clean_ids(df: pd.DataFrame, id_cols: list[str]) -> pd.DataFrame:
    """Normalise ID columns (trim, upper-case) and drop rows missing an ID."""
    for col in id_cols:
        df[col] = df[col].astype("string").str.strip().str.upper()
        df[col] = df[col].replace("", pd.NA)
    return df.dropna(subset=id_cols).copy()


def _clean_locations(
    df: pd.DataFrame, id_col: str, name_col: str, label: str,
    warnings: list[str],
) -> pd.DataFrame:
    """Clean a location table (societies or restaurants)."""
    rows_before = len(df)
    df = _clean_ids(df.copy(), [id_col])

    # Coordinates must be numeric and inside valid lat/lon ranges.
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    valid_coords = (
        df["Latitude"].between(-90, 90) & df["Longitude"].between(-180, 180)
    )
    df = df[valid_coords].copy()

    # Descriptive text: fill blanks so display code never sees NaN.
    df[name_col] = df[name_col].astype("string").str.strip()
    df[name_col] = df[name_col].fillna(df[id_col])
    for col in OPTIONAL_TEXT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("string").str.strip().fillna("")
        else:
            df[col] = ""
    df["Rating"] = pd.to_numeric(
        df["Rating"] if "Rating" in df.columns else pd.NA, errors="coerce"
    )

    duplicated = df[id_col].duplicated(keep="first")
    if duplicated.any():
        warnings.append(
            f"{label}: {int(duplicated.sum())} duplicate ID(s) ignored."
        )
        df = df[~duplicated]

    dropped = rows_before - len(df)
    if dropped:
        warnings.append(
            f"{label}: {dropped} row(s) dropped (missing ID or invalid "
            f"coordinates)."
        )
    if df.empty:
        raise DataError(f"{label}: no valid rows after cleaning.")
    return df.reset_index(drop=True)


def _clean_edges(
    df: pd.DataFrame, id_cols: list[str], numeric_cols: list[str],
    label: str, warnings: list[str],
) -> pd.DataFrame:
    """Clean an edge table: normalise IDs and coerce weights to numbers.

    Invalid weights become NaN here; graph construction then skips only the
    affected direction (a bad return time should not discard the forward
    road as well).
    """
    rows_before = len(df)
    df = _clean_ids(df.copy(), id_cols)
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    dropped = rows_before - len(df)
    if dropped:
        warnings.append(f"{label}: {dropped} row(s) dropped (missing IDs).")
    bad_values = int(df[numeric_cols].isna().sum().sum())
    if bad_values:
        warnings.append(
            f"{label}: {bad_values} missing/non-numeric distance or time "
            f"value(s)."
        )
    if df.empty:
        raise DataError(f"{label}: no valid rows after cleaning.")
    return df.reset_index(drop=True)


def load_dataset(data_dir: str) -> Dataset:
    """Load and clean every table in ``data_dir``.

    Raises:
        DataError: if a file is missing, empty, unparsable or lacks columns.
    """
    if not os.path.isdir(data_dir):
        raise DataError(f"Data folder '{data_dir}' does not exist.")

    warnings: list[str] = []
    raw: dict[str, pd.DataFrame] = {}
    for table, (pattern, required) in DATASET_SPEC.items():
        raw[table] = _read_csv(_find_file(data_dir, pattern, warnings),
                               required)

    return Dataset(
        societies=_clean_locations(
            raw["societies"], "Society_ID", "Society_Name", "Societies",
            warnings,
        ),
        restaurants=_clean_locations(
            raw["restaurants"], "Restaurant_ID", "Restaurant_Name",
            "Restaurants", warnings,
        ),
        connections=_clean_edges(
            raw["connections"], ["Society_A", "Society_B"],
            ["Distance_km", "Travel_Time_min"],
            "Society connections", warnings,
        ),
        society_restaurants=_clean_edges(
            raw["society_restaurants"], ["Society_ID", "Restaurant_ID"],
            ["Distance_km", "Travel_Time_min",
             "Return_Distance_km", "Return_Travel_Time_min"],
            "Society-restaurant links", warnings,
        ),
        warnings=warnings,
    )
