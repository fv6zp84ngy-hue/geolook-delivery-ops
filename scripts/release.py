"""Public Alpha RC release, migration, backup and safety helpers.

This module deliberately uses only the Python standard library.  It keeps
release metadata separate from the delivery schema so legacy projects
can still be read by the Phase 1--11 compatibility layer before an explicit
migration writes anything.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


from version import PRODUCT_STAGE, RELEASE_LABEL, SCHEMA_VERSION as PROJECT_SCHEMA_VERSION, get_distribution_version


RC_VERSION = get_distribution_version()
RELEASE_CHANNEL = "public-alpha"
MIGRATION_ID = "public-alpha-rc2-schema2"
DEFAULT_FEATURE_FLAGS = {
    "delivery": True,
    "industry_templates": True,
    "business_signals": True,
    "external_integrations": True,
}
CAPACITY_LIMITS = {
    "max_pages": 500,
    "max_tasks": 200,
    "max_events": 100_000,
    "max_assets": 1_000,
    "max_csv_rows": 100_000,
    "max_report_bytes": 8 * 1024 * 1024,
    "max_jsonl_bytes": 64 * 1024 * 1024,
}
SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|webhook[_-]?key)")
SENSITIVE_TEXT_RE = re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s,;]+")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def version_info() -> dict:
    return {
        "distribution_version": RC_VERSION,
        "schema_version": PROJECT_SCHEMA_VERSION,
        "product_stage": PRODUCT_STAGE,
        "release_label": RELEASE_LABEL,
        "release_channel": RELEASE_CHANNEL,
        "migration_id": MIGRATION_ID,
        "feature_flags": copy.deepcopy(DEFAULT_FEATURE_FLAGS),
    }


def ensure_project_metadata(config: dict, *, applied: bool = False, at: str | None = None) -> dict:
    """Return a metadata-normalized copy; reads never append migration history."""
    out = copy.deepcopy(config) if isinstance(config, dict) else {}
    out["schema_version"] = PROJECT_SCHEMA_VERSION
    out["distribution_version"] = RC_VERSION
    out["product_stage"] = PRODUCT_STAGE
    out.pop("app_" + "version", None)
    flags = out.get("feature_flags") if isinstance(out.get("feature_flags"), dict) else {}
    out["feature_flags"] = {**DEFAULT_FEATURE_FLAGS, **flags}
    history = out.get("migration_history")
    if not isinstance(history, list):
        history = []
    if isinstance(out.get("migrations"), list) and not history:
        history = copy.deepcopy(out["migrations"])
    if applied and not any(isinstance(row, dict) and row.get("id") == MIGRATION_ID for row in history):
        history.append({
            "id": MIGRATION_ID,
            "at": at or now_iso(),
            "from_schema": str(config.get("schema_version") or "legacy"),
            "to_schema": PROJECT_SCHEMA_VERSION,
            "status": "applied",
        })
    out["migration_history"] = history
    # `migrations` was used by an early alpha draft; keep it as a read-compatible alias.
    out["migrations"] = copy.deepcopy(history)
    delivery = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    delivery["mode"] = "public_alpha"
    delivery["product_stage"] = PRODUCT_STAGE
    delivery["schema_version"] = PROJECT_SCHEMA_VERSION
    out["delivery"] = delivery
    return out


def _safe_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("slug 格式非法")
    return value


def _atomic_json(path: Path, value: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _project(slug: str):
    import geolib as G
    return G, G.project_dir(_safe_slug(slug))


def _load_project(slug: str) -> tuple[dict, dict, object, Path]:
    G, pdir = _project(slug)
    cfg = G.load_config(slug)
    import tasks as T
    data = T.load(slug)
    return (cfg if isinstance(cfg, dict) else {}, data if isinstance(data, dict) else {}, G, pdir)


def migrate_project(slug: str, *, dry_run: bool = False) -> dict:
    """Upgrade a project idempotently, with a recoverable pre-write backup."""
    _safe_slug(slug)
    cfg, data, G, pdir = _load_project(slug)
    import delivery
    delivery_cfg = delivery.normalize_project_config(cfg)
    cycle_id = delivery_cfg.get("delivery", {}).get("current_cycle_id")
    normalized_tasks = delivery.normalize_tasks_data(data, cycle_id=cycle_id)
    metadata = ensure_project_metadata(cfg, applied=True)
    metadata["delivery"] = delivery_cfg.get("delivery", {})
    changes = {
        "config_metadata": metadata != cfg,
        "tasks_normalized": normalized_tasks != data,
        "task_count": len(normalized_tasks.get("tasks", [])),
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "slug": slug, "changes": changes,
                "schema_version": metadata["schema_version"], "product_stage": PRODUCT_STAGE}
    with G.project_lock(slug):
        # Back up the exact pre-migration JSON before any atomic replace.
        backup_id = f"migration-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        backup_dir = pdir / "backups" / backup_id
        backup_dir.mkdir(parents=True, exist_ok=True)
        for name in ("geo.json", "tasks.json"):
            source = pdir / name
            if source.exists():
                shutil.copy2(source, backup_dir / name)
        _atomic_json(backup_dir / "manifest.json", {"backup_id": backup_id, "reason": MIGRATION_ID, "created_at": now_iso()})
        G.save_config(slug, metadata)
        import tasks as T
        T.save(slug, normalized_tasks)
    return {"ok": True, "dry_run": False, "slug": slug, "backup_id": backup_id,
            "changes": changes, "schema_version": PROJECT_SCHEMA_VERSION,
            "product_stage": PRODUCT_STAGE, "distribution_version": RC_VERSION}


def rollback_project(slug: str, backup_id: str) -> dict:
    """Restore only a named migration backup; never accepts arbitrary paths."""
    _safe_slug(slug)
    if not re.fullmatch(r"migration-[0-9TZ-]{10,40}", str(backup_id or "")):
        raise ValueError("backup_id 格式非法")
    G, pdir = _project(slug)
    backup_dir = (pdir / "backups" / backup_id).resolve()
    if backup_dir.parent != (pdir / "backups").resolve() or not backup_dir.is_dir():
        raise FileNotFoundError("找不到迁移备份")
    for name in ("geo.json", "tasks.json"):
        source = backup_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"迁移备份缺少 {name}")
    with G.project_lock(slug):
        for name in ("geo.json", "tasks.json"):
            target = pdir / name
            payload = json.loads((backup_dir / name).read_text(encoding="utf-8"))
            _atomic_json(target, payload)
    return {"ok": True, "slug": slug, "backup_id": backup_id, "rolled_back": ["geo.json", "tasks.json"]}


def validate_external_url(value: str) -> str:
    """Reject non-web, local/private and credential-bearing URLs before fetching."""
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("外部 URL 必须是无凭证的 http(s) 地址")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("拒绝访问本机 URL")
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise ValueError("拒绝访问私有或保留地址")
    except ValueError as exc:
        if str(exc).startswith("拒绝访问"):
            raise
    return text


def _safe_rel(name: str) -> bool:
    path = Path(str(name).replace("\\", "/"))
    return not path.is_absolute() and ".." not in path.parts and "\x00" not in str(name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capacity_audit(project_dir: str | Path) -> dict:
    root = Path(project_dir).resolve()
    result = {"limits": copy.deepcopy(CAPACITY_LIMITS), "counts": {}, "checks": []}
    def check(name: str, value: int, limit: int):
        result["counts"][name] = value
        result["checks"].append({"name": name, "value": value, "limit": limit,
                                  "status": "FAIL" if value > limit else "PASS"})
    tasks_path = root / "tasks.json"
    task_count = 0
    if tasks_path.is_file():
        try:
            task_count = len(json.loads(tasks_path.read_text(encoding="utf-8")).get("tasks", []))
        except (OSError, ValueError, AttributeError):
            pass
    check("max_tasks", task_count, CAPACITY_LIMITS["max_tasks"])
    audit = root / "audit.json"
    page_count = 0
    if audit.is_file():
        try:
            raw = json.loads(audit.read_text(encoding="utf-8"))
            page_count = len(raw.get("pages", raw.get("results", []))) if isinstance(raw, dict) else 0
        except (OSError, ValueError):
            pass
    check("max_pages", page_count, CAPACITY_LIMITS["max_pages"])
    assets = [p for p in (root / "assets").rglob("*") if p.is_file()] if (root / "assets").exists() else []
    check("max_assets", len(assets), CAPACITY_LIMITS["max_assets"])
    events = list((root / "delivery" / "events").glob("*.jsonl")) if (root / "delivery" / "events").exists() else []
    event_lines = 0
    for path in events:
        try:
            event_lines += sum(1 for _ in path.open("r", encoding="utf-8"))
        except OSError:
            pass
    check("max_events", event_lines, CAPACITY_LIMITS["max_events"])
    signal_paths = list((root / "imports").rglob("*.csv")) if (root / "imports").exists() else []
    csv_rows = 0
    for path in signal_paths:
        try:
            csv_rows += max(0, sum(1 for _ in path.open("r", encoding="utf-8", errors="replace")) - 1)
        except OSError:
            pass
    check("max_csv_rows", csv_rows, CAPACITY_LIMITS["max_csv_rows"])
    jsonl_bytes = sum(p.stat().st_size for p in root.rglob("*.jsonl") if p.is_file())
    check("max_jsonl_bytes", jsonl_bytes, CAPACITY_LIMITS["max_jsonl_bytes"])
    report_bytes = sum(p.stat().st_size for p in (root / "deliverables").glob("*") if p.is_file()) if (root / "deliverables").exists() else 0
    check("max_report_bytes", report_bytes, CAPACITY_LIMITS["max_report_bytes"])
    result["ok"] = all(row["status"] == "PASS" for row in result["checks"])
    return result


def security_audit(project_dir: str | Path) -> dict:
    root = Path(project_dir).resolve()
    findings = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".html", ".md", ".txt", ".csv"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        if not _safe_rel(rel):
            findings.append({"severity": "FAIL", "code": "path_escape", "path": rel})
        if SENSITIVE_TEXT_RE.search(text) and ".env" not in rel:
            findings.append({"severity": "FAIL", "code": "secret_like_text", "path": rel})
        if "/Users/" in text or "/home/" in text or re.search(r"[A-Za-z]:\\Users\\", text):
            findings.append({"severity": "FAIL", "code": "absolute_path", "path": rel})
    return {"ok": not any(row["severity"] == "FAIL" for row in findings), "findings": findings}


def doctor_project(slug: str) -> dict:
    cfg, data, _G, pdir = _load_project(slug)
    checks = []
    meta_ok = (
        cfg.get("schema_version") == PROJECT_SCHEMA_VERSION
        and cfg.get("product_stage") == PRODUCT_STAGE
        and cfg.get("distribution_version") == RC_VERSION
        and isinstance(cfg.get("feature_flags"), dict)
    )
    checks.append({"name": "project_metadata", "status": "PASS" if meta_ok else "WARN", "message": "配置元数据完整" if meta_ok else "需要执行 migrate"})
    try:
        import delivery
        delivery_report = delivery.delivery_doctor(slug)
        for row in delivery_report.get("checks", []):
            item = dict(row)
            item["status"] = item.get("status") or item.get("level") or "WARN"
            item["name"] = item.get("name") or item.get("code") or "delivery_check"
            item["message"] = item.get("message") or item.get("detail") or ""
            checks.append(item)
    except Exception as exc:  # doctor must report, never hide a malformed project
        checks.append({"name": "delivery_doctor", "status": "FAIL", "message": f"{type(exc).__name__}: {exc}"})
    cap = capacity_audit(pdir)
    checks.extend({"name": f"capacity.{row['name']}", "status": row["status"], "message": f"{row['value']} / {row['limit']}"} for row in cap["checks"])
    sec = security_audit(pdir)
    checks.extend({"name": f"security.{row['code']}", "status": row["severity"], "message": row["path"]} for row in sec["findings"])
    return {"ok": not any(row.get("status") == "FAIL" for row in checks), "slug": slug, "checks": checks,
            "capacity": cap, "security": sec, "version": version_info()}


def doctor_all(*, slug: str | None = None) -> dict:
    import geolib as G
    slugs = [_safe_slug(slug)] if slug else sorted(p.name for p in Path(G.WORK).iterdir() if p.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", p.name))
    projects = []
    for item in slugs:
        try:
            projects.append(doctor_project(item))
        except Exception as exc:
            projects.append({"ok": False, "slug": item, "checks": [{"name": "doctor_runtime", "status": "FAIL", "message": f"{type(exc).__name__}: {exc}"}]})
    return {"ok": all(row["ok"] for row in projects), "projects": projects, "version": version_info()}


def format_doctor(report: dict) -> str:
    rows = []
    for project in report.get("projects", [report]):
        rows.append(f"Project {project.get('slug', '')}")
        rows.extend(f"{row.get('status', 'WARN'):5}  {row.get('name', '')} {row.get('message', '')}" for row in project.get("checks", []))
    return "\n".join(rows) + ("\nFAIL doctor\n" if not report.get("ok") else "\nPASS doctor\n")


def export_backup(slug: str, target: str | Path | None = None) -> dict:
    _safe_slug(slug)
    _G, root = _project(slug)
    if target is None:
        target_path = root / "backups" / f"{slug}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    else:
        target_path = Path(target).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    included = []
    excluded = []
    for path in root.rglob("*"):
        if not path.is_file() or path == target_path or ".lock" in path.name or path.name == ".env" or "__pycache__" in path.parts:
            if path.is_file() and (path.name == ".env" or path == target_path):
                excluded.append(path.relative_to(root).as_posix())
            continue
        rel = path.relative_to(root).as_posix()
        if not _safe_rel(rel) or rel.startswith("backups/"):
            excluded.append(rel)
            continue
        included.append(path)
    manifest = {"distribution_version": RC_VERSION,
                "schema_version": PROJECT_SCHEMA_VERSION, "product_stage": PRODUCT_STAGE,
                "slug": slug,
                "created_at": now_iso(), "files": []}
    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in included:
            rel = path.relative_to(root).as_posix()
            archive.write(path, rel)
            manifest["files"].append({"path": rel, "size": path.stat().st_size, "sha256": _sha256(path)})
        archive.writestr("BACKUP_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return {"ok": True, "path": str(target_path), "file_count": len(included), "excluded": excluded, "manifest": manifest}
