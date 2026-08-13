"""Explicit CSV business-signal imports and observational summaries.

This module intentionally stays below attribution: it records what an imported
source says, maps only on explicit page/deployment evidence, and never claims
that GEO caused a conversion or revenue outcome.
"""

from __future__ import annotations

import csv
import copy
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


SOURCE_TYPES = ("ga4", "gsc", "crm", "forms", "sales_manual", "sales_ai_report")
CONFIDENCE_VALUES = ("observed", "reported", "self_reported", "unknown")
_AI_WORDS = ("chatgpt", "openai", "perplexity", "claude", "gemini", "copilot", "ai")
_PRIVATE_HEADER = re.compile(r"(?:email|e-mail|phone|mobile|contact|first.?name|last.?name|full.?name|person)", re.I)


def _header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _clean(value: object) -> str:
    return str(value or "").strip()


def _headers(rows_or_headers: object) -> list[str]:
    if isinstance(rows_or_headers, dict):
        return [_header(x) for x in rows_or_headers]
    if isinstance(rows_or_headers, (list, tuple)):
        if not rows_or_headers:
            return []
        if isinstance(rows_or_headers[0], dict):
            return [_header(x) for x in rows_or_headers[0]]
        return [_header(x) for x in rows_or_headers]
    return []


def detect_csv_type(rows_or_headers: object, hint: str | None = None) -> str:
    """Return the most likely supported source type.

    A caller-provided type wins after validation. Detection is intentionally
    conservative and only uses column names; it does not inspect people or
    infer a CRM from free-form content.
    """
    if hint:
        value = _clean(hint).lower().replace("-", "_")
        aliases = {"search_console": "gsc", "hubspot": "crm", "pipedrive": "crm",
                   "zoho": "crm", "sales": "sales_manual", "ai_sales": "sales_ai_report"}
        value = aliases.get(value, value)
        if value not in SOURCE_TYPES:
            raise ValueError(f"不支持的 CSV 类型：{hint}")
        return value
    hs = set(_headers(rows_or_headers))
    if {"impressions", "clicks"} & hs and ({"query", "queries", "search_query"} & hs):
        return "gsc"
    if {"sessions", "users", "session"} & hs and ({"page_path", "landing_page", "source_medium", "medium"} & hs):
        return "ga4"
    if {"deal_stage", "lifecycle_stage", "lead_status", "pipeline"} & hs or {"lead_source", "contact_source"} & hs:
        return "crm"
    if {"form_name", "form_id", "submission_id", "submitted_at", "form_submission"} & hs:
        return "forms"
    if {"ai_source", "heard_from_ai", "ai_mentioned", "self_reported_ai"} & hs:
        return "sales_ai_report"
    if {"note", "sales_note", "rep_note", "seller"} & hs:
        return "sales_manual"
    raise ValueError("无法根据列名识别 CSV 类型，请显式指定 --type")


def _pick(row: dict, *names: str) -> str:
    normalized = {_header(k): _clean(v) for k, v in row.items()}
    for name in names:
        value = normalized.get(_header(name), "")
        if value:
            return value
    return ""


def _parse_date(value: str) -> str | None:
    text = _clean(value)
    if not text:
        return None
    text = text.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _count(row: dict) -> float | int:
    raw = _pick(row, "count", "sessions", "users", "clicks", "conversions", "leads",
                "submissions", "value", "records")
    if not raw:
        return 1
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return 1
    if value < 0:
        return value
    return int(value) if value.is_integer() else round(value, 6)


def _source_channel(source_type: str, row: dict) -> tuple[str, str]:
    source = _pick(row, "channel", "source", "medium", "lead_source", "origin", "ai_source")
    low = source.lower()
    platform = _pick(row, "platform", "source_platform", "network", "source")
    if source_type == "gsc":
        return "organic_search", "google_search_console"
    if any(word in low for word in _AI_WORDS):
        return "ai_referral", platform or source or "ai_platform"
    if source_type == "ga4":
        return ("organic_search" if "organic" in low else "referral"), platform or source or "ga4"
    if source_type == "crm":
        return "lead", platform or source or "crm"
    if source_type == "forms":
        return "form", platform or source or "website_form"
    if source_type == "sales_ai_report":
        return "ai_self_report", platform or source or "sales"
    return "sales_note", platform or source or "sales"


def validate_rows(rows: list[dict] | None, source_type: str | None = None) -> dict:
    """Validate rows without retaining or displaying contact-level fields."""
    rows = rows if isinstance(rows, list) else []
    source_type = detect_csv_type(rows, source_type) if source_type else detect_csv_type(rows)
    errors: list[dict] = []
    valid: list[dict] = []
    for number, row in enumerate(rows, 2):
        if not isinstance(row, dict):
            errors.append({"row": number, "code": "not_object", "message": "CSV 行不是对象"})
            continue
        if not _parse_date(_pick(row, "date", "day", "created_at", "submitted_at", "occurred_at", "timestamp")):
            errors.append({"row": number, "code": "date_required", "message": "缺少可识别的 date"})
            continue
        count = _count(row)
        if count < 0:
            errors.append({"row": number, "code": "count_invalid", "message": "count 不能为负数"})
            continue
        valid.append(row)
    return {
        "source_type": source_type,
        "valid_rows": valid,
        "errors": errors,
        "accepted_count": len(valid),
        "rejected_count": len(errors),
        "private_columns_ignored": sorted({key for row in rows for key in row if _PRIVATE_HEADER.search(str(key))}),
    }


def normalize_signals(rows: list[dict] | None, source_type: str, source_name: str = "") -> list[dict]:
    """Convert validated rows into the small, stable observation model."""
    source_type = detect_csv_type(rows or [], source_type)
    out = []
    confidence = "self_reported" if source_type == "sales_ai_report" else (
        "reported" if source_type == "sales_manual" else "observed"
    )
    for number, row in enumerate(rows if isinstance(rows, list) else [], 2):
        when = _parse_date(_pick(row, "date", "day", "created_at", "submitted_at", "occurred_at", "timestamp"))
        if not when:
            continue
        channel, platform = _source_channel(source_type, row)
        landing_page = _pick(row, "landing_page", "page_path", "page", "url", "target_url", "path")
        parsed = urlparse(landing_page)
        if parsed.path:
            landing_page = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        conversion = _pick(row, "conversion_type", "conversion", "event_name", "form_name", "deal_stage", "lead_status")
        if not conversion:
            conversion = "demo_request" if source_type in {"crm", "forms", "sales_ai_report"} else "traffic"
        signal = {
            "signal_id": f"sig-{source_type}-{number:05d}",
            "date": when,
            "source_type": source_type,
            "channel": channel,
            "platform": platform,
            "landing_page": landing_page,
            "conversion_type": conversion,
            "count": _count(row),
            "market": _pick(row, "market", "country", "region") or "unknown",
            "product_line": _pick(row, "product_line", "product", "product_name"),
            "confidence": confidence,
            "source_file": Path(source_name).name if source_name else "",
            "source_row": number,
        }
        out.append(signal)
    return out


def _path(value: object) -> str:
    text = _clean(value)
    parsed = urlparse(text)
    return (parsed.path or text or "/").rstrip("/") or "/"


def map_signals_to_tasks(signals: list[dict] | None, tasks: list[dict] | None, cfg: dict | None = None) -> list[dict]:
    """Bind by explicit target-page/deployment path; otherwise keep project scope."""
    tasks = tasks if isinstance(tasks, list) else []
    rows = []
    index: list[tuple[str, str, set[str]]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
        paths = set()
        for value in assignment.get("target_pages", []) or []:
            paths.add(_path(value))
        for value in delivery.get("target_pages", []) or []:
            paths.add(_path(value))
        for value in (task.get("target_urls", []) or []) + (assignment.get("target_urls", []) or []):
            paths.add(_path(value))
        for value in (task.get("target_url"), delivery.get("target_url")):
            if value:
                paths.add(_path(value))
        for deployment in delivery.get("deployments", []) or []:
            if isinstance(deployment, dict):
                paths.add(_path(deployment.get("target_url")))
        for asset in delivery.get("assets", []) or []:
            if isinstance(asset, dict):
                for value in asset.get("target_urls", []) or []:
                    paths.add(_path(value))
        index.append((str(task.get("id") or ""), str(task.get("product_line") or delivery.get("product_line") or ""), paths))
    for signal in signals if isinstance(signals, list) else []:
        row = copy.deepcopy(signal)
        landing = _path(row.get("landing_page"))
        candidates = [item for item in index if landing in item[2] and landing != "/"]
        if not candidates and landing and landing != "/":
            candidates = [item for item in index if any(path != "/" and (path.startswith(landing) or landing.startswith(path)) for path in item[2])]
        if len(candidates) == 1:
            task_id, product_line, paths = candidates[0]
            row["mapping"] = {"scope": "task", "task_id": task_id, "confidence": "explicit_page", "reason": "landing_page 命中目标页面或部署 URL"}
            row["task_id"] = task_id
            if not row.get("product_line"):
                row["product_line"] = product_line
        else:
            row["mapping"] = {"scope": "project", "task_id": None, "confidence": "unmapped", "reason": "没有唯一的目标页面/部署记录匹配，保留为项目级信号"}
            row["task_id"] = None
        rows.append(row)
    return rows


def build_signal_summary(signals: list[dict] | None) -> dict:
    rows = signals if isinstance(signals, list) else []
    by_channel = Counter(str(row.get("channel") or "unknown") for row in rows)
    by_conversion = Counter(str(row.get("conversion_type") or "unknown") for row in rows)
    by_source = Counter(str(row.get("source_type") or "unknown") for row in rows)
    mapped = [row for row in rows if (row.get("mapping") or {}).get("scope") == "task"]
    total_count = sum(float(row.get("count") or 0) for row in rows)
    def compact(values):
        return int(values) if float(values).is_integer() else round(values, 6)
    return {
        "signal_rows": len(rows),
        "observed_count": compact(total_count),
        "ai_referral_sessions": compact(sum(float(row.get("count") or 0) for row in rows if row.get("channel") == "ai_referral" and row.get("conversion_type") in {"traffic", "session", "sessions"})),
        "ai_referral_count": compact(sum(float(row.get("count") or 0) for row in rows if row.get("channel") in {"ai_referral", "ai_self_report"})),
        "form_submissions": compact(sum(float(row.get("count") or 0) for row in rows if row.get("conversion_type") in {"form_submission", "form_submit", "submission"} or row.get("channel") == "form")),
        "demo_rfq": compact(sum(float(row.get("count") or 0) for row in rows if any(token in str(row.get("conversion_type") or "").lower() for token in ("demo", "rfq", "quote")))),
        "sales_ai_self_report": compact(sum(float(row.get("count") or 0) for row in rows if row.get("channel") == "ai_self_report")),
        "by_channel": dict(sorted(by_channel.items())),
        "by_conversion_type": dict(sorted(by_conversion.items())),
        "by_source_type": dict(sorted(by_source.items())),
        "mapped_rows": len(mapped),
        "unmapped_rows": len(rows) - len(mapped),
        "mapping_rate": round(len(mapped) / len(rows), 4) if rows else 0,
        "data_completeness": {
            "date": round(sum(bool(row.get("date")) for row in rows) / len(rows), 4) if rows else 0,
            "landing_page": round(sum(bool(row.get("landing_page")) for row in rows) / len(rows), 4) if rows else 0,
            "market": round(sum(row.get("market") not in {None, "", "unknown"} for row in rows) / len(rows), 4) if rows else 0,
            "product_line": round(sum(bool(row.get("product_line")) for row in rows) / len(rows), 4) if rows else 0,
        },
        "observational_boundary": "这些是用户显式导入的观察信号；没有收入归因，也不证明 GEO 导致转化。",
    }


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("signal_id"):
            rows.append(value)
    return rows


def read_signal_log(path: str | Path) -> list[dict]:
    """Read a signal log while tolerating one damaged JSONL line."""
    return _read_log(Path(path))


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def import_csv(slug: str, file_path: str | Path, source_type: str | None = None,
               file_name: str = "") -> dict:
    """Import one explicitly selected CSV into a project under its lock."""
    import geolib as G
    import tasks as T

    source = Path(file_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(str(file_path))
    raw = source.read_bytes()
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("CSV 文件超过 20 MB 限制")
    import_id = hashlib.sha256(raw).hexdigest()[:16]
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    detected = detect_csv_type(rows, source_type)
    checked = validate_rows(rows, detected)
    pdir = G.project_dir(slug)
    with G.project_lock(slug):
        log_path = pdir / "business_signals.jsonl"
        existing = _read_log(log_path)
        already = [row for row in existing if row.get("import_id") == import_id]
        if already:
            return {
                "ok": True, "already_imported": True, "import_id": import_id,
                "source_type": detected, "summary": build_signal_summary(existing),
                "errors": checked["errors"], "signals": already,
            }
        normalized = normalize_signals(checked["valid_rows"], detected, file_name or source.name)
        cfg = G.load_config(slug)
        task_data = T.load(slug)
        mapped = map_signals_to_tasks(normalized, task_data.get("tasks", []), cfg)
        for row in mapped:
            row["import_id"] = import_id
        import_dir = pdir / "imports" / detected
        import_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(file_name or source.name).name)[:100] or "import.csv"
        stored = import_dir / f"{import_id}-{safe_name}"
        shutil.copyfile(source, stored)
        all_rows = existing + mapped
        _atomic_jsonl(log_path, all_rows)
        report = {
            "import_id": import_id, "source_type": detected, "source_file": stored.relative_to(pdir).as_posix(),
            "accepted_count": checked["accepted_count"], "rejected_count": checked["rejected_count"],
            "errors": checked["errors"], "private_columns_ignored": checked["private_columns_ignored"],
            "mapping": {"mapped": sum(1 for row in mapped if row.get("task_id")), "unmapped": sum(1 for row in mapped if not row.get("task_id"))},
            "summary": build_signal_summary(all_rows),
            "signals": mapped,
            "observational_boundary": "导入记录仅表示观察信号；不可解释为收入归因或 GEO 因果结论。",
        }
        report_path = import_dir / f"{import_id}-mapping.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def signal_view(slug: str) -> dict:
    import geolib as G
    import tasks as T
    pdir = G.project_dir(slug)
    rows = _read_log(pdir / "business_signals.jsonl")
    cfg = G.load_config(slug)
    data = T.load(slug)
    mapped = map_signals_to_tasks(rows, data.get("tasks", []), cfg)
    return {"summary": build_signal_summary(mapped), "signals": mapped[-200:],
            "imports": sorted((p.name for p in (pdir / "imports").glob("*") if p.is_dir()), reverse=True)
            if (pdir / "imports").exists() else [],
            "source_log": "business_signals.jsonl",
            "observational_boundary": "观察层：记录导入数据与交付页面的同向变化，不提供收入归因。"}
