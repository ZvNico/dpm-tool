from __future__ import annotations

import re
from collections.abc import Iterable

from dpm._constants import CODE_RE, COL_RE, METRIC_LABEL_RE, QNAME_RE, QNAME_TOKEN_RE, ROW_RE


def norm(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ").replace("\r", " ").strip())


def first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    return match.group(0).upper() if match else ""


def extract_qname(text: str) -> str:
    match = QNAME_TOKEN_RE.search(norm(text))
    return match.group(0) if match else ""


def extract_metric_label(text: str) -> str:
    match = METRIC_LABEL_RE.search(norm(text))
    return match.group(1).strip() if match else ""


def is_qname(text: str) -> bool:
    text = norm(text)
    return bool(extract_qname(text) or QNAME_RE.search(text))


def nearest_text(values: Iterable[str]) -> str:
    for value in values:
        text = norm(value)
        if (
            text
            and not ROW_RE.fullmatch(text)
            and not COL_RE.fullmatch(text)
            and not CODE_RE.fullmatch(text)
            and text.lower() != "metrics"
            and not is_qname(text)
        ):
            return text[:500]
    return ""
