"""Deterministic one-way exports for delivery execution tools."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


FIELDS = ("task_id", "title", "priority", "owner_role", "stage", "action",
          "target_url", "asset_paths", "acceptance", "next_action", "deadline")
EXPORT_FORMATS = ("csv", "markdown", "json", "github", "notion_csv", "jira_csv")


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(x) for x in value if x not in (None, ""))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "")


def _neutralize_csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution in exported CSV files.

    CSV is intentionally a plain interchange format, but spreadsheet programs
    may evaluate cells beginning with ``=``, ``+``, ``-`` or ``@`` as formulas.
    Prefixing those values with an apostrophe keeps the visible text while
    making the export inert when opened by a spreadsheet application.
    """
    if not isinstance(value, str) or not value:
        return value
    return "'" + value if value[0] in "=+-@" else value


def _target_url(task: dict) -> str:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    deployments = delivery.get("deployments") if isinstance(delivery.get("deployments"), list) else []
    for row in reversed(deployments):
        if isinstance(row, dict) and row.get("target_url"):
            return str(row["target_url"])
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    targets = assignment.get("target_pages") or delivery.get("target_pages") or []
    return _text(targets[0] if isinstance(targets, list) and targets else task.get("target_url"))


def task_row(task: dict) -> dict:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    assets = delivery.get("assets") if isinstance(delivery.get("assets"), list) else []
    checks = (task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {})
    return {
        "task_id": str(task.get("id") or ""),
        "title": str(task.get("title") or ""),
        "priority": str(task.get("priority") or ""),
        "owner_role": str(assignment.get("owner_role") or task.get("owner") or ""),
        "stage": str(delivery.get("stage") or ""),
        "action": str(task.get("action") or ""),
        "target_url": _target_url(task),
        "asset_paths": _text([row.get("path") for row in assets if isinstance(row, dict) and row.get("path")]),
        "acceptance": _text(checks),
        "next_action": str(delivery.get("next_action") or task.get("next_action") or ""),
        "deadline": str(task.get("deadline") or assignment.get("deadline") or delivery.get("deadline") or ""),
    }


def rows(tasks: list[dict] | None) -> list[dict]:
    return [task_row(task) for task in (tasks or []) if isinstance(task, dict)]


def export_csv(tasks: list[dict], *, dialect: str = "excel") -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, dialect=dialect, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: _neutralize_csv_cell(value) for key, value in row.items()}
                     for row in rows(tasks))
    return output.getvalue()


def export_json(tasks: list[dict]) -> str:
    return json.dumps(rows(tasks), ensure_ascii=False, indent=2) + "\n"


def export_markdown(tasks: list[dict]) -> str:
    output = ["# GEO Delivery Tasks", "", "| " + " | ".join(FIELDS) + " |",
              "|" + "|".join("---" for _ in FIELDS) + "|"]
    for row in rows(tasks):
        output.append("| " + " | ".join(str(row[field]).replace("|", "\\|").replace("\n", " ") for field in FIELDS) + " |")
    output += ["", "> This is an execution copy. GeoLook remains the evidence and verification source of truth."]
    return "\n".join(output) + "\n"


def github_issue_payload(task: dict) -> dict:
    row = task_row(task)
    body = "\n".join([
        f"## Action\n{row['action'] or '—'}",
        f"## Acceptance\n{row['acceptance'] or '—'}",
        f"## Delivery context\n- Task: `{row['task_id']}`\n- Stage: `{row['stage']}`\n- Owner: `{row['owner_role']}`\n- Target URL: {row['target_url'] or '—'}\n- Assets: {row['asset_paths'] or '—'}\n- Next action: {row['next_action'] or '—'}\n- Deadline: {row['deadline'] or '—'}",
        "\n> Execution copy from GEO Delivery Ops. Do not use GitHub state to close the internal task.",
    ])
    return {"title": f"[{row['priority'] or 'TASK'}] {row['title']}",
            "body": body, "labels": ["geo-delivery", row["priority"] or "unprioritized"]}


def export_github(tasks: list[dict]) -> str:
    return json.dumps([github_issue_payload(task) for task in tasks if isinstance(task, dict)], ensure_ascii=False, indent=2) + "\n"


def render(tasks: list[dict], fmt: str) -> str:
    fmt = str(fmt or "").lower()
    if fmt in {"csv", "notion_csv", "jira_csv"}:
        return export_csv(tasks)
    if fmt == "markdown":
        return export_markdown(tasks)
    if fmt == "json":
        return export_json(tasks)
    if fmt == "github":
        return export_github(tasks)
    raise ValueError(f"不支持的导出格式：{fmt}")


def project_export(slug: str, fmt: str) -> str:
    import geolib as G
    import tasks as T
    return render(T.load(slug).get("tasks", []), fmt)


def write_project_export(slug: str, fmt: str, path: str | Path | None = None) -> dict:
    import tasks as T
    text = project_export(slug, fmt)
    target = Path(path) if path else Path(f"delivery-{slug}.{ 'md' if fmt == 'markdown' else 'json' if fmt in {'json', 'github'} else 'csv'}")
    target.write_text(text, encoding="utf-8")
    return {"path": str(target), "format": fmt, "task_count": len(T.load(slug).get("tasks", []))}
