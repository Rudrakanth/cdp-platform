"""SQL dialects. The compiler is engine-agnostic; every engine-specific
spelling (literals, string/list/JSON functions) lives here.

Literal rendering is the injection boundary: values are never concatenated
raw. Strings are escaped per dialect, numbers go through float()/int().
"""
from __future__ import annotations

import math
from datetime import datetime

from ..errors import ValidationError


class Dialect:
    name = "base"

    # ---- literals
    def str(self, s: str) -> str:
        raise NotImplementedError

    def num(self, x) -> str:
        f = float(x)
        if math.isnan(f) or math.isinf(f):
            raise ValidationError("non-finite number")
        return repr(f)

    def ts(self, dt: datetime) -> str:
        raise NotImplementedError

    # ---- table reference (ClickHouse needs FINAL for ReplacingMergeTree)
    def tbl(self, name: str) -> str:
        return name

    # ---- haversine distance in km (identical formula in both engines)
    def distance_km(self, latc: str, lonc: str, lat: float, lon: float) -> str:
        la, lo = self.num(lat), self.num(lon)
        return (f"(12742.0 * asin(sqrt(least(1.0, pow(sin(radians({latc} - {la}) / 2), 2) + "
                f"cos(radians({la})) * cos(radians({latc})) * pow(sin(radians({lonc} - {lo}) / 2), 2)))))")

    def bool_true(self, col: str) -> str:
        return f"{col} = true"

    def bool_false(self, col: str) -> str:
        return f"{col} = false"


class DuckDialect(Dialect):
    name = "duckdb"

    def str(self, s: str) -> str:
        if "\x00" in s:
            raise ValidationError("NUL character in string")
        return "'" + s.replace("'", "''") + "'"

    def ts(self, dt: datetime) -> str:
        return f"TIMESTAMP '{dt:%Y-%m-%d %H:%M:%S.%f}'"

    def contains_ci(self, col, s): return f"contains(lower({col}), lower({self.str(s)}))"
    def starts_ci(self, col, s): return f"starts_with(lower({col}), lower({self.str(s)}))"
    def ends_ci(self, col, s): return f"ends_with(lower({col}), lower({self.str(s)}))"
    def regex(self, col, s): return f"regexp_matches({col}, {self.str(s)})"

    def unnest(self, col): return f"unnest({col})"
    def list_has(self, col, s): return f"list_contains({col}, {self.str(s)})"
    def list_any(self, col, xs): return f"list_has_any({col}, [{', '.join(self.str(x) for x in xs)}])"
    def list_all(self, col, xs): return f"list_has_all({col}, [{', '.join(self.str(x) for x in xs)}])"
    def list_len(self, col): return f"len({col})"
    def list_first(self, col): return f"list_extract({col}, 1)"

    def json_has(self, col, path): return f"json_type({col}, '$.{path}') IS NOT NULL"
    def json_is_num(self, col, path): return f"json_type({col}, '$.{path}') IN ('BIGINT','UBIGINT','DOUBLE')"
    def json_is_str(self, col, path): return f"json_type({col}, '$.{path}') = 'VARCHAR'"
    def json_str(self, col, path): return f"json_extract_string({col}, '$.{path}')"
    def json_num(self, col, path): return f"TRY_CAST(json_extract_string({col}, '$.{path}') AS DOUBLE)"


class ClickHouseDialect(Dialect):
    name = "clickhouse"

    def str(self, s: str) -> str:
        if "\x00" in s:
            raise ValidationError("NUL character in string")
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def ts(self, dt: datetime) -> str:
        return f"toDateTime64('{dt:%Y-%m-%d %H:%M:%S.%f}', 6, 'UTC')"

    def tbl(self, name: str) -> str:
        return f"{name} FINAL"

    def contains_ci(self, col, s): return f"(positionCaseInsensitiveUTF8({col}, {self.str(s)}) > 0)"
    def starts_ci(self, col, s): return f"startsWith(lowerUTF8({col}), lowerUTF8({self.str(s)}))"
    def ends_ci(self, col, s): return f"endsWith(lowerUTF8({col}), lowerUTF8({self.str(s)}))"
    def regex(self, col, s): return f"match({col}, {self.str(s)})"

    def unnest(self, col): return f"arrayJoin({col})"
    def list_has(self, col, s): return f"has({col}, {self.str(s)})"
    def list_any(self, col, xs): return f"hasAny({col}, [{', '.join(self.str(x) for x in xs)}])"
    def list_all(self, col, xs): return f"hasAll({col}, [{', '.join(self.str(x) for x in xs)}])"
    def list_len(self, col): return f"length({col})"
    def list_first(self, col): return f"arrayElement({col}, 1)"

    @staticmethod
    def _p(path): return ", ".join("'" + p + "'" for p in path.split("."))

    def json_has(self, col, path): return f"JSONHas({col}, {self._p(path)})"
    def json_is_num(self, col, path): return f"JSONType({col}, {self._p(path)}) IN ('Int64','UInt64','Double')"
    def json_is_str(self, col, path): return f"JSONType({col}, {self._p(path)}) = 'String'"
    def json_str(self, col, path): return f"JSONExtractString({col}, {self._p(path)})"
    def json_num(self, col, path): return f"JSONExtractFloat({col}, {self._p(path)})"
