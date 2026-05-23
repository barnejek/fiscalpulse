#!/usr/bin/env python3
"""
FiscalPulse — Data Fetcher
===========================
Fetches macrofiscal data from IMF WEO (SDMX 3.0 API) and writes
data/fiscal_data.json consumed by the dashboard.

Usage:
    pip install -r requirements.txt
    python fetch_data.py              # fetch fresh data from IMF API
    python fetch_data.py --from-cache # use existing CSV cache (offline)

Output:
    data/fiscal_data.json
    cache/weo_YYYYMMDD.csv  (raw cache)
"""

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SCRIPT_DIR = Path(__file__).parent
CACHE_DIR  = SCRIPT_DIR / "cache"
DATA_DIR   = SCRIPT_DIR / "data"

# ── Countries ─────────────────────────────────────────────────────────────────
COUNTRIES = {
    "AUS": {"name": "Australia",        "flag": "🇦🇺"},
    "CAN": {"name": "Canada",           "flag": "🇨🇦"},
    "CZE": {"name": "Czech Republic",   "flag": "🇨🇿"},
    "FRA": {"name": "France",           "flag": "🇫🇷"},
    "DEU": {"name": "Germany",          "flag": "🇩🇪"},
    "GRC": {"name": "Greece",           "flag": "🇬🇷"},
    "HUN": {"name": "Hungary",          "flag": "🇭🇺"},
    "ISR": {"name": "Israel",           "flag": "🇮🇱"},
    "ITA": {"name": "Italy",            "flag": "🇮🇹"},
    "JPN": {"name": "Japan",            "flag": "🇯🇵"},
    "KOR": {"name": "Korea",            "flag": "🇰🇷"},
    "MEX": {"name": "Mexico",           "flag": "🇲🇽"},
    "NLD": {"name": "Netherlands",      "flag": "🇳🇱"},
    "NZL": {"name": "New Zealand",      "flag": "🇳🇿"},
    "POL": {"name": "Poland",           "flag": "🇵🇱"},
    "PRT": {"name": "Portugal",         "flag": "🇵🇹"},
    "ESP": {"name": "Spain",            "flag": "🇪🇸"},
    "TUR": {"name": "Turkey",           "flag": "🇹🇷"},
    "GBR": {"name": "United Kingdom",   "flag": "🇬🇧"},
    "USA": {"name": "United States",    "flag": "🇺🇸"},
}

WEO_INDICATORS = {
    "GGXCNL_NGDP":  "Overall Balance % GDP",
    "GGXONLB_NGDP": "Primary Balance % GDP",
    "GGSB_NPGDP":   "Structural Balance % Pot. GDP",
    "GGR_NGDP":     "Revenue % GDP",
    "GGX_NGDP":     "Expenditure % GDP",
    "GGXWDG_NGDP":  "Gross Debt % GDP",
    "GGXWDN_NGDP":  "Net Debt % GDP",
    "NGDP_RPCH":    "Real GDP Growth %",
    "PCPIPCH":      "CPI Inflation %",
    "NGDPD":        "Nominal GDP USD bn",
}

YEAR_START   = 2015
YEAR_END     = 2031
CURRENT_YEAR = date.today().year
PROJ_START   = CURRENT_YEAR

IMF_BASE = "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.RES/WEO/+/"

THRESHOLDS = {
    "debt_gross":         {"direction": "higher_worse", "breaks": [40, 60, 90, 120]},
    "debt_change_3y":     {"direction": "higher_worse", "breaks": [-5, 0, 3, 7]},
    "structural_balance": {"direction": "lower_worse",  "breaks": [-1.0, -2.0, -3.5, -5.0]},
    "r_minus_g":          {"direction": "higher_worse", "breaks": [-2.0, 0.0, 1.0, 3.0]},
    "interest_revenue":   {"direction": "higher_worse", "breaks": [5, 10, 15, 20]},
}

SCORE_WEIGHTS = {
    "debt_gross": 0.25, "debt_change_3y": 0.20,
    "structural_balance": 0.20, "r_minus_g": 0.20, "interest_revenue": 0.15,
}

SCORE_COL_MAP = {
    "debt_gross":         "GGXWDG_NGDP",
    "debt_change_3y":     "debt_change_3y",
    "structural_balance": "GGSB_NPGDP",
    "r_minus_g":          "r_minus_g",
    "interest_revenue":   "interest_rev",
}

RISK_TIERS = [(1.0, 2.0, "Investment Grade"), (2.0, 3.0, "Watch"), (3.0, 6.0, "High Risk")]

STRESS_SCENARIOS = {
    "geopolitical": {"name": "Geopolitical Shock",    "delta_growth": -1.5, "delta_rate": +1.0, "delta_pb": -0.5, "years": 3},
    "repricing":    {"name": "Market Repricing",      "delta_growth": -1.0, "delta_rate": +2.0, "delta_pb": -0.7, "years": 3},
    "us_spillover": {"name": "US Spillover",          "delta_growth": -0.5, "delta_rate": +0.8, "delta_pb": -0.3, "years": 3},
}


# ── IMF API ────────────────────────────────────────────────────────────────────
def _parse_sdmx(raw: dict) -> pd.DataFrame:
    try:
        structure = raw["data"]["structures"][0]
    except (KeyError, IndexError):
        structure = raw["data"].get("structure", {})

    series_dims = structure.get("dimensions", {}).get("series", [])
    obs_dims    = structure.get("dimensions", {}).get("observation", [])
    dim_by_pos  = {}
    for dim in series_dims:
        pos = dim.get("keyPosition")
        if pos is not None:
            vals = [v.get("id", v.get("value", "")) for v in dim.get("values", [])]
            dim_by_pos[pos] = (dim["id"], vals)

    if not obs_dims:
        return pd.DataFrame()
    time_values = [v.get("value", v.get("id", "")) for v in obs_dims[0].get("values", [])]

    datasets = raw["data"].get("dataSets", raw["data"].get("dataSet", []))
    records  = []
    for ds in datasets:
        for sk, sd in ds.get("series", {}).items():
            idx  = [int(x) for x in sk.split(":")]
            meta = {}
            for pos, (did, dvals) in dim_by_pos.items():
                if pos < len(idx) and idx[pos] < len(dvals):
                    meta[did] = dvals[idx[pos]]
            for ok, ov in sd.get("observations", {}).items():
                ti = int(ok)
                if ti < len(time_values) and ov and ov[0] not in (None, "n/a", ""):
                    records.append({**meta, "year": int(time_values[ti]), "value": float(ov[0])})

    return pd.DataFrame(records)


def fetch_indicator(code: str, retries: int = 3, pause: float = 2.0) -> pd.DataFrame:
    url = f"{IMF_BASE}*.{code}.A"
    iso_set = set(COUNTRIES.keys())
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"Accept": "application/json"}, timeout=60)
            if r.status_code == 200:
                df = _parse_sdmx(r.json())
                if df.empty:
                    return pd.DataFrame()
                country_col = next((c for c in df.columns if df[c].isin(iso_set).any()), None)
                if country_col and country_col != "country":
                    df = df.rename(columns={country_col: "country"})
                elif not country_col:
                    return pd.DataFrame()
                df["indicator"] = code
                df = df[df["country"].isin(iso_set)]
                return df[["country", "year", "value", "indicator"]]
            else:
                print(f"    [{code}] HTTP {r.status_code}, attempt {attempt+1}/{retries}")
        except Exception as e:
            print(f"    [{code}] {e}, attempt {attempt+1}/{retries}")
        time.sleep(pause)
    return pd.DataFrame()


def fetch_all() -> pd.DataFrame:
    frames = []
    total  = len(WEO_INDICATORS)
    for i, (code, label) in enumerate(WEO_INDICATORS.items(), 1):
        print(f"  [{i}/{total}] {code} — {label}")
        df = fetch_indicator(code)
        if not df.empty:
            frames.append(df)
        time.sleep(1.5)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_or_fetch(from_cache: bool = False) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    today      = date.today().strftime("%Y%m%d")
    cache_file = CACHE_DIR / f"weo_{today}.csv"

    # Try today's cache first
    if cache_file.exists():
        print(f"Using today's cache: {cache_file.name}")
        return pd.read_csv(cache_file)

    # Try any existing cache if offline mode
    if from_cache:
        existing = sorted(CACHE_DIR.glob("weo_*.csv"))
        if existing:
            latest = existing[-1]
            print(f"Using cached file: {latest.name}")
            return pd.read_csv(latest)
        print("No cache found. Run without --from-cache to fetch from API.")
        sys.exit(1)

    print("Fetching data from IMF API (~1-2 minutes)...")
    df = fetch_all()
    if not df.empty:
        df.to_csv(cache_file, index=False)
        print(f"Cache saved: {cache_file.name}")
    return df


# ── Processing ────────────────────────────────────────────────────────────────
def pivot_data(df_long: pd.DataFrame) -> pd.DataFrame:
    return (
        df_long
        .pivot_table(index=["country", "year"], columns="indicator", values="value")
        .reset_index()
        .rename_axis(None, axis=1)
    )


def compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_values(["country", "year"])

    if "GGXONLB_NGDP" in df.columns and "GGXCNL_NGDP" in df.columns:
        df["interest_pct_gdp"] = df["GGXONLB_NGDP"] - df["GGXCNL_NGDP"]

    if "NGDP_RPCH" in df.columns and "PCPIPCH" in df.columns:
        df["g_nominal"] = df["NGDP_RPCH"] + df["PCPIPCH"]

    if "interest_pct_gdp" in df.columns and "GGXWDG_NGDP" in df.columns:
        df["debt_lag"] = df.groupby("country")["GGXWDG_NGDP"].shift(1)
        df["r_implicit"] = np.where(
            df["debt_lag"] > 0,
            df["interest_pct_gdp"] / df["debt_lag"] * 100,
            np.nan,
        )

    if "r_implicit" in df.columns and "g_nominal" in df.columns:
        df["r_minus_g"] = df["r_implicit"] - df["g_nominal"]

    if "r_minus_g" in df.columns and "GGXWDG_NGDP" in df.columns:
        g_frac  = df.get("g_nominal", pd.Series(dtype=float)) / 100
        rg_frac = df["r_minus_g"] / 100
        df["dspb"] = (rg_frac / (1 + g_frac)) * df["GGXWDG_NGDP"]

    if "interest_pct_gdp" in df.columns and "GGR_NGDP" in df.columns:
        df["interest_rev"] = np.where(
            df["GGR_NGDP"] > 0,
            df["interest_pct_gdp"] / df["GGR_NGDP"] * 100,
            np.nan,
        )

    if "GGXWDG_NGDP" in df.columns:
        df["debt_change_3y"] = df.groupby("country")["GGXWDG_NGDP"].transform(
            lambda x: x.shift(-3) - x
        )

    return df


def score_metric(value, cfg: dict) -> int:
    if pd.isna(value):
        return 3
    breaks = cfg["breaks"]
    if cfg["direction"] == "higher_worse":
        if value < breaks[0]: return 1
        if value < breaks[1]: return 2
        if value < breaks[2]: return 3
        if value < breaks[3]: return 4
        return 5
    else:
        if value > breaks[0]: return 1
        if value > breaks[1]: return 2
        if value > breaks[2]: return 3
        if value > breaks[3]: return 4
        return 5


def get_risk_tier(composite: float) -> str:
    for lo, hi, label in RISK_TIERS:
        if lo <= composite < hi:
            return label
    return "High Risk"


def run_stress(df_country: pd.DataFrame) -> dict:
    base_row = df_country[df_country["year"] == CURRENT_YEAR]
    if base_row.empty:
        base_row = df_country[df_country["year"] == CURRENT_YEAR - 1]
    if base_row.empty:
        return {}

    b     = base_row.iloc[0]
    debt0 = b.get("GGXWDG_NGDP", np.nan)
    g0    = b.get("g_nominal",    3.0)
    r0    = b.get("r_implicit",   3.5)
    pb0   = b.get("GGXONLB_NGDP", 0.0)

    if any(pd.isna(v) for v in (debt0, g0, r0, pb0)):
        return {}

    results = {}
    d = debt0
    baseline_traj = []
    for offset in range(1, 4):
        yr   = CURRENT_YEAR + offset
        proj = df_country[df_country["year"] == yr]
        if not proj.empty and not pd.isna(proj.iloc[0].get("GGXWDG_NGDP", np.nan)):
            d = proj.iloc[0]["GGXWDG_NGDP"]
        else:
            d = d * (1 + r0 / 100) / (1 + g0 / 100) - pb0
        baseline_traj.append(round(d, 1))
    results["baseline"] = baseline_traj

    for key, params in STRESS_SCENARIOS.items():
        traj = []
        d = debt0
        for _ in range(params["years"]):
            g_s  = g0  + params["delta_growth"]
            r_s  = r0  + params["delta_rate"]
            pb_s = pb0 + params["delta_pb"]
            denom = 1 + max(g_s, -10.0) / 100
            if denom != 0:
                d = d * (1 + r_s / 100) / denom - pb_s
            traj.append(round(d, 1))
        results[key] = traj

    return results


# ── JSON builder ──────────────────────────────────────────────────────────────
ALL_SERIES_COLS = [
    "GGXWDG_NGDP", "GGXWDN_NGDP", "GGXCNL_NGDP", "GGXONLB_NGDP",
    "GGSB_NPGDP", "GGR_NGDP", "GGX_NGDP", "NGDP_RPCH", "PCPIPCH",
    "g_nominal", "r_implicit", "r_minus_g", "interest_pct_gdp",
    "interest_rev", "dspb", "NGDPD",
]


def _v(x):
    """Return rounded float or None."""
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return None
    return round(float(x), 2)


def build_json(df_wide: pd.DataFrame) -> dict:
    out = {
        "metadata": {
            "last_updated": date.today().isoformat(),
            "source": "IMF World Economic Outlook via SDMX 3.0 API",
            "year_start": YEAR_START,
            "year_end": YEAR_END,
            "current_year": CURRENT_YEAR,
            "proj_start": PROJ_START,
        },
        "countries": {},
    }

    years = list(range(YEAR_START, YEAR_END + 1))

    for iso, meta in COUNTRIES.items():
        df_c = df_wide[df_wide["country"] == iso].copy()
        if df_c.empty:
            print(f"  [!] No data for {iso}")
            continue

        # Series
        series = {}
        for col in ALL_SERIES_COLS:
            if col not in df_c.columns:
                continue
            series[col] = {}
            for yr in years:
                row = df_c[df_c["year"] == yr]
                if not row.empty:
                    series[col][str(yr)] = _v(row.iloc[0][col])

        # Latest historical row
        hist = df_c[df_c["year"] <= CURRENT_YEAR].sort_values("year")
        latest_row = hist.iloc[-1] if not hist.empty else pd.Series(dtype=float)

        # Scores
        components = {}
        for metric, cfg in THRESHOLDS.items():
            col = SCORE_COL_MAP.get(metric)
            val = latest_row.get(col, np.nan) if col and not latest_row.empty else np.nan
            components[metric] = score_metric(val, cfg)

        composite = round(
            sum(SCORE_WEIGHTS[m] * s for m, s in components.items()), 2
        )

        # Latest key values
        latest = {}
        for col in ALL_SERIES_COLS:
            if col in latest_row.index:
                latest[col] = _v(latest_row.get(col))

        out["countries"][iso] = {
            "name":   meta["name"],
            "flag":   meta["flag"],
            "series": series,
            "latest": latest,
            "latest_year": int(latest_row.get("year", CURRENT_YEAR)) if not latest_row.empty else CURRENT_YEAR,
            "scores": {
                "composite":  composite,
                "tier":       get_risk_tier(composite),
                "components": components,
            },
            "stress": run_stress(df_c),
        }

    return out


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="FiscalPulse data fetcher")
    parser.add_argument("--from-cache", action="store_true", help="Use existing CSV cache")
    args = parser.parse_args()

    DATA_DIR.mkdir(exist_ok=True)

    print("=" * 55)
    print(f"  FiscalPulse — Data Fetcher  |  {date.today():%Y-%m-%d}")
    print("=" * 55)

    df_long = load_or_fetch(from_cache=args.from_cache)
    if df_long.empty:
        print("ERROR: No data retrieved.")
        sys.exit(1)

    print("Processing...")
    df_wide = pivot_data(df_long)
    df_wide = compute_derived(df_wide)
    print(f"  Rows: {len(df_wide)} | Countries: {df_wide['country'].nunique()}")

    print("Building JSON...")
    data = build_json(df_wide)

    out_path = DATA_DIR / "fiscal_data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n  ✓ {out_path}")
    print(f"  Countries: {len(data['countries'])}")
    print("=" * 55)


if __name__ == "__main__":
    main()
