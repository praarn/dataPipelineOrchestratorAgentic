"""
Small compatibility shim.

Pandas 3.x introduced a default `str` (StringDtype) storage for text columns,
replacing the old catch-all `object` dtype. Plain `series.dtype == object`
checks silently stop matching text columns under pandas 3.x. These helpers
treat both as "textual" so the agents behave the same way regardless of
which pandas major version is installed.
"""
from __future__ import annotations

import pandas as pd

TEXTUAL_DTYPE_KINDS = ("object", "string")


def is_textual_dtype(series: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)


def textual_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if is_textual_dtype(df[c])]
