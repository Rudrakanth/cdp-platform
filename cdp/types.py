"""KPI data types and the operator matrix.

The matrix is the single source of truth: the compiler validates against it,
the API exposes it, and the UI builds its operator drop-downs from it.
"""

KPI_TYPES = ("number", "string", "bool", "datetime", "list", "geo", "json")

# physical table per logical type
TABLE_FOR = {
    "number": "kpi_num",
    "string": "kpi_str",
    "bool": "kpi_bool",
    "datetime": "kpi_ts",
    "list": "kpi_list",
    "geo": "kpi_geo",
    "json": "kpi_json",
}

NULLS = ("is_null", "is_not_null")

OPERATORS = {
    "number": ("eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "not_in") + NULLS,
    "string": ("eq", "ne", "in", "not_in", "contains", "not_contains",
               "starts_with", "ends_with", "regex") + NULLS,
    "bool": ("is_true", "is_false") + NULLS,
    "datetime": ("on_date", "before", "after", "between", "within_last",
                 "not_within_last") + NULLS,
    "list": ("contains", "not_contains", "contains_any", "contains_all", "size_eq",
             "size_gt", "size_gte", "size_lt", "is_empty", "first_is") + NULLS,
    "geo": ("within_radius", "outside_radius", "within_bbox") + NULLS,
    "json": ("json_eq", "json_ne", "json_gt", "json_gte", "json_lt", "json_lte",
             "json_contains") + NULLS,
}

# operators that take no value
NO_VALUE = {"is_null", "is_not_null", "is_true", "is_false", "is_empty"}

# value shape hint for the UI
VALUE_SHAPE = {
    "eq": "scalar", "ne": "scalar", "gt": "scalar", "gte": "scalar", "lt": "scalar",
    "lte": "scalar", "between": "range", "in": "multi", "not_in": "multi",
    "contains": "scalar", "not_contains": "scalar", "starts_with": "scalar",
    "ends_with": "scalar", "regex": "scalar", "on_date": "scalar", "before": "scalar",
    "after": "scalar", "within_last": "relative", "not_within_last": "relative",
    "contains_any": "multi", "contains_all": "multi", "size_eq": "scalar",
    "size_gt": "scalar", "size_gte": "scalar", "size_lt": "scalar", "first_is": "scalar",
    "within_radius": "radius", "outside_radius": "radius", "within_bbox": "bbox",
    "json_eq": "json", "json_ne": "json", "json_gt": "json", "json_gte": "json",
    "json_lt": "json", "json_lte": "json", "json_contains": "json",
}

REL_UNITS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}

KPI_NAME_RE = r"^[a-z][a-z0-9_]{0,63}$"
JSON_PATH_RE = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$"
