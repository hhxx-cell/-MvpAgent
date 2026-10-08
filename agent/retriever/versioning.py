"""政策生效日期与冲突裁决。"""

from datetime import date
from typing import Any


def current_version(chunks: list[dict[str, Any]], today: date | None = None) -> tuple[list[dict[str, Any]], bool, bool]:
    today = today or date.today()
    eligible = [chunk for chunk in chunks if date.fromisoformat(str(chunk["effective_date"])) <= today]
    if not eligible:
        return [], False, False
    ordered = sorted(
        eligible, key=lambda item: (str(item["effective_date"]), int(item.get("version", 0))), reverse=True
    )
    top = ordered[0]
    top_date = str(top["effective_date"])
    top_version = int(top.get("version", 0))
    selected = [
        item
        for item in ordered
        if str(item["effective_date"]) == top_date and int(item.get("version", 0)) == top_version
    ]
    ambiguous = len({item["source_file"] for item in selected}) > 1
    had_older = any(
        (str(item["effective_date"]), int(item.get("version", 0))) < (top_date, top_version) for item in ordered
    )
    return selected, had_older, ambiguous
