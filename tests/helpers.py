from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq


def write_parquet(path: str, **cols) -> str:
    pq.write_table(pa.table(cols), path)
    return str(path)
