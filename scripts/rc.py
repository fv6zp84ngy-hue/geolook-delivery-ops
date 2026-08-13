"""Release Candidate initialization and deterministic offline Demo helpers.

The helper intentionally does not fetch a site or call an AI service. It creates
the same local project shape used by the delivery CLI, so a new operator can
learn the delivery flow without sending data to an external service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from version import PRODUCT_STAGE, RELEASE_LABEL, SCHEMA_VERSION, get_distribution_version


RC_VERSION = get_distribution_version()
DEFAULT_SLUG = "geo-delivery-demo"


def _slug(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("slug must contain only letters, numbers, '-' or '_'")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _project(root: str | Path, slug: str) -> Path:
    root = Path(root).expanduser().resolve()
    project = (root / "work" / _slug(slug)).resolve()
    if project.parent != (root / "work").resolve():
        raise ValueError("project path escapes work/")
    return project


def init_project(root: str | Path = ".", slug: str = DEFAULT_SLUG, *, industry: str = "saas", force: bool = False) -> dict:
    """Create a blank local project; never overwrites without ``force``."""
    project = _project(root, slug)
    if project.exists() and any(project.iterdir()) and not force:
        raise FileExistsError(f"project already exists: {project}")
    project.mkdir(parents=True, exist_ok=True)
    for name in ("assets", "verify", "deliverables", "delivery/events", "delivery/snapshots", "delivery/manifests", "imports"):
        (project / name).mkdir(parents=True, exist_ok=True)
    cfg = {
        "schema_version": SCHEMA_VERSION,
        "distribution_version": RC_VERSION,
        "product_stage": PRODUCT_STAGE,
        "release_label": RELEASE_LABEL,
        "feature_flags": {"delivery": True, "industry_templates": True, "business_signals": True, "external_integrations": True},
        "migration_history": [],
        "delivery": {
            "enabled": True,
            "mode": PRODUCT_STAGE,
            "product_stage": PRODUCT_STAGE,
            "industry": industry if industry in {"saas", "ai", "smart_hardware", "other"} else "other",
            "current_cycle_id": None,
            "target_markets": [],
            "product_lines": [],
            "conversion_goal": "demo",
            "policy": {"max_cycle_tasks": 12, "require_scope_approval": True, "require_deployment_evidence": True},
        },
    }
    _write_json(project / "geo.json", cfg)
    _write_json(project / "tasks.json", {"tasks": []})
    return {"ok": True, "slug": _slug(slug), "path": str(project), "created": True}


def demo_project(root: str | Path = ".", slug: str = DEFAULT_SLUG, *, industry: str = "saas", force: bool = False) -> dict:
    """Create a fixed, offline Demo project with a replayable evidence chain."""
    result = init_project(root, slug, industry=industry, force=force)
    project = Path(result["path"])
    cycle_id = "rc-demo-20260804"
    cfg = json.loads((project / "geo.json").read_text(encoding="utf-8"))
    cfg["delivery"].update({
        "current_cycle_id": cycle_id,
        "current_baseline_ref": f"delivery/snapshots/{cycle_id}/baseline.json",
        "target_markets": ["US"],
        "product_lines": ["AI Support"],
        "conversion_goal": "demo",
    })
    _write_json(project / "geo.json", cfg)
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "captured_at": "2026-08-04T00:00:00+00:00",
        "source": "offline_rc_demo",
        "audit": {"pages": [{"url": "https://demo.invalid/product", "status": 200}]},
        "samples": [{"prompt": "What is Demo Support?", "answer": "A support workspace for SaaS teams."}],
    }
    _write_json(project / "delivery" / "snapshots" / cycle_id / "baseline.json", baseline)
    asset = project / "assets" / "definition-demo.md"
    asset.write_text("# Demo Support\n\nA fictional support workspace for this offline demo.\n", encoding="utf-8")
    task = {
        "id": "T-RC-001",
        "title": "Clarify the product category definition",
        "owner": "content_owner",
        "action": "Publish the approved definition block on the product page.",
        "status": "todo",
        "acceptance": {"required": [{"checker": "asset.exists", "path": "assets/definition-demo.md"}]},
        "delivery": {
            "cycle_id": cycle_id,
            "stage": "asset_ready",
            "source_refs": [{"type": "page", "ref": "https://demo.invalid/product", "note": "offline fixture"}],
            "scope_decision": {"status": "approved", "decided_by": "demo_operator", "reason": "fixed RC walkthrough"},
            "assignment": {"status": "confirmed", "owner_role": "content_owner", "action": "Publish definition block."},
            "assets": [{"path": "assets/definition-demo.md", "version": "1.0", "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(), "approval": {"status": "approved"}}],
            "approvals": [{"role": "content_owner", "status": "approved", "note": "offline fixture"}],
            "deployments": [],
            "verification": {"required": [{"checker": "asset.exists", "status": "pass"}], "can_close": False},
        },
    }
    _write_json(project / "tasks.json", {"tasks": [task]})
    events = project / "delivery" / "events" / f"{cycle_id}.jsonl"
    events.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in [
        {"event_id": "evt-rc-001", "type": "cycle_created", "cycle_id": cycle_id, "at": baseline["captured_at"]},
        {"event_id": "evt-rc-002", "type": "baseline_locked", "cycle_id": cycle_id, "at": baseline["captured_at"]},
        {"event_id": "evt-rc-003", "type": "scope_approved", "cycle_id": cycle_id, "task_id": "T-RC-001", "at": baseline["captured_at"]},
    ]) + "\n", encoding="utf-8")
    _write_json(project / "demo-summary.json", {
        "distribution_version": RC_VERSION, "product_stage": PRODUCT_STAGE,
        "slug": _slug(slug), "cycle_id": cycle_id,
        "next_steps": ["bash serve_public_demo.sh",
                       "python3 scripts/rc.py verify-demo --root /tmp/geo-delivery-demo"],
        "observational_boundary": "All URLs and answers are fictional offline fixtures.",
    })
    result.update({"cycle_id": cycle_id, "task_id": "T-RC-001", "next": "run verify-demo"})
    return result


def verify_demo(root: str | Path = ".", slug: str = DEFAULT_SLUG) -> dict:
    project = _project(root, slug)
    required = [project / "geo.json", project / "tasks.json", project / "demo-summary.json",
                project / "delivery" / "snapshots" / "rc-demo-20260804" / "baseline.json"]
    missing = [str(path) for path in required if not path.is_file()]
    return {"ok": not missing, "slug": _slug(slug), "missing": missing, "project": str(project)}


def wizard(root: str | Path = ".", slug: str = "", industry: str = "") -> dict:
    """Small guided initializer for operators who do not know the JSON schema."""
    slug = slug.strip() or input("Project slug [my-project]: ").strip() or "my-project"
    industry = industry.strip() or input("Industry (saas/ai/smart_hardware/other) [saas]: ").strip() or "saas"
    return init_project(root, slug, industry=industry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GEO Delivery Ops Release Candidate helper")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("demo", help="create the offline RC demo")
    command.add_argument("--root", default=".")
    command.add_argument("--slug", default=DEFAULT_SLUG)
    command.add_argument("--industry", default="saas", choices=["saas", "ai", "smart_hardware", "other"])
    command.add_argument("--force", action="store_true")
    check = sub.add_parser("verify-demo", help="verify deterministic demo files")
    check.add_argument("--root", default=".")
    check.add_argument("--slug", default=DEFAULT_SLUG)
    args = parser.parse_args(argv)
    if args.command == "demo":
        result = demo_project(args.root, args.slug, industry=args.industry, force=args.force)
    else:
        result = verify_demo(args.root, args.slug)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
