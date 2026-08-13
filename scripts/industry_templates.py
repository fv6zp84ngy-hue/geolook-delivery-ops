"""Lightweight, candidate-only industry templates for GEO delivery.

Templates improve the relevance of questions, task suggestions and asset choices.
They never create evidence, approve scope or facts, write approval records, or move a
delivery stage.  The real audit and human gates remain authoritative.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "references" / "industry-templates"
TEMPLATE_FILES = {
    "saas": "saas.json",
    "ai_product": "ai-product.json",
    "smart_hardware": "smart-hardware.json",
}
ALIASES = {
    "saas": "saas",
    "software_as_a_service": "saas",
    "ai": "ai_product",
    "ai_product": "ai_product",
    "ai_software": "ai_product",
    "artificial_intelligence": "ai_product",
    "smart_hardware": "smart_hardware",
    "hardware": "smart_hardware",
    "iot": "smart_hardware",
}
SECTIONS = (
    "required_facts",
    "question_patterns",
    "diagnosis_rules",
    "asset_templates",
    "approval_defaults",
    "acceptance_defaults",
    "risk_expressions",
)
CASE_TYPES = {
    "technical",
    "fact_error",
    "content_gap",
    "external_evidence_gap",
    "result_metric_gap",
    "other",
}
GENERATOR_FAMILIES = {"llms", "jsonld", "snippets", "outlines"}
_AUTHORIZATION_KEYS = {
    "source_refs",
    "scope_decision",
    "approvals",
    "deployments",
    "verification",
    "verification_history",
    "regressions",
    "stage",
}


class TemplateValidationError(ValueError):
    """Raised when a template can authorize work or has broken references."""


def canonical_industry_type(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return ALIASES.get(text)


def _ids(rows: list[dict]) -> set[str]:
    return {str(row.get("id") or "") for row in rows if isinstance(row, dict)}


def _authorization_errors(value: Any, path: str = "template") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}"
            if key in _AUTHORIZATION_KEYS:
                errors.append(f"{here} is an operational field and is forbidden in templates")
            if key == "status" and str(item).lower() in {
                "approved", "confirmed", "done", "deployed", "verified"
            }:
                errors.append(f"{here} must not authorize or complete work")
            errors.extend(_authorization_errors(item, here))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_authorization_errors(item, f"{path}[{index}]"))
    return errors


def validate_template(template: Any) -> dict:
    errors: list[str] = []
    if not isinstance(template, dict):
        return {"valid": False, "errors": ["template must be an object"]}
    for key in ("schema_version", "template_id", "template_version", "industry_type"):
        if not str(template.get(key) or "").strip():
            errors.append(f"missing {key}")
    if template.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if template.get("candidate_only") is not True:
        errors.append("candidate_only must be true")
    if template.get("supported_language") != "en":
        errors.append("supported_language must be en")
    industry = canonical_industry_type(template.get("industry_type"))
    if not industry or industry != template.get("industry_type"):
        errors.append("industry_type must use a supported canonical name")
    families = template.get("generator_families")
    if not isinstance(families, list) or not families:
        errors.append("generator_families must be a non-empty list")
    elif set(families) - GENERATOR_FAMILIES:
        errors.append("generator_families contains an unsupported existing generator")

    section_ids: dict[str, set[str]] = {}
    for section in SECTIONS:
        rows = template.get(section)
        if not isinstance(rows, list) or not rows:
            errors.append(f"{section} must be a non-empty list")
            section_ids[section] = set()
            continue
        ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or any(not item for item in ids):
            errors.append(f"{section} rows must be objects with ids")
        if len(ids) != len(set(ids)):
            errors.append(f"{section} ids must be unique")
        section_ids[section] = set(ids)

    question_ids = section_ids.get("question_patterns", set())
    asset_ids = section_ids.get("asset_templates", set())
    approval_ids = section_ids.get("approval_defaults", set())
    acceptance_ids = section_ids.get("acceptance_defaults", set())
    for row in template.get("diagnosis_rules") or []:
        if not isinstance(row, dict):
            continue
        if row.get("case_type") not in CASE_TYPES:
            errors.append(f"{row.get('id')}: unsupported case_type")
        for ref in row.get("question_pattern_ids") or []:
            if ref not in question_ids:
                errors.append(f"{row.get('id')}: unknown question {ref}")
        for ref in row.get("asset_template_ids") or []:
            if ref not in asset_ids:
                errors.append(f"{row.get('id')}: unknown asset {ref}")
        if not (row.get("required_evidence_types") or []):
            errors.append(f"{row.get('id')}: required_evidence_types must not be empty")
    for row in template.get("question_patterns") or []:
        if not isinstance(row, dict):
            continue
        placeholders = set(re.findall(r"\{([^{}]+)\}", str(row.get("pattern") or "")))
        if placeholders - {"brand", "product_line", "competitor"}:
            errors.append(f"{row.get('id')}: unsupported question placeholder")
    for row in template.get("asset_templates") or []:
        if not isinstance(row, dict):
            continue
        if row.get("generator_family") not in GENERATOR_FAMILIES:
            errors.append(f"{row.get('id')}: unsupported generator_family")
        for ref in row.get("approval_default_ids") or []:
            if ref not in approval_ids:
                errors.append(f"{row.get('id')}: unknown approval default {ref}")
        for ref in row.get("acceptance_default_ids") or []:
            if ref not in acceptance_ids:
                errors.append(f"{row.get('id')}: unknown acceptance default {ref}")
    errors.extend(_authorization_errors(template))
    return {"valid": not errors, "errors": errors}


def load_template(industry_type: Any, template_dir: Path | str | None = None) -> dict:
    canonical = canonical_industry_type(industry_type)
    if canonical not in TEMPLATE_FILES:
        raise TemplateValidationError(f"unsupported industry_type: {industry_type!r}")
    root = Path(template_dir) if template_dir is not None else TEMPLATE_DIR
    path = root / TEMPLATE_FILES[canonical]
    try:
        template = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateValidationError(f"cannot read industry template {path}: {exc}") from exc
    report = validate_template(template)
    if not report["valid"]:
        raise TemplateValidationError("; ".join(report["errors"]))
    return copy.deepcopy(template)


def list_templates(template_dir: Path | str | None = None) -> list[dict]:
    return [
        {
            "industry_type": name,
            "template_id": template["template_id"],
            "template_version": template["template_version"],
            "supported_language": template["supported_language"],
            "candidate_only": True,
        }
        for name in TEMPLATE_FILES
        for template in [load_template(name, template_dir)]
    ]


def infer_industry_type(cfg: dict | None) -> str | None:
    cfg = cfg if isinstance(cfg, dict) else {}
    delivery = cfg.get("delivery") if isinstance(cfg.get("delivery"), dict) else {}
    profile = delivery.get("customer_profile") if isinstance(delivery.get("customer_profile"), dict) else {}
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    for value in (
        delivery.get("industry"),
        delivery.get("industry_type"),
        profile.get("product_type"),
        cfg.get("industry_type"),
        brand.get("industry"),
    ):
        canonical = canonical_industry_type(value)
        if canonical:
            return canonical
        text = str(value or "").lower()
        if "saas" in text or "software as a service" in text:
            return "saas"
        if "artificial intelligence" in text or re.search(r"\bai\b", text):
            return "ai_product"
        if "smart hardware" in text or "iot" in text:
            return "smart_hardware"
    return None


def _english_project(cfg: dict) -> bool:
    delivery = cfg.get("delivery") if isinstance(cfg.get("delivery"), dict) else {}
    profile = delivery.get("customer_profile") if isinstance(delivery.get("customer_profile"), dict) else {}
    market = str(cfg.get("market") or "").strip().lower()
    if market and market not in {"global", "both"}:
        return False
    language = str(
        delivery.get("primary_language") or profile.get("primary_language")
        or cfg.get("language") or ""
    ).strip().lower()
    if language:
        return language.startswith("en")
    return market in {"global", "both"}


def _profile(cfg: dict) -> dict[str, str]:
    delivery = cfg.get("delivery") if isinstance(cfg.get("delivery"), dict) else {}
    profile = delivery.get("customer_profile") if isinstance(delivery.get("customer_profile"), dict) else {}
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    competitors = cfg.get("competitors") if isinstance(cfg.get("competitors"), list) else []
    product_lines = delivery.get("product_lines") if isinstance(delivery.get("product_lines"), list) else []
    if not product_lines and isinstance(profile.get("product_lines"), list):
        product_lines = profile["product_lines"]
    products = brand.get("products") if isinstance(brand.get("products"), list) else []
    competitor = next(
        (str(row.get("name")) for row in competitors if isinstance(row, dict) and row.get("name")),
        "another product",
    )
    return {
        "brand": str(brand.get("name") or "the product"),
        "product_line": str((product_lines or products or ["its target use case"])[0]),
        "competitor": competitor,
    }


def question_candidates(cfg: dict | None, limit: int | None = None) -> list[dict]:
    cfg = cfg if isinstance(cfg, dict) else {}
    industry = infer_industry_type(cfg)
    if not industry or not _english_project(cfg):
        return []
    template = load_template(industry)
    profile = _profile(cfg)
    candidates = []
    for row in template["question_patterns"]:
        candidates.append({
            "candidate_id": f"template-question:{row['id']}",
            "template_id": template["template_id"],
            "pattern_id": row["id"],
            "group": row["group"],
            "market": "global",
            "language": "en",
            "text": row["pattern"].format_map(profile),
            "intent": row["intent"],
            "priority": row["priority"],
            "status": "candidate",
            "candidate_only": True,
            "requires_evidence": True,
            "requires_human_review": True,
        })
    return candidates[:limit] if isinstance(limit, int) and limit >= 0 else candidates


def apply_bootstrap_candidates(cfg: dict | None) -> dict:
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    industry = infer_industry_type(out)
    questions = question_candidates(out)
    if not industry or not questions:
        return out
    template = load_template(industry)
    delivery = out.setdefault("delivery", {})
    delivery["industry_template_candidates"] = {
        "schema_version": SCHEMA_VERSION,
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "industry_type": industry,
        "language": "en",
        "candidate_only": True,
        "evidence_status": "unconfirmed",
        "questions": questions,
        "required_fact_candidates": copy.deepcopy(template["required_facts"]),
        "applicable_conversion_goals": copy.deepcopy(template["applicable_conversion_goals"]),
        "risk_expression_ids": [row["id"] for row in template["risk_expressions"]],
    }
    return out


def _task_text(task: dict) -> str:
    values = [task.get("title"), task.get("why"), task.get("action"), task.get("package")]
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    values.extend([acceptance.get("check"), acceptance.get("desc")])
    values.extend(acceptance.get("checks") or [])
    return " ".join(str(value or "") for value in values).lower()


def match_diagnosis_rules(task: dict, template: dict) -> list[dict]:
    text = _task_text(task)
    matches = []
    for rule in template["diagnosis_rules"]:
        match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
        keywords = [str(value).lower() for value in match.get("keywords") or []]
        packages = [str(value).lower() for value in match.get("packages") or []]
        checks = [str(value).lower() for value in match.get("checks") or []]
        package_match = bool(packages and str(task.get("package") or "").lower() in packages)
        keyword_match = bool(keywords and any(word in text for word in keywords))
        check_match = bool(checks and any(value in text for value in checks))
        if package_match or keyword_match or check_match:
            matches.append(copy.deepcopy(rule))
    return matches


def _rows_by_id(rows: list[dict]) -> dict[str, dict]:
    return {str(row["id"]): row for row in rows}


def _select_rows(rows: list[dict], wanted: set[str]) -> list[dict]:
    return [copy.deepcopy(row) for row in rows if row.get("id") in wanted]


def task_template_candidates(task: dict, cfg: dict) -> dict:
    industry = infer_industry_type(cfg)
    if not industry or not _english_project(cfg):
        return {}
    template = load_template(industry)
    rules = match_diagnosis_rules(task, template)
    asset_ids = {
        ref for rule in rules for ref in (rule.get("asset_template_ids") or [])
    }
    question_ids = {
        ref for rule in rules for ref in (rule.get("question_pattern_ids") or [])
    }
    asset_index = _rows_by_id(template["asset_templates"])
    approval_ids = {
        ref for asset_id in asset_ids for ref in asset_index[asset_id].get("approval_default_ids") or []
    }
    acceptance_ids = {
        ref for asset_id in asset_ids for ref in asset_index[asset_id].get("acceptance_default_ids") or []
    }
    approvals = _select_rows(template["approval_defaults"], approval_ids)
    acceptance = _select_rows(template["acceptance_defaults"], acceptance_ids)
    for row in approvals:
        row["candidate_status"] = "suggested"
    for row in acceptance:
        row["candidate_status"] = "suggested"
    return {
        "schema_version": SCHEMA_VERSION,
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "industry_type": industry,
        "language": "en",
        "candidate_only": True,
        "evidence_status": "unconfirmed",
        "matched_rule_ids": [row["id"] for row in rules],
        "required_evidence_types": sorted({
            ref for row in rules for ref in (row.get("required_evidence_types") or [])
        }),
        "question_pattern_ids": sorted(question_ids),
        "asset_candidates": _select_rows(template["asset_templates"], asset_ids),
        "approval_defaults": approvals,
        "acceptance_defaults": acceptance,
        "risk_expression_ids": [row["id"] for row in template["risk_expressions"]],
    }


def apply_task_templates_data(data: dict | None, cfg: dict | None) -> dict:
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    if not infer_industry_type(cfg) or not _english_project(cfg):
        return out
    rows = out.get("tasks")
    if not isinstance(rows, list):
        return out
    for task in rows:
        if not isinstance(task, dict):
            continue
        delivery = task.setdefault("delivery", {})
        existing = delivery.get("industry_template")
        existing = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        generated = task_template_candidates(task, cfg)
        if generated:
            existing.update(generated)
            delivery["industry_template"] = existing
    return out


def normalize_task_template_fields(task: dict) -> dict:
    """Normalize candidate metadata only; operational delivery fields stay untouched."""
    out = copy.deepcopy(task) if isinstance(task, dict) else {}
    delivery = out.get("delivery") if isinstance(out.get("delivery"), dict) else None
    metadata = delivery.get("industry_template") if delivery else None
    if not isinstance(metadata, dict) or not metadata:
        return out
    industry = canonical_industry_type(metadata.get("industry_type"))
    if not industry:
        return out
    template = load_template(industry)
    metadata["schema_version"] = SCHEMA_VERSION
    metadata["template_id"] = template["template_id"]
    metadata["template_version"] = template["template_version"]
    metadata["industry_type"] = industry
    metadata["language"] = "en"
    metadata["candidate_only"] = True
    metadata["evidence_status"] = "unconfirmed"
    requested_rule_ids = set(metadata.get("matched_rule_ids") or [])
    rules = [row for row in template["diagnosis_rules"] if row["id"] in requested_rule_ids]
    asset_ids = {ref for row in rules for ref in row.get("asset_template_ids") or []}
    question_ids = {ref for row in rules for ref in row.get("question_pattern_ids") or []}
    asset_index = _rows_by_id(template["asset_templates"])
    approval_ids = {
        ref for asset_id in asset_ids for ref in asset_index[asset_id].get("approval_default_ids") or []
    }
    acceptance_ids = {
        ref for asset_id in asset_ids for ref in asset_index[asset_id].get("acceptance_default_ids") or []
    }
    metadata["matched_rule_ids"] = [row["id"] for row in rules]
    metadata["required_evidence_types"] = sorted({
        ref for row in rules for ref in row.get("required_evidence_types") or []
    })
    metadata["question_pattern_ids"] = sorted(question_ids)
    metadata["asset_candidates"] = _select_rows(template["asset_templates"], asset_ids)
    approvals = _select_rows(template["approval_defaults"], approval_ids)
    acceptance = _select_rows(template["acceptance_defaults"], acceptance_ids)
    for row in approvals + acceptance:
        row["candidate_status"] = "suggested"
    metadata["approval_defaults"] = approvals
    metadata["acceptance_defaults"] = acceptance
    metadata["risk_expression_ids"] = [row["id"] for row in template["risk_expressions"]]
    return out


def generator_defaults(cfg: dict | None, tasks_data: dict | None = None) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    industry = infer_industry_type(cfg)
    if not industry or not _english_project(cfg):
        return {}
    template = load_template(industry)
    matched_assets: set[str] = set()
    rows = (tasks_data or {}).get("tasks") if isinstance(tasks_data, dict) else []
    for task in rows if isinstance(rows, list) else []:
        metadata = (task.get("delivery") or {}).get("industry_template") if isinstance(task, dict) else {}
        for row in metadata.get("asset_candidates") or [] if isinstance(metadata, dict) else []:
            if isinstance(row, dict) and row.get("id"):
                matched_assets.add(row["id"])
    assets = [
        copy.deepcopy(row) for row in template["asset_templates"]
        if not matched_assets or row["id"] in matched_assets
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "industry_type": industry,
        "language": "en",
        "candidate_only": True,
        "selection_basis": "industry_template_candidate",
        "generator_families": copy.deepcopy(template["generator_families"]),
        "asset_candidates": assets,
        "requires_evidence": True,
        "requires_human_review": True,
    }
