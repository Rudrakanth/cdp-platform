"""Synthetic data for the sample organisation: one group, five business units.

`generate_core` writes ONE wide Parquet file with ~75 KPIs across the areas a
campaign manager cares about (purchases, ML, location, spend, behaviour,
family, profile). `generate_ml_model` writes extra wide files with N KPIs to
mimic ML models that keep producing new KPIs. Everything is hash-based, so a
given (n, seed) always yields the same data.
"""
from __future__ import annotations

from datetime import datetime

import duckdb

BUS = ["RETAIL", "FASHION", "GROCERY", "FINANCE", "TRAVEL"]
CITIES = [("Abu Dhabi", 24.4539, 54.3773), ("Dubai", 25.2048, 55.2708), ("Sharjah", 25.3463, 55.4209),
          ("Al Ain", 24.2075, 55.7447), ("Ras Al Khaimah", 25.6741, 55.9804), ("Riyadh", 24.7136, 46.6753),
          ("Doha", 25.2854, 51.5310), ("Muscat", 23.5880, 58.3829)]


def _pick(u: str, options: list[str]) -> str:
    items = ", ".join("'" + o + "'" for o in options)
    return f"list_extract([{items}], 1 + CAST(floor({u} * {len(options)}) AS INTEGER))"


def _meta() -> dict:
    """KPI metadata (description/category/BU/freshness SLA) for the core file."""
    m: dict = {}

    def add(name, cat, desc, bu="ALL", max_age=None):
        m[name] = {"category": cat, "description": desc, "bu": bu, **({"max_age_hours": max_age} if max_age else {})}

    for n, d in [("age", "Age in years"), ("gender", "Gender"), ("nationality", "Nationality code"),
                 ("language", "Preferred language"), ("loyalty_tier", "Group loyalty tier"),
                 ("signup_at", "First sign-up"), ("primary_bu", "BU with most spend")]:
        add(n, "profile", d)
    add("purchases_total_count", "purchases", "Lifetime purchases", max_age=36)
    add("purchases_30d", "purchases", "Purchases in last 30 days", max_age=36)
    add("last_purchase_at", "purchases", "Timestamp of last purchase", max_age=36)
    add("first_purchase_at", "purchases", "Timestamp of first purchase")
    add("avg_basket_value", "purchases", "Average basket value (AED)", max_age=36)
    add("returns_rate", "purchases", "Share of purchases returned")
    for b in BUS:
        add(f"spend_{b.lower()}_12m", "spend", f"12-month spend in {b} (AED)", bu=b, max_age=36)
        add(f"ml_propensity_{b.lower()}", "ml", f"ML propensity to buy in {b}", bu=b, max_age=170)
        add(f"ml_top_products_{b.lower()}", "ml", f"ML top products for the customer in {b}", bu=b, max_age=170)
    add("spend_total_12m", "spend", "12-month spend across the group (AED)", max_age=36)
    add("spend_trend_pct", "spend", "Spend trend vs previous 12 months, %")
    add("spend_band", "spend", "low / mid / high / vip")
    add("top_category", "spend", "Most purchased category")
    add("payment_method_pref", "spend", "Preferred payment method")
    add("discount_sensitivity", "spend", "0..1, how much discounts drive purchases")
    add("ml_churn_score", "ml", "Churn probability", max_age=170)
    add("ml_clv_pred", "ml", "Predicted 3y customer lifetime value (AED)", max_age=170)
    add("ml_next_best_offer", "ml", "Next best offer", max_age=170)
    add("ml_segment_label", "ml", "ML behavioural segment", max_age=170)
    add("ml_cluster_id", "ml", "Embedding cluster id")
    add("ml_churn_explain", "ml", "Nested JSON: churn drivers, model version, confidence", max_age=170)
    add("last_active_location", "location", "Last location where the customer was active (lat/lon)", max_age=24)
    add("last_active_city", "location", "City of last activity", max_age=24)
    add("home_city", "location", "Inferred home city")
    add("last_active_at", "location", "Timestamp of last activity", max_age=24)
    for n, d in [("app_sessions_30d", "App sessions in 30 days"), ("email_open_rate", "Email open rate 0..1"),
                 ("push_opt_in", "Push consent"), ("email_opt_in", "Email consent"), ("sms_opt_in", "SMS consent"),
                 ("preferred_channel", "Best channel"), ("last_login_at", "Last login"),
                 ("device_type", "ios / android / web"), ("browsing_interests", "Interest tags")]:
        add(n, "behaviour", d)
    for n, d in [("household_size", "Persons in household"), ("has_children", "Has children"),
                 ("children_age_bands", "Age bands of children"), ("marital_status", "Marital status"),
                 ("family_spend_share", "Share of household spend by this customer"),
                 ("family_plan_member", "Member of family plan")]:
        add(n, "family", d)
    return m


CORE_META = _meta()


def generate_core(path: str, n_profiles: int = 100_000, ref_now: datetime | None = None, seed: int = 7) -> dict:
    ref_now = ref_now or datetime(2026, 11, 1)
    ref = f"TIMESTAMP '{ref_now:%Y-%m-%d %H:%M:%S}'"
    k = [0]

    def u() -> str:       # fresh deterministic uniform [0,1) per call site
        k[0] += 1
        return f"((hash(i * 7919 + {k[0]} * 104729 + {seed} * 1299709) % 1000003) / 1000003.0)"

    def nullable(expr: str, p: float) -> str:
        return f"CASE WHEN {u()} < {p} THEN NULL ELSE {expr} END"

    def days_ago(x: str) -> str:
        return f"({ref} - to_seconds(CAST(({x}) * 86400 AS BIGINT)))"

    cols: list[str] = []

    def col(name, expr):
        cols.append(f"{expr} AS {name}")

    col("customer_id", "'C' || lpad(CAST(i AS VARCHAR), 9, '0')")
    # profile
    col("age", f"18 + CAST(floor({u()} * 60) AS INTEGER)")
    col("gender", f"CASE WHEN {u()} < 0.48 THEN 'female' WHEN {u()} < 0.96 THEN 'male' ELSE 'unspecified' END")
    col("nationality", _pick(u(), ["AE", "IN", "PK", "EG", "PH", "GB", "SA", "JO"]))
    col("language", _pick(u(), ["en", "ar", "hi", "ur"]))
    col("loyalty_tier", f"CASE WHEN {u()} < 0.55 THEN 'bronze' WHEN {u()} < 0.8 THEN 'silver' WHEN {u()} < 0.95 THEN 'gold' ELSE 'platinum' END")
    col("signup_at", f"TIMESTAMP '2019-01-01' + to_days(CAST(floor({u()} * 2500) AS INTEGER))")
    col("primary_bu", _pick(u(), BUS))
    # purchases
    col("purchases_total_count", f"CAST(floor(pow({u()}, 2) * 120) AS BIGINT)")
    col("purchases_30d", f"CAST(floor(pow({u()}, 3) * 12) AS BIGINT)")
    col("last_purchase_at", nullable(days_ago(f"pow({u()}, 2) * 400"), 0.12))
    col("first_purchase_at", nullable(days_ago(f"400 + {u()} * 1500"), 0.12))
    col("avg_basket_value", nullable(f"round(20 + pow({u()}, 2) * 480, 2)", 0.12))
    col("returns_rate", nullable(f"round(pow({u()}, 3) * 0.4, 4)", 0.12))
    # spend per BU
    for b in BUS:
        col(f"spend_{b.lower()}_12m", nullable(f"round(pow({u()}, 3) * 20000, 2)", 0.45))
    tot = " + ".join(f"coalesce(spend_{b.lower()}_12m, 0)" for b in BUS)
    col("spend_total_12m", f"round({tot}, 2)")
    col("spend_trend_pct", nullable(f"round(({u()} - 0.4) * 100, 2)", 0.1))
    col("spend_band", "CASE WHEN spend_total_12m < 500 THEN 'low' WHEN spend_total_12m < 5000 THEN 'mid' WHEN spend_total_12m < 20000 THEN 'high' ELSE 'vip' END")
    col("top_category", _pick(u(), ["grocery", "fashion", "electronics", "travel", "finance", "home", "beauty"]))
    col("payment_method_pref", nullable(_pick(u(), ["card", "cash", "wallet", "bnpl"]), 0.05))
    col("discount_sensitivity", f"round({u()}, 4)")
    # ML
    col("ml_churn_score", nullable(f"round(pow({u()}, 2), 4)", 0.03))
    col("ml_clv_pred", nullable(f"round(pow({u()}, 3) * 50000, 2)", 0.03))
    for b in BUS:
        col(f"ml_propensity_{b.lower()}", nullable(f"round({u()}, 4)", 0.05))
    col("ml_next_best_offer", nullable(_pick(u(), ["free_shipping", "10_pct_off", "loyalty_double_points", "bundle_deal", "none"]), 0.03))
    col("ml_segment_label", nullable(_pick(u(), ["bargain_hunter", "brand_loyal", "explorer", "dormant", "high_roller"]), 0.03))
    col("ml_cluster_id", f"CAST(floor({u()} * 12) AS BIGINT)")
    for bi, b in enumerate(BUS):
        x1 = f"CAST(floor(pow({u()}, 2) * 300) AS INTEGER)"
        d2, d3 = f"1 + CAST(floor({u()} * 20) AS INTEGER)", f"1 + CAST(floor({u()} * 20) AS INTEGER)"
        pre = f"'{b[:3]}-'"
        lst = (f"[{pre} || lpad(CAST(({x1}) AS VARCHAR), 4, '0'), "
               f"{pre} || lpad(CAST(({x1}) + {d2} AS VARCHAR), 4, '0'), "
               f"{pre} || lpad(CAST(({x1}) + {d2} + {d3} AS VARCHAR), 4, '0')]")
        col(f"ml_top_products_{b.lower()}", nullable(lst, 0.2))
    col("ml_churn_explain",
        nullable(f"{{'drivers': {{'price': round({u()}, 4), 'service': round({u()}, 4), 'delivery': round({u()}, 4)}}, "
                 f"'model': 'churn_v3', 'confidence': round({u()}, 4)}}", 0.04))
    # location
    city = f"CAST(floor({u()} * {len(CITIES)}) AS INTEGER)"
    lats = "[" + ",".join(str(c[1]) for c in CITIES) + "]"
    lons = "[" + ",".join(str(c[2]) for c in CITIES) + "]"
    names = "[" + ",".join("'" + c[0] + "'" for c in CITIES) + "]"
    col("_city", city)
    col("last_active_location",
        nullable(f"struct_pack(lat := round(list_extract({lats}, _city + 1) + ({u()} - 0.5) * 0.4, 5), "
                 f"lon := round(list_extract({lons}, _city + 1) + ({u()} - 0.5) * 0.4, 5))", 0.1))
    col("last_active_city", f"CASE WHEN last_active_location IS NULL THEN NULL ELSE list_extract({names}, _city + 1) END")
    col("home_city", f"CASE WHEN {u()} < 0.8 THEN list_extract({names}, _city + 1) ELSE list_extract({names}, 1 + CAST(floor({u()} * {len(CITIES)}) AS INTEGER)) END")
    col("last_active_at", nullable(f"({ref} - to_seconds(CAST(pow({u()}, 2) * 60 * 86400 AS BIGINT)))", 0.1))
    # behaviour
    col("app_sessions_30d", f"CAST(floor(pow({u()}, 2) * 60) AS BIGINT)")
    col("email_open_rate", nullable(f"round({u()}, 4)", 0.15))
    col("push_opt_in", f"{u()} < 0.55")
    col("email_opt_in", f"{u()} < 0.8")
    col("sms_opt_in", f"{u()} < 0.4")
    col("preferred_channel", nullable(_pick(u(), ["email", "sms", "push", "whatsapp"]), 0.05))
    col("last_login_at", nullable(f"({ref} - to_seconds(CAST(pow({u()}, 2) * 90 * 86400 AS BIGINT)))", 0.08))
    col("device_type", _pick(u(), ["ios", "android", "web"]))
    sal = k[0] + 1
    col("browsing_interests",
        f"list_filter(['sports','tech','fashion','travel','food','finance','kids','home'], "
        f"lambda x: hash(x || CAST(i AS VARCHAR) || '{sal}_{seed}') % 100 < 30)")
    # family
    col("household_size", f"1 + CAST(floor(pow({u()}, 1.5) * 6) AS BIGINT)")
    col("has_children", f"{u()} < 0.42")
    sal2 = k[0] + 1
    col("children_age_bands",
        f"CASE WHEN has_children THEN list_filter(['0-2','3-5','6-12','13-17'], lambda x: hash(x || CAST(i AS VARCHAR) || '{sal2}_{seed}') % 100 < 45) "
        f"ELSE CAST([] AS VARCHAR[]) END")
    col("marital_status", _pick(u(), ["single", "married", "divorced", "widowed"]))
    col("family_spend_share", nullable(f"round({u()}, 4)", 0.3))
    col("family_plan_member", f"{u()} < 0.2")

    inner = ",\n  ".join(cols)
    sql = (f"COPY (SELECT * EXCLUDE (_city) FROM (SELECT\n  {inner}\nFROM (SELECT * FROM range({int(n_profiles)}) t(i))) ORDER BY customer_id) "
           f"TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 100000)")
    duckdb.connect().execute(sql)
    return {"path": path, "profiles": n_profiles, "columns": len(cols) - 1 - 1}   # minus customer_id and _city


def generate_ml_model(path: str, model: str, n_kpis: int, n_profiles: int, seed: int = 11) -> dict:
    """Wide file for an ML model emitting many KPIs (mixed types). KPI names: ml_<model>_k0001..."""
    cols = ["'C' || lpad(CAST(i AS VARCHAR), 9, '0') AS customer_id"]
    for j in range(1, n_kpis + 1):
        u = f"((hash(i * 7919 + {j} * 104729 + {seed} * 1299709) % 1000003) / 1000003.0)"
        name = f"ml_{model}_k{j:04d}"
        if j % 20 == 0:
            cols.append(f"{u} < 0.5 AS {name}")
        elif j % 10 == 0:
            cols.append(f"list_extract(['a','b','c','d'], 1 + CAST(floor({u} * 4) AS INTEGER)) AS {name}")
        elif j % 7 == 0:
            cols.append(f"CASE WHEN {u} < 0.5 THEN ['x{j % 5}', 'y'] ELSE ['z'] END AS {name}")
        else:
            cols.append(f"CASE WHEN {u} < 0.2 THEN NULL ELSE round({u}, 4) END AS {name}")
    sql = (f"COPY (SELECT {', '.join(cols)} FROM range({int(n_profiles)}) t(i)) TO '{path}' (FORMAT parquet)")
    duckdb.connect().execute(sql)
    return {"path": path, "kpis": n_kpis, "profiles": n_profiles}
