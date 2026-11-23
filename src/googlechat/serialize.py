"""Turn library results into compact JSON (shared by the CLI and the MCP server)."""

import dataclasses
import json
from datetime import datetime
from typing import Any

from .types import Attachment, DriveFile, SharedItem


def to_jsonable(obj: Any) -> Any:
    """Dataclasses -> dicts without empty fields; datetimes -> ISO 8601 strings."""
    if isinstance(obj, SharedItem):
        kind = ("drive_file" if isinstance(obj.item, DriveFile)
                else "attachment" if isinstance(obj.item, Attachment) else "link")
        rest = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj) if f.name != "item"}
        return {"type": kind, **to_jsonable(obj.item), **to_jsonable(rest)}
    if dataclasses.is_dataclass(obj):
        obj = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items() if v not in (None, [], "")}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def to_json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False)


def status_report(statuses: dict) -> dict:
    """get_status result -> {"statuses": [...], "no_status": [ids Google didn't answer for]}."""
    return {
        "statuses": [s for s in statuses.values() if s is not None],
        "no_status": [uid for uid, s in statuses.items() if s is None],
    }
