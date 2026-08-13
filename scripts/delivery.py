"""GEO Delivery Ops 的 Schema、兼容层和阶段 1–7 交付控制。

Change Set 1 与阶段 1–7 处理基线、诊断、范围、分派、资产、部署证据和验收复盘：

* 读取旧项目时在内存中补齐默认字段，不产生写操作；
* 保留所有未知字段和旧 ``acceptance.check``；
* 显式 ``delivery-sync`` 才通过 GeoLook 现有写入路径落盘；
* ``compute_stage`` 根据实际证据给出建议阶段，不静默覆盖已有阶段。
* 范围确认用哈希绑定当时配置，基线快照创建后不覆盖；
* 诊断证据只保存可回溯引用和小型快照，不复制页面或回答全文；
* 自动绑定的证据保持 pending，人工确认或排除必须记录角色、时间和说明。
* 复用 GeoLook 资产生成结果，补路径、版本、哈希、预检和分类型审批。
* 只有 Web Owner 显式提交并留下 URL/渠道证据后才重抓并计算 deployed；
* 多检查项验收保留 before/after/target，人工结论绑定当前交付版本；
* 通过后的失败复跑自动重开，并把验收、回归和关闭写入周期事件日志。
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from version import PRODUCT_STAGE, SCHEMA_VERSION as PROJECT_SCHEMA_VERSION, get_distribution_version


SCHEMA_VERSION = PROJECT_SCHEMA_VERSION
DELIVERY_STAGES = (
    "baseline",
    "diagnosed",
    "scoped",
    "assigned",
    "asset_ready",
    "deployed",
    "verified",
)
_STAGE_SET = set(DELIVERY_STAGES)
PRODUCT_TYPES = {"saas", "ai", "smart_hardware", "other"}
CONVERSION_GOALS = {"demo", "signup", "lead", "rfq", "partner", "other"}
CASE_TYPES = {
    "technical",
    "fact_error",
    "content_gap",
    "external_evidence_gap",
    "outcome_metric_gap",
}
SOURCE_REF_TYPES = {"audit", "sample", "metric", "fact", "page", "external", "manual"}
REVIEW_STATUSES = {"pending", "confirmed", "rejected"}
REVIEW_ROLES = {"geo_operator", "reviewer"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
SCOPE_STATUSES = {"pending", "approved", "deferred", "rejected", "needs_evidence"}
SCOPE_DECISION_STATUSES = SCOPE_STATUSES - {"pending"}
PUBLIC_ALPHA_SCOPE_LIMIT = 12
EFFORT_POINTS = {"S": 1, "M": 3, "L": 5}
TECHNICAL_GATE_CHECKS = {
    "site.no_ai_bot_block",
    "site.has_sitemap",
    "pages.static_text",
    "pages.has_jsonld",
}
ASSIGNMENT_STATUSES = {"pending", "confirmed", "blocked"}
ASSET_TYPES = {
    "llms_txt", "jsonld", "html_snippet", "outline", "draft",
    "content", "deployment_guide", "other",
}
ASSET_APPROVAL_STATUSES = {"not_required", "pending", "approved", "rejected"}
ASSET_DECISION_STATUSES = {"approved", "rejected"}
DEPLOYMENT_CHANNELS = {
    "website", "github", "wordpress", "wechat", "external_platform", "other",
}
DEPLOYMENT_EVIDENCE_TYPES = {"url", "file", "screenshot", "manual"}
DEPLOYMENT_STATUSES = {"submitted", "reachable", "verified", "failed"}
MAX_NOTE_LENGTH = 2000
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
EVENT_TYPES = {
    "cycle_created", "cycle_ended", "baseline_locked",
    "diagnosis_created", "evidence_confirmed", "evidence_rejected",
    "scope_approved", "scope_deferred", "scope_rejected", "scope_needs_evidence",
    "task_assigned", "assignment_blocked", "task_status_changed",
    "asset_created", "asset_updated", "asset_approved", "asset_rejected",
    "asset_approval_regressed", "deployment_submitted", "deployment_checked",
    "deployment_regressed", "verification_started", "verification_passed",
    "verification_failed", "verification_manual", "verification_regressed",
    "task_closed", "task_reopened", "legacy_project_normalized",
    "external_sync_recorded",
}
_CYCLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
OWNER_ROLE_MAP = {
    "开发": "web_owner",
    "内容": "content_owner",
    "市场": "project_owner",
    "GEO顾问": "geo_operator",
    "法务": "fact_approver",
    "设计": "content_owner",
}
DELIVERY_ROLE_KEYS = {
    "project_owner", "geo_operator", "fact_approver",
    "content_owner", "web_owner", "reviewer",
}
ASSET_PACKAGE_PREFIXES = {
    "实体消歧": ("llms", "jsonld/organization", "snippets/definition"),
    "页面技术": ("jsonld/", "snippets/"),
    "内容矩阵": ("snippets/", "outlines/", "drafts/"),
    "标题体系": ("outlines/", "drafts/"),
    "知识库": ("llms", "jsonld/", "snippets/definition"),
}
ASSET_METADATA_FILES = {
    "index.json", "_index.json", "_lint.json", "_delivery_manifest.json",
}


class ScopeValidationError(ValueError):
    """项目范围不满足基线锁定条件。"""

    def __init__(self, result: dict):
        self.result = result
        details = "；".join(row["message"] for row in result.get("errors", []))
        super().__init__(details or "项目范围未通过校验")


def _validated_text(
    value: Any,
    field: str,
    *,
    required: bool = True,
    max_length: int = MAX_NOTE_LENGTH,
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} 不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field} 不能超过 {max_length} 个字符")
    return text


def _validate_cycle_id(cycle_id: Any) -> str:
    value = str(cycle_id or "").strip()
    if not _CYCLE_ID_RE.fullmatch(value):
        raise ValueError("cycle_id 格式非法")
    return value


def default_delivery_config() -> dict:
    """返回全新的 Public Alpha 默认配置。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "product_stage": PRODUCT_STAGE,
        "enabled": True,
        "mode": "public_alpha",
        "current_cycle_id": None,
        "current_baseline_ref": None,
        "industry": "other",
        "target_markets": [],
        "product_lines": [],
        "icps": [],
        "conversion_goal": "lead",
        "conversion_goals": ["lead"],
        "strategic_priorities": [],
        "cycle_history": [],
        "customer_profile": {
            "company_size": "10-200",
            "product_type": "other",
            "primary_language": "en",
            "target_markets": [],
            "product_lines": [],
            "icp": "",
            "conversion_goal": "lead",
        },
        "roles": {},
        "policy": {
            "max_scoped_tasks": 12,
            "max_cycle_tasks": 12,
            "max_large_tasks": 2,
            "require_scope_approval": True,
            "require_fact_approval": True,
            "require_asset_approval": True,
            "require_deployment_evidence": True,
            "require_final_review": False,
        },
        "planning": {
            "team_resources": {
                "capacity_points": None,
                "available_owners": [],
            },
            "business_priorities": [],
        },
    }


def default_task_delivery(cycle_id: str | None = None) -> dict:
    """返回全新的工单交付字段默认值。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "stage": "baseline",
        "case_type": "other",
        "product_line": "",
        "icp": "",
        "market": "",
        "buyer_role": "",
        "buying_stage": "",
        "conversion_goal": "",
        "source_refs": [],
        "diagnosis": {
            "confidence": "low",
            "review_required": True,
            "review_reasons": [],
            "ready_for_scope": False,
        },
        "scope_recommendation": {},
        "scope_decision": {},
        "priority": {},
        "industry_template": {},
        "assignment": {
            "status": "pending",
            "evidence_refs": [],
            "owner_role": "",
            "owner_role_name": "",
            "dependencies": [],
            "question_ids": [],
            "target_pages": [],
            "target_assets": [],
            "target_changes": [],
            "approval_requirements": [],
            "acceptance_checks": [],
            "missing_fields": [],
            "executable": False,
        },
        "assets": [],
        "approvals": [],
        "deployments": [],
        "verification": {},
        "verification_history": [],
        "regressions": [],
        "next_action": "",
        "blocker": "",
    }


def _merge_defaults(value: Any, defaults: dict) -> dict:
    """深合并默认值，同时保留调用方的未知字段。"""
    out = copy.deepcopy(value) if isinstance(value, dict) else {}
    for key, default in defaults.items():
        if key not in out:
            out[key] = copy.deepcopy(default)
        elif isinstance(default, dict) and isinstance(out[key], dict):
            out[key] = _merge_defaults(out[key], default)
    return out


def normalize_config(cfg: dict | None) -> dict:
    """标准化 ``geo.json.delivery``，把旧命名迁移到唯一的公开契约。"""
    raw = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    profile = raw.get("customer_profile") if isinstance(raw.get("customer_profile"), dict) else {}
    if "current_cycle_id" not in raw and raw.get("active_cycle_id"):
        raw["current_cycle_id"] = raw.get("active_cycle_id")
    if "current_baseline_ref" not in raw and raw.get("active_baseline_ref"):
        raw["current_baseline_ref"] = raw.get("active_baseline_ref")
    if "industry" not in raw and profile.get("product_type"):
        raw["industry"] = profile.get("product_type")
    for key in ("target_markets", "product_lines", "conversion_goal"):
        if key not in raw and key in profile:
            raw[key] = copy.deepcopy(profile[key])
    if "icps" not in raw and profile.get("icp"):
        raw["icps"] = [str(profile.get("icp"))]
    if "conversion_goals" not in raw and raw.get("conversion_goal"):
        raw["conversion_goals"] = [raw.get("conversion_goal")]
    planning = raw.get("planning") if isinstance(raw.get("planning"), dict) else {}
    if "strategic_priorities" not in raw and isinstance(planning.get("business_priorities"), list):
        raw["strategic_priorities"] = copy.deepcopy(planning["business_priorities"])
    policy = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    if "max_cycle_tasks" not in policy and "max_scoped_tasks" in policy:
        policy["max_cycle_tasks"] = policy.get("max_scoped_tasks")
        raw["policy"] = policy
    # 旧字段是已知别名，不作为未知扩展继续传播；显式 delivery-sync 后只落盘规范名。
    raw.pop("active_cycle_id", None)
    raw.pop("active_baseline_ref", None)
    out = _merge_defaults(raw, default_delivery_config())
    out["schema_version"] = SCHEMA_VERSION
    out["product_stage"] = PRODUCT_STAGE
    out["enabled"] = bool(out.get("enabled", True))
    out["industry"] = str(out.get("industry") or "other").strip().lower()
    out["target_markets"] = _clean_string_list(out.get("target_markets"))
    out["product_lines"] = _clean_string_list(out.get("product_lines"))
    out["icps"] = copy.deepcopy(out.get("icps")) if isinstance(out.get("icps"), list) else []
    out["conversion_goals"] = _clean_string_list(out.get("conversion_goals"))
    out["conversion_goal"] = str(
        out["conversion_goals"][0] if out["conversion_goals"]
        else out.get("conversion_goal") or "lead"
    ).strip().lower()
    if not out["conversion_goals"]:
        out["conversion_goals"] = [out["conversion_goal"]]
    out["strategic_priorities"] = (
        copy.deepcopy(out.get("strategic_priorities"))
        if isinstance(out.get("strategic_priorities"), list) else []
    )
    if not isinstance(out.get("planning"), dict):
        out["planning"] = copy.deepcopy(default_delivery_config()["planning"])
    out["planning"]["business_priorities"] = copy.deepcopy(out["strategic_priorities"])
    out["customer_profile"].update({
        "product_type": out["industry"],
        "target_markets": copy.deepcopy(out["target_markets"]),
        "product_lines": copy.deepcopy(out["product_lines"]),
        "conversion_goal": out["conversion_goal"],
    })
    return out


def normalize_project_config(cfg: dict | None) -> dict:
    """标准化完整 ``geo.json``，保留所有旧字段和未知字段。"""
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    out["schema_version"] = SCHEMA_VERSION
    out["product_stage"] = PRODUCT_STAGE
    out["distribution_version"] = get_distribution_version()
    out.pop("app_" + "version", None)
    out["delivery"] = normalize_config(out.get("delivery"))
    return out


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if text and text not in out:
            out.append(text)
    return out


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _question_ids(cfg: dict) -> set[str]:
    questions = cfg.get("questions") if isinstance(cfg.get("questions"), list) else []
    out = set()
    for row in questions:
        if isinstance(row, dict):
            value = row.get("id") or row.get("qid")
        else:
            value = row
        text = str(value).strip() if value is not None else ""
        if text:
            out.add(text)
    return out


def _scope_lock_payload(cfg: dict, confirmation: dict | None = None) -> dict:
    delivery_cfg = normalize_config(cfg.get("delivery"))
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    questions = cfg.get("questions") if isinstance(cfg.get("questions"), list) else []
    payload = {
        "brand": str(brand.get("name") or "").strip(),
        "site": str(brand.get("site") or "").strip(),
        "customer_profile": copy.deepcopy(delivery_cfg["customer_profile"]),
        "project_owner": str(
            (delivery_cfg.get("roles") or {}).get("project_owner") or ""
        ).strip(),
        "competitors": copy.deepcopy(cfg.get("competitors") or []),
        "question_bank_sha256": _canonical_sha256(questions),
        "selected_problem_ids": _clean_string_list(
            confirmation.get("selected_problem_ids")
        ),
        "facts_reviewed": confirmation.get("facts_reviewed") is True,
        "pending_fact_ids": _clean_string_list(
            confirmation.get("pending_fact_ids")
        ),
    }
    # Phase 1 baselines did not hash the Phase 9 business context.  Preserve
    # those active cycles; all newly confirmed scopes use schema 2.0.
    if confirmation.get("scope_schema_version") == "2.0":
        payload["business_context"] = {
            "product_lines": copy.deepcopy(delivery_cfg["product_lines"]),
            "target_markets": copy.deepcopy(delivery_cfg["target_markets"]),
            "icps": copy.deepcopy(delivery_cfg["icps"]),
            "conversion_goals": copy.deepcopy(delivery_cfg["conversion_goals"]),
            "strategic_priorities": copy.deepcopy(delivery_cfg["strategic_priorities"]),
            "capacity": {
                "max_cycle_tasks": (delivery_cfg.get("policy") or {}).get("max_cycle_tasks"),
                "max_large_tasks": (delivery_cfg.get("policy") or {}).get("max_large_tasks"),
            },
        }
    return payload


def validate_scope(cfg: dict | None, require_confirmation: bool = True) -> dict:
    """校验项目范围是否可以锁定为基线。

    返回结构化 ``valid/errors/warnings``，不修改输入。``other`` 是
    SPEC 允许的枚举，但属于 Public Alpha 非优先 ICP，因此只警告。
    """
    project = normalize_project_config(cfg)
    delivery_cfg = project["delivery"]
    profile = delivery_cfg["customer_profile"]
    policy = delivery_cfg["policy"]
    brand = project.get("brand") if isinstance(project.get("brand"), dict) else {}
    confirmation = delivery_cfg.get("scope_confirmation")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    errors: list[dict] = []
    warnings: list[dict] = []

    def error(field: str, code: str, message: str) -> None:
        errors.append({"field": field, "code": code, "message": message})

    site = str(brand.get("site") or "").strip()
    parsed = urlparse(site)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        error("brand.site", "invalid_url", "需要有效的英文官网 HTTP(S) URL")

    product_type = profile.get("product_type")
    if product_type not in PRODUCT_TYPES:
        error("delivery.customer_profile.product_type", "invalid_enum", "产品类型不在允许枚举中")
    elif product_type == "other":
        warnings.append({
            "field": "delivery.customer_profile.product_type",
            "code": "outside_primary_icp",
            "message": "product_type=other 不属于 Public Alpha 优先 ICP",
        })

    if not _clean_string_list(profile.get("target_markets")):
        error("delivery.customer_profile.target_markets", "required", "至少需要一个目标市场")
    if not _clean_string_list(profile.get("product_lines")):
        error("delivery.customer_profile.product_lines", "required", "至少需要一条核心产品线")
    if not str(profile.get("icp") or "").strip():
        error("delivery.customer_profile.icp", "required", "需要 ICP 简述")
    if profile.get("conversion_goal") not in CONVERSION_GOALS:
        error("delivery.customer_profile.conversion_goal", "invalid_enum", "转化目标不在允许枚举中")
    if profile.get("primary_language") != "en":
        error("delivery.customer_profile.primary_language", "unsupported", "Public Alpha 基线要求 primary_language=en")
    if not str(profile.get("company_size") or "").strip():
        error("delivery.customer_profile.company_size", "required", "需要企业规模")
    elif profile.get("company_size") != "10-200":
        warnings.append({
            "field": "delivery.customer_profile.company_size",
            "code": "outside_primary_icp",
            "message": "企业规模不在 Public Alpha 优先的 10-200 人范围",
        })
    if not _clean_string_list(project.get("competitors")):
        error("competitors", "required", "至少需要一个主要竞品")
    known_problem_ids = _question_ids(project)
    if not known_problem_ids:
        error("questions", "required", "需要先生成并复核问题库")

    roles = delivery_cfg.get("roles")
    if not isinstance(roles, dict) or not str(roles.get("project_owner") or "").strip():
        error("delivery.roles.project_owner", "required", "需要设置 Project Owner")

    if require_confirmation and policy.get("require_scope_approval", True):
        if confirmation.get("status") != "approved":
            error("delivery.scope_confirmation.status", "approval_required", "Project Owner 尚未批准项目范围")
        if confirmation.get("role") != "project_owner":
            error("delivery.scope_confirmation.role", "invalid_role", "范围必须由 project_owner 角色确认")
        if not confirmation.get("at"):
            error("delivery.scope_confirmation.at", "required", "范围确认缺少时间")
        selected = _clean_string_list(confirmation.get("selected_problem_ids"))
        if not selected:
            error("delivery.scope_confirmation.selected_problem_ids", "required", "至少确认一个本周期问题")
        unknown = sorted(set(selected) - known_problem_ids)
        if unknown:
            error(
                "delivery.scope_confirmation.selected_problem_ids",
                "unknown_problem",
                f"范围包含问题库中不存在的 ID：{'、'.join(unknown)}",
            )
        max_scoped = policy.get("max_scoped_tasks", 12)
        if not isinstance(max_scoped, int) or max_scoped < 1:
            error("delivery.policy.max_scoped_tasks", "invalid", "max_scoped_tasks 必须是正整数")
        elif len(selected) > max_scoped:
            error(
                "delivery.scope_confirmation.selected_problem_ids",
                "scope_limit_exceeded",
                f"已选 {len(selected)} 项，超过本周期上限 {max_scoped}",
            )
        if confirmation.get("facts_reviewed") is not True:
            error("delivery.scope_confirmation.facts_reviewed", "required", "Project Owner 必须确认已复核品牌事实")
        if not isinstance(confirmation.get("pending_fact_ids"), list):
            error("delivery.scope_confirmation.pending_fact_ids", "required", "必须显式记录待确认事实列表，可为空数组")

        expected_hash = _canonical_sha256(_scope_lock_payload(project, confirmation))
        if confirmation.get("scope_sha256") != expected_hash:
            error("delivery.scope_confirmation.scope_sha256", "scope_changed", "项目范围已在确认后变更，需要重新确认")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "scope_sha256": _canonical_sha256(_scope_lock_payload(project, confirmation)),
    }


def _legacy_check_item(expr: str) -> dict:
    return {"id": "legacy", "check": expr, "required": True}


def _normalize_acceptance(task: dict) -> None:
    acc = task.get("acceptance")
    if not isinstance(acc, dict):
        return

    checks = acc.get("checks")
    if not isinstance(checks, list):
        checks = []
        acc["checks"] = checks

    legacy = acc.get("check")
    if legacy and not any(
        isinstance(item, dict) and item.get("check") == legacy for item in checks
    ):
        checks.insert(0, _legacy_check_item(str(legacy)))

    acc.setdefault("mode", "all")
    acc.setdefault("review_required", False)


def _latest_approval(task_delivery: dict, approval_type: str) -> dict | None:
    rows = task_delivery.get("approvals")
    if not isinstance(rows, list):
        return None
    for row in reversed(rows):
        if isinstance(row, dict) and row.get("type") == approval_type:
            return row
    return None


def _review_is_satisfied(task: dict, task_delivery: dict) -> bool:
    acc = task.get("acceptance")
    if not isinstance(acc, dict) or not acc.get("review_required"):
        return True
    final = _latest_approval(task_delivery, "final")
    return bool(final and final.get("status") == "approved")


def _verification_passed(task: dict, task_delivery: dict) -> bool:
    verification = task_delivery.get("verification")
    if isinstance(verification, dict):
        if verification.get("can_close") is True:
            return _review_is_satisfied(task, task_delivery)
        # Public Alpha 周期必须满足完整关闭门槛；不能用单独的 pass 或旧式
        # checks 绕过资产、部署、Reviewer 和证据链检查。
        if task_delivery.get("cycle_id"):
            return False
        if verification.get("verdict") == "pass":
            return _review_is_satisfied(task, task_delivery)

        checks = verification.get("checks")
        if isinstance(checks, list) and checks:
            required = [
                row
                for row in checks
                if isinstance(row, dict) and row.get("required", True)
            ]
            if required and all(row.get("verdict") == "pass" for row in required):
                return _review_is_satisfied(task, task_delivery)

    # 兼容旧 verify.py 写入 evidence 的验收记录。
    evidence = task.get("evidence")
    if isinstance(evidence, list):
        for row in reversed(evidence):
            if not isinstance(row, dict):
                continue
            if row.get("result") == "pass":
                return _review_is_satisfied(task, task_delivery)
    return False


def _deployment_stage(task: dict, task_delivery: dict) -> str | None:
    deployments = task_delivery.get("deployments")
    if not isinstance(deployments, list) or not deployments:
        return None
    if task_delivery.get("cycle_id"):
        current_assets = {
            row.get("id"): row
            for row in task_delivery.get("assets", [])
            if isinstance(row, dict)
            and row.get("required", True)
            and row.get(
                "deployment_required",
                row.get("type") not in {"outline", "deployment_guide"},
            )
            and not row.get("missing")
        }
        if not current_assets:
            return None
        covered = set()
        for row in reversed(deployments):
            if not isinstance(row, dict) or row.get("deployment_complete") is not True:
                continue
            if row.get("deployed_by_role") != "web_owner" or row.get("human_confirmed") is not True:
                continue
            versions = row.get("asset_versions") if isinstance(row.get("asset_versions"), list) else []
            if not versions or not all(isinstance(version, dict) for version in versions):
                continue
            if all(
                current_assets.get(version.get("id"))
                and current_assets[version["id"]].get("sha256") == version.get("sha256")
                and current_assets[version["id"]].get("version") == version.get("version")
                and current_assets[version["id"]].get("approval_status") in {"approved", "not_required"}
                for version in versions
            ):
                covered.update(version.get("id") for version in versions)
        if set(current_assets).issubset(covered):
            return "deployed"
        return None
    statuses = {
        row.get("status")
        for row in deployments
        if isinstance(row, dict)
    }
    if statuses.intersection({"submitted", "reachable", "verified"}):
        return "deployed"
    return None


def _assets_ready(task: dict, task_delivery: dict) -> bool:
    refs = task_delivery.get("assets")
    if isinstance(refs, list) and refs:
        required = [
            row
            for row in refs
            if isinstance(row, dict) and row.get("required", True)
        ]
        if not required:
            return False
        strict = bool(task_delivery.get("cycle_id"))
        return all(
            not row.get("missing")
            and (not strict or (
                isinstance(row.get("version"), int) and row["version"] >= 1
                and isinstance(row.get("size"), int) and row["size"] > 0
                and len(str(row.get("sha256") or "")) == 64
                and ((row.get("preflight") or {}).get("can_submit") is True)
            ))
            and row.get("approval_status", "pending") in {"approved", "not_required"}
            for row in required
        )

    # 旧 GeoLook 只有顶层 assets；显式同步时按迁移规格推断 asset_ready。
    assets = task.get("assets")
    return isinstance(assets, list) and bool(assets)


def _assignment_complete(task: dict) -> bool:
    acc = task.get("acceptance")
    return bool(
        task.get("owner")
        and task.get("action")
        and isinstance(acc, dict)
        and (acc.get("check") or acc.get("checks") or acc.get("type") == "manual")
    )


def _has_confirmed_source(task_delivery: dict) -> bool:
    refs = task_delivery.get("source_refs")
    if not isinstance(refs, list):
        return False
    return any(
        isinstance(row, dict) and row.get("review_status") == "confirmed"
        for row in refs
    )


def compute_stage(task: dict) -> str:
    """按实际字段计算建议阶段。

    ``wontfix`` 是显式终态；此时保留最后记录的合法交付阶段，不根据后续
    缺失字段自动重开。
    """
    if not isinstance(task, dict):
        return "baseline"

    raw_delivery = task.get("delivery")
    delivery = _merge_defaults(raw_delivery, default_task_delivery())
    stored = delivery.get("stage")
    if task.get("status") == "wontfix" and stored in _STAGE_SET:
        return stored

    scope = delivery.get("scope_decision")
    scope_ready = (
        isinstance(scope, dict)
        and scope.get("status") == "approved"
        and _has_confirmed_source(delivery)
    )
    assignment = delivery.get("assignment")
    assignment_ready = (
        scope_ready
        and isinstance(assignment, dict)
        and assignment.get("status") == "confirmed"
        and _assignment_complete(task)
    )

    # 新交付周期严格遵守阶段门槛：旧 assets/done/deployment 数据不能让
    # 未经范围批准或负责人确认的工单跳过 diagnosed/scoped/assigned。
    if delivery.get("cycle_id") and not assignment_ready:
        if scope_ready:
            return "scoped"
        refs = delivery.get("source_refs")
        if isinstance(refs, list) and refs:
            return "diagnosed"
        return "baseline"

    deployment_stage = _deployment_stage(task, delivery)
    if _verification_passed(task, delivery) and (
        not delivery.get("cycle_id") or deployment_stage
    ):
        return "verified"
    if deployment_stage:
        return "deployed"

    # 旧任务 status=done 但没有可回放的 pass，只能推断“曾部署”，不能
    # 伪装成 verified；normalize_task 会返回 stage_warning。
    if task.get("status") == "done" and not delivery.get("cycle_id"):
        return "deployed"

    if _assets_ready(task, delivery):
        return "asset_ready"
    if assignment_ready:
        return "assigned"
    if scope_ready:
        return "scoped"

    refs = delivery.get("source_refs")
    if isinstance(refs, list) and refs:
        return "diagnosed"
    # 无 cycle/source_refs 的旧 GeoLook 工单继续按旧字段推断，避免兼容
    # 读取把历史任务降级；一旦进入新交付周期，则必须按阶段门槛推进。
    if not delivery.get("cycle_id") and _assignment_complete(task):
        return "assigned"
    return "baseline"


def normalize_task(task: dict | None, cycle_id: str | None = None) -> dict:
    """补齐单条工单的新字段，不修改输入，不删除未知字段。"""
    out = copy.deepcopy(task) if isinstance(task, dict) else {}
    if not isinstance(out.get("external_refs"), list):
        out["external_refs"] = []
    else:
        out["external_refs"] = [copy.deepcopy(row) for row in out["external_refs"] if isinstance(row, dict)]
    _normalize_acceptance(out)

    had_delivery = isinstance(out.get("delivery"), dict)
    raw_delivery = copy.deepcopy(out.get("delivery")) if had_delivery else {}
    if "buying_stage" not in raw_delivery and raw_delivery.get("funnel_stage"):
        raw_delivery["buying_stage"] = raw_delivery.get("funnel_stage")
    raw_delivery.pop("funnel_stage", None)
    if "assets" not in raw_delivery and isinstance(raw_delivery.get("asset_refs"), list):
        raw_delivery["assets"] = copy.deepcopy(raw_delivery["asset_refs"])
    raw_delivery.pop("asset_refs", None)
    delivery = _merge_defaults(
        raw_delivery,
        default_task_delivery(cycle_id=cycle_id),
    )
    if not delivery.get("cycle_id") and cycle_id:
        delivery["cycle_id"] = cycle_id
    out["delivery"] = delivery

    suggested = compute_stage(out)
    stored = delivery.get("stage")
    if not had_delivery or stored not in _STAGE_SET:
        delivery["stage"] = suggested
        if (
            not had_delivery
            and out.get("status") == "done"
            and suggested == "deployed"
        ):
            delivery["stage_warning"] = (
                "旧任务 status='done' 但没有可回放的验收通过证据；"
                "仅推断为 deployed"
            )
        elif had_delivery and stored not in (None, ""):
            delivery["stage_warning"] = (
                f"未知存储阶段 {stored!r}，建议阶段为 {suggested!r}"
            )
    elif stored != suggested:
        delivery["stage_warning"] = (
            f"存储阶段 {stored!r} 与建议阶段 {suggested!r} 不一致"
        )
    else:
        delivery.pop("stage_warning", None)
    if delivery.get("stage") == "baseline" and not delivery.get("cycle_id"):
        delivery["stage_warning"] = "baseline 尚未锁定：缺少 cycle_id 和基线快照"
    # Industry templates normalize recommendation metadata only.  They cannot
    # modify evidence, scope, approvals, verification, status, or stage.
    try:
        import industry_templates
    except ModuleNotFoundError:
        return out
    return industry_templates.normalize_task_template_fields(out)


def normalize_tasks_data(data: dict | None, cycle_id: str | None = None) -> dict:
    """标准化完整 ``tasks.json`` 数据，不修改输入对象。"""
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    rows = out.get("tasks", [])
    if not isinstance(rows, list):
        raise ValueError("tasks.json 的 tasks 必须是数组；已拒绝迁移")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("tasks.json 包含非对象工单；已拒绝迁移")
    out["tasks"] = [
        normalize_task(row, cycle_id=cycle_id)
        for row in rows
    ]
    return out


# ---------------------------------------------------------------- 证据化诊断

def _checks_of(task: dict) -> list[str]:
    acceptance = task.get("acceptance")
    if not isinstance(acceptance, dict):
        return []
    checks = []
    for row in acceptance.get("checks", []) if isinstance(acceptance.get("checks"), list) else []:
        if isinstance(row, dict) and row.get("check"):
            checks.append(str(row["check"]))
    if acceptance.get("check") and str(acceptance["check"]) not in checks:
        checks.append(str(acceptance["check"]))
    return checks


def classify_case_type(task: dict) -> str:
    """把现有 GeoLook 工单确定性归入五类诊断。"""
    package = str(task.get("package") or "")
    text = " ".join(
        str(task.get(key) or "") for key in ("title", "why", "action")
    ).lower()
    checks = _checks_of(task)
    if package == "监测闭环" or any(c.startswith("metrics.mention_rate") for c in checks):
        return "outcome_metric_gap"
    if package == "外部证据" or any(
        c.startswith(("external.", "metrics.own_cite")) for c in checks
    ):
        return "external_evidence_gap"
    fact_tokens = ("事实", "实体消歧", "别名", "型号", "定义句", "百科")
    if package == "实体消歧" or any(token.lower() in text for token in fact_tokens):
        return "fact_error"
    if package == "页面技术":
        return "technical"
    if package in {"内容矩阵", "标题体系", "知识库"}:
        return "content_gap"
    if any(c.startswith(("site.", "pages.")) for c in checks):
        return "technical"
    return "content_gap"


def _stable_source_id(task_id: str, ref_type: str, ref: str) -> str:
    digest = hashlib.sha256(
        f"{task_id}\x1f{ref_type}\x1f{ref}".encode("utf-8")
    ).hexdigest()[:12]
    return f"src-{digest}"


def _compact_text(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source_ref(
    task_id: str,
    ref_type: str,
    ref: str,
    label: str,
    captured_at: str,
    snapshot: dict,
    confidence: str = "high",
) -> dict:
    return {
        "id": _stable_source_id(task_id, ref_type, ref),
        "type": ref_type,
        "ref": ref,
        "label": label,
        "captured_at": captured_at,
        "snapshot": copy.deepcopy(snapshot),
        "confidence": confidence,
        "review_status": "pending",
        "binding_method": "deterministic",
    }


def _url_key(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _audit_page_snapshot(page: dict) -> dict:
    keys = (
        "url", "title", "status", "score", "word_count", "issue_codes",
        "jsonld_types", "dimensions", "blocks",
    )
    return {key: copy.deepcopy(page[key]) for key in keys if key in page}


def _audit_refs(task: dict, audit: dict, captured_at: str) -> list[dict]:
    task_id = str(task.get("id") or "")
    pages = audit.get("pages") if isinstance(audit.get("pages"), list) else []
    page_index = {
        _url_key(page.get("url")): (index, page)
        for index, page in enumerate(pages)
        if isinstance(page, dict) and page.get("url")
    }
    refs = []
    for url in _clean_string_list(task.get("affected")):
        match = page_index.get(_url_key(url))
        if not match:
            continue
        index, page = match
        ref = f"audit.json#pages[{index}]"
        refs.append(_source_ref(
            task_id, "audit", ref,
            f"审计页面：{url}",
            str(audit.get("generated_at") or audit.get("audited_at") or captured_at),
            _audit_page_snapshot(page),
        ))

    site = audit.get("site") if isinstance(audit.get("site"), dict) else {}
    checker_fields = {
        "site.no_ai_bot_block": ("site.ai_bots_blocked", site.get("ai_bots_blocked")),
        "site.has_sitemap": ("site.has_sitemap", site.get("has_sitemap")),
        "site.has_llms_txt": ("site.has_llms_txt", site.get("has_llms_txt")),
        "site.en_pages_gte": ("language_coverage", audit.get("language_coverage")),
        "site.lang_balance": ("language_coverage", audit.get("language_coverage")),
        "site.avg_score_gte": ("avg_score", audit.get("avg_score")),
    }
    for checker in _checks_of(task):
        prefix = checker.split(":", 1)[0]
        if prefix not in checker_fields:
            continue
        field, value = checker_fields[prefix]
        ref = f"audit.json#{field}"
        refs.append(_source_ref(
            task_id, "audit", ref,
            f"审计项：{checker}",
            str(audit.get("generated_at") or audit.get("audited_at") or captured_at),
            {"checker": checker, "value": copy.deepcopy(value)},
        ))

    if not refs and any(c.startswith("pages.") for c in _checks_of(task)):
        refs.append(_source_ref(
            task_id, "audit", "audit.json#pages",
            "页面审计集合",
            str(audit.get("generated_at") or audit.get("audited_at") or captured_at),
            {"page_count": audit.get("page_count", len(pages)), "avg_score": audit.get("avg_score")},
            "medium",
        ))
    return refs


def _metric_refs(
    task: dict,
    metrics: dict | None,
    metrics_ref: str | None,
    captured_at: str,
) -> list[dict]:
    if not isinstance(metrics, dict) or not metrics_ref:
        return []
    task_id = str(task.get("id") or "")
    platforms = metrics.get("platforms") if isinstance(metrics.get("platforms"), dict) else {}
    market = task.get("market")
    rows = {
        name: row for name, row in platforms.items()
        if isinstance(row, dict) and (market in (None, "", "both") or row.get("market") == market)
    }
    refs = []
    for checker in _checks_of(task):
        parts = checker.split(":")
        if checker.startswith("metrics.mention_rate_gte"):
            values = [row.get("mention_rate") for row in rows.values() if row.get("mention_rate") is not None]
            snapshot = {
                "market": market,
                "metric": "mention_rate",
                "average": round(sum(values) / len(values), 3) if values else None,
                "platforms": {name: row.get("mention_rate") for name, row in rows.items()},
                "target": float(parts[2]) if len(parts) > 2 else None,
            }
        elif checker.startswith("metrics.own_cite_gte"):
            values = [row.get("own_domain_cite_rate") for row in rows.values() if row.get("own_domain_cite_rate") is not None]
            snapshot = {
                "market": market,
                "metric": "own_domain_cite_rate",
                "average": round(sum(values) / len(values), 3) if values else None,
                "platforms": {name: row.get("own_domain_cite_rate") for name, row in rows.items()},
                "target": float(parts[2]) if len(parts) > 2 else None,
            }
        else:
            continue
        ref = f"{metrics_ref}#platforms[{market or 'all'}]"
        refs.append(_source_ref(
            task_id, "metric", ref, f"AI 采样指标：{checker}",
            str(metrics.get("generated_at") or captured_at), snapshot,
        ))
    return refs


def _external_refs(
    task: dict,
    metrics: dict | None,
    metrics_ref: str | None,
    captured_at: str,
) -> list[dict]:
    if not isinstance(metrics, dict) or not metrics_ref:
        return []
    targets = []
    for checker in _checks_of(task):
        if checker.startswith("external.any:"):
            targets.extend(x.strip().lower() for x in checker.split(":", 1)[1].split(",") if x.strip())
    if not targets:
        return []
    market = task.get("market")
    observed = {}
    platforms = metrics.get("platforms") if isinstance(metrics.get("platforms"), dict) else {}
    for name, row in platforms.items():
        if not isinstance(row, dict) or (market not in (None, "", "both") and row.get("market") != market):
            continue
        for domain, count in (row.get("top_cited_domains") or {}).items():
            observed[str(domain).lower()] = observed.get(str(domain).lower(), 0) + int(count or 0)
    ref = f"{metrics_ref}#platforms.*.top_cited_domains"
    return [_source_ref(
        str(task.get("id") or ""), "external", ref,
        "目标外部信源在采样引用域名中的覆盖",
        str(metrics.get("generated_at") or captured_at),
        {
            "market": market,
            "target_domains": sorted(set(targets)),
            "observed_target_counts": {d: observed.get(d, 0) for d in sorted(set(targets))},
            "observed_domain_count": len(observed),
        },
    )]


def _sample_refs(
    task: dict,
    samples: list[dict] | None,
    samples_ref: str | None,
    captured_at: str,
) -> list[dict]:
    if not samples or not samples_ref:
        return []
    case_type = classify_case_type(task)
    if case_type not in {"outcome_metric_gap", "external_evidence_gap", "fact_error"}:
        return []
    market = task.get("market")
    chosen = []
    seen_platforms = set()
    for line_no, row in enumerate(samples, 1):
        if not isinstance(row, dict) or not row.get("ok", True):
            continue
        if market not in (None, "", "both") and row.get("market") != market:
            continue
        analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else {}
        relevant = (
            case_type == "fact_error" and (row.get("brand_in_question") or row.get("needs_review"))
        ) or (
            case_type == "outcome_metric_gap" and not row.get("brand_in_question")
        ) or (
            case_type == "external_evidence_gap" and not analysis.get("own_domain_cited")
        )
        platform = str(row.get("platform") or "unknown")
        if not relevant or platform in seen_platforms:
            continue
        seen_platforms.add(platform)
        ref = f"{samples_ref}#line={line_no}"
        chosen.append(_source_ref(
            str(task.get("id") or ""), "sample", ref,
            f"{row.get('platform_name') or platform} 回答样本：{row.get('question_id') or '未编号'}",
            str(row.get("ts") or captured_at),
            {
                "platform": platform,
                "market": row.get("market"),
                "question_id": row.get("question_id"),
                "prompt": _compact_text(row.get("question"), 300),
                "answer_excerpt": _compact_text(row.get("answer"), 500),
                "cited_domains": copy.deepcopy(analysis.get("cited_domains") or []),
                "brand_mentioned": analysis.get("brand_mentioned"),
                "competitors_mentioned": copy.deepcopy(analysis.get("competitors_mentioned") or []),
                "negative_cues": copy.deepcopy(analysis.get("negative_cues") or []),
                "source_needs_review": bool(row.get("needs_review") or analysis.get("needs_review")),
            },
            "medium",
        ))
        if len(chosen) >= 3:
            break
    return chosen


def _fact_ref(
    task: dict,
    cfg: dict,
    facts_text: str,
    facts_ref: str,
    captured_at: str,
) -> list[dict]:
    if classify_case_type(task) != "fact_error":
        return []
    task_id = str(task.get("id") or "")
    if facts_text:
        headings = [
            line[3:].strip() for line in facts_text.splitlines()
            if line.startswith("## ")
        ]
        return [_source_ref(
            task_id, "fact", facts_ref, "品牌事实库",
            captured_at,
            {
                "sha256": hashlib.sha256(facts_text.encode("utf-8")).hexdigest(),
                "headings": headings[:20],
            },
        )]
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    if brand:
        return [_source_ref(
            task_id, "fact", "geo.json#brand", "项目品牌事实",
            captured_at,
            {key: copy.deepcopy(brand.get(key)) for key in ("name", "aliases", "products", "industry")},
            "medium",
        )]
    return []


def _manual_material_refs(task: dict, materials: Any, captured_at: str) -> list[dict]:
    if not isinstance(materials, list):
        return []
    refs = []
    for index, row in enumerate(materials):
        if not isinstance(row, dict) or not str(row.get("note") or "").strip():
            continue
        task_ids = _clean_string_list(row.get("task_ids"))
        packages = _clean_string_list(row.get("packages"))
        if task_ids and task.get("id") not in task_ids:
            continue
        if packages and task.get("package") not in packages:
            continue
        if not task_ids and not packages:
            continue
        ref = str(row.get("ref") or row.get("url") or row.get("path") or f"geo.json#materials[{index}]")
        item = _source_ref(
            str(task.get("id") or ""), "manual", ref,
            str(row.get("label") or "人工补充材料"),
            str(row.get("captured_at") or captured_at),
            {"note": _compact_text(row.get("note"), 500)},
            str(row.get("confidence") or "medium") if row.get("confidence") in CONFIDENCE_LEVELS else "medium",
        )
        item["note"] = str(row["note"]).strip()
        refs.append(item)
    return refs


def _review_reasons(task: dict, refs: list[dict]) -> list[str]:
    reasons = []
    checks = _checks_of(task)
    if task.get("affected") or any(c.startswith("pages.") for c in checks):
        reasons.append("复核页面抓取与渲染结构，排除抓取结构误判")
    if classify_case_type(task) == "fact_error":
        reasons.append("复核产品型号、品牌别名与实体指代")
    sample_snapshots = [row.get("snapshot") or {} for row in refs if row.get("type") == "sample"]
    if sample_snapshots:
        reasons.append("复核 AI 回答中的否定、引用与竞品语境")
    if any(snapshot.get("source_needs_review") for snapshot in sample_snapshots):
        reasons.append("原始采样已标记 needs_review")
    return reasons


def validate_diagnosis(task: dict, require_confirmed: bool = False) -> dict:
    """校验诊断案例；人工判断缺少说明时拒绝进入下一阶段。"""
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    errors = []

    def error(field: str, code: str, message: str) -> None:
        errors.append({"field": field, "code": code, "message": message})

    if delivery.get("case_type") not in CASE_TYPES:
        error("delivery.case_type", "invalid_enum", "诊断类型不在五类允许值中")
    if not refs:
        error("delivery.source_refs", "evidence_required", "诊断至少需要一条证据")
    confirmed = 0
    for index, row in enumerate(refs):
        field = f"delivery.source_refs[{index}]"
        if not isinstance(row, dict):
            error(field, "invalid", "证据引用必须是对象")
            continue
        if row.get("type") not in SOURCE_REF_TYPES:
            error(field + ".type", "invalid_enum", "证据类型无效")
        if not row.get("id") or not row.get("ref"):
            error(field, "required", "证据必须包含 id 和 ref")
        if row.get("confidence") not in CONFIDENCE_LEVELS:
            error(field + ".confidence", "invalid_enum", "置信度无效")
        status = row.get("review_status")
        if status not in REVIEW_STATUSES:
            error(field + ".review_status", "invalid_enum", "证据复核状态无效")
        if row.get("type") == "manual" and not str(row.get("note") or "").strip():
            error(field + ".note", "required", "manual 证据必须有说明")
        if status in {"confirmed", "rejected"}:
            if not str(row.get("review_note") or "").strip():
                error(field + ".review_note", "required", "人工判断必须有说明")
            if row.get("reviewed_by_role") not in REVIEW_ROLES:
                error(field + ".reviewed_by_role", "invalid_role", "人工判断角色必须是 geo_operator 或 reviewer")
            if not row.get("reviewed_at"):
                error(field + ".reviewed_at", "required", "人工判断必须记录时间")
        if status == "confirmed":
            confirmed += 1
    if require_confirmed and not confirmed:
        error("delivery.source_refs", "confirmed_evidence_required", "进入 scoped 前至少需要一条 confirmed 证据")
    return {
        "valid": not errors,
        "errors": errors,
        "evidence_count": len(refs),
        "confirmed_count": confirmed,
        "ready_for_scope": bool(refs) and confirmed > 0 and not errors,
    }


def attach_source_refs(
    task: dict,
    *,
    audit: dict,
    cfg: dict,
    metrics: dict | None = None,
    metrics_ref: str | None = None,
    samples: list[dict] | None = None,
    samples_ref: str | None = None,
    facts_text: str = "",
    facts_ref: str = "content/facts.md",
    materials: Any = None,
    captured_at: str = "",
    cycle_id: str | None = None,
) -> dict:
    """为一条现有工单绑定确定性证据，保留已复核和人工证据。"""
    out = normalize_task(task, cycle_id=cycle_id)
    delivery = out["delivery"]
    delivery["case_type"] = classify_case_type(out)
    now = captured_at or datetime.now().astimezone().isoformat(timespec="seconds")
    generated = (
        _audit_refs(out, audit or {}, now)
        + _metric_refs(out, metrics, metrics_ref, now)
        + _external_refs(out, metrics, metrics_ref, now)
        + _sample_refs(out, samples, samples_ref, now)
        + _fact_ref(out, cfg or {}, facts_text, facts_ref, now)
        + _manual_material_refs(out, materials, now)
    )

    previous = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    previous_by_id = {
        row.get("id"): copy.deepcopy(row)
        for row in previous
        if isinstance(row, dict) and row.get("id")
    }
    merged = []
    generated_ids = set()
    for row in generated:
        generated_ids.add(row["id"])
        old = previous_by_id.get(row["id"])
        if old and (
            old.get("review_status") in {"confirmed", "rejected"}
            or old.get("binding_method") != "deterministic"
        ):
            merged.append(old)
        else:
            merged.append(row)
    for row in previous:
        if not isinstance(row, dict) or row.get("id") in generated_ids:
            continue
        if row.get("binding_method") != "deterministic" or row.get("review_status") in {"confirmed", "rejected"}:
            merged.append(copy.deepcopy(row))

    # ID 去重并保持确定性顺序。
    unique = []
    seen = set()
    for row in merged:
        if row.get("id") in seen:
            continue
        seen.add(row.get("id"))
        unique.append(row)
    delivery["source_refs"] = unique

    confidence = "low"
    if any(row.get("confidence") == "high" for row in unique):
        confidence = "high"
    elif any(row.get("confidence") == "medium" for row in unique):
        confidence = "medium"
    validation = validate_diagnosis(out)
    pending = any(row.get("review_status") == "pending" for row in unique)
    delivery["diagnosis"] = {
        "confidence": confidence,
        "review_required": pending,
        "review_reasons": _review_reasons(out, unique),
        "ready_for_scope": validation["ready_for_scope"],
        "evidence_count": len(unique),
        "confirmed_evidence_count": validation["confirmed_count"],
    }
    delivery["stage"] = compute_stage(out)
    delivery.pop("stage_warning", None)
    return out


def diagnose_tasks_data(
    data: dict,
    *,
    audit: dict,
    cfg: dict,
    metrics: dict | None = None,
    metrics_ref: str | None = None,
    samples: list[dict] | None = None,
    samples_ref: str | None = None,
    facts_text: str = "",
    facts_ref: str = "content/facts.md",
    materials: Any = None,
    captured_at: str = "",
    cycle_id: str | None = None,
) -> dict:
    """把 ``tasks.build`` 产物转成带证据的候选诊断集合。"""
    out = copy.deepcopy(data) if isinstance(data, dict) else {"tasks": []}
    rows = out.get("tasks")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("tasks.json 的 tasks 必须是对象数组")
    out["tasks"] = [
        attach_source_refs(
            row,
            audit=audit,
            cfg=cfg,
            metrics=metrics,
            metrics_ref=metrics_ref,
            samples=samples,
            samples_ref=samples_ref,
            facts_text=facts_text,
            facts_ref=facts_ref,
            materials=materials,
            captured_at=captured_at,
            cycle_id=cycle_id,
        )
        for row in rows
    ]
    out["diagnosis"] = {
        "generated_at": captured_at,
        "candidate_count": len(out["tasks"]),
        "with_evidence": sum(bool(row["delivery"]["source_refs"]) for row in out["tasks"]),
        "ready_for_scope": sum(row["delivery"]["diagnosis"]["ready_for_scope"] for row in out["tasks"]),
        "needs_review": sum(row["delivery"]["diagnosis"]["review_required"] for row in out["tasks"]),
    }
    return out


# ---------------------------------------------------------------- 优先级与范围确认

def _dimension(value: Any, default: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(5, number))


def _is_technical_gate(task: dict) -> bool:
    if classify_case_type(task) != "technical":
        return False
    if task.get("priority") == "P0":
        return True
    return any(check.split(":", 1)[0] in TECHNICAL_GATE_CHECKS for check in _checks_of(task))


def _matches_business_priority(task: dict, priority: dict) -> bool:
    filters = {
        "market": task.get("market"),
        "product_line": (task.get("delivery") or {}).get("product_line"),
        "package": task.get("package"),
        "case_type": (task.get("delivery") or {}).get("case_type"),
        "funnel_stage": (task.get("delivery") or {}).get("funnel_stage"),
    }
    used = False
    for key, actual in filters.items():
        expected = priority.get(key)
        if expected in (None, "", []):
            continue
        used = True
        values = expected if isinstance(expected, list) else [expected]
        if actual not in values and "both" not in values:
            return False
    return used


def _business_priority(task: dict, cfg: dict) -> tuple[int, list[str]]:
    planning = (cfg.get("delivery") or {}).get("planning") or {}
    rows = planning.get("business_priorities")
    if not isinstance(rows, list):
        return 1, []
    matched = []
    weight = 1
    for row in rows:
        if not isinstance(row, dict) or not _matches_business_priority(task, row):
            continue
        row_weight = _dimension(row.get("weight"), 3)
        weight = max(weight, row_weight)
        matched.append(str(row.get("reason") or row.get("name") or "命中业务优先项"))
    return weight, matched


def score_candidate(task: dict, cfg: dict) -> dict:
    """兼容旧调用方，权威评分写入 ``delivery.priority``。"""
    import prioritize

    candidate = copy.deepcopy(task)
    gate = _is_technical_gate(candidate)
    if gate:
        candidate["priority"] = "P0"
    priority = prioritize.score_task(candidate, cfg)
    legacy_weight, legacy_reasons = _business_priority(candidate, cfg)
    reasons = copy.deepcopy(priority["explanation"])
    if gate and not any("技术门票" in row for row in reasons):
        reasons.insert(0, "技术门票规则：保持 P0，不被综合评分降级")
    return {
        "priority_score": priority["score"],
        "score": priority["score"],
        "recommended_priority": priority["recommended_priority"],
        "priority_locked": priority["p0_capacity_reserved"],
        "dimensions": {
            "business_value": priority["business_value"],
            "buyer_intent": priority["buyer_intent"],
            "visibility_gap": priority["visibility_gap"],
            "evidence_confidence": priority["evidence_confidence"],
            "feasibility": priority["feasibility"],
            "effort_penalty": priority["effort_penalty"],
            # Phase 3 兼容字段；新 UI 和报告只读取上面的 0–3 维度。
            "urgency": 5 if gate else legacy_weight,
        },
        "reasons": reasons,
        "matched_business_priorities": legacy_reasons or copy.deepcopy(priority["matched_strategic_priority_ids"]),
        "advisory_only": True,
    }


def _scope_limit(cfg: dict) -> int:
    policy = (cfg.get("delivery") or {}).get("policy") or {}
    configured = policy.get("max_cycle_tasks", policy.get("max_scoped_tasks", PUBLIC_ALPHA_SCOPE_LIMIT))
    if not isinstance(configured, int) or configured < 1:
        configured = PUBLIC_ALPHA_SCOPE_LIMIT
    return min(configured, PUBLIC_ALPHA_SCOPE_LIMIT)


def _resource_plan(cfg: dict) -> dict:
    planning = (cfg.get("delivery") or {}).get("planning") or {}
    resources = planning.get("team_resources")
    resources = resources if isinstance(resources, dict) else {}
    capacity = resources.get("capacity_points")
    if not isinstance(capacity, int) or capacity < 0:
        capacity = None
    owner_capacity = resources.get("owner_capacity_points")
    owner_capacity = owner_capacity if isinstance(owner_capacity, dict) else {}
    owner_capacity = {
        str(owner): points
        for owner, points in owner_capacity.items()
        if isinstance(points, int) and points >= 0
    }
    return {
        "capacity_points": capacity,
        "available_owners": _clean_string_list(resources.get("available_owners")),
        "owner_capacity_points": owner_capacity,
    }


def recommend_scope_data(data: dict, cfg: dict, generated_at: str = "") -> dict:
    """为一个 Public Alpha 周期建议有限范围，但不替 Project Owner 决策。"""
    import prioritize

    project = normalize_project_config(cfg)
    cycle_id = project["delivery"].get("current_cycle_id")
    out = normalize_tasks_data(data, cycle_id=cycle_id)
    for task in out["tasks"]:
        if _is_technical_gate(task):
            task["priority"] = "P0"
    return prioritize.recommend_cycle_scope(out, project, generated_at=generated_at)


def validate_cycle_scope(data: dict, cfg: dict) -> dict:
    project = normalize_project_config(cfg)
    limit = _scope_limit(project)
    cycle_id = project["delivery"].get("current_cycle_id")
    rows = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else []
    errors = []
    warnings = []
    counts = {status: 0 for status in SCOPE_STATUSES}
    approved_task_ids = []
    approved_large_task_ids = []
    undecided_task_ids = []
    for task in rows:
        if not isinstance(task, dict):
            continue
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        decision = delivery.get("scope_decision") if isinstance(delivery.get("scope_decision"), dict) else {}
        status = decision.get("status", "pending")
        if status not in SCOPE_STATUSES:
            errors.append({"task_id": task.get("id"), "code": "invalid_status", "message": "范围决策状态无效"})
            continue
        counts[status] += 1
        if status == "pending":
            undecided_task_ids.append(task.get("id"))
        else:
            if decision.get("decided_by_role") != "project_owner":
                errors.append({
                    "task_id": task.get("id"),
                    "code": "invalid_decision_role",
                    "message": "范围决策必须由 project_owner 作出",
                })
            if not decision.get("decided_at"):
                errors.append({
                    "task_id": task.get("id"),
                    "code": "decision_time_required",
                    "message": "范围决策必须记录时间",
                })
            if not str(decision.get("reason") or "").strip():
                errors.append({
                    "task_id": task.get("id"),
                    "code": "decision_reason_required",
                    "message": "范围决策必须记录说明",
                })
        if status == "approved":
            approved_task_ids.append(task.get("id"))
            if str(task.get("effort") or "").upper() == "L":
                approved_large_task_ids.append(task.get("id"))
            diagnosis = validate_diagnosis(task, require_confirmed=True)
            if not diagnosis["valid"]:
                errors.append({
                    "task_id": task.get("id"),
                    "code": "evidence_gate_failed",
                    "message": "批准事项缺少有效 confirmed 证据",
                })
        elif status in {"deferred", "rejected"} and task.get("priority") == "P0":
            warnings.append({
                "task_id": task.get("id"),
                "code": "p0_not_in_cycle",
                "message": "P0 未进入本周期，Project Owner 必须保留明确调整理由",
            })
        if delivery.get("cycle_id") not in (None, cycle_id):
            errors.append({"task_id": task.get("id"), "code": "cycle_mismatch", "message": "工单不属于 active cycle"})
        stage = delivery.get("stage")
        if stage in {"assigned", "asset_ready", "deployed", "verified"} and status != "approved":
            errors.append({
                "task_id": task.get("id"),
                "code": "execution_without_approval",
                "message": "进入执行的事项必须有 approved 范围决策",
            })
    if len(approved_task_ids) > limit:
        errors.append({
            "task_id": None,
            "code": "scope_limit_exceeded",
            "message": f"已批准 {len(approved_task_ids)} 条，超过 Public Alpha 上限 {limit}",
        })
    max_large = (project["delivery"].get("policy") or {}).get("max_large_tasks", 2)
    max_large = max_large if isinstance(max_large, int) and max_large >= 0 else 2
    if len(approved_large_task_ids) > max_large:
        warnings.append({
            "task_id": None,
            "code": "large_task_limit_exceeded",
            "message": f"已批准 {len(approved_large_task_ids)} 条 L 级任务，超过建议上限 {max_large}",
        })
    return {
        "valid": not errors,
        "complete": not errors and not undecided_task_ids,
        "cycle_id": cycle_id,
        "max_tasks": limit,
        "approved_task_ids": approved_task_ids,
        "approved_count": len(approved_task_ids),
        "approved_large_task_ids": approved_large_task_ids,
        "approved_large_count": len(approved_large_task_ids),
        "max_large_tasks": max_large,
        "undecided_task_ids": undecided_task_ids,
        "counts": counts,
        "bounded": len(approved_task_ids) <= limit and len(approved_large_task_ids) <= max_large,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 可分派工单

def _owner_role(owner: Any) -> str:
    value = str(owner or "").strip()
    if value in DELIVERY_ROLE_KEYS:
        return value
    return OWNER_ROLE_MAP.get(value, "")


def _dependency_ids(task: dict) -> list[str]:
    values = task.get("dependencies")
    if not isinstance(values, list):
        values = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
    out = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("task_id") or item.get("id") or item.get("ref")
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _linked_question_ids(task: dict, cfg: dict) -> list[str]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    out = _clean_string_list(task.get("question_ids"))
    out.extend(x for x in _clean_string_list(delivery.get("question_ids")) if x not in out)
    for ref in delivery.get("source_refs", []) if isinstance(delivery.get("source_refs"), list) else []:
        if not isinstance(ref, dict):
            continue
        snapshot = ref.get("snapshot") if isinstance(ref.get("snapshot"), dict) else {}
        qid = str(snapshot.get("question_id") or "").strip()
        if qid and qid not in out:
            out.append(qid)
    text = " ".join(str(task.get(key) or "") for key in ("title", "why", "action"))
    for qid in re.findall(r"\b[qQ]\d{3,}\b", text):
        if qid not in out:
            out.append(qid)

    questions = cfg.get("questions") if isinstance(cfg.get("questions"), list) else []
    known = {
        str(row.get("id") or row.get("qid") or ""): row
        for row in questions if isinstance(row, dict)
    }
    confirmation = ((cfg.get("delivery") or {}).get("scope_confirmation") or {})
    fallback = _clean_string_list(confirmation.get("selected_problem_ids"))
    if not fallback:
        fallback = [qid for qid in known if qid]
    market = task.get("market")
    for qid in fallback:
        question = known.get(qid) or {}
        qmarket = question.get("market")
        if qmarket and market not in (None, "", "both") and qmarket not in (market, "both"):
            continue
        if qid not in out:
            out.append(qid)
        if len(out) >= 5:
            break
    return out[:20]


def _target_pages(task: dict, cfg: dict) -> list[str]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    out = _clean_string_list(task.get("affected"))
    for ref in delivery.get("source_refs", []) if isinstance(delivery.get("source_refs"), list) else []:
        if not isinstance(ref, dict):
            continue
        snapshot = ref.get("snapshot") if isinstance(ref.get("snapshot"), dict) else {}
        url = str(snapshot.get("url") or "").strip()
        if url and url not in out:
            out.append(url)
    if not out:
        brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
        site = str(brand.get("site") or "").strip()
        if site:
            out.append(site)
    return out


def _target_assets(task: dict) -> list[dict]:
    out = []
    for item in task.get("assets", []) if isinstance(task.get("assets"), list) else []:
        if isinstance(item, dict):
            row = copy.deepcopy(item)
            path = str(row.get("path") or "").strip()
        else:
            path = str(item or "").strip()
            row = {"path": path}
        if not path:
            continue
        row.setdefault("type", "other")
        row.setdefault("required", True)
        row.setdefault("binding_method", "existing_task_asset")
        if not any(existing.get("path") == path for existing in out):
            out.append(row)

    checks = _checks_of(task)
    inferred = []
    if any(check.split(":", 1)[0] == "site.has_llms_txt" for check in checks):
        inferred.append({"path": "assets/llms.txt", "type": "llms_txt"})
    if any(check.split(":", 1)[0] == "pages.has_jsonld" for check in checks):
        inferred.append({"path": "assets/jsonld/", "type": "jsonld"})
    if classify_case_type(task) == "fact_error" and "事实" in str(task.get("title") or ""):
        inferred.append({"path": "content/facts.md", "type": "content"})
    for row in inferred:
        if any(existing.get("path") == row["path"] for existing in out):
            continue
        out.append({
            **row,
            "required": True,
            "binding_method": "deterministic_target",
            "expected": True,
        })
    return out


def _target_changes(task: dict, pages: list[str]) -> list[dict]:
    action = str(task.get("action") or "").strip()
    if not action:
        return []
    change_type = {
        "technical": "website_technical_change",
        "fact_error": "fact_or_entity_change",
        "content_gap": "content_change",
        "external_evidence_gap": "external_presence_change",
        "outcome_metric_gap": "measurement_target_change",
    }.get(classify_case_type(task), "other")
    return [{
        "type": change_type,
        "description": action,
        "targets": copy.deepcopy(pages),
        "required": True,
    }]


def _approval_requirements(task: dict, cfg: dict, assets: list[dict]) -> list[dict]:
    policy = (cfg.get("delivery") or {}).get("policy") or {}
    case_type = classify_case_type(task)
    rows = []
    if case_type == "fact_error" and policy.get("require_fact_approval", True):
        rows.append({
            "type": "fact", "role": "fact_approver", "required": True,
            "reason": "涉及品牌事实、实体、型号或对外声明",
        })
    if assets and policy.get("require_asset_approval", True):
        role = "web_owner" if case_type == "technical" else "content_owner"
        rows.append({
            "type": "asset", "role": role, "required": True,
            "reason": "目标资产生成后必须在部署前审批",
        })
    return rows


def _assignment_checks(task: dict) -> list[dict]:
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    rows = []
    for index, item in enumerate(acceptance.get("checks", []) if isinstance(acceptance.get("checks"), list) else []):
        if not isinstance(item, dict) or not item.get("check"):
            continue
        row = copy.deepcopy(item)
        row.setdefault("id", f"check-{index + 1}")
        row.setdefault("required", True)
        rows.append(row)
    legacy = acceptance.get("check")
    if legacy and not any(row.get("check") == legacy for row in rows):
        rows.insert(0, {"id": "legacy", "check": str(legacy), "required": True})
    if not rows and acceptance.get("type") == "manual" and str(acceptance.get("desc") or "").strip():
        rows.append({
            "id": "manual",
            "type": "manual",
            "desc": str(acceptance["desc"]).strip(),
            "required": True,
        })
    return rows


def _assignment_evidence_refs(task: dict) -> list[dict]:
    """绑定已确认诊断证据的稳定引用，不复制页面或回答正文。"""
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    out = []
    for row in refs:
        if not isinstance(row, dict) or row.get("review_status") != "confirmed":
            continue
        source_id = str(row.get("id") or "").strip()
        if not source_id:
            continue
        out.append({
            "id": source_id,
            "type": str(row.get("type") or "").strip(),
            "ref": str(row.get("ref") or "").strip(),
            "confidence": str(row.get("confidence") or "").strip(),
            "review_status": "confirmed",
        })
    return out


def _assignment_spec_payload(task: dict, assignment: dict) -> dict:
    return {
        "task_id": task.get("id"),
        "why": task.get("why"),
        "owner": task.get("owner"),
        "action": task.get("action"),
        "effort": task.get("effort"),
        "evidence_refs": assignment.get("evidence_refs"),
        "owner_role": assignment.get("owner_role"),
        "dependencies": assignment.get("dependencies"),
        "question_ids": assignment.get("question_ids"),
        "target_pages": assignment.get("target_pages"),
        "target_assets": assignment.get("target_assets"),
        "target_changes": assignment.get("target_changes"),
        "approval_requirements": assignment.get("approval_requirements"),
        "acceptance_checks": assignment.get("acceptance_checks"),
        "product_lines": assignment.get("product_lines"),
        "confirmed_source_ids": [
            row.get("id") for row in (task.get("delivery") or {}).get("source_refs", [])
            if isinstance(row, dict) and row.get("review_status") == "confirmed"
        ],
    }


def validate_assignment(task: dict) -> dict:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    errors = []

    def missing(field: str, message: str) -> None:
        errors.append({"field": field, "code": "required", "message": message})

    if ((delivery.get("scope_decision") or {}).get("status") != "approved"):
        missing("delivery.scope_decision.status", "只有 approved 诊断可以进入分派")
    if not str(task.get("why") or "").strip():
        missing("why", "工单缺少原因说明")
    if not _has_confirmed_source(delivery):
        missing("delivery.source_refs", "工单缺少 confirmed 诊断证据")
    if not assignment.get("evidence_refs"):
        missing("delivery.assignment.evidence_refs", "执行规格必须绑定 confirmed 诊断证据")
    if not str(task.get("owner") or "").strip():
        missing("owner", "工单缺少负责人")
    if assignment.get("owner_role") not in DELIVERY_ROLE_KEYS:
        missing("delivery.assignment.owner_role", "无法映射负责人角色")
    if not str(task.get("action") or "").strip():
        missing("action", "工单缺少操作说明")
    if str(task.get("effort") or "").upper() not in EFFORT_POINTS:
        missing("effort", "工作量必须是 S、M 或 L")
    if not assignment.get("question_ids"):
        missing("delivery.assignment.question_ids", "工单至少关联一个问题")
    if not assignment.get("target_pages"):
        missing("delivery.assignment.target_pages", "工单至少关联一个目标页面")
    if not assignment.get("product_lines"):
        missing("delivery.assignment.product_lines", "工单至少关联一条产品线")
    if not assignment.get("target_assets") and not assignment.get("target_changes"):
        missing("delivery.assignment.targets", "工单缺少目标资产或变更")
    if not assignment.get("acceptance_checks"):
        missing("delivery.assignment.acceptance_checks", "工单缺少验收条件")
    status = assignment.get("status", "pending")
    if status not in ASSIGNMENT_STATUSES:
        errors.append({
            "field": "delivery.assignment.status", "code": "invalid_enum",
            "message": "assignment status 必须是 pending、confirmed 或 blocked",
        })
    if status == "blocked" and not str(assignment.get("blocker") or delivery.get("blocker") or "").strip():
        missing("delivery.assignment.blocker", "无法执行时必须记录阻塞原因")
    spec_sha256 = _canonical_sha256(_assignment_spec_payload(task, assignment))
    if status == "confirmed" and assignment.get("confirmed_spec_sha256") != spec_sha256:
        errors.append({
            "field": "delivery.assignment.confirmed_spec_sha256",
            "code": "assignment_changed",
            "message": "工单执行规格已变化，需要负责人重新确认",
        })
    return {
        "valid": not errors,
        "errors": errors,
        "missing_fields": [row["field"] for row in errors],
        "spec_sha256": spec_sha256,
        "executable": not errors and status != "blocked",
    }


def build_assignment(task: dict, cfg: dict, prepared_at: str = "") -> dict:
    """为一条已批准 GeoLook 工单补齐交付执行规格，不修改输入。"""
    cycle_id = (cfg.get("delivery") or {}).get("current_cycle_id")
    out = normalize_task(task, cycle_id=cycle_id)
    delivery = out["delivery"]
    if ((delivery.get("scope_decision") or {}).get("status") != "approved"):
        return out

    previous = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    pages = _target_pages(out, cfg)
    assets = _target_assets(out)
    profile = (cfg.get("delivery") or {}).get("customer_profile") or {}
    product_lines = _clean_string_list(delivery.get("product_lines"))
    if not product_lines and delivery.get("product_line"):
        product_lines = [str(delivery["product_line"])]
    if not product_lines:
        product_lines = _clean_string_list(profile.get("product_lines"))
    owner_role = _owner_role(out.get("owner"))
    role_names = (cfg.get("delivery") or {}).get("roles") or {}
    assignment = _merge_defaults(previous, default_task_delivery()["assignment"])
    assignment.update({
        "evidence_refs": _assignment_evidence_refs(out),
        "owner_role": owner_role,
        "owner_role_name": str(role_names.get(owner_role) or out.get("owner") or ""),
        "dependencies": _dependency_ids(out),
        "question_ids": _linked_question_ids(out, cfg),
        "target_pages": pages,
        "target_assets": assets,
        "target_changes": _target_changes(out, pages),
        "approval_requirements": _approval_requirements(out, cfg, assets),
        "acceptance_checks": _assignment_checks(out),
        "product_lines": product_lines,
        "prepared_at": prepared_at,
        "prepared_by": "delivery.build_assignment",
    })
    spec_sha256 = _canonical_sha256(_assignment_spec_payload(out, assignment))
    assignment["spec_sha256"] = spec_sha256
    previous_status = previous.get("status", "pending")
    previous_hash = previous.get("confirmed_spec_sha256") or previous.get("spec_sha256")
    if previous_status in {"confirmed", "blocked"} and previous_hash != spec_sha256:
        assignment["status"] = "pending"
        assignment["confirmation_invalidated"] = "执行规格已变化，需要负责人重新确认"
        for key in ("confirmed_at", "confirmed_by_role", "confirmation_note", "confirmed_spec_sha256", "blocker"):
            assignment.pop(key, None)
    else:
        assignment["status"] = previous_status if previous_status in ASSIGNMENT_STATUSES else "pending"
        assignment.pop("confirmation_invalidated", None)
    delivery["assignment"] = assignment
    validation = validate_assignment(out)
    assignment["missing_fields"] = validation["missing_fields"]
    assignment["executable"] = validation["valid"] and assignment["status"] != "blocked"
    delivery["stage"] = compute_stage(out)
    delivery.pop("stage_warning", None)
    return out


def prepare_assignments_data(data: dict, cfg: dict, prepared_at: str = "") -> dict:
    """补齐所有已批准诊断，返回可分派列表与缺失字段摘要。"""
    project = normalize_project_config(cfg)
    cycle_id = project["delivery"].get("current_cycle_id")
    out = normalize_tasks_data(data, cycle_id=cycle_id)
    approved = []
    assignable = []
    ready_for_confirmation = []
    blocked = []
    incomplete = []
    for index, task in enumerate(out["tasks"]):
        if ((task.get("delivery") or {}).get("scope_decision") or {}).get("status") != "approved":
            continue
        prepared = build_assignment(task, project, prepared_at=prepared_at)
        out["tasks"][index] = prepared
        approved.append(prepared.get("id"))
        assignment = prepared["delivery"]["assignment"]
        if assignment.get("status") == "blocked":
            blocked.append(prepared.get("id"))
        elif assignment.get("missing_fields"):
            incomplete.append(prepared.get("id"))
        elif assignment.get("status") == "confirmed":
            assignable.append(prepared.get("id"))
        else:
            ready_for_confirmation.append(prepared.get("id"))
    out["assignment_plan"] = {
        "prepared_at": prepared_at,
        "cycle_id": cycle_id,
        "approved_task_ids": approved,
        "assignable_task_ids": assignable,
        "ready_for_confirmation_task_ids": ready_for_confirmation,
        "blocked_task_ids": blocked,
        "incomplete_task_ids": incomplete,
        "approved_count": len(approved),
        "assignable_count": len(assignable),
        "ready_for_confirmation_count": len(ready_for_confirmation),
        "all_specs_complete": not incomplete,
        "all_approved_tasks_complete": not incomplete and not blocked and not ready_for_confirmation,
    }
    return out


def _normalize_asset_path(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("path")
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        raise ValueError("资产路径必须是 assets/ 下的相对路径")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("assets/"):
        text = text[len("assets/"):]
    if not text or text.startswith("content/"):
        return ""
    parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("资产路径不能逃逸 assets/ 目录")
    normalized = "/".join(parts)
    return normalized + ("/" if text.endswith("/") else "")


def _asset_type(path: str) -> str:
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if name in {"llms.txt", "llms.en.txt"}:
        return "llms_txt"
    if lower.startswith("jsonld/") and lower.endswith(".json"):
        return "jsonld"
    if lower.startswith("snippets/") and lower.endswith((".html", ".htm")):
        return "html_snippet"
    if lower.startswith("outlines/") and lower.endswith(".md"):
        return "outline"
    if lower.startswith("drafts/") and lower.endswith(".md"):
        return "draft"
    if "deployment" in lower or "deploy" in lower:
        return "deployment_guide"
    if lower.endswith((".md", ".html", ".htm", ".txt")):
        return "content"
    return "other"


def _asset_id(task_id: str, path: str) -> str:
    digest = hashlib.sha256(f"{task_id}:{path}".encode("utf-8")).hexdigest()[:12]
    return f"asset-{digest}"


def _file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _asset_text(path: Path, size: int) -> str:
    if size > 4 * 1024 * 1024:
        return ""
    try:
        return path.read_text("utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def _factcheck_claims(factcheck: Any) -> list[dict]:
    if isinstance(factcheck, list):
        rows = factcheck
    elif isinstance(factcheck, dict):
        if factcheck.get("status") and any(
            factcheck.get(key) for key in ("original", "claim", "text", "statement", "value")
        ):
            rows = [factcheck]
        else:
            rows = next((
                factcheck.get(key) for key in ("claims", "facts", "items", "reviews")
                if isinstance(factcheck.get(key), list)
            ), [])
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        original = next((
            str(row.get(key)).strip()
            for key in ("original", "claim", "text", "statement", "value")
            if str(row.get(key) or "").strip()
        ), "")
        if status and original:
            out.append({
                "id": str(row.get("id") or "").strip(),
                "status": status,
                "original": original,
            })
    return out


_NUMBER_CLAIM_RE = re.compile(
    r"\d[\d,.]*\s*(?:%|％|万|亿|倍|元|美元|港币|HK\$|US\$|\$|人|家|天|小时|分钟|customers?|users?)",
    re.I,
)
_PLACEHOLDER_RE = re.compile(r"<填|待补|待确认|\b(?:TODO|TBD)\b|示例公司|XX公司", re.I)
_SOURCE_MARKER_RE = re.compile(r"https?://|参考(?:来源)?|来源|source|reference|citation", re.I)
_FACT_SENSITIVE_RE = re.compile(
    r"客户|案例|customers?|trusted by|SOC\s*2|ISO\s*\d+|GDPR|HIPAA|合规|认证|certified|guarantee",
    re.I,
)


def _match_claims(text: str, claims: list[dict], statuses: set[str]) -> list[dict]:
    folded = text.casefold()
    out = []
    for claim in claims:
        if claim.get("status") not in statuses:
            continue
        raw = claim["original"]
        pos = folded.find(raw.casefold())
        if pos < 0:
            continue
        excerpt = text[max(0, pos - 30):pos + min(len(raw), 60) + 30]
        out.append({
            "claim_id": claim.get("id") or "",
            "status": claim["status"],
            "excerpt": re.sub(r"\s+", " ", excerpt).strip()[:120],
        })
    return out


def _citation_preflight(
    path: str,
    asset_type: str,
    text: str,
    size: int,
    task: dict,
    cfg: dict,
    factcheck: Any,
    facts_text: str,
) -> dict:
    claims = _factcheck_claims(factcheck)
    rejected = _match_claims(text, claims, {"rejected", "forbidden"})
    pending = _match_claims(text, claims, {"pending", "needs_review", "unverified"})
    known_numbers = {match.group(0).casefold() for match in _NUMBER_CLAIM_RE.finditer(facts_text)}
    unknown_numbers = []
    for match in _NUMBER_CLAIM_RE.finditer(text):
        value = match.group(0)
        if value.casefold() in known_numbers:
            continue
        segment = text[max(0, match.start() - 30):match.end() + 30]
        if _PLACEHOLDER_RE.search(segment):
            continue
        if value not in unknown_numbers:
            unknown_numbers.append(value)

    fact_verdict = "fail" if rejected else ("manual" if pending or unknown_numbers else "pass")
    checks = [{
        "id": "non_empty",
        "verdict": "pass" if size > 0 else "fail",
        "detail": f"{size} bytes" if size else "资产为空",
    }]
    placeholder = _PLACEHOLDER_RE.search(text)
    checks.append({
        "id": "no_placeholders",
        "verdict": "manual" if placeholder else "pass",
        "detail": "包含待补或占位内容" if placeholder else "未发现占位内容",
    })

    structural_fail = False
    if asset_type == "jsonld":
        try:
            parsed = json.loads(text)
            valid = isinstance(parsed, (dict, list))
        except (json.JSONDecodeError, TypeError):
            valid = False
        structural_fail = not valid
        checks.append({
            "id": "valid_json",
            "verdict": "pass" if valid else "fail",
            "detail": "JSON 可解析" if valid else "JSON 无法解析",
        })
    elif asset_type == "html_snippet":
        visible = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->|<[^>]+>", " ", text, flags=re.I | re.S)
        visible = re.sub(r"\s+", " ", visible).strip()
        structural_fail = not visible
        checks.append({
            "id": "static_visible_text",
            "verdict": "pass" if visible else "fail",
            "detail": "存在静态可见文本" if visible else "未发现静态可见文本",
        })

    if asset_type in {"outline", "draft", "content", "llms_txt"} or "faq" in path.lower():
        has_source = bool(_SOURCE_MARKER_RE.search(text))
        checks.append({
            "id": "source_traceability",
            "verdict": "pass" if has_source else "manual",
            "detail": "存在来源或参考标记" if has_source else "未发现来源或参考标记，需人工确认可引用度",
        })

    qids = ((task.get("delivery") or {}).get("assignment") or {}).get("question_ids") or []
    if qids and (asset_type in {"outline", "draft"} or "faq" in path.lower()):
        question_texts = {
            str(row.get("id") or row.get("qid") or ""): str(row.get("text") or "")
            for row in (cfg.get("questions") or []) if isinstance(row, dict)
        }
        aligned = any(
            str(qid).casefold() in text.casefold()
            or (question_texts.get(str(qid)) and question_texts[str(qid)].casefold() in text.casefold())
            for qid in qids
        )
        checks.append({
            "id": "question_alignment",
            "verdict": "pass" if aligned else "manual",
            "detail": "命中目标问题" if aligned else "未直接命中目标问题，需人工核对",
        })

    can_submit = size > 0 and not rejected and not structural_fail
    check_verdicts = {row["verdict"] for row in checks}
    verdict = "fail" if not can_submit else (
        "manual" if fact_verdict == "manual" or "manual" in check_verdicts else "pass"
    )
    return {
        "verdict": verdict,
        "can_submit": can_submit,
        "fact_check": {
            "verdict": fact_verdict,
            "rejected_claim_matches": rejected,
            "pending_claim_matches": pending,
            "unapproved_numeric_claims": unknown_numbers[:20],
        },
        "citation_readiness": {
            "verdict": "fail" if "fail" in check_verdicts else (
                "manual" if "manual" in check_verdicts else "pass"
            ),
            "checks": checks,
        },
    }


def _asset_approval_requirements(path: str, asset_type: str, text: str, cfg: dict) -> list[dict]:
    policy = (cfg.get("delivery") or {}).get("policy") or {}
    lower = path.lower()
    rows = []

    def add(requirement_id: str, roles: list[str], reason: str) -> None:
        if not any(row["id"] == requirement_id for row in rows):
            rows.append({
                "id": requirement_id,
                "roles": roles,
                "required": True,
                "reason": reason,
            })

    fact_sensitive = (
        asset_type in {"llms_txt", "jsonld"}
        or "definition" in lower
        or bool(_NUMBER_CLAIM_RE.search(text))
        or bool(_FACT_SENSITIVE_RE.search(text))
    )
    if fact_sensitive and policy.get("require_fact_approval", True):
        add("fact", ["fact_approver"], "包含品牌定义、数字、客户名称或合规声明")
    if policy.get("require_asset_approval", True):
        if asset_type in {"outline", "draft", "content"} or "faq" in lower or "compar" in lower or "对比" in text:
            add("content", ["content_owner", "reviewer"], "页面正文、FAQ 或对比内容需内容审批")
        if asset_type in {"llms_txt", "jsonld", "html_snippet", "deployment_guide"}:
            add("technical", ["web_owner"], "待部署技术资产需 Web Owner 审批")
    return rows


def _approval_state(asset: dict, approvals: list[dict]) -> tuple[str, list[dict]]:
    requirements = [row for row in asset.get("approval_requirements", []) if row.get("required", True)]
    if not requirements:
        return "not_required", []
    results = []
    for requirement in requirements:
        latest = next((
            row for row in reversed(approvals)
            if isinstance(row, dict)
            and row.get("type") == "asset"
            and row.get("target") == f"asset:{asset.get('id')}"
            and row.get("asset_sha256") == asset.get("sha256")
            and row.get("requirement_id") == requirement.get("id")
        ), None)
        results.append({
            "requirement_id": requirement.get("id"),
            "status": latest.get("status") if latest else "pending",
            "role": latest.get("role") if latest else "",
            "at": latest.get("at") if latest else "",
            "note": latest.get("note") if latest else "",
        })
    statuses = {row["status"] for row in results}
    if "rejected" in statuses:
        return "rejected", results
    if statuses == {"approved"}:
        return "approved", results
    return "pending", results


def _discover_assets(project_dir: Path) -> dict[str, dict]:
    root = (project_dir / "assets").resolve()
    if not root.exists():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in ASSET_METADATA_FILES or path.name.startswith("."):
            continue
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        sha256, size = _file_sha256(resolved)
        out[rel] = {
            "path": rel,
            "absolute_path": resolved,
            "type": _asset_type(rel),
            "sha256": sha256,
            "size": size,
            "text": _asset_text(resolved, size),
        }
    return out


def _task_asset_matches(task: dict, discovered: dict[str, dict]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    assignment = ((task.get("delivery") or {}).get("assignment") or {})
    matches: dict[str, str] = {}
    missing_targets = []

    def bind_target(value: Any, method: str) -> None:
        try:
            target = _normalize_asset_path(value)
        except ValueError:
            return
        if not target:
            return
        found = [
            path for path in discovered
            if path == target.rstrip("/") or (target.endswith("/") and path.startswith(target))
        ]
        for path in found:
            matches.setdefault(path, method)
        if not found:
            missing_targets.append((target, method))

    for value in task.get("assets", []) if isinstance(task.get("assets"), list) else []:
        bind_target(value, "existing_task_asset")
    for value in assignment.get("target_assets", []) if isinstance(assignment.get("target_assets"), list) else []:
        bind_target(value, str(value.get("binding_method") or "assignment_target") if isinstance(value, dict) else "assignment_target")
    for value in (task.get("delivery") or {}).get("assets", []):
        if isinstance(value, dict) and value.get("binding_method") == "manual":
            bind_target(value, "manual")

    qids = [str(value).casefold() for value in assignment.get("question_ids", []) if str(value).strip()]
    for path, info in discovered.items():
        haystack = f"{path}\n{info.get('text', '')}".casefold()
        if qids and any(qid in haystack for qid in qids):
            matches.setdefault(path, "question_id")

    prefixes = ASSET_PACKAGE_PREFIXES.get(str(task.get("package") or ""), ())
    for path in discovered:
        if any(path.lower().startswith(prefix.lower()) for prefix in prefixes):
            matches.setdefault(path, "package_map")
    return matches, missing_targets


def _current_asset_ref(
    task: dict,
    info: dict,
    binding_method: str,
    cfg: dict,
    factcheck: Any,
    facts_text: str,
    scanned_at: str,
    generated_by: str,
) -> dict:
    delivery = task.get("delivery") or {}
    previous = next((
        row for row in delivery.get("assets", [])
        if isinstance(row, dict) and row.get("path") == info["path"]
    ), {})
    same_content = previous.get("sha256") == info["sha256"] and not previous.get("missing")
    version = int(previous.get("version") or 0) if same_content else int(previous.get("version") or 0) + 1
    generated_at = previous.get("generated_at") if same_content else scanned_at
    asset = {
        "id": previous.get("id") or _asset_id(str(task.get("id") or ""), info["path"]),
        "path": info["path"],
        "type": info["type"] if info["type"] in ASSET_TYPES else "other",
        "version": max(version, 1),
        "sha256": info["sha256"],
        "size": info["size"],
        "generated_at": generated_at,
        "generated_by": previous.get("generated_by") if same_content else generated_by,
        "scanned_at": scanned_at,
        "task_ids": [task.get("id")],
        "required": bool(previous.get("required", True)),
        "deployment_required": info["type"] not in {"outline", "deployment_guide"},
        "missing": False,
        "binding_method": binding_method,
    }
    asset["preflight"] = _citation_preflight(
        info["path"], info["type"], info["text"], info["size"],
        task, cfg, factcheck, facts_text,
    )
    asset["approval_requirements"] = _asset_approval_requirements(
        info["path"], info["type"], info["text"], cfg,
    )
    status, results = _approval_state(asset, delivery.get("approvals", []))
    asset["approval_status"] = status
    asset["approval_results"] = results
    return asset


def _missing_asset_ref(task: dict, path: str, method: str, scanned_at: str) -> dict:
    previous = next((
        row for row in ((task.get("delivery") or {}).get("assets") or [])
        if isinstance(row, dict) and row.get("path") == path
    ), {})
    return {
        **copy.deepcopy(previous),
        "id": previous.get("id") or _asset_id(str(task.get("id") or ""), path),
        "path": path,
        "type": previous.get("type") or _asset_type(path),
        "version": int(previous.get("version") or 0),
        "sha256": previous.get("sha256") or "",
        "size": 0,
        "scanned_at": scanned_at,
        "task_ids": [task.get("id")],
        "required": True,
        "deployment_required": _asset_type(path) not in {"outline", "deployment_guide"},
        "missing": True,
        "binding_method": method,
        "approval_status": "pending",
        "preflight": {
            "verdict": "fail",
            "can_submit": False,
            "fact_check": {"verdict": "data_missing", "rejected_claim_matches": []},
            "citation_readiness": {"verdict": "fail", "checks": []},
        },
    }


def scan_assets_data(
    data: dict,
    cfg: dict,
    project_dir: Path,
    scanned_at: str = "",
    generated_by: str = "generate.py",
) -> dict:
    """扫描 GeoLook 已生成资产并绑定 assigned 工单，不生成资产正文。"""
    project = normalize_project_config(cfg)
    cycle_id = project["delivery"].get("current_cycle_id")
    out = normalize_tasks_data(data, cycle_id=cycle_id)
    discovered = _discover_assets(Path(project_dir))
    factcheck_path = Path(project_dir) / "factcheck.json"
    factcheck = json.loads(factcheck_path.read_text("utf-8")) if factcheck_path.exists() else {}
    facts_path = Path(project_dir) / "content" / "facts.md"
    facts_text = facts_path.read_text("utf-8") if facts_path.exists() else ""
    bound_paths = set()
    assigned_ids = []
    for task in out["tasks"]:
        assignment = ((task.get("delivery") or {}).get("assignment") or {})
        if assignment.get("status") != "confirmed":
            continue
        assigned_ids.append(task.get("id"))
        matches, missing_targets = _task_asset_matches(task, discovered)
        refs = []
        for path, method in sorted(matches.items()):
            refs.append(_current_asset_ref(
                task, discovered[path], method, project, factcheck, facts_text,
                scanned_at, generated_by,
            ))
            bound_paths.add(path)
        matched_prefixes = tuple(path for path in matches)
        for target, method in missing_targets:
            if target.rstrip("/") in matched_prefixes:
                continue
            refs.append(_missing_asset_ref(task, target, method, scanned_at))
        task["delivery"]["assets"] = refs
        task["delivery"]["stage"] = compute_stage(task)
        task["delivery"].pop("stage_warning", None)

    out["asset_package"] = {
        "cycle_id": cycle_id,
        "scanned_at": scanned_at,
        "generated_by": generated_by,
        "assigned_task_ids": assigned_ids,
        "discovered_asset_count": len(discovered),
        "bound_asset_paths": sorted(bound_paths),
        "unbound_asset_paths": sorted(set(discovered) - bound_paths),
    }
    out["asset_review"] = asset_status_data(out)
    return out


def asset_status_data(data: dict) -> dict:
    rows = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else []
    assigned = []
    ready = []
    pending = []
    rejected = []
    unsafe = []
    missing = []
    no_assets = []
    for task in rows:
        if not isinstance(task, dict):
            continue
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        if ((delivery.get("assignment") or {}).get("status") != "confirmed"):
            continue
        task_id = task.get("id")
        assigned.append(task_id)
        refs = [row for row in delivery.get("assets", []) if isinstance(row, dict) and row.get("required", True)]
        if not refs:
            no_assets.append(task_id)
            continue
        if any(row.get("missing") or not row.get("size") for row in refs):
            missing.append(task_id)
        if any(((row.get("preflight") or {}).get("can_submit") is not True) for row in refs):
            unsafe.append(task_id)
        statuses = {row.get("approval_status", "pending") for row in refs}
        if "rejected" in statuses:
            rejected.append(task_id)
        elif "pending" in statuses:
            pending.append(task_id)
        if _assets_ready(task, delivery):
            ready.append(task_id)
    return {
        "assigned_task_ids": assigned,
        "asset_ready_task_ids": ready,
        "pending_approval_task_ids": pending,
        "rejected_asset_task_ids": rejected,
        "unsafe_asset_task_ids": unsafe,
        "missing_asset_task_ids": missing,
        "no_asset_task_ids": no_assets,
        "review_complete": len(ready) == len(assigned),
    }


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fallback_visible_text(html: str) -> str:
    text = re.sub(
        r"<script\b.*?</script>|"
        r"<style\b.*?</style>|<!--.*?-->|<[^>]+>",
        " ", html or "", flags=re.I | re.S,
    )
    return _normalized_text(text)


def _fallback_jsonld_details(html: str) -> tuple[list[str], list[dict]]:
    out = []
    errors = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            raw = value.get("@type")
            if isinstance(raw, list):
                out.extend(str(item) for item in raw)
            elif raw:
                out.append(str(raw))
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for index, match in enumerate(re.finditer(
        r"<script\b[^>]*type=[\"'][^\"']*ld\+json[^\"']*[\"'][^>]*>(.*?)</script>",
        html or "", re.I | re.S,
    ), 1):
        try:
            collect(json.loads(match.group(1)))
        except json.JSONDecodeError as exc:
            errors.append({
                "block": index,
                "line": exc.lineno,
                "column": exc.colno,
                "message": str(exc.msg)[:160],
            })
        except TypeError as exc:
            errors.append({"block": index, "message": type(exc).__name__})
    return sorted(set(out)), errors


def _fallback_jsonld_types(html: str) -> list[str]:
    return _fallback_jsonld_details(html)[0]


def _deployment_asset_file(project_dir: Path, relative_path: str) -> Path | None:
    try:
        rel = _normalize_asset_path(relative_path)
    except ValueError:
        return None
    if not rel or rel.endswith("/"):
        return None
    root = (Path(project_dir) / "assets").resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target if target.is_file() else None


def _asset_deployment_hints(asset: dict, project_dir: Path) -> dict:
    path = str(asset.get("path") or "")
    asset_type = str(asset.get("type") or _asset_type(path))
    target = _deployment_asset_file(project_dir, path)
    text = target.read_text("utf-8", errors="replace") if target else ""
    lower = path.lower()
    insertion = {
        "llms_txt": "网站根目录，对外路径 /llms.txt 或 /llms.en.txt",
        "jsonld": "目标页面 <head> 内的 application/ld+json 脚本",
        "html_snippet": "目标页面静态 HTML 正文；definition 位于首屏下方，FAQ 位于正文 FAQ 区",
        "outline": "内容工作流或 CMS 草稿附件，不直接视为上线正文",
        "draft": "目标页面 CMS 正文，发布前保留来源与审核修改",
        "content": "目标页面静态正文",
        "deployment_guide": "按指南标注的位置执行",
    }.get(asset_type, "由 Web Owner 在部署确认时填写")

    snippets = []
    jsonld_types = []
    if asset_type == "jsonld":
        try:
            parsed = json.loads(text)
            jsonld_types = _jsonld_types_from_value(parsed)
        except (json.JSONDecodeError, TypeError):
            jsonld_types = []
    elif asset_type == "html_snippet":
        paragraph_matches = re.findall(r"<(?:p|h[1-6]|summary)[^>]*>(.*?)</(?:p|h[1-6]|summary)>", text, re.I | re.S)
        candidates = [_fallback_visible_text(value) for value in paragraph_matches]
        snippets = [value for value in candidates if 12 <= len(value) <= 240][:3]
    else:
        candidates = []
        for line in text.splitlines():
            value = _normalized_text(re.sub(r"^[#>*\-\d.)\s]+", "", line))
            if not value or _PLACEHOLDER_RE.search(value) or value.startswith("<!--"):
                continue
            if 12 <= len(value) <= 240:
                candidates.append(value)
        snippets = list(dict.fromkeys(candidates))[:3]
    return {
        "asset_id": asset.get("id"),
        "path": path,
        "type": asset_type,
        "version": asset.get("version"),
        "sha256": asset.get("sha256"),
        "insertion_position": insertion,
        "expected_snippets": snippets,
        "expected_jsonld_types": jsonld_types,
        "deployable": asset_type not in {"outline"} and bool(target),
        "source_exists": bool(target),
        "source_size": target.stat().st_size if target else 0,
        "path_hint": lower,
    }


def _jsonld_types_from_value(value: Any) -> list[str]:
    out = []

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            raw = item.get("@type")
            if isinstance(raw, list):
                out.extend(str(child) for child in raw)
            elif raw:
                out.append(str(raw))
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    collect(value)
    return sorted(set(out))


def build_deployment_plan(task: dict, cfg: dict, project_dir: Path, generated_at: str = "") -> dict:
    """根据已批准资产生成部署清单，不产生部署记录或阶段推进。"""
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    refs = [
        row for row in delivery.get("assets", [])
        if isinstance(row, dict)
        and row.get("required", True)
        and not row.get("missing")
        and row.get("approval_status") in {"approved", "not_required"}
    ]
    assets = [_asset_deployment_hints(row, Path(project_dir)) for row in refs]
    snippets = list(dict.fromkeys(
        snippet for row in assets for snippet in row["expected_snippets"]
    ))
    jsonld_types = sorted(set(
        schema_type for row in assets for schema_type in row["expected_jsonld_types"]
    ))
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    page_targets = _clean_string_list(assignment.get("target_pages"))
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    site = str(brand.get("site") or "").rstrip("/")
    for row in assets:
        if row.get("type") == "llms_txt" and site:
            row["target_urls"] = [f"{site}/{Path(row['path']).name}"]
        else:
            row["target_urls"] = copy.deepcopy(page_targets)
    target_urls = list(dict.fromkeys(
        url for row in assets for url in row.get("target_urls", [])
    )) or page_targets
    rules = [{"check": "deployment.url_2xx", "required": True}]
    if snippets:
        rules.append({
            "check": "deployment.contains_text", "required": True,
            "expected_count": len(snippets),
        })
    for schema_type in jsonld_types:
        rules.append({
            "check": f"deployment.jsonld_type:{schema_type}", "required": True,
        })
    if not snippets and not jsonld_types:
        rules.append({
            "check": "deployment.manual_observable_change", "required": True,
            "manual": True,
        })
    payload = {
        "task_id": task.get("id"),
        "asset_versions": [
            {key: row.get(key) for key in ("asset_id", "version", "sha256")}
            for row in assets
        ],
        "target_urls": target_urls,
        "expected_snippets": snippets,
        "expected_jsonld_types": jsonld_types,
        "acceptance_rules": rules,
    }
    return {
        **payload,
        "generated_at": generated_at,
        "assets": assets,
        "insertion_positions": list(dict.fromkeys(
            row["insertion_position"] for row in assets
        )),
        "default_channel": "website",
        "requires_web_owner_confirmation": True,
        "ready_for_submission": _assets_ready(task, delivery) and any(
            row.get("deployable") for row in assets
        ),
        "plan_sha256": _canonical_sha256(payload),
    }


def prepare_deployments_data(
    data: dict,
    cfg: dict,
    project_dir: Path,
    generated_at: str = "",
) -> dict:
    project = normalize_project_config(cfg)
    cycle_id = project["delivery"].get("current_cycle_id")
    out = normalize_tasks_data(data, cycle_id=cycle_id)
    planned = []
    for task in out["tasks"]:
        delivery = task["delivery"]
        if not _assets_ready(task, delivery):
            continue
        plan = build_deployment_plan(task, project, Path(project_dir), generated_at)
        delivery["deployment_plan"] = plan
        planned.append(task.get("id"))
    out["deployment_plan"] = {
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        "planned_task_ids": planned,
        "planned_count": len(planned),
    }
    out["deployment_review"] = deployment_status_data(out)
    return out


def _validate_public_url(value: str, field: str = "target_url") -> str:
    text = str(value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{field} 必须是有效的 http/https URL")
    host = (parsed.hostname or "").lower().rstrip(".")
    # A loopback target is rejected in normal operation.  The explicit
    # test-only switch lets the browser E2E harness verify a real re-fetch
    # against its controlled local website without weakening the default SSRF
    # boundary or making private deployment URLs acceptable to users.
    allow_local_test_target = (
        os.environ.get("GEO_E2E") == "1"
        and os.environ.get("GEO_ALLOW_LOCAL_DEPLOYMENT") == "1"
    )
    if not allow_local_test_target and (host in {"localhost", "localhost.localdomain"} or host.endswith(".local")):
        raise ValueError(f"{field} 不能指向本机或私有地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not allow_local_test_target and (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_reserved or address.is_unspecified
    ):
        raise ValueError(f"{field} 不能指向本机或私有地址")
    return text


def _deployment_check_snapshot(
    G: Any,
    target_url: str,
    expected_snippets: list[str],
    expected_jsonld_types: list[str],
    checked_at: str,
) -> dict:
    fetched = G.fetch(target_url)
    html = str(fetched.get("html") or "")
    fallback_types, jsonld_errors = _fallback_jsonld_details(html)
    if hasattr(G, "parse_html") and hasattr(G, "main_text"):
        try:
            soup = G.parse_html(html)
            visible_text = G.main_text(soup)
            types = G.jsonld_types(G.jsonld(soup)) if hasattr(G, "jsonld_types") and hasattr(G, "jsonld") else fallback_types
        except Exception:  # noqa: BLE001
            visible_text = _fallback_visible_text(html)
            types = fallback_types
    else:
        visible_text = _fallback_visible_text(html)
        types = fallback_types
    normalized_page = _normalized_text(visible_text).casefold()
    snippet_results = []
    for snippet in expected_snippets:
        normalized = _normalized_text(snippet)
        snippet_results.append({
            "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "excerpt": normalized[:120],
            "matched": normalized.casefold() in normalized_page if normalized else False,
        })
    expected_types = list(dict.fromkeys(str(value) for value in expected_jsonld_types if str(value).strip()))
    type_results = [{"type": value, "matched": value in types} for value in expected_types]
    status = int(fetched.get("status") or 0)
    http_verdict = "pass" if 200 <= status < 300 else (
        "manual" if status in {401, 403} else "fail"
    )
    snippets_verdict = "manual" if not snippet_results else (
        "pass" if all(row["matched"] for row in snippet_results) else "fail"
    )
    jsonld_verdict = "fail" if jsonld_errors else (
        "manual" if not type_results else (
            "pass" if all(row["matched"] for row in type_results) else "fail"
        )
    )
    observable_checks = []
    if snippet_results:
        observable_checks.append(snippets_verdict)
    if type_results:
        observable_checks.append(jsonld_verdict)
    automatic_verified = http_verdict == "pass" and bool(observable_checks) and all(
        verdict == "pass" for verdict in observable_checks
    )
    return {
        "checked_at": checked_at,
        "requested_url": target_url,
        "final_url": str(fetched.get("final_url") or target_url),
        "http_status": status,
        "content_type": str(fetched.get("content_type") or ""),
        "elapsed": fetched.get("elapsed"),
        "fetch_error": str(fetched.get("error") or ""),
        "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest() if html else "",
        "visible_text_sha256": hashlib.sha256(visible_text.encode("utf-8")).hexdigest() if visible_text else "",
        "visible_text_excerpt": _normalized_text(visible_text)[:500],
        "jsonld_types": types,
        "jsonld_errors": jsonld_errors,
        "checks": {
            "url_2xx": {"verdict": http_verdict, "status": status},
            "contains_text": {"verdict": snippets_verdict, "items": snippet_results},
            "jsonld_types": {
                "verdict": jsonld_verdict,
                "items": type_results,
                "errors": jsonld_errors,
            },
        },
        "automatic_verified": automatic_verified,
    }


def _deployment_error_snapshot(target_url: str, checked_at: str, exc: Exception) -> dict:
    """把首次提交或复查时的网络异常保存成可验收的失败快照。"""
    return {
        "checked_at": checked_at,
        "requested_url": target_url,
        "final_url": target_url,
        "http_status": 0,
        "content_type": "",
        "elapsed": None,
        "fetch_error": f"{type(exc).__name__}: {exc}",
        "html_sha256": "",
        "visible_text_sha256": "",
        "visible_text_excerpt": "",
        "jsonld_types": [],
        "jsonld_errors": [],
        "checks": {
            "url_2xx": {"verdict": "fail", "status": 0},
            "contains_text": {"verdict": "data_missing", "items": []},
            "jsonld_types": {"verdict": "data_missing", "items": [], "errors": []},
        },
        "automatic_verified": False,
    }


def _deployment_status_from_snapshot(
    snapshot: dict,
    channel: str,
    evidence_type: str,
    evidence_ref: str,
) -> tuple[str, bool, str]:
    if snapshot.get("automatic_verified") is True:
        return "verified", True, "automatic"
    http_verdict = ((snapshot.get("checks") or {}).get("url_2xx") or {}).get("verdict")
    http_status = int(snapshot.get("http_status") or 0)
    external_manual_ok = (
        channel != "website"
        and bool(evidence_ref)
        and (
            http_verdict == "pass"
            or http_status in {401, 403, 429}
            or (http_status == 0 and evidence_type in {"file", "screenshot", "manual"})
        )
    )
    if external_manual_ok:
        return "submitted", True, "manual_external"
    if http_verdict == "pass":
        return "reachable", False, "automatic_incomplete"
    return "failed", False, "automatic_failed"


def deployment_status_data(data: dict) -> dict:
    rows = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else []
    eligible = []
    deployed = []
    pending = []
    failed = []
    manual = []
    for task in rows:
        if not isinstance(task, dict):
            continue
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        if not _assets_ready(task, delivery):
            continue
        task_id = task.get("id")
        eligible.append(task_id)
        records = [row for row in delivery.get("deployments", []) if isinstance(row, dict)]
        if _deployment_stage(task, delivery):
            deployed.append(task_id)
            if records and records[-1].get("completion_method") == "manual_external":
                manual.append(task_id)
        elif records and records[-1].get("status") == "failed":
            failed.append(task_id)
        else:
            pending.append(task_id)
    return {
        "eligible_task_ids": eligible,
        "deployed_task_ids": deployed,
        "pending_deployment_task_ids": pending,
        "failed_deployment_task_ids": failed,
        "manual_evidence_task_ids": manual,
        "review_complete": len(deployed) == len(eligible),
    }


def signal_view(slug: str) -> dict:
    """Compatibility facade used by the dashboard project aggregator."""
    import signals
    return signals.signal_view(slug)


def delivery_overview_data(cfg: dict, data: dict, project_dir: Path | None = None) -> dict:
    """交付流水线总览的唯一聚合口径，供 dashboard 和测试复用。"""
    project = normalize_project_config(cfg)
    delivery_cfg = project["delivery"]
    cycle_id = delivery_cfg.get("current_cycle_id")
    normalized = normalize_tasks_data(data, cycle_id=None)
    scope_recommendation = (
        copy.deepcopy(normalized.get("scope_recommendation"))
        if isinstance(normalized.get("scope_recommendation"), dict) else {}
    )
    scope_review = validate_cycle_scope(normalized, project)
    tasks = [
        task for task in normalized.get("tasks", [])
        if cycle_id and (task.get("delivery") or {}).get("cycle_id") == cycle_id
    ]
    stage_counts = {stage: 0 for stage in DELIVERY_STAGES}
    waiting_customer = []
    waiting_deployment = []
    waiting_verification = []
    failed = []
    auto_verifiable = []
    for task in tasks:
        delivery = task["delivery"]
        stage = compute_stage(task)
        stage_counts[stage] += 1
        scope_status = (delivery.get("scope_decision") or {}).get("status", "pending")
        assets = [row for row in delivery.get("assets", []) if isinstance(row, dict) and row.get("required", True)]
        verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
        source_refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
        customer_gate = (
            (stage == "diagnosed" and scope_status in {"pending", "needs_evidence"})
            or any(
                isinstance(row, dict) and row.get("review_status") == "pending"
                for row in source_refs
            )
            or any(row.get("approval_status") == "pending" for row in assets)
            or (
                verification.get("review_required") is True
                and verification.get("review_status") == "pending"
            )
        )
        if customer_gate:
            waiting_customer.append(task.get("id"))
        if stage == "asset_ready":
            waiting_deployment.append(task.get("id"))
        if stage == "deployed":
            waiting_verification.append(task.get("id"))
        deployment_rows = delivery.get("deployments") if isinstance(delivery.get("deployments"), list) else []
        is_failed = (
            task.get("status") == "blocked"
            or any(row.get("approval_status") == "rejected" for row in assets)
            or any(isinstance(row, dict) and row.get("status") == "failed" for row in deployment_rows)
            or verification.get("verdict") in {"fail", "error"}
        )
        if is_failed:
            failed.append(task.get("id"))
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
        checks = _acceptance_rows(task)
        if acceptance.get("type") == "auto" and checks and not any(
            row.get("required", True) and (row.get("type") == "manual" or row.get("manual") is True)
            for row in checks
        ):
            auto_verifiable.append(task.get("id"))
    baseline_ref = delivery_cfg.get("current_baseline_ref")
    baseline_exists = bool(
        baseline_ref
        and project_dir is not None
        and (Path(project_dir) / str(baseline_ref)).is_file()
    )
    stage_rows = []
    for index, stage in enumerate(DELIVERY_STAGES):
        stage_rows.append({
            "stage": stage,
            "label": {
                "baseline": "基线锁定", "diagnosed": "证据诊断",
                "scoped": "范围确认", "assigned": "工单分派",
                "asset_ready": "资产就绪", "deployed": "部署完成",
                "verified": "验收通过",
            }[stage],
            "count": stage_counts[stage],
            "at_or_beyond": sum(
                count for candidate, count in stage_counts.items()
                if DELIVERY_STAGES.index(candidate) >= index
            ),
        })
    signal_summary = {}
    try:
        import signals
        signal_summary = signals.build_signal_summary(
            signals.read_signal_log(Path(project_dir) / "business_signals.jsonl")
        ) if project_dir is not None else signals.build_signal_summary([])
    except Exception:  # noqa: BLE001
        signal_summary = {"signal_rows": 0, "unavailable": True}
    total = len(tasks)
    return {
        "enabled": delivery_cfg.get("enabled") is True,
        "current_cycle": {
            "cycle_id": cycle_id,
            "status": "active" if cycle_id else "not_started",
            "baseline_ref": baseline_ref,
            "baseline_exists": baseline_exists,
            "industry": delivery_cfg.get("industry"),
            "target_markets": copy.deepcopy(delivery_cfg.get("target_markets", [])),
            "product_lines": copy.deepcopy(delivery_cfg.get("product_lines", [])),
            "conversion_goal": delivery_cfg.get("conversion_goal"),
            "conversion_goals": copy.deepcopy(delivery_cfg.get("conversion_goals", [])),
            "icps": copy.deepcopy(delivery_cfg.get("icps", [])),
            "strategic_priorities": copy.deepcopy(delivery_cfg.get("strategic_priorities", [])),
        },
        "task_total": total,
        "stage_counts": stage_counts,
        "stages": stage_rows,
        "waiting_customer_count": len(waiting_customer),
        "waiting_customer_task_ids": waiting_customer,
        "waiting_deployment_count": len(waiting_deployment),
        "waiting_deployment_task_ids": waiting_deployment,
        "waiting_verification_count": len(waiting_verification),
        "waiting_verification_task_ids": waiting_verification,
        "failed_count": len(failed),
        "failed_task_ids": failed,
        "p0_open_count": sum(
            task.get("priority") == "P0" and task.get("status") not in {"done", "wontfix"}
            for task in tasks
        ),
        "auto_verifiable_count": len(auto_verifiable),
        "auto_verifiable_ratio": (len(auto_verifiable) / total) if total else 0.0,
        "scope_recommendation": scope_recommendation,
        "scope_review": scope_review,
        "business_signals": signal_summary,
    }


def verification_verdict_counts(results: list[dict]) -> dict:
    """兼容旧中文与新结构化 verdict，作为 dashboard 历史聚合统一口径。"""
    rows = [row for row in results if isinstance(row, dict)]
    return {
        "pass": sum(row.get("verdict") in {"通过", "pass"} for row in rows),
        "fail": sum(row.get("verdict") in {"未达标", "fail", "error"} for row in rows),
        "manual": sum(row.get("verdict") in {"待人工", "manual", "data_missing"} for row in rows),
    }


def _redact_sensitive(value: Any) -> Any:
    """API 投影不泄露未来扩展字段中可能出现的凭证。"""
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    out = {}
    for key, item in value.items():
        folded = str(key).strip().lower()
        if (
            folded in {"api_key", "apikey", "password", "passwd", "secret", "token", ".env"}
            or folded.endswith(("_api_key", "_password", "_secret", "_token"))
        ):
            continue
        out[key] = _redact_sensitive(item)
    return out


def delivery_view(slug: str) -> dict:
    """供 CLI/API 共用的当前周期完整交付数据只读投影。"""
    import geolib as G

    project_dir = G.project_dir(slug)
    cfg = normalize_project_config(G.load_config(slug))
    cycle_id = cfg["delivery"].get("current_cycle_id")
    data = normalize_tasks_data(_load_raw_tasks(G, slug), cycle_id=cycle_id)
    tasks = [
        row for row in data.get("tasks", [])
        if not cycle_id or ((row.get("delivery") or {}).get("cycle_id") == cycle_id)
    ]
    scoped_data = {**data, "tasks": tasks}
    overview = delivery_overview_data(cfg, scoped_data, project_dir)
    log = (
        read_cycle_event_log(project_dir, str(cycle_id))
        if cycle_id else {"event_log_ref": None, "events": [], "warnings": []}
    )
    warnings = list(log["warnings"])
    if cycle_id and not overview["current_cycle"].get("baseline_exists"):
        warnings.append({"code": "baseline_missing", "cycle_id": cycle_id})
    for task in tasks:
        warning = ((task.get("delivery") or {}).get("stage_warning"))
        if warning:
            warnings.append({
                "code": "stage_mismatch", "task_id": task.get("id"), "message": warning,
            })
    return _redact_sensitive({
        "project": {
            "slug": slug,
            "brand": copy.deepcopy(cfg.get("brand") or {}),
            "industry": cfg["delivery"].get("industry"),
            "target_markets": copy.deepcopy(cfg["delivery"].get("target_markets") or []),
            "product_lines": copy.deepcopy(cfg["delivery"].get("product_lines") or []),
            "conversion_goal": cfg["delivery"].get("conversion_goal"),
            "conversion_goals": copy.deepcopy(cfg["delivery"].get("conversion_goals") or []),
            "icps": copy.deepcopy(cfg["delivery"].get("icps") or []),
            "strategic_priorities": copy.deepcopy(cfg["delivery"].get("strategic_priorities") or []),
        },
        "cycle": overview["current_cycle"],
        "summary": {key: copy.deepcopy(value) for key, value in overview.items() if key not in {"current_cycle", "stages"}},
        "stages": overview["stages"],
        "tasks": tasks,
        "events": log["events"],
        "event_log_ref": log["event_log_ref"],
        "warnings": warnings,
        "business_signals": overview.get("business_signals", {}),
    })


def _new_cycle_id() -> str:
    return f"{datetime.now().strftime('%Y-%m-%d')}-{secrets.token_hex(4)}"


def _load_raw_tasks(G: Any, slug: str) -> dict:
    """直接解析 tasks.json，避免兼容读取的默认值掩盖 JSON 损坏。"""
    path = G.project_dir(slug) / "tasks.json"
    if not path.exists():
        return {"tasks": []}
    raw = json.loads(path.read_text("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("tasks.json 顶层必须是对象；已拒绝迁移")
    return raw


def record_scope_confirmation(
    slug: str,
    selected_problem_ids: list[str],
    pending_fact_ids: list[str],
    note: str = "",
) -> dict:
    """记录 Project Owner 对当前项目范围的显式确认。

    ``scope_sha256`` 将确认与当时的官网、客户画像、竞品、问题
    范围和事实复核结果绑定。任一范围字段变更后，原确认失效。
    """
    import geolib as G

    if not isinstance(selected_problem_ids, list):
        raise TypeError("selected_problem_ids 必须是数组")
    if not isinstance(pending_fact_ids, list):
        raise TypeError("pending_fact_ids 必须是数组，可为空数组")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        base_validation = validate_scope(cfg, require_confirmation=False)
        if not base_validation["valid"]:
            raise ScopeValidationError(base_validation)

        selected = _clean_string_list(selected_problem_ids)
        pending = _clean_string_list(pending_fact_ids)
        max_scoped = _scope_limit(cfg)
        local_errors = []
        if not selected:
            local_errors.append({
                "field": "delivery.scope_confirmation.selected_problem_ids",
                "code": "required",
                "message": "至少确认一个本周期问题",
            })
        if isinstance(max_scoped, int) and len(selected) > max_scoped:
            local_errors.append({
                "field": "delivery.scope_confirmation.selected_problem_ids",
                "code": "scope_limit_exceeded",
                "message": f"已选 {len(selected)} 项，超过本周期上限 {max_scoped}",
            })
        unknown = sorted(set(selected) - _question_ids(cfg))
        if unknown:
            local_errors.append({
                "field": "delivery.scope_confirmation.selected_problem_ids",
                "code": "unknown_problem",
                "message": f"范围包含问题库中不存在的 ID：{'、'.join(unknown)}",
            })
        if local_errors:
            raise ScopeValidationError({
                "valid": False,
                "errors": local_errors,
                "warnings": base_validation["warnings"],
            })

        record = {
            "scope_schema_version": "2.0",
            "status": "approved",
            "role": "project_owner",
            "approved_by_role_name": cfg["delivery"]["roles"]["project_owner"],
            "at": G.now_iso(),
            "selected_problem_ids": selected,
            "facts_reviewed": True,
            "pending_fact_ids": pending,
            "note": _validated_text(note, "note", required=False),
        }
        record["scope_sha256"] = _canonical_sha256(
            _scope_lock_payload(cfg, record)
        )

        active_ref = cfg["delivery"].get("current_baseline_ref")
        if active_ref:
            baseline_path = G.project_dir(slug) / active_ref
            if baseline_path.exists():
                locked = _strict_read_json(baseline_path, "baseline.json")
                locked_hash = (locked.get("scope") or {}).get("scope_sha256")
                if locked_hash != record["scope_sha256"]:
                    raise ScopeValidationError({
                        "valid": False,
                        "errors": [{
                            "field": "delivery.scope_confirmation.scope_sha256",
                            "code": "baseline_locked",
                            "message": "active cycle 基线已锁定，不能用新范围覆盖",
                        }],
                        "warnings": base_validation["warnings"],
                    })
                return copy.deepcopy(cfg["delivery"]["scope_confirmation"])
        cfg["delivery"]["scope_confirmation"] = record

        validation = validate_scope(cfg, require_confirmation=True)
        if not validation["valid"]:
            raise ScopeValidationError(validation)
        G.save_config(slug, cfg)
    return copy.deepcopy(record)


def _strict_read_json(path: Any, label: str) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"缺少 {label}：{path}")
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} 顶层必须是对象")
    return value


def _issue_fingerprint(audit: dict) -> dict:
    rows = []
    for page in audit.get("pages", []) if isinstance(audit.get("pages"), list) else []:
        if not isinstance(page, dict):
            continue
        codes = page.get("issue_codes")
        if not isinstance(codes, list):
            codes = page.get("issues") if isinstance(page.get("issues"), list) else []
        rows.append({
            "url": page.get("url"),
            "score": page.get("score"),
            "issues": sorted(str(code) for code in codes),
        })
    rows.sort(key=lambda row: str(row.get("url") or ""))
    return {
        "page_issue_count": sum(len(row["issues"]) for row in rows),
        "affected_page_count": sum(bool(row["issues"]) for row in rows),
        "sha256": _canonical_sha256(rows),
    }


def _latest_metrics_snapshot(project_dir: Any) -> dict:
    metrics_dir = project_dir / "metrics"
    files = sorted(metrics_dir.glob("*.json")) if metrics_dir.exists() else []
    if not files:
        return {"status": "missing", "file": None, "date": None}
    latest = files[-1]
    data = _strict_read_json(latest, "metrics")
    return {
        "status": "captured",
        "file": f"metrics/{latest.name}",
        "date": data.get("date"),
        "sample_count": data.get("sample_count"),
        "question_count": data.get("question_count"),
    }


def _build_baseline_snapshot(G: Any, slug: str, cfg: dict, cycle_id: str) -> dict:
    project_dir = G.project_dir(slug)
    audit = _strict_read_json(project_dir / "audit.json", "audit.json")
    delivery_cfg = cfg["delivery"]
    confirmation = delivery_cfg["scope_confirmation"]
    questions = cfg.get("questions") if isinstance(cfg.get("questions"), list) else []
    brand = cfg.get("brand") if isinstance(cfg.get("brand"), dict) else {}
    audit_site = audit.get("site") if isinstance(audit.get("site"), dict) else {}
    site_keys = (
        "robots_status",
        "ai_bots_blocked",
        "has_sitemap",
        "sitemap_url_count",
        "has_llms_txt",
    )
    audit_site_summary = {
        key: copy.deepcopy(audit_site[key])
        for key in site_keys
        if key in audit_site
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "created_at": G.now_iso(),
        "project": {
            "slug": slug,
            "brand": brand.get("name"),
            "site": brand.get("site"),
        },
        "scope": {
            "customer_profile": copy.deepcopy(delivery_cfg["customer_profile"]),
            "competitors": copy.deepcopy(cfg.get("competitors") or []),
            "selected_problem_ids": copy.deepcopy(
                confirmation["selected_problem_ids"]
            ),
            "project_owner": confirmation.get("approved_by_role_name"),
            "scope_sha256": confirmation["scope_sha256"],
            "confirmed_at": confirmation["at"],
        },
        "baseline": {
            "audit": {
                "file": "audit.json",
                "generated_at": audit.get("generated_at") or audit.get("audited_at"),
                "avg_score": audit.get("avg_score"),
                "page_count": audit.get("page_count", len(audit.get("pages", []))),
                "site": audit_site_summary,
            },
            "issues": _issue_fingerprint(audit),
            "question_bank": {
                "count": len(questions),
                "sha256": _canonical_sha256(questions),
            },
            "metrics": _latest_metrics_snapshot(project_dir),
        },
        "facts": {
            "reviewed": True,
            "pending_fact_ids": copy.deepcopy(
                confirmation.get("pending_fact_ids", [])
            ),
        },
    }


def _bind_tasks_to_cycle(G: Any, T: Any, slug: str, cycle_id: str) -> int:
    path = G.project_dir(slug) / "tasks.json"
    if not path.exists():
        return 0
    raw = _load_raw_tasks(G, slug)
    normalized = normalize_tasks_data(raw, cycle_id=cycle_id)
    if normalized != raw:
        T.save(slug, normalized)
    return len(normalized.get("tasks", []))


def _upsert_cycle_history(delivery_cfg: dict, record: dict) -> None:
    rows = delivery_cfg.setdefault("cycle_history", [])
    for index, row in enumerate(rows):
        if isinstance(row, dict) and row.get("cycle_id") == record.get("cycle_id"):
            rows[index] = {**row, **copy.deepcopy(record)}
            return
    rows.append(copy.deepcopy(record))


def _append_cycle_started_events(
    project_dir: Path,
    cycle_id: str,
    snapshot: dict,
) -> str:
    """幂等补齐周期创建、范围批准和基线锁定事件。"""
    created_at = str(snapshot.get("created_at") or "")
    scope = snapshot.get("scope") if isinstance(snapshot.get("scope"), dict) else {}
    candidates = [
        _milestone_event(
            cycle_id, "", "cycle_created", "system", created_at,
            cycle_id, {"baseline_ref": f"delivery/snapshots/{cycle_id}/baseline.json"},
        ),
        _milestone_event(
            cycle_id, "", "scope_approved", "project_owner",
            str(scope.get("confirmed_at") or created_at),
            str(scope.get("scope_sha256") or cycle_id),
            {
                "project_scope": True,
                "scope_sha256": scope.get("scope_sha256"),
                "selected_problem_ids": copy.deepcopy(scope.get("selected_problem_ids") or []),
            },
        ),
        _milestone_event(
            cycle_id, "", "baseline_locked", "system", created_at,
            f"delivery/snapshots/{cycle_id}/baseline.json",
            {"baseline_ref": f"delivery/snapshots/{cycle_id}/baseline.json"},
        ),
    ]
    existing = {
        row.get("event_id") for row in read_cycle_events(project_dir, cycle_id)
        if isinstance(row, dict)
    }
    return _append_events(
        project_dir, cycle_id,
        [row for row in candidates if row.get("event_id") not in existing],
    )


def create_cycle(slug: str) -> dict:
    """校验已确认范围并幂等创建 ``baseline.json``。

    原子性由 GeoLook ``G.write_json`` 保证。已存在的基线永不覆盖；
    重复调用只会返回原快照并确保现有工单绑定同一周期。
    """
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        raw_cfg = G.load_config(slug)
        cfg = normalize_project_config(raw_cfg)
        if cfg["delivery"].get("enabled") is not True:
            raise ValueError("delivery.enabled=false，不能创建交付周期")
        validation = validate_scope(cfg, require_confirmation=True)
        if not validation["valid"]:
            raise ScopeValidationError(validation)

        delivery_cfg = cfg["delivery"]
        cycle_id = delivery_cfg.get("current_cycle_id")
        if not cycle_id:
            known_ids = {
                row.get("cycle_id") for row in delivery_cfg.get("cycle_history", [])
                if isinstance(row, dict)
            }
            cycle_id = _new_cycle_id()
            while (
                cycle_id in known_ids
                or (G.project_dir(slug) / "delivery" / "snapshots" / cycle_id).exists()
            ):
                cycle_id = _new_cycle_id()
        relative_ref = f"delivery/snapshots/{cycle_id}/baseline.json"
        path = G.project_dir(slug) / relative_ref

        if path.exists():
            snapshot = _strict_read_json(path, "baseline.json")
            if snapshot.get("cycle_id") != cycle_id:
                raise ValueError("baseline.json 的 cycle_id 与目录不一致")
            locked_hash = (snapshot.get("scope") or {}).get("scope_sha256")
            current_hash = delivery_cfg["scope_confirmation"]["scope_sha256"]
            if locked_hash != current_hash:
                raise ScopeValidationError({
                    "valid": False,
                    "errors": [{
                        "field": "delivery.scope_confirmation.scope_sha256",
                        "code": "baseline_locked",
                        "message": "active cycle 基线与当前范围不一致，已拒绝覆盖",
                    }],
                    "warnings": validation["warnings"],
                })
            delivery_cfg["current_cycle_id"] = cycle_id
            delivery_cfg["current_baseline_ref"] = relative_ref
            _upsert_cycle_history(delivery_cfg, {
                "cycle_id": cycle_id, "status": "active",
                "baseline_ref": relative_ref,
                "started_at": snapshot.get("created_at"),
            })
            _append_cycle_started_events(G.project_dir(slug), cycle_id, snapshot)
            cfg["delivery"] = delivery_cfg
            if cfg != raw_cfg:
                G.save_config(slug, cfg)
            _bind_tasks_to_cycle(G, T, slug, cycle_id)
            return snapshot

        snapshot = _build_baseline_snapshot(G, slug, cfg, cycle_id)
        # 必须先成功原子写快照，再对外声称 active cycle 已存在。
        G.write_json(path, snapshot)
        _append_cycle_started_events(G.project_dir(slug), cycle_id, snapshot)

        delivery_cfg["current_cycle_id"] = cycle_id
        delivery_cfg["current_baseline_ref"] = relative_ref
        delivery_cfg["baseline_locked_at"] = snapshot["created_at"]
        _upsert_cycle_history(delivery_cfg, {
            "cycle_id": cycle_id, "status": "active",
            "baseline_ref": relative_ref,
            "started_at": snapshot["created_at"],
        })
        cfg["delivery"] = delivery_cfg
        G.save_config(slug, cfg)
        _bind_tasks_to_cycle(G, T, slug, cycle_id)
        return snapshot


def end_cycle(
    slug: str,
    note: str,
    role: str = "project_owner",
    allow_open: bool = False,
) -> dict:
    """显式结束当前周期；默认拒绝遗留未关闭工单。"""
    import geolib as G

    if str(role or "").strip() != "project_owner":
        raise ValueError("交付周期只能由 project_owner 结束")
    note = _validated_text(note, "结束周期说明")
    with G.project_lock(slug):
        raw_cfg = G.load_config(slug)
        cfg = normalize_project_config(raw_cfg)
        delivery_cfg = cfg["delivery"]
        cycle_id = str(delivery_cfg.get("current_cycle_id") or "")
        if not cycle_id:
            raise ValueError("当前没有 active delivery cycle")
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cycle_id=None)
        cycle_tasks = [
            task for task in data.get("tasks", [])
            if ((task.get("delivery") or {}).get("cycle_id") == cycle_id)
        ]
        open_tasks = [
            task for task in cycle_tasks
            if task.get("status") not in {"done", "wontfix"}
        ]
        if open_tasks and not allow_open:
            raise ValueError(
                f"当前周期仍有 {len(open_tasks)} 条未关闭工单；"
                "处理完毕或显式 allow_open 后再结束"
            )
        now = G.now_iso()
        event = _event(
            cycle_id, "", "cycle_ended", now,
            {
                "task_count": len(cycle_tasks),
                "open_task_count": len(open_tasks),
                "note": note,
            },
            actor_role="project_owner",
        )
        event_ref = _append_events(G.project_dir(slug), cycle_id, [event])
        _upsert_cycle_history(delivery_cfg, {
            "cycle_id": cycle_id, "status": "ended",
            "baseline_ref": delivery_cfg.get("current_baseline_ref"),
            "ended_at": now, "ended_by_role": "project_owner",
            "end_note": note, "task_count": len(cycle_tasks),
            "open_task_count": len(open_tasks), "event_log_ref": event_ref,
        })
        delivery_cfg["current_cycle_id"] = None
        delivery_cfg["current_baseline_ref"] = None
        delivery_cfg["cycle_ended_at"] = now
        cfg["delivery"] = delivery_cfg
        G.save_config(slug, cfg)
    ended_cycle = next(
        item for item in delivery_cfg["cycle_history"] if item.get("cycle_id") == cycle_id
    )
    return copy.deepcopy(ended_cycle)


def start_new_cycle(slug: str) -> dict:
    """在没有活动周期时校验当前范围并创建全新的 cycle_id 与基线。"""
    import geolib as G

    cfg = normalize_project_config(G.load_config(slug))
    if cfg["delivery"].get("current_cycle_id"):
        raise ValueError("已有活动周期；必须先显式 end_cycle()")
    return create_cycle(slug)


def _read_jsonl_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {line_no} 行不是有效 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} 第 {line_no} 行必须是对象")
        rows.append(row)
    return rows


def load_diagnosis_inputs(project_dir: Path) -> dict:
    metric_files = sorted((project_dir / "metrics").glob("*.json")) if (project_dir / "metrics").exists() else []
    metrics_path = metric_files[-1] if metric_files else None
    metrics = _strict_read_json(metrics_path, "metrics") if metrics_path else None
    sample_path = None
    if metrics:
        date = str(metrics.get("date") or "").strip()
        candidate = project_dir / "samples" / f"{date}.jsonl"
        if date and candidate.exists():
            sample_path = candidate
    if sample_path is None and (project_dir / "samples").exists():
        sample_files = sorted((project_dir / "samples").glob("*.jsonl"))
        sample_path = sample_files[-1] if sample_files else None
    facts_path = project_dir / "content" / "facts.md"
    return {
        "metrics": metrics,
        "metrics_ref": f"metrics/{metrics_path.name}" if metrics_path else None,
        "samples": _read_jsonl_file(sample_path) if sample_path else [],
        "samples_ref": f"samples/{sample_path.name}" if sample_path else None,
        "facts_text": facts_path.read_text("utf-8") if facts_path.exists() else "",
        "facts_ref": "content/facts.md",
    }


def diagnose_tasks(slug: str) -> dict:
    """读取现有项目输入并原子写回带证据的候选诊断。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        project_dir = G.project_dir(slug)
        audit = _strict_read_json(project_dir / "audit.json", "audit.json")
        raw = _load_raw_tasks(G, slug)
        inputs = load_diagnosis_inputs(project_dir)
        captured_at = G.now_iso()
        data = diagnose_tasks_data(
            raw,
            audit=audit,
            cfg=cfg,
            materials=cfg.get("materials"),
            captured_at=captured_at,
            cycle_id=cfg["delivery"].get("current_cycle_id"),
            **inputs,
        )
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if cycle_id:
            existing_ids = {
                row.get("event_id") for row in read_cycle_events(project_dir, str(cycle_id))
                if isinstance(row, dict)
            }
            events = []
            for task in data.get("tasks", []):
                refs = ((task.get("delivery") or {}).get("source_refs") or [])
                if not refs:
                    continue
                source_id = _canonical_sha256([
                    {key: row.get(key) for key in ("id", "type", "ref", "review_status")}
                    for row in refs if isinstance(row, dict)
                ])[:16]
                event = _milestone_event(
                    str(cycle_id), str(task.get("id") or ""), "diagnosis_created",
                    "system", captured_at, source_id,
                    {"evidence_count": len(refs), "case_type": (task.get("delivery") or {}).get("case_type")},
                )
                if event["event_id"] not in existing_ids:
                    events.append(event)
            _append_events(project_dir, str(cycle_id), events)
        T.save(slug, data)
    return data


def _refresh_diagnosis(task: dict) -> None:
    delivery = task["delivery"]
    refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    validation = validate_diagnosis(task)
    diagnosis = delivery.get("diagnosis") if isinstance(delivery.get("diagnosis"), dict) else {}
    diagnosis["confidence"] = (
        "high" if any(row.get("confidence") == "high" for row in refs if isinstance(row, dict))
        else "medium" if any(row.get("confidence") == "medium" for row in refs if isinstance(row, dict))
        else "low"
    )
    diagnosis["review_required"] = any(
        isinstance(row, dict) and row.get("review_status") == "pending"
        for row in refs
    )
    diagnosis["review_reasons"] = _review_reasons(task, refs)
    diagnosis["ready_for_scope"] = validation["ready_for_scope"]
    diagnosis["evidence_count"] = len(refs)
    diagnosis["confirmed_evidence_count"] = validation["confirmed_count"]
    delivery["diagnosis"] = diagnosis
    delivery["stage"] = compute_stage(task)
    delivery.pop("stage_warning", None)


def review_source_ref(
    slug: str,
    task_id: str,
    source_ref_id: str,
    status: str,
    note: str,
    role: str = "geo_operator",
) -> dict:
    """确认或排除一条诊断证据；人工判断说明为强制字段。"""
    import geolib as G
    import tasks as T

    if status not in {"confirmed", "rejected"}:
        raise ValueError("status 必须是 confirmed 或 rejected")
    if role not in REVIEW_ROLES:
        raise ValueError("role 必须是 geo_operator 或 reviewer")
    note = _validated_text(note, "人工判断 note")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        data = normalize_tasks_data(
            _load_raw_tasks(G, slug),
            cycle_id=cfg["delivery"].get("current_cycle_id"),
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        ref = next(
            (row for row in task["delivery"]["source_refs"] if row.get("id") == source_ref_id),
            None,
        )
        if ref is None:
            raise KeyError(f"工单 {task_id} 找不到证据 {source_ref_id}")
        now = G.now_iso()
        ref.update({
            "review_status": status,
            "reviewed_by_role": role,
            "reviewed_by_role_name": (cfg["delivery"].get("roles") or {}).get(role, ""),
            "reviewed_at": now,
            "review_note": note,
        })
        _refresh_diagnosis(task)
        validation = validate_diagnosis(task)
        if not validation["valid"]:
            raise ValueError("；".join(row["message"] for row in validation["errors"]))
        cycle_id = task["delivery"].get("cycle_id")
        if cycle_id:
            _append_events(G.project_dir(slug), str(cycle_id), [_event(
                str(cycle_id), str(task_id), f"evidence_{status}", now,
                {
                    "source_ref_id": source_ref_id,
                    "type": ref.get("type"),
                    "ref": ref.get("ref"),
                    "note": note,
                },
                actor_role=role,
            )])
        T.save(slug, data)
    return copy.deepcopy(task)


def add_manual_source_ref(
    slug: str,
    task_id: str,
    label: str,
    note: str,
    ref: str = "",
    role: str = "geo_operator",
    confidence: str = "medium",
) -> dict:
    """给无法确定性映射的诊断补一条待复核人工证据。"""
    import geolib as G
    import tasks as T

    if role not in REVIEW_ROLES:
        raise ValueError("role 必须是 geo_operator 或 reviewer")
    if confidence not in CONFIDENCE_LEVELS:
        raise ValueError("confidence 必须是 high、medium 或 low")
    label = _validated_text(label, "manual 证据 label", max_length=200)
    note = _validated_text(note, "manual 证据 note")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        data = normalize_tasks_data(
            _load_raw_tasks(G, slug),
            cycle_id=cfg["delivery"].get("current_cycle_id"),
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        actual_ref = str(ref or "").strip() or (
            "manual:" + hashlib.sha256(f"{task_id}\x1f{label}\x1f{note}".encode("utf-8")).hexdigest()[:12]
        )
        now = G.now_iso()
        row = _source_ref(
            task_id, "manual", actual_ref, label, now,
            {"note": _compact_text(note, 500)}, confidence,
        )
        row.update({
            "note": note,
            "binding_method": "manual",
            "submitted_by_role": role,
            "submitted_by_role_name": (cfg["delivery"].get("roles") or {}).get(role, ""),
            "submitted_at": now,
        })
        refs = task["delivery"]["source_refs"]
        refs[:] = [existing for existing in refs if existing.get("id") != row["id"]]
        refs.append(row)
        task["delivery"]["case_type"] = classify_case_type(task)
        _refresh_diagnosis(task)
        validation = validate_diagnosis(task)
        if not validation["valid"]:
            raise ValueError("；".join(item["message"] for item in validation["errors"]))
        cycle_id = task["delivery"].get("cycle_id")
        if cycle_id:
            _append_events(G.project_dir(slug), str(cycle_id), [_event(
                str(cycle_id), str(task_id), "diagnosis_created", now,
                {
                    "source_ref_id": row.get("id"),
                    "type": row.get("type"),
                    "ref": row.get("ref"),
                    "note": note,
                },
                actor_role=role,
            )])
        T.save(slug, data)
    return copy.deepcopy(row)


def recommend_scope(slug: str) -> dict:
    """写回有限、可解释的范围建议，不产生任何人工批准。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        if not cfg["delivery"].get("current_cycle_id"):
            raise ValueError("尚未锁定 active cycle，不能生成阶段 3 范围建议")
        data = recommend_scope_data(
            _load_raw_tasks(G, slug),
            cfg,
            generated_at=G.now_iso(),
        )
        data["scope_review"] = validate_cycle_scope(data, cfg)
        T.save(slug, data)
    return data


def record_scope_decision(
    slug: str,
    task_id: str,
    status: str,
    reason: str,
) -> dict:
    """记录 Project Owner 对单条候选诊断的范围决策。"""
    import geolib as G
    import tasks as T

    if status not in SCOPE_DECISION_STATUSES:
        raise ValueError("status 必须是 approved、deferred、rejected 或 needs_evidence")
    reason = _validated_text(reason, "Project Owner 范围决策 reason")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if not cycle_id:
            raise ValueError("尚未锁定 active cycle，不能进行范围决策")
        role_name = str((cfg["delivery"].get("roles") or {}).get("project_owner") or "").strip()
        if not role_name:
            raise ValueError("geo.json.delivery.roles.project_owner 未设置")
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cycle_id=cycle_id)
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        if delivery.get("cycle_id") != cycle_id:
            raise ValueError(f"工单 {task_id} 不属于 active cycle {cycle_id}")
        if status != "approved" and delivery.get("stage") in {
            "assigned", "asset_ready", "deployed", "verified",
        }:
            raise ValueError("工单已进入执行，不能直接改为非批准范围")
        previous_decision = (
            delivery.get("scope_decision")
            if isinstance(delivery.get("scope_decision"), dict)
            else {}
        )
        if (
            previous_decision.get("status") == status
            and previous_decision.get("reason") == reason
        ):
            return copy.deepcopy(task)

        if status == "approved":
            diagnosis = validate_diagnosis(task, require_confirmed=True)
            if not diagnosis["valid"]:
                raise ValueError("；".join(row["message"] for row in diagnosis["errors"]))
            already_approved = sum(
                1 for row in data["tasks"]
                if row.get("id") != task_id
                and ((row.get("delivery") or {}).get("scope_decision") or {}).get("status") == "approved"
            )
            if already_approved >= _scope_limit(cfg):
                raise ValueError(f"Public Alpha 已达到 {_scope_limit(cfg)} 条批准上限")

        recommendation = delivery.get("scope_recommendation")
        if not isinstance(recommendation, dict) or not recommendation.get("priority_score"):
            recommendation = score_candidate(task, cfg)
        priority = delivery.get("priority") if isinstance(delivery.get("priority"), dict) else {}
        if not priority:
            import prioritize
            priority = prioritize.score_task(task, cfg)
            delivery["priority"] = copy.deepcopy(priority)
        recommendation_kind = str(recommendation.get("recommendation") or "")
        recommended_decision = {
            "recommended": "approved",
            "next_cycle": "deferred",
            "needs_evidence": "needs_evidence",
        }.get(recommendation_kind)
        manual_adjustment = bool(recommended_decision and status != recommended_decision)
        now = G.now_iso()
        decision = {
            "decision_id": f"scope-{secrets.token_hex(6)}",
            "status": status,
            "decided_by_role": "project_owner",
            "decided_by_role_name": role_name,
            "decided_at": now,
            "reason": reason,
            "priority_score": recommendation.get("priority_score"),
            "recommended_priority": recommendation.get("recommended_priority"),
            "dimensions": copy.deepcopy(recommendation.get("dimensions") or {}),
            "recommended_decision": recommended_decision,
            "manual_adjustment": manual_adjustment,
            "adjustment_reason": reason if manual_adjustment else "",
            "priority": copy.deepcopy(priority),
        }
        delivery["scope_decision"] = decision
        if status == "approved":
            delivery["next_action"] = "进入阶段 4，确认负责人、动作、依赖和验收条件"
        elif status == "needs_evidence":
            delivery["next_action"] = "补充并确认诊断证据后重新提交范围审批"
        elif status == "deferred":
            delivery["next_action"] = "保留到后续周期重新评估"
        else:
            delivery["next_action"] = "本周期不再推进"
        delivery["stage"] = compute_stage(task)
        delivery.pop("stage_warning", None)

        data["scope_review"] = validate_cycle_scope(data, cfg)
        if not data["scope_review"]["valid"]:
            raise ValueError("；".join(row["message"] for row in data["scope_review"]["errors"]))
        _append_events(G.project_dir(slug), str(cycle_id), [_event(
            str(cycle_id), str(task_id), f"scope_{status}", now,
            {
                "decision_id": decision["decision_id"],
                "status": status,
                "reason": reason,
                "priority_score": decision.get("priority_score"),
                "recommended_decision": recommended_decision,
                "manual_adjustment": manual_adjustment,
            },
            actor_role="project_owner",
        )])
        T.save(slug, data)
    return copy.deepcopy(task)


def scope_status(slug: str) -> dict:
    """返回当前周期已批准范围和逐条决策完成度，不写文件。"""
    import geolib as G

    cfg = normalize_project_config(G.load_config(slug))
    data = normalize_tasks_data(
        _load_raw_tasks(G, slug),
        cycle_id=cfg["delivery"].get("current_cycle_id"),
    )
    return validate_cycle_scope(data, cfg)


def prepare_assignments(slug: str) -> dict:
    """显式补齐已批准工单的执行规格，但不代替负责人确认。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        if not cfg["delivery"].get("current_cycle_id"):
            raise ValueError("尚未锁定 active cycle，不能准备可分派工单")
        data = prepare_assignments_data(
            _load_raw_tasks(G, slug), cfg, prepared_at=G.now_iso()
        )
        data["assignment_review"] = assignment_status_data(data)
        T.save(slug, data)
    return data


def assignment_status_data(data: dict) -> dict:
    rows = data.get("tasks") if isinstance(data, dict) and isinstance(data.get("tasks"), list) else []
    approved = []
    confirmed = []
    blocked = []
    pending = []
    incomplete = []
    errors = []
    for task in rows:
        if not isinstance(task, dict):
            continue
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        if ((delivery.get("scope_decision") or {}).get("status") != "approved"):
            continue
        task_id = task.get("id")
        approved.append(task_id)
        assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
        status = assignment.get("status", "pending")
        if status == "confirmed":
            validation = validate_assignment(task)
            if validation["valid"]:
                confirmed.append(task_id)
            else:
                incomplete.append(task_id)
                errors.extend({"task_id": task_id, **row} for row in validation["errors"])
        elif status == "blocked":
            blocker = str(assignment.get("blocker") or delivery.get("blocker") or "").strip()
            if blocker:
                blocked.append(task_id)
            else:
                incomplete.append(task_id)
                errors.append({
                    "task_id": task_id,
                    "field": "delivery.assignment.blocker",
                    "code": "required",
                    "message": "blocked 工单必须记录阻塞原因",
                })
        elif status == "pending":
            pending.append(task_id)
            validation = validate_assignment(task)
            if not validation["valid"]:
                incomplete.append(task_id)
        else:
            incomplete.append(task_id)
            errors.append({
                "task_id": task_id,
                "field": "delivery.assignment.status",
                "code": "invalid_enum",
                "message": "assignment status 无效",
            })
    return {
        "approved_task_ids": approved,
        "assignable_task_ids": confirmed,
        "blocked_task_ids": blocked,
        "pending_confirmation_task_ids": pending,
        "incomplete_task_ids": list(dict.fromkeys(incomplete)),
        "review_complete": len(confirmed) + len(blocked) == len(approved) and not errors,
        "errors": errors,
    }


def confirm_assignment(
    slug: str,
    task_id: str,
    status: str,
    note: str,
    role: str = "",
) -> dict:
    """由负责人确认可执行，或用明确原因阻塞工单。"""
    import geolib as G
    import tasks as T

    if status not in {"confirmed", "blocked"}:
        raise ValueError("status 必须是 confirmed 或 blocked")
    note = _validated_text(note, "负责人确认 note")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if not cycle_id:
            raise ValueError("尚未锁定 active cycle，不能确认分派")
        data = prepare_assignments_data(
            _load_raw_tasks(G, slug), cfg, prepared_at=G.now_iso()
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        if ((delivery.get("scope_decision") or {}).get("status") != "approved"):
            raise ValueError("只有 Project Owner 已批准的诊断可以确认分派")
        assignment = delivery["assignment"]
        owner_role = assignment.get("owner_role")
        acting_role = str(role or owner_role or "").strip()
        if acting_role != owner_role or acting_role not in DELIVERY_ROLE_KEYS:
            raise ValueError(f"必须由负责人角色 {owner_role or '未映射'} 确认")

        now = G.now_iso()
        if status == "blocked":
            assignment.update({
                "status": "blocked",
                "blocker": note,
                "blocked_at": now,
                "blocked_by_role": acting_role,
                "confirmation_note": note,
                "executable": False,
            })
            delivery["blocker"] = note
            delivery["next_action"] = "解除阻塞后由负责人重新确认任务可执行"
            task["status"] = "blocked"
        else:
            validation = validate_assignment(task)
            if not validation["valid"]:
                raise ValueError("；".join(row["message"] for row in validation["errors"]))
            assignment.update({
                "status": "confirmed",
                "confirmed_at": now,
                "confirmed_by_role": acting_role,
                "confirmed_by_role_name": assignment.get("owner_role_name") or task.get("owner"),
                "confirmation_note": note,
                "confirmed_spec_sha256": assignment["spec_sha256"],
                "missing_fields": [],
                "executable": True,
            })
            for key in ("blocker", "blocked_at", "blocked_by_role"):
                assignment.pop(key, None)
            delivery["blocker"] = ""
            delivery["next_action"] = "按工单执行目标变更并生成或绑定所需资产"
            if task.get("status") == "blocked":
                task["status"] = "todo"
        delivery["stage"] = compute_stage(task)
        delivery.pop("stage_warning", None)
        data["assignment_review"] = assignment_status_data(data)
        if delivery.get("cycle_id"):
            _append_events(G.project_dir(slug), str(delivery["cycle_id"]), [_event(
                str(delivery["cycle_id"]), str(task_id),
                "task_assigned" if status == "confirmed" else "assignment_blocked",
                now,
                {
                    "status": status,
                    "owner_role": owner_role,
                    "spec_sha256": assignment.get("spec_sha256"),
                    "note": note,
                },
                actor_role=acting_role,
            )])
        T.save(slug, data)
    return copy.deepcopy(task)


def assignment_status(slug: str) -> dict:
    """读取负责人确认、阻塞和可分派工单列表，不写文件。"""
    import geolib as G

    cfg = normalize_project_config(G.load_config(slug))
    data = normalize_tasks_data(
        _load_raw_tasks(G, slug),
        cycle_id=cfg["delivery"].get("current_cycle_id"),
    )
    return assignment_status_data(data)


def _asset_manifest(data: dict) -> dict:
    tasks = []
    for task in data.get("tasks", []):
        if not isinstance(task, dict):
            continue
        refs = ((task.get("delivery") or {}).get("assets") or [])
        if refs:
            tasks.append({
                "task_id": task.get("id"),
                "stage": (task.get("delivery") or {}).get("stage"),
                "assets": copy.deepcopy(refs),
            })
    return {
        "schema_version": SCHEMA_VERSION,
        **copy.deepcopy(data.get("asset_package") or {}),
        "review": copy.deepcopy(data.get("asset_review") or asset_status_data(data)),
        "tasks": tasks,
    }


def _save_asset_state(G: Any, T: Any, slug: str, data: dict) -> None:
    data["asset_review"] = asset_status_data(data)
    T.save(slug, data)
    G.write_json(
        G.project_dir(slug) / "assets" / "_delivery_manifest.json",
        _asset_manifest(data),
    )


def scan_assets(slug: str, generated_by: str = "asset_scan") -> dict:
    """扫描现有 GeoLook 资产目录并原子写回工单绑定和资产 Manifest。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if not cycle_id:
            raise ValueError("尚未锁定 active cycle，不能准备资产包")
        raw = _load_raw_tasks(G, slug)
        before = normalize_tasks_data(raw, cycle_id=cycle_id)
        scanned_at = G.now_iso()
        data = scan_assets_data(
            raw, cfg, G.project_dir(slug),
            scanned_at=scanned_at, generated_by=generated_by,
        )
        previous = {
            (str(task.get("id") or ""), str(asset.get("path") or "")): asset
            for task in before.get("tasks", [])
            for asset in ((task.get("delivery") or {}).get("assets") or [])
            if isinstance(asset, dict) and asset.get("path")
        }
        events = []
        for task in data.get("tasks", []):
            delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
            for asset in (delivery.get("assets") or []):
                if not isinstance(asset, dict) or not asset.get("path"):
                    continue
                old = previous.get((str(task.get("id") or ""), str(asset.get("path"))))
                unchanged = bool(
                    old
                    and old.get("sha256") == asset.get("sha256")
                    and old.get("required") == asset.get("required")
                    and bool(old.get("missing")) == bool(asset.get("missing"))
                )
                if unchanged:
                    continue
                events.append(_event(
                    str(cycle_id), str(task.get("id") or ""),
                    "asset_created" if old is None else "asset_updated", scanned_at,
                    {key: asset.get(key) for key in (
                        "id", "path", "version", "sha256", "required", "size", "missing", "generated_by",
                    )},
                ))
                if asset.get("missing") and old and not old.get("missing"):
                    reopened = _invalidate_delivery_completion(
                        task,
                        f"必需资产已从磁盘删除：assets/{asset.get('path')}",
                        "asset_missing",
                        scanned_at,
                    )
                    if reopened:
                        events.append(_event(
                            str(cycle_id), str(task.get("id") or ""),
                            "task_reopened", scanned_at,
                            {
                                "reason": reopened["reason"],
                                "regression_id": reopened["id"],
                                "asset_id": asset.get("id"),
                                "asset_path": asset.get("path"),
                            },
                        ))
            delivery["stage"] = compute_stage(task)
            delivery.pop("stage_warning", None)
        _append_events(G.project_dir(slug), str(cycle_id), events)
        _save_asset_state(G, T, slug, data)
    return data


def on_assets_generated(slug: str, generator_result: dict | None = None) -> dict:
    """GeoLook ``generate.run`` 后的轻量 hook；不重做资产生成。"""
    # 保留参数用于与 generate.run 的返回值衔接；当前状态只以磁盘文件为准，
    # 避免把生成器的展示型 index 当成资产存在性证据。
    _ = generator_result
    return scan_assets(slug, generated_by="generate.py")


def bind_asset(slug: str, task_id: str, path: str, required: bool = True) -> dict:
    """人工绑定 assets/ 下的现有文件；路径逃逸和非文件目标会被拒绝。"""
    rel = _normalize_asset_path(path)
    if not rel or rel.endswith("/"):
        raise ValueError("人工绑定必须指定 assets/ 下的文件")

    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        root = (G.project_dir(slug) / "assets").resolve()
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("资产路径不能逃逸 assets/ 目录") from exc
        if not target.is_file():
            raise FileNotFoundError(f"资产不存在：assets/{rel}")
        data = normalize_tasks_data(
            _load_raw_tasks(G, slug), cycle_id=cfg["delivery"].get("current_cycle_id")
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        if ((task.get("delivery") or {}).get("assignment") or {}).get("status") != "confirmed":
            raise ValueError("只有负责人已确认的工单可以绑定资产")
        refs = task["delivery"].setdefault("assets", [])
        previous = next((row for row in refs if isinstance(row, dict) and row.get("path") == rel), None)
        previous_state = copy.deepcopy(previous) if previous is not None else None
        if previous is None:
            refs.append({
                "id": _asset_id(task_id, rel),
                "path": rel,
                "required": bool(required),
                "binding_method": "manual",
            })
        else:
            previous.update({"required": bool(required), "binding_method": "manual"})
        data = scan_assets_data(
            data, cfg, G.project_dir(slug), scanned_at=G.now_iso(), generated_by="manual",
        )
        task = next(row for row in data["tasks"] if row.get("id") == task_id)
        asset = next(
            row for row in task["delivery"].get("assets", [])
            if isinstance(row, dict) and row.get("path") == rel
        )
        event_state = {
            key: asset.get(key)
            for key in ("id", "path", "version", "sha256", "required", "missing", "size")
        }
        previous_event_state = {
            key: previous_state.get(key)
            for key in ("id", "path", "version", "sha256", "required", "missing", "size")
        } if previous_state is not None else None
        if event_state != previous_event_state and task["delivery"].get("cycle_id"):
            _append_events(
                G.project_dir(slug), str(task["delivery"]["cycle_id"]), [_event(
                    str(task["delivery"]["cycle_id"]), str(task_id),
                    "asset_created" if previous_state is None else "asset_updated",
                    G.now_iso(), event_state,
                )],
            )
        _save_asset_state(G, T, slug, data)
    return copy.deepcopy(task)


def _invalidate_delivery_completion(
    task: dict,
    reason: str,
    kind: str,
    at: str,
) -> dict | None:
    """让已关闭的新周期工单真实重开，并保留原验收快照供回放。"""
    if task.get("status") != "done":
        return None
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    if not delivery.get("cycle_id"):
        return None
    verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
    if verification.get("id"):
        history = delivery.setdefault("verification_history", [])
        if not any(
            isinstance(row, dict) and row.get("id") == verification.get("id")
            for row in history
        ):
            history.append(copy.deepcopy(verification))
    verification["can_close"] = False
    verification["invalidated_at"] = at
    verification["invalidation_reason"] = reason
    verification["invalidation_type"] = kind
    task["status"] = "todo"
    task["closed_at"] = None
    record = {
        "id": f"reg-{secrets.token_hex(6)}",
        "type": kind,
        "detected_at": at,
        "previous_verification_id": verification.get("id"),
        "reason": reason,
    }
    delivery.setdefault("regressions", []).append(record)
    return record


def add_asset_approval(
    slug: str,
    task_id: str,
    asset_id: str,
    status: str,
    role: str,
    note: str,
    requirement_id: str = "",
) -> dict:
    """为当前资产版本追加批准或驳回记录，并重新计算 asset_ready。"""
    import geolib as G
    import tasks as T

    if status not in ASSET_DECISION_STATUSES:
        raise ValueError("status 必须是 approved 或 rejected")
    note = _validated_text(note, "资产审批 note")
    role = str(role or "").strip()

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        data = normalize_tasks_data(
            _load_raw_tasks(G, slug), cycle_id=cfg["delivery"].get("current_cycle_id")
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        asset = next((
            row for row in delivery.get("assets", [])
            if isinstance(row, dict) and row.get("id") == asset_id
        ), None)
        if asset is None:
            raise KeyError(f"工单 {task_id} 未绑定资产 {asset_id}")
        if asset.get("missing") or not asset.get("size"):
            raise ValueError("资产不存在或为空，不能审批")
        if status == "approved" and ((asset.get("preflight") or {}).get("can_submit") is not True):
            raise ValueError("资产事实或结构预检未通过，不能批准进入部署")

        requirements = [
            row for row in asset.get("approval_requirements", [])
            if row.get("required", True)
            and (not requirement_id or row.get("id") == requirement_id)
            and role in row.get("roles", [])
        ]
        if not requirements:
            expected = [
                f"{row.get('id')}:{'/'.join(row.get('roles', []))}"
                for row in asset.get("approval_requirements", []) if row.get("required", True)
            ]
            raise ValueError(f"角色 {role or '未提供'} 无权审批；需要 {', '.join(expected) or '无需审批'}")
        if len(requirements) > 1 and not requirement_id:
            raise ValueError("角色匹配多个审批项，必须指定 requirement_id")
        requirement = requirements[0]
        duplicate = next((
            row for row in reversed(delivery.get("approvals", []))
            if isinstance(row, dict)
            and row.get("type") == "asset"
            and row.get("target") == f"asset:{asset_id}"
            and row.get("asset_version") == asset.get("version")
            and row.get("asset_sha256") == asset.get("sha256")
            and row.get("requirement_id") == requirement.get("id")
            and row.get("status") == status
            and row.get("role") == role
            and row.get("note") == note
        ), None)
        if duplicate is not None:
            return {
                "task": copy.deepcopy(task),
                "asset": copy.deepcopy(asset),
                "approval": copy.deepcopy(duplicate),
                "idempotent_replay": True,
            }
        now = G.now_iso()
        approval = {
            "id": f"ap-{secrets.token_hex(6)}",
            "type": "asset",
            "target": f"asset:{asset_id}",
            "task_id": task_id,
            "asset_path": asset.get("path"),
            "asset_version": asset.get("version"),
            "asset_sha256": asset.get("sha256"),
            "requirement_id": requirement.get("id"),
            "status": status,
            "role": role,
            "at": now,
            "note": note,
        }
        delivery.setdefault("approvals", []).append(approval)
        asset["approval_status"], asset["approval_results"] = _approval_state(
            asset, delivery["approvals"]
        )
        reopened = None
        if status == "rejected":
            reopened = _invalidate_delivery_completion(
                task,
                f"当前资产版本审批被 {role} 驳回：{note}",
                "asset_approval_rejected",
                now,
            )
        delivery["stage"] = compute_stage(task)
        delivery["next_action"] = (
            "提交部署 URL 和部署证据"
            if delivery["stage"] == "asset_ready"
            else "完成剩余资产审批或按驳回说明修改后重新生成"
        )
        delivery.pop("stage_warning", None)
        if delivery.get("cycle_id"):
            events = [_event(
                str(delivery["cycle_id"]), str(task_id), f"asset_{status}", now,
                {key: approval.get(key) for key in (
                    "id", "type", "target", "status", "requirement_id", "asset_version",
                    "asset_sha256", "note",
                )},
                actor_role=role,
            )]
            if reopened:
                events.extend([
                    _event(str(delivery["cycle_id"]), str(task_id), "asset_approval_regressed", now, reopened),
                    _event(str(delivery["cycle_id"]), str(task_id), "task_reopened", now, {
                        "reason": reopened["reason"], "regression_id": reopened["id"],
                    }),
                ])
            _append_events(G.project_dir(slug), str(delivery["cycle_id"]), events)
        _save_asset_state(G, T, slug, data)
    return {
        "task": copy.deepcopy(task),
        "asset": copy.deepcopy(asset),
        "approval": approval,
    }


def asset_status(slug: str) -> dict:
    """读取资产存在性、预检和审批完成度，不写文件。"""
    import geolib as G

    cfg = normalize_project_config(G.load_config(slug))
    data = normalize_tasks_data(
        _load_raw_tasks(G, slug), cycle_id=cfg["delivery"].get("current_cycle_id")
    )
    return asset_status_data(data)


def _normalize_deployment_evidence_ref(
    project_dir: Path,
    evidence_type: str,
    evidence_ref: str,
    target_url: str,
) -> str:
    value = str(evidence_ref or "").strip()
    if evidence_type == "url":
        return _validate_public_url(value or target_url, "evidence_ref")
    if not value:
        raise ValueError(f"evidence_type={evidence_type} 必须提供 evidence_ref")
    if value.startswith(("http://", "https://")):
        return _validate_public_url(value, "evidence_ref")
    root = Path(project_dir).resolve()
    target = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("部署证据文件必须位于项目目录内") from exc
    if not target.is_file() or target.stat().st_size <= 0:
        raise ValueError("部署证据文件不存在或为空")
    return relative.as_posix()


def _write_deployment_snapshot(
    G: Any,
    slug: str,
    cycle_id: str,
    deployment_id: str,
    check_index: int,
    snapshot: dict,
) -> str:
    relative = (
        Path("delivery") / "snapshots" / cycle_id / "deployments"
        / f"{deployment_id}-check-{check_index:03d}.json"
    )
    G.write_json(G.project_dir(slug) / relative, snapshot)
    return relative.as_posix()


def prepare_deployments(slug: str) -> dict:
    """为 asset_ready 工单生成部署清单，不创建部署成功记录。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        if not cfg["delivery"].get("current_cycle_id"):
            raise ValueError("尚未锁定 active cycle，不能准备部署清单")
        data = prepare_deployments_data(
            _load_raw_tasks(G, slug), cfg, G.project_dir(slug), generated_at=G.now_iso()
        )
        T.save(slug, data)
    return data


def add_deployment(
    slug: str,
    task_id: str,
    target_url: str,
    asset_ids: list[str],
    role: str,
    note: str,
    channel: str = "website",
    evidence_type: str = "url",
    evidence_ref: str = "",
    insertion_position: str = "",
    expected_snippets: list[str] | None = None,
    expected_jsonld_types: list[str] | None = None,
    request_id: str = "",
) -> dict:
    """Web Owner 明确提交真实部署并立即重抓；本地资产不会触发此函数。"""
    import geolib as G
    import tasks as T

    if channel not in DEPLOYMENT_CHANNELS:
        raise ValueError(f"channel 必须是 {', '.join(sorted(DEPLOYMENT_CHANNELS))}")
    if evidence_type not in DEPLOYMENT_EVIDENCE_TYPES:
        raise ValueError(f"evidence_type 必须是 {', '.join(sorted(DEPLOYMENT_EVIDENCE_TYPES))}")
    if str(role or "").strip() != "web_owner":
        raise ValueError("实际部署必须由 web_owner 明确确认")
    note = _validated_text(note, "Web Owner 部署确认 note")
    request_id = _validated_text(
        request_id, "request_id", required=False, max_length=128,
    )
    target_url = _validate_public_url(target_url)
    selected_ids = _clean_string_list(asset_ids)
    if not selected_ids:
        raise ValueError("部署记录必须至少关联一个已批准资产")

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if not cycle_id:
            raise ValueError("尚未锁定 active cycle，不能提交部署证据")
        now = G.now_iso()
        data = prepare_deployments_data(
            _load_raw_tasks(G, slug), cfg, G.project_dir(slug), generated_at=now,
        )
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        if not _assets_ready(task, delivery):
            raise ValueError("工单资产尚未完成存在性、事实预检和必需审批")
        refs = {
            row.get("id"): row for row in delivery.get("assets", [])
            if isinstance(row, dict) and row.get("required", True)
        }
        unknown = [value for value in selected_ids if value not in refs]
        if unknown:
            raise ValueError(f"工单未绑定资产：{', '.join(unknown)}")
        selected = [refs[value] for value in selected_ids]
        if any(row.get("approval_status") not in {"approved", "not_required"} for row in selected):
            raise ValueError("只能部署当前版本已批准的资产")

        plan = delivery.get("deployment_plan") or build_deployment_plan(
            task, cfg, G.project_dir(slug), now
        )
        plan_assets = {
            row.get("asset_id"): row for row in plan.get("assets", []) if isinstance(row, dict)
        }
        selected_plan_assets = [plan_assets[value] for value in selected_ids if value in plan_assets]
        nondeployable = [
            value for value in selected_ids
            if value not in plan_assets or not plan_assets[value].get("deployable")
        ]
        if nondeployable:
            raise ValueError(
                "以下资产是支持材料而非可部署资产：" + ", ".join(nondeployable)
            )
        snippets = _clean_string_list(expected_snippets) if expected_snippets is not None else list(dict.fromkeys(
            value for row in selected_plan_assets for value in row.get("expected_snippets", [])
        ))
        schema_types = _clean_string_list(expected_jsonld_types) if expected_jsonld_types is not None else sorted(set(
            value for row in selected_plan_assets for value in row.get("expected_jsonld_types", [])
        ))
        position = str(insertion_position or "").strip() or "；".join(dict.fromkeys(
            row.get("insertion_position", "") for row in selected_plan_assets
            if row.get("insertion_position")
        ))
        normalized_evidence_ref = _normalize_deployment_evidence_ref(
            G.project_dir(slug), evidence_type, evidence_ref, target_url,
        )
        request_payload = {
            "task_id": task_id,
            "target_url": target_url,
            "asset_ids": sorted(selected_ids),
            "channel": channel,
            "evidence_type": evidence_type,
            "evidence_ref": normalized_evidence_ref,
            "insertion_position": position,
            "expected_snippets": snippets,
            "expected_jsonld_types": schema_types,
            "note": note,
        }
        request_sha256 = _canonical_sha256(request_payload)
        if request_id:
            duplicate = next((
                row for row in delivery.get("deployments", [])
                if isinstance(row, dict) and row.get("request_id") == request_id
            ), None)
            if duplicate is not None:
                if duplicate.get("request_sha256") != request_sha256:
                    raise ValueError("request_id 已用于不同的部署请求")
                return {
                    "task": copy.deepcopy(task),
                    "deployment": copy.deepcopy(duplicate),
                    "idempotent_replay": True,
                }
        try:
            snapshot = _deployment_check_snapshot(
                G, target_url, snippets, schema_types, checked_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            # 首次部署提交也必须留下失败证据；网络超时不能让 Web Owner
            # 误以为没有提交过，或靠重复点击制造不一致记录。
            snapshot = _deployment_error_snapshot(target_url, now, exc)
        status, complete, completion_method = _deployment_status_from_snapshot(
            snapshot, channel, evidence_type, normalized_evidence_ref,
        )
        deployment_id = f"dep-{secrets.token_hex(6)}"
        snapshot_ref = _write_deployment_snapshot(
            G, slug, cycle_id, deployment_id, 1, snapshot,
        )
        record = {
            "id": deployment_id,
            "request_id": request_id or None,
            "request_sha256": request_sha256,
            "target_url": target_url,
            "channel": channel,
            "asset_ids": selected_ids,
            "asset_versions": [
                {"id": row.get("id"), "version": row.get("version"), "sha256": row.get("sha256")}
                for row in selected
            ],
            "deployed_by_role": "web_owner",
            "deployed_by_role_name": str((cfg["delivery"].get("roles") or {}).get("web_owner") or ""),
            "deployed_at": now,
            "human_confirmed": True,
            "evidence_type": evidence_type,
            "evidence_ref": normalized_evidence_ref,
            "insertion_position": position,
            "expected_snippets": snippets,
            "expected_jsonld_types": schema_types,
            "status": status,
            "deployment_complete": complete,
            "completion_method": completion_method,
            "last_checked_at": now,
            "last_http_status": snapshot.get("http_status"),
            "last_snapshot_ref": snapshot_ref,
            "page_snapshot": snapshot,
            "check_history": [{
                "checked_at": now,
                "snapshot_ref": snapshot_ref,
                "status": status,
                "deployment_complete": complete,
            }],
            "note": note,
        }
        delivery.setdefault("deployments", []).append(record)
        delivery.setdefault("approvals", []).append({
            "id": f"ap-{secrets.token_hex(6)}",
            "type": "deployment",
            "target": f"deployment:{deployment_id}",
            "status": "approved",
            "role": "web_owner",
            "at": now,
            "note": note,
        })
        delivery["stage"] = compute_stage(task)
        delivery["next_action"] = (
            "进入阶段 7，运行 required 自动验收"
            if delivery["stage"] == "deployed"
            else "修复目标 URL、缓存、SSR、关键文本或 JSON-LD 后重新抓取"
        )
        delivery.pop("stage_warning", None)
        _append_events(G.project_dir(slug), str(cycle_id), [_event(
            str(cycle_id), str(task_id), "deployment_submitted", now,
            {key: record.get(key) for key in (
                "id", "request_id", "target_url", "channel", "asset_versions",
                "evidence_type", "evidence_ref", "status", "deployment_complete",
                "last_snapshot_ref", "note",
            )},
            actor_role="web_owner",
        )])
        data["deployment_review"] = deployment_status_data(data)
        T.save(slug, data)
    return {"task": copy.deepcopy(task), "deployment": copy.deepcopy(record)}


def recheck_deployment(slug: str, task_id: str, deployment_id: str) -> dict:
    """重抓已由 Web Owner 提交的 URL；不创建新的人工确认。"""
    import geolib as G
    import tasks as T

    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cycle_id=cycle_id)
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        record = next((
            row for row in delivery.get("deployments", [])
            if isinstance(row, dict) and row.get("id") == deployment_id
        ), None)
        if record is None:
            raise KeyError(f"找不到部署记录 {deployment_id}")
        if record.get("human_confirmed") is not True or record.get("deployed_by_role") != "web_owner":
            raise ValueError("部署记录缺少 Web Owner 人工确认，不能伪装为已部署")
        now = G.now_iso()
        snapshot = _deployment_check_snapshot(
            G,
            _validate_public_url(record.get("target_url")),
            _clean_string_list(record.get("expected_snippets")),
            _clean_string_list(record.get("expected_jsonld_types")),
            checked_at=now,
        )
        status, complete, method = _deployment_status_from_snapshot(
            snapshot,
            record.get("channel", "website"),
            record.get("evidence_type", "url"),
            record.get("evidence_ref", ""),
        )
        history = record.setdefault("check_history", [])
        snapshot_ref = _write_deployment_snapshot(
            G, slug, cycle_id, deployment_id, len(history) + 1, snapshot,
        )
        history.append({
            "checked_at": now,
            "snapshot_ref": snapshot_ref,
            "status": status,
            "deployment_complete": complete,
        })
        record.update({
            "status": status,
            "deployment_complete": complete,
            "completion_method": method,
            "last_checked_at": now,
            "last_http_status": snapshot.get("http_status"),
            "last_snapshot_ref": snapshot_ref,
            "page_snapshot": snapshot,
        })
        reopened = None
        if record.get("deployment_complete") is not True:
            reopened = _invalidate_delivery_completion(
                task,
                f"部署内容复查未通过：HTTP {snapshot.get('http_status') or 0}",
                "deployment_regressed",
                now,
            )
        delivery["stage"] = compute_stage(task)
        delivery["next_action"] = (
            "进入阶段 7，运行 required 自动验收"
            if delivery["stage"] == "deployed"
            else "继续修复部署可访问性和关键内容后重抓"
        )
        delivery.pop("stage_warning", None)
        if delivery.get("cycle_id"):
            events = [_event(
                str(delivery["cycle_id"]), str(task_id), "deployment_checked", now,
                {
                    "deployment_id": deployment_id,
                    "status": status,
                    "deployment_complete": complete,
                    "snapshot_ref": snapshot_ref,
                },
            )]
            if reopened:
                events.extend([
                    _event(str(delivery["cycle_id"]), str(task_id), "deployment_regressed", now, reopened),
                    _event(str(delivery["cycle_id"]), str(task_id), "task_reopened", now, {
                        "reason": reopened["reason"], "regression_id": reopened["id"],
                    }),
                ])
            _append_events(G.project_dir(slug), str(delivery["cycle_id"]), events)
        data["deployment_review"] = deployment_status_data(data)
        T.save(slug, data)
    return {"task": copy.deepcopy(task), "deployment": copy.deepcopy(record)}


def deployment_status(slug: str) -> dict:
    """读取部署计划、提交、失败和人工第三方证据状态，不写文件。"""
    import geolib as G

    cfg = normalize_project_config(G.load_config(slug))
    data = normalize_tasks_data(
        _load_raw_tasks(G, slug), cycle_id=cfg["delivery"].get("current_cycle_id")
    )
    return deployment_status_data(data)


# ---------------------------------------------------------------- 自动验收与周期复盘

DELIVERY_CHECKERS = {
    "asset.exists",
    "approval.required",
    "deployment.url_2xx",
    "deployment.contains_text",
    "deployment.jsonld_type",
    "facts.no_rejected_claims",
}
VERIFICATION_VERDICTS = {
    "pass", "fail", "manual", "data_missing", "error", "warning",
}


def _acceptance_rows(task: dict) -> list[dict]:
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    rows = []
    for index, item in enumerate(
        acceptance.get("checks", []) if isinstance(acceptance.get("checks"), list) else []
    ):
        if not isinstance(item, dict):
            continue
        row = copy.deepcopy(item)
        row.setdefault("id", f"check-{index + 1}")
        row.setdefault("required", True)
        if row.get("check") or row.get("type") == "manual" or row.get("manual") is True:
            rows.append(row)
    legacy = str(acceptance.get("check") or "").strip()
    if legacy and not any(row.get("check") == legacy for row in rows):
        rows.insert(0, {"id": "legacy", "check": legacy, "required": True})
    if not rows and acceptance.get("type") == "manual":
        rows.append({
            "id": "manual", "type": "manual", "required": True,
            "desc": str(acceptance.get("desc") or "需 Reviewer 人工判断").strip(),
        })
    return rows


def _checker_name(expr: str) -> str:
    return str(expr or "").split(":", 1)[0]


def _current_deployments(task: dict) -> list[dict]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    current = {
        row.get("id"): (row.get("version"), row.get("sha256"))
        for row in delivery.get("assets", [])
        if isinstance(row, dict)
        and row.get("required", True)
        and row.get("deployment_required", row.get("type") not in {"outline", "deployment_guide"})
        and not row.get("missing")
    }
    rows = []
    for record in delivery.get("deployments", []) if isinstance(delivery.get("deployments"), list) else []:
        if not isinstance(record, dict):
            continue
        if record.get("human_confirmed") is not True or record.get("deployed_by_role") != "web_owner":
            continue
        versions = record.get("asset_versions") if isinstance(record.get("asset_versions"), list) else []
        if not versions or not all(isinstance(version, dict) for version in versions):
            continue
        if all(
            version.get("id") in current
            and current[version.get("id")] == (version.get("version"), version.get("sha256"))
            for version in versions
        ):
            rows.append(record)
    return rows


def _approval_required_check(task: dict, approval_type: str) -> tuple[str, str, Any]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    kind = str(approval_type or "asset").strip()
    if kind == "asset":
        assets = [
            row for row in delivery.get("assets", [])
            if isinstance(row, dict) and row.get("required", True)
        ]
        if not assets:
            return "data_missing", "没有可核对的必需资产", 0
        pending = [
            row.get("path") or row.get("id") for row in assets
            if row.get("approval_status") not in {"approved", "not_required"}
        ]
        return (
            ("fail", f"仍有 {len(pending)} 个资产未获必需审批", len(pending))
            if pending else ("pass", "当前版本的必需资产审批均已通过", 0)
        )
    if kind == "deployment":
        rows = _current_deployments(task)
        ok = bool(rows) and any(row.get("deployment_complete") is True for row in rows)
        return ("pass", "Web Owner 部署确认有效", len(rows)) if ok else (
            "fail", "缺少当前资产版本的 Web Owner 有效部署确认", 0,
        )
    approval = _latest_approval(delivery, kind)
    if approval is None:
        return "manual", f"缺少 {kind} 类型人工结论", None
    status = approval.get("status")
    return (
        ("pass", f"{kind} 审批已通过", status)
        if status == "approved" else ("fail", f"{kind} 审批未通过", status)
    )


def _asset_exists_check(task: dict, project_dir: Path, requested: str) -> tuple[str, str, Any]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    wanted = _normalize_asset_path(requested) if requested else ""
    refs = [
        row for row in delivery.get("assets", [])
        if isinstance(row, dict) and row.get("required", True)
        and (not wanted or row.get("path") == wanted)
    ]
    if not refs:
        return "data_missing", (f"未绑定资产 assets/{wanted}" if wanted else "未绑定必需资产"), 0
    root = (Path(project_dir) / "assets").resolve()
    missing = []
    for row in refs:
        rel = _normalize_asset_path(row.get("path"))
        target = (root / rel).resolve() if rel else root
        try:
            target.relative_to(root)
        except ValueError:
            missing.append(rel or str(row.get("id") or ""))
            continue
        if not target.is_file() or target.stat().st_size <= 0:
            missing.append(rel or str(row.get("id") or ""))
    return (
        ("fail", f"缺失或为空：{', '.join(missing)}", len(missing))
        if missing else ("pass", f"{len(refs)} 个必需资产存在且非空", len(refs))
    )


def _deployment_check(task: dict, expr: str) -> tuple[str, str, Any]:
    rows = _current_deployments(task)
    if not rows:
        return "data_missing", "没有当前资产版本的部署记录", None
    snapshots = [row.get("page_snapshot") for row in rows if isinstance(row.get("page_snapshot"), dict)]
    if not snapshots:
        return "data_missing", "部署记录缺少页面快照", None
    name, _, arg = expr.partition(":")
    if name == "deployment.url_2xx":
        values = [int(row.get("http_status") or 0) for row in snapshots]
        if any(value in {401, 403} for value in values):
            return "manual", f"部署 URL 返回受限状态：{values}", values
        ok = all(200 <= value < 300 for value in values)
        return ("pass" if ok else "fail", f"部署 URL HTTP 状态：{values}", values)
    if name == "deployment.contains_text":
        items = [
            item for snapshot in snapshots
            for item in (((snapshot.get("checks") or {}).get("contains_text") or {}).get("items") or [])
            if isinstance(item, dict)
        ]
        if not items:
            return "data_missing", "部署清单没有可自动核对的关键文本", None
        missed = [item.get("excerpt") or item.get("sha256") for item in items if not item.get("matched")]
        return (
            ("fail", f"仍有 {len(missed)} 段关键文本未抓取到", missed)
            if missed else ("pass", f"{len(items)} 段关键文本均可抓取", len(items))
        )
    if name == "deployment.jsonld_type":
        expected = str(arg or "").strip()
        if not expected:
            return "error", "deployment.jsonld_type 缺少 schema 类型", None
        parse_errors = [
            error for snapshot in snapshots
            for error in snapshot.get("jsonld_errors", [])
            if isinstance(error, dict)
        ]
        if parse_errors:
            first = parse_errors[0]
            summary = (
                f"第 {first.get('block')} 个 JSON-LD 块解析失败"
                f"（{first.get('message') or 'invalid JSON'}）"
            )
            return "fail", f"{summary}；共 {len(parse_errors)} 个错误", parse_errors
        found = sorted({str(value) for snapshot in snapshots for value in snapshot.get("jsonld_types", [])})
        ok = expected in found
        return ("pass" if ok else "fail", f"JSON-LD 类型：{', '.join(found) or '无'}；目标 {expected}", found)
    return "error", f"未知部署检查器 {expr}", None


def _facts_check(task: dict, project_dir: Path) -> tuple[str, str, Any]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    factcheck_path = Path(project_dir) / "factcheck.json"
    factcheck = {}
    if factcheck_path.exists():
        try:
            factcheck = json.loads(factcheck_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return "error", f"factcheck.json 无法读取：{type(exc).__name__}", None
    claims = _factcheck_claims(factcheck)
    rejected = []
    checked = 0
    for asset in delivery.get("assets", []) if isinstance(delivery.get("assets"), list) else []:
        if not isinstance(asset, dict) or not asset.get("required", True) or asset.get("missing"):
            continue
        rel = _normalize_asset_path(asset.get("path"))
        path = Path(project_dir) / "assets" / rel
        if not path.is_file() or path.stat().st_size <= 0:
            continue
        checked += 1
        text = _asset_text(path, path.stat().st_size)
        rejected.extend({"path": rel, **row} for row in _match_claims(
            text, claims, {"rejected", "forbidden"},
        ))
        preflight_rejected = ((asset.get("preflight") or {}).get("factcheck") or {}).get("rejected_claims")
        if isinstance(preflight_rejected, list):
            rejected.extend({"path": rel, **row} for row in preflight_rejected if isinstance(row, dict))
    if not checked:
        return "data_missing", "没有可执行事实检查的当前资产", None
    unique = {(row.get("path"), row.get("claim_id"), row.get("excerpt")) for row in rejected}
    return (
        ("fail", f"待部署资产命中 {len(unique)} 条禁用事实", len(unique))
        if unique else ("pass", f"{checked} 个当前资产未命中禁用事实", 0)
    )


def _baseline_value(expr: str, baseline: dict | None, task: dict, progress: dict | None) -> Any:
    base = (baseline or {}).get("baseline") if isinstance(baseline, dict) else {}
    audit = base.get("audit") if isinstance(base, dict) and isinstance(base.get("audit"), dict) else {}
    site = audit.get("site") if isinstance(audit.get("site"), dict) else {}
    if expr.startswith("site.avg_score_gte:"):
        return audit.get("avg_score")
    if expr == "site.has_sitemap":
        return site.get("has_sitemap")
    if expr == "site.has_llms_txt":
        return site.get("has_llms_txt")
    if expr == "site.no_ai_bot_block":
        return site.get("ai_bots_blocked")
    if isinstance(progress, dict) and progress.get("base") is not None:
        return progress.get("base")
    if task.get("baseline_count") is not None:
        return task.get("baseline_count")
    return None


def _target_value(expr: str, progress: dict | None, row: dict) -> Any:
    if isinstance(progress, dict) and "target" in progress:
        return progress.get("target")
    if _checker_name(expr) in {"asset.exists", "approval.required", "deployment.url_2xx", "deployment.contains_text", "deployment.jsonld_type", "facts.no_rejected_claims"}:
        return row.get("target", "pass")
    return row.get("target")


def run_check_item(
    task: dict,
    row: dict,
    audit: dict,
    metrics: dict | None,
    project_dir: Path,
    baseline: dict | None = None,
    legacy_checker: Any = None,
) -> dict:
    """执行单个检查并统一为可审计的 verdict/before/after/target。"""
    check_id = str(row.get("id") or "check")
    required = bool(row.get("required", True))
    expr = str(row.get("check") or "").strip()
    progress = None
    if row.get("type") == "manual" or row.get("manual") is True or not expr:
        verdict, note, after = "manual", str(row.get("desc") or "需 Reviewer 人工判断"), None
    else:
        name, _, arg = expr.partition(":")
        try:
            if name == "asset.exists":
                verdict, note, after = _asset_exists_check(task, Path(project_dir), arg)
            elif name == "approval.required":
                verdict, note, after = _approval_required_check(task, arg or "asset")
            elif name.startswith("deployment."):
                verdict, note, after = _deployment_check(task, expr)
            elif name == "facts.no_rejected_claims":
                verdict, note, after = _facts_check(task, Path(project_dir))
            elif legacy_checker is None:
                verdict, note, after = "error", f"没有可用的旧检查器：{expr}", None
            else:
                probe = copy.deepcopy(task)
                probe["acceptance"] = {"type": "auto", "check": expr}
                ok, note, progress = legacy_checker(probe, audit, metrics)
                after = progress.get("cur") if isinstance(progress, dict) else ok
                if ok is True:
                    verdict = "pass"
                elif ok is False:
                    verdict = "fail"
                elif "无采样数据" in str(note) or "无数据" in str(note):
                    verdict = "data_missing"
                elif "检查器出错" in str(note) or "未知检查器" in str(note):
                    verdict = "error"
                else:
                    verdict = "manual"
        except Exception as exc:  # noqa: BLE001
            verdict, note, after = "error", f"检查器出错：{type(exc).__name__}: {exc}", None
    if verdict not in VERIFICATION_VERDICTS:
        verdict, note = "error", f"非法检查结果：{verdict}"
    effective = verdict
    if not required and verdict in {"fail", "manual", "data_missing", "error"}:
        effective = "warning"
    return {
        "id": check_id,
        "check": expr or "manual.review",
        "required": required,
        "verdict": effective,
        "raw_verdict": verdict,
        "note": str(note),
        "before": _baseline_value(expr, baseline, task, progress),
        "after": after,
        "target": _target_value(expr, progress, row),
        "progress": copy.deepcopy(progress),
    }


def _aggregate_checks(checks: list[dict], mode: str = "all") -> str:
    required = [row for row in checks if row.get("required", True)]
    if not required:
        return "pass"
    verdicts = [row.get("raw_verdict") for row in required]
    if mode == "any":
        if "pass" in verdicts:
            return "pass"
        if "manual" in verdicts:
            return "manual"
        if "data_missing" in verdicts:
            return "data_missing"
        return "error" if "error" in verdicts else "fail"
    if "error" in verdicts:
        return "error"
    if "fail" in verdicts:
        return "fail"
    if "manual" in verdicts:
        return "manual"
    if "data_missing" in verdicts:
        return "data_missing"
    return "pass"


def _verification_target(task: dict, checks: list[dict]) -> str:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    payload = {
        "task_id": task.get("id"),
        "checks": [
            {key: row.get(key) for key in ("id", "check", "required")}
            for row in checks
        ],
        "assets": [
            {key: row.get(key) for key in ("id", "version", "sha256")}
            for row in delivery.get("assets", []) if isinstance(row, dict) and row.get("required", True)
        ],
        "deployments": [
            {
                "id": row.get("id"),
                "asset_versions": row.get("asset_versions"),
                "last_snapshot_ref": row.get("last_snapshot_ref"),
            }
            for row in _current_deployments(task)
        ],
    }
    return f"verification:{_canonical_sha256(payload)}"


def _matching_final_approval(task: dict, target: str) -> dict | None:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    for row in reversed(delivery.get("approvals", []) if isinstance(delivery.get("approvals"), list) else []):
        if isinstance(row, dict) and row.get("type") == "final" and row.get("target") == target:
            return row
    return None


def check_all(
    task: dict,
    audit: dict,
    metrics: dict | None,
    project_dir: Path,
    baseline: dict | None = None,
    legacy_checker: Any = None,
    verified_at: str = "",
    verification_id: str = "",
) -> dict:
    """运行全部检查；失败不短路，optional 失败只产生 warning。"""
    rows = _acceptance_rows(task)
    checks = [
        run_check_item(task, row, audit, metrics, Path(project_dir), baseline, legacy_checker)
        for row in rows
    ]
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    automated = _aggregate_checks(checks, str(acceptance.get("mode") or "all"))
    review_needed = bool(acceptance.get("review_required")) or any(
        row.get("required", True) and row.get("raw_verdict") in {"manual", "data_missing"}
        for row in checks
    )
    target = _verification_target(task, checks)
    approval = _matching_final_approval(task, target) if review_needed else None
    review_status = "not_required" if not review_needed else (
        str(approval.get("status")) if approval else "pending"
    )
    verdict = automated
    if approval and approval.get("status") == "rejected":
        verdict = "fail"
    elif review_needed and review_status != "approved" and automated not in {"fail", "error"}:
        verdict = "manual"
    elif review_needed and review_status == "approved" and automated in {"manual", "data_missing", "pass"}:
        verdict = "pass"
    return {
        "id": verification_id or f"ver-{secrets.token_hex(6)}",
        "cycle_id": (task.get("delivery") or {}).get("cycle_id"),
        "verified_at": verified_at,
        "verdict": verdict,
        "automated_verdict": automated,
        "mode": str(acceptance.get("mode") or "all"),
        "checks": checks,
        "review_required": review_needed,
        "review_status": review_status,
        "review_target": target,
        "review": copy.deepcopy(approval) if approval else None,
        "before": {row["id"]: row.get("before") for row in checks},
        "after": {row["id"]: row.get("after") for row in checks},
        "target": {row["id"]: row.get("target") for row in checks},
        "can_close": False,
    }


def build_evidence_chain(
    task: dict,
    baseline_ref: str = "",
    project_dir: Path | None = None,
) -> dict:
    """生成关闭时可独立回放的证据索引；只存引用和哈希，不复制敏感正文。"""
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
    source_refs = [
        {key: row.get(key) for key in ("id", "type", "ref", "review_status", "reviewed_by_role", "reviewed_at", "review_note")}
        for row in delivery.get("source_refs", [])
        if isinstance(row, dict) and row.get("review_status") == "confirmed"
    ]
    assets = [
        {key: row.get(key) for key in ("id", "path", "type", "version", "sha256", "approval_status", "missing")}
        for row in delivery.get("assets", [])
        if isinstance(row, dict) and row.get("required", True)
    ]
    deployments = [
        {key: row.get(key) for key in (
            "id", "target_url", "channel", "asset_versions", "deployed_by_role",
            "deployed_at", "human_confirmed", "evidence_type", "evidence_ref",
            "status", "deployment_complete", "last_snapshot_ref", "last_checked_at",
        )}
        for row in _current_deployments(task)
    ]
    approvals = [
        {key: row.get(key) for key in (
            "id", "type", "target", "requirement_id", "status", "role", "at",
            "asset_version", "asset_sha256",
        )}
        for row in delivery.get("approvals", []) if isinstance(row, dict)
    ]
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    scope = delivery.get("scope_decision") if isinstance(delivery.get("scope_decision"), dict) else {}
    strict = bool(delivery.get("cycle_id"))
    missing = []
    if strict:
        expected_ref = f"delivery/snapshots/{delivery.get('cycle_id')}/baseline.json"
        if not baseline_ref:
            missing.append("baseline_snapshot")
        elif baseline_ref != expected_ref:
            missing.append("baseline_cycle_mismatch")
        elif project_dir is not None:
            baseline_path = Path(project_dir) / baseline_ref
            if not baseline_path.is_file():
                missing.append("baseline_snapshot")
            else:
                try:
                    baseline_data = json.loads(baseline_path.read_text("utf-8"))
                except (OSError, json.JSONDecodeError):
                    missing.append("baseline_snapshot_invalid")
                else:
                    if not isinstance(baseline_data, dict) or baseline_data.get("cycle_id") != delivery.get("cycle_id"):
                        missing.append("baseline_cycle_mismatch")
    if strict and not source_refs:
        missing.append("confirmed_diagnosis_evidence")
    if strict and scope.get("status") != "approved":
        missing.append("approved_scope")
    if strict and assignment.get("status") != "confirmed":
        missing.append("confirmed_assignment")
    if strict and not _assets_ready(task, delivery):
        missing.append("approved_current_assets")
    if strict and not _deployment_stage(task, delivery):
        missing.append("current_deployment_evidence")
    if not verification.get("checks"):
        missing.append("verification_checks")
    if verification.get("review_required") and verification.get("review_status") != "approved":
        missing.append("reviewer_conclusion")
    return {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": delivery.get("cycle_id"),
        "baseline_ref": baseline_ref,
        "diagnosis_evidence": source_refs,
        "scope": {key: scope.get(key) for key in ("status", "reason", "decided_by_role", "decided_at")},
        "assignment": {key: assignment.get(key) for key in (
            "status", "owner_role", "spec_sha256", "confirmed_by_role", "confirmed_at",
        )},
        "assets": assets,
        "approvals": approvals,
        "deployments": deployments,
        "verification": {
            "id": verification.get("id"),
            "verified_at": verification.get("verified_at"),
            "verdict": verification.get("verdict"),
            "review_status": verification.get("review_status"),
            "check_ids": [row.get("id") for row in verification.get("checks", []) if isinstance(row, dict)],
        },
        "complete": not missing,
        "missing": missing,
    }


def _event(
    cycle_id: str,
    task_id: str,
    event_type: str,
    at: str,
    payload: dict | None = None,
    actor_role: str = "system",
) -> dict:
    return {
        "event_id": f"evt-{secrets.token_hex(8)}",
        "cycle_id": cycle_id,
        "task_id": task_id,
        "event_type": event_type,
        "actor_role": actor_role,
        "at": at,
        "payload": copy.deepcopy(payload) if isinstance(payload, dict) else {},
    }


def _milestone_event(
    cycle_id: str,
    task_id: str,
    event_type: str,
    actor_role: str,
    at: str,
    source_id: str,
    payload: dict,
) -> dict:
    """为升级前已存在的交付里程碑生成幂等事件，不伪造新的业务时间。"""
    identity = {
        "cycle_id": cycle_id, "task_id": task_id,
        "event_type": event_type, "source_id": source_id,
    }
    return {
        "event_id": f"evt-{_canonical_sha256(identity)[:16]}",
        "cycle_id": cycle_id, "task_id": task_id,
        "event_type": event_type, "actor_role": actor_role or "agent",
        "at": at, "payload": {"backfilled": True, **copy.deepcopy(payload)},
    }


def evidence_milestone_events(task: dict, fallback_at: str) -> list[dict]:
    """把阶段 2–6 当前真相转换为可去重的周期里程碑事件。"""
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    cycle_id = str(delivery.get("cycle_id") or "legacy")
    task_id = str(task.get("id") or "")
    events = []
    for row in delivery.get("source_refs", []) if isinstance(delivery.get("source_refs"), list) else []:
        if not isinstance(row, dict) or row.get("review_status") != "confirmed" or not row.get("id"):
            continue
        events.append(_milestone_event(
            cycle_id, task_id, "evidence_confirmed",
            str(row.get("reviewed_by_role") or "geo_operator"),
            str(row.get("reviewed_at") or fallback_at), str(row["id"]),
            {"source_ref_id": row.get("id"), "type": row.get("type"), "ref": row.get("ref")},
        ))
    scope = delivery.get("scope_decision") if isinstance(delivery.get("scope_decision"), dict) else {}
    if scope.get("status") and scope.get("status") != "pending":
        source = str(scope.get("decision_id") or _canonical_sha256(scope)[:12])
        events.append(_milestone_event(
            cycle_id, task_id, f"scope_{scope.get('status')}", str(scope.get("decided_by_role") or "project_owner"),
            str(scope.get("decided_at") or fallback_at), source,
            {"status": scope.get("status"), "decision_id": scope.get("decision_id")},
        ))
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    if assignment.get("status") in {"confirmed", "blocked"}:
        source = str(assignment.get("confirmation_id") or assignment.get("spec_sha256") or _canonical_sha256(assignment)[:12])
        events.append(_milestone_event(
            cycle_id, task_id, "task_assigned" if assignment.get("status") == "confirmed" else "assignment_blocked",
            str(assignment.get("confirmed_by_role") or assignment.get("owner_role") or "agent"),
            str(assignment.get("confirmed_at") or fallback_at), source,
            {"status": assignment.get("status"), "spec_sha256": assignment.get("spec_sha256")},
        ))
    for asset in delivery.get("assets", []) if isinstance(delivery.get("assets"), list) else []:
        if not isinstance(asset, dict) or not asset.get("id") or asset.get("missing"):
            continue
        source = f"{asset.get('id')}:{asset.get('version')}:{asset.get('sha256')}"
        events.append(_milestone_event(
            cycle_id, task_id, "asset_updated", "system",
            str(asset.get("scanned_at") or asset.get("generated_at") or fallback_at), source,
            {key: asset.get(key) for key in ("id", "path", "version", "sha256", "approval_status")},
        ))
    for approval in delivery.get("approvals", []) if isinstance(delivery.get("approvals"), list) else []:
        if (
            not isinstance(approval, dict)
            or not approval.get("id")
            or approval.get("type") != "asset"
            or approval.get("status") not in ASSET_DECISION_STATUSES
        ):
            continue
        events.append(_milestone_event(
            cycle_id, task_id, f"asset_{approval.get('status')}", str(approval.get("role") or "system"),
            str(approval.get("at") or fallback_at), str(approval["id"]),
            {key: approval.get(key) for key in ("id", "type", "target", "status", "requirement_id")},
        ))
    for deployment in delivery.get("deployments", []) if isinstance(delivery.get("deployments"), list) else []:
        if not isinstance(deployment, dict) or not deployment.get("id") or deployment.get("human_confirmed") is not True:
            continue
        events.append(_milestone_event(
            cycle_id, task_id, "deployment_submitted",
            str(deployment.get("deployed_by_role") or "web_owner"),
            str(deployment.get("deployed_at") or fallback_at), str(deployment["id"]),
            {key: deployment.get(key) for key in (
                "id", "target_url", "channel", "asset_versions", "evidence_type",
                "evidence_ref", "status", "last_snapshot_ref",
            )},
        ))
    return events


def _append_events(project_dir: Path, cycle_id: str, events: list[dict]) -> str:
    """在状态写回前 append+fsync；日志失败时调用方不得保存任务状态。"""
    cycle_id = _validate_cycle_id(cycle_id)
    path = Path(project_dir) / "delivery" / "events" / f"{cycle_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = []
    for row in events:
        if not isinstance(row, dict):
            raise TypeError("事件必须是 JSON 对象")
        missing = [
            key for key in ("event_id", "cycle_id", "task_id", "event_type", "actor_role", "at", "payload")
            if key not in row
        ]
        if missing:
            raise ValueError("事件缺少字段：" + ", ".join(missing))
        if row.get("cycle_id") != cycle_id:
            raise ValueError("事件 cycle_id 与日志文件不一致")
        if row.get("event_type") not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{row.get('event_type')}")
        if not str(row.get("actor_role") or "").strip() or not str(row.get("at") or "").strip():
            raise ValueError("事件必须包含 actor_role 和 at")
        if not isinstance(row.get("payload"), dict):
            raise TypeError("事件 payload 必须是 JSON 对象")
        try:
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("事件包含不可序列化字段") from exc
        if len(line.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
            raise ValueError("单条事件不能超过 64 KiB")
        encoded.append(line + "\n")
    lines = "".join(encoded).encode("utf-8")
    if lines:
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            offset = 0
            while offset < len(lines):
                written = os.write(fd, lines[offset:])
                if written <= 0:
                    raise OSError("事件日志写入未取得进展")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
    return path.relative_to(project_dir).as_posix()


def read_cycle_events(project_dir: Path, cycle_id: str) -> list[dict]:
    return read_cycle_event_log(project_dir, cycle_id)["events"]


def read_cycle_event_log(project_dir: Path, cycle_id: str) -> dict:
    """容错读取事件日志；损坏行进入 warnings，不遮蔽其他合法事件。"""
    cycle_id = _validate_cycle_id(cycle_id)
    path = Path(project_dir) / "delivery" / "events" / f"{cycle_id}.jsonl"
    if not path.exists():
        return {"event_log_ref": path.relative_to(project_dir).as_posix(), "events": [], "warnings": []}
    rows = []
    warnings = []
    for line_no, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            warnings.append({"code": "invalid_event_json", "line": line_no})
            continue
        if not isinstance(row, dict):
            warnings.append({"code": "invalid_event_object", "line": line_no})
            continue
        if row.get("cycle_id") != cycle_id:
            warnings.append({"code": "event_cycle_mismatch", "line": line_no})
            continue
        rows.append(row)
    return {
        "event_log_ref": path.relative_to(project_dir).as_posix(),
        "events": rows,
        "warnings": warnings,
    }


def append_event(slug: str, cycle_id: str, event: dict) -> dict:
    """在项目锁内追加一条经过校验的事件，供 CLI/API 扩展复用。"""
    import geolib as G

    cycle_id = _validate_cycle_id(cycle_id)
    if not isinstance(event, dict):
        raise TypeError("event 必须是 JSON 对象")
    row = copy.deepcopy(event)
    row.setdefault("event_id", f"evt-{secrets.token_hex(8)}")
    row["cycle_id"] = cycle_id
    row.setdefault("task_id", "")
    row.setdefault("actor_role", "system")
    row.setdefault("at", G.now_iso())
    row.setdefault("payload", {})
    with G.project_lock(slug):
        _append_events(G.project_dir(slug), cycle_id, [row])
    return row


def _next_cycle_recommendations(results: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for result in results:
        if result.get("regression"):
            key = (result.get("id"), "regression")
            if key not in seen:
                out.append({
                    "priority": "P0", "task_id": result.get("id"),
                    "reason": "已关闭事项发生回归，下一周期先恢复上次通过状态",
                    "action": "按失败检查项修复并重新部署、复验",
                })
                seen.add(key)
        for check in result.get("checks", []):
            verdict = check.get("raw_verdict")
            if verdict == "pass" or (not check.get("required", True) and verdict != "error"):
                continue
            key = (result.get("id"), check.get("id"))
            if key in seen:
                continue
            if verdict == "data_missing":
                action = "补齐采样、抓取或可核验部署数据后再判断"
            elif verdict == "manual":
                action = "由 Reviewer 给出批准或驳回结论并填写说明"
            else:
                action = "修复检查项并复跑验收"
            out.append({
                "priority": "P0" if result.get("regression") else "P1",
                "task_id": result.get("id"),
                "check_id": check.get("id"),
                "reason": check.get("note"),
                "action": action,
            })
            seen.add(key)
    return out[:12]


def verify_tasks_data(
    data: dict,
    cfg: dict,
    project_dir: Path,
    audit: dict,
    metrics: dict | None,
    baseline: dict | None,
    legacy_checker: Any,
    verified_at: str,
    task_ids: set[str] | None = None,
) -> tuple[dict, dict, list[dict]]:
    """纯数据层验收：运行全部检查、关闭合格工单、识别并记录回归。"""
    project = normalize_project_config(cfg)
    cycle_id = project["delivery"].get("current_cycle_id")
    baseline_ref = str(project["delivery"].get("current_baseline_ref") or "")
    out = normalize_tasks_data(data, cycle_id=cycle_id)
    results = []
    events = []
    changed = 0
    for task in out["tasks"]:
        if task_ids is not None and str(task.get("id") or "") not in task_ids:
            continue
        delivery = task["delivery"]
        task_cycle = str(delivery.get("cycle_id") or cycle_id or "legacy")
        previous = copy.deepcopy(delivery.get("verification")) if isinstance(delivery.get("verification"), dict) else {}
        previous_pass = previous.get("verdict") == "pass" and previous.get("can_close") is True
        was = task.get("status")
        verification = check_all(
            task, audit, metrics, Path(project_dir), baseline, legacy_checker,
            verified_at=verified_at, verification_id=f"ver-{secrets.token_hex(6)}",
        )
        if previous.get("id"):
            delivery.setdefault("verification_history", []).append(previous)
        delivery["verification"] = verification
        chain = build_evidence_chain(task, baseline_ref, project_dir=Path(project_dir))
        strict_ready = (
            _assets_ready(task, delivery)
            and _deployment_stage(task, delivery) == "deployed"
        ) if delivery.get("cycle_id") else True
        verification["evidence_chain"] = chain
        verification["can_close"] = bool(
            verification.get("verdict") == "pass"
            and strict_ready
            and chain.get("complete")
        )
        events.append(_event(
            task_cycle, str(task.get("id") or ""), "verification_started", verified_at,
            {"verification_id": verification["id"], "check_count": len(verification["checks"])},
        ))
        regression = None
        if verification["can_close"]:
            if task.get("status") != "done":
                task["status"] = "done"
                task["closed_at"] = verified_at
                changed += 1
                events.append(_event(
                    task_cycle, str(task.get("id") or ""), "task_closed", verified_at,
                    {"verification_id": verification["id"]},
                ))
            events.append(_event(
                task_cycle, str(task.get("id") or ""), "verification_passed", verified_at,
                {"verification_id": verification["id"], "review_status": verification["review_status"]},
            ))
            delivery["next_action"] = "持续周期复跑；若后续不达标将自动重开"
        else:
            # 任何导致当前关闭证据链失效的变化都属于回归，包括新增人工门槛、
            # baseline/审批/部署缺失；不能只处理显式 fail/error。
            blocking_regression = previous_pass and not verification["can_close"]
            if task.get("status") == "done" and blocking_regression:
                regression = {
                    "id": f"reg-{secrets.token_hex(6)}",
                    "detected_at": verified_at,
                    "previous_verification_id": previous.get("id"),
                    "verification_id": verification["id"],
                    "failed_check_ids": [
                        row.get("id") for row in verification["checks"]
                        if row.get("required", True) and row.get("raw_verdict") != "pass"
                    ],
                    "reason": "已通过工单在周期复跑中不再满足关闭条件",
                }
                delivery.setdefault("regressions", []).append(regression)
                verification["regression"] = copy.deepcopy(regression)
                task["status"] = "todo"
                task["closed_at"] = None
                changed += 1
                events.extend([
                    _event(task_cycle, str(task.get("id") or ""), "verification_regressed", verified_at, regression),
                    _event(task_cycle, str(task.get("id") or ""), "task_reopened", verified_at, {
                        "verification_id": verification["id"], "reason": regression["reason"],
                    }),
                ])
            event_type = "verification_failed" if verification.get("verdict") in {"fail", "error"} else "verification_manual"
            events.append(_event(
                task_cycle, str(task.get("id") or ""), event_type, verified_at,
                {"verification_id": verification["id"], "verdict": verification.get("verdict")},
            ))
            delivery["next_action"] = (
                "Reviewer 必须给出明确结论并填写说明"
                if verification.get("verdict") == "manual"
                else "按 required 失败项修复后重新验收"
            )
        if regression and _deployment_stage(task, delivery) == "deployed":
            delivery["stage"] = "deployed"
        else:
            delivery["stage"] = compute_stage(task)
        delivery.pop("stage_warning", None)
        task.setdefault("evidence", []).append({
            "at": verified_at,
            "check": "multi_check",
            "result": verification.get("verdict"),
            "note": f"{len(verification['checks'])} 项检查；can_close={verification['can_close']}",
            "verification_id": verification["id"],
        })
        task["evidence"] = task["evidence"][-12:]
        results.append({
            "id": task.get("id"), "title": task.get("title"),
            "priority": task.get("priority"), "market": task.get("market"),
            "package": task.get("package"), "verdict": verification.get("verdict"),
            "can_close": verification.get("can_close"), "review_status": verification.get("review_status"),
            "checks": copy.deepcopy(verification["checks"]),
            "was": was, "now": task.get("status"), "stage": delivery.get("stage"),
            "evidence_chain_complete": chain.get("complete"),
            "regression": copy.deepcopy(regression),
        })
    report = {
        "schema_version": SCHEMA_VERSION,
        "slug": project.get("slug"),
        "cycle_id": cycle_id,
        "verified_at": verified_at,
        "baseline_ref": baseline_ref,
        "audit_avg_score": audit.get("avg_score"),
        "metrics_date": metrics.get("date") if isinstance(metrics, dict) else None,
        "changed": changed,
        "results": results,
        "verdict_counts": {
            verdict: sum(row.get("verdict") == verdict for row in results)
            for verdict in ("pass", "fail", "manual", "data_missing", "error")
        },
        "close_ready_count": sum(row.get("can_close") is True for row in results),
        "close_blocked_count": sum(
            row.get("verdict") == "pass" and row.get("can_close") is not True
            for row in results
        ),
        "regressions": [row["regression"] for row in results if row.get("regression")],
        "next_cycle_recommendations": _next_cycle_recommendations(results),
    }
    return out, report, events


def _refresh_deployments(G: Any, slug: str, task: dict, checked_at: str) -> None:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    cycle_id = str(delivery.get("cycle_id") or "legacy")
    for record in delivery.get("deployments", []) if isinstance(delivery.get("deployments"), list) else []:
        if not isinstance(record, dict) or record.get("human_confirmed") is not True:
            continue
        try:
            snapshot = _deployment_check_snapshot(
                G,
                _validate_public_url(record.get("target_url")),
                _clean_string_list(record.get("expected_snippets")),
                _clean_string_list(record.get("expected_jsonld_types")),
                checked_at=checked_at,
            )
        except Exception as exc:  # noqa: BLE001
            snapshot = _deployment_error_snapshot(
                str(record.get("target_url") or ""), checked_at, exc,
            )
        status, complete, method = _deployment_status_from_snapshot(
            snapshot,
            record.get("channel", "website"),
            record.get("evidence_type", "url"),
            record.get("evidence_ref", ""),
        )
        history = record.setdefault("check_history", [])
        snapshot_ref = _write_deployment_snapshot(
            G, slug, cycle_id, str(record.get("id") or "deployment"), len(history) + 1, snapshot,
        )
        history.append({
            "checked_at": checked_at, "snapshot_ref": snapshot_ref,
            "status": status, "deployment_complete": complete,
        })
        record.update({
            "status": status, "deployment_complete": complete,
            "completion_method": method, "last_checked_at": checked_at,
            "last_http_status": snapshot.get("http_status"),
            "last_snapshot_ref": snapshot_ref, "page_snapshot": snapshot,
        })


def _load_baseline(project_dir: Path, ref: str) -> dict | None:
    if not ref:
        return None
    path = (Path(project_dir) / ref).resolve()
    try:
        path.relative_to(Path(project_dir).resolve())
    except ValueError:
        raise ValueError("current_baseline_ref 不能逃逸项目目录")
    return _strict_read_json(path, "baseline.json") if path.exists() else None


_CLIENT_DROP_KEYS = {
    "api_key", "apikey", "password", "passwd", "secret", "token",
    "authorization", "cookie", "cookies", "prompt", "answer", "raw",
    "html", "body", "content", "excerpt", "request_headers", "response_headers",
    "review_note", "confirmation_note", "note", "expected_snippets",
}
_CLIENT_SENSITIVE_KEY_SUFFIXES = (
    "_api_key", "_password", "_secret", "_token", "_credential",
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def _client_string(value: str) -> str:
    """脱敏客户账本中的凭证、邮箱和本机绝对路径。"""
    text = _EMAIL_RE.sub("[redacted-email]", str(value))
    text = _BEARER_RE.sub("[redacted-credential]", text)
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        return f"[local-path]/{Path(text).name}"
    return text[:2000] + ("…" if len(text) > 2000 else "")


def redact_client_payload(value: Any, key: str = "") -> Any:
    """统一的客户交付脱敏出口；未知字段默认保留，敏感内容默认删除。"""
    folded = str(key or "").strip().lower()
    if (
        folded in _CLIENT_DROP_KEYS
        or folded.endswith(_CLIENT_SENSITIVE_KEY_SUFFIXES)
        or "credential" in folded
    ):
        return None
    if isinstance(value, dict):
        out = {}
        for child_key, child in value.items():
            redacted = redact_client_payload(child, str(child_key))
            if redacted is not None:
                out[str(child_key)] = redacted
        return out
    if isinstance(value, list):
        return [
            redacted for index, item in enumerate(value)
            if (redacted := redact_client_payload(item, f"{key}[{index}]")) is not None
        ]
    if isinstance(value, str):
        return _client_string(value)
    return copy.deepcopy(value)


def _count_values(values: list[Any], defaults: tuple[str, ...] = ()) -> dict:
    counts = {key: 0 for key in defaults}
    for value in values:
        key = str(value if value not in (None, "") else "missing")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _artifact_record(project_dir: Path, ref: str, kind: str) -> dict | None:
    if not ref:
        return None
    root = Path(project_dir).resolve()
    path = (root / str(ref)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return {
        "kind": kind,
        "ref": Path(ref).as_posix(),
        "size": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _cycle_tasks(data: dict, cycle_id: str | None) -> list[dict]:
    rows = data.get("tasks", []) if isinstance(data, dict) else []
    if not cycle_id or cycle_id == "legacy":
        return [row for row in rows if isinstance(row, dict)]
    return [
        row for row in rows if isinstance(row, dict)
        and str(((row.get("delivery") or {}).get("cycle_id")) or "") == str(cycle_id)
    ]


def build_cycle_manifest(
    cfg: dict,
    data: dict,
    report: dict,
    generated_at: str,
    project_dir: Path | None = None,
    events: list[dict] | None = None,
    ledger_refs: list[str] | None = None,
) -> dict:
    """构建当前周期客户可交付 Manifest，而不是 tasks.json 的内部镜像。"""
    delivery_cfg = (cfg.get("delivery") or {}) if isinstance(cfg, dict) else {}
    cycle_id = str(delivery_cfg.get("current_cycle_id") or report.get("cycle_id") or "legacy")
    rows = _cycle_tasks(data, cycle_id)
    event_rows = [
        row for row in (events or []) if isinstance(row, dict)
        and str(row.get("cycle_id") or "") == cycle_id
    ]
    tasks = []
    for task in rows:
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
        source_refs = [
            row for row in delivery.get("source_refs", [])
            if isinstance(row, dict)
        ]
        approvals = [
            {
                **{
                    key: row.get(key) for key in
                    ("id", "type", "target", "requirement_id", "status", "role", "at")
                },
                "explanation_recorded": bool(str(row.get("note") or "").strip()),
            }
            for row in delivery.get("approvals", []) if isinstance(row, dict)
        ]
        tasks.append({
            "id": task.get("id"),
            "title": task.get("title"),
            "priority": task.get("priority"),
            "status": task.get("status"),
            "stage": delivery.get("stage"),
            "diagnosis": {
                "case_type": delivery.get("case_type"),
                "confidence": (delivery.get("diagnosis") or {}).get("confidence"),
                "evidence_count": len(source_refs),
                "confirmed_evidence_count": sum(
                    row.get("review_status") == "confirmed" for row in source_refs
                ),
                "reviews": [
                    {
                        "evidence_id": row.get("id"),
                        "status": row.get("review_status"),
                        "role": row.get("reviewed_by_role"),
                        "at": row.get("reviewed_at"),
                        "explanation_recorded": bool(
                            str(row.get("review_note") or "").strip()
                        ),
                    }
                    for row in source_refs if row.get("review_status") in {"confirmed", "rejected"}
                ],
            },
            "scope": {
                "status": (delivery.get("scope_decision") or {}).get("status"),
                "reason": (delivery.get("scope_decision") or {}).get("reason"),
                "priority_score": (delivery.get("scope_decision") or {}).get("priority_score"),
            },
            "assignment": {
                "status": (delivery.get("assignment") or {}).get("status"),
                "owner_role": (delivery.get("assignment") or {}).get("owner_role"),
                "spec_sha256": (delivery.get("assignment") or {}).get("spec_sha256"),
                "explanation_recorded": bool(str(
                    (delivery.get("assignment") or {}).get("confirmation_note")
                    or (delivery.get("assignment") or {}).get("blocker")
                    or ""
                ).strip()),
            },
            "assets": [
                {key: row.get(key) for key in (
                    "id", "path", "type", "version", "sha256", "size",
                    "required", "approval_status",
                )}
                for row in delivery.get("assets", []) if isinstance(row, dict)
            ],
            "approvals": approvals,
            "deployments": [
                {
                    **{key: row.get(key) for key in (
                        "id", "target_url", "channel", "status", "deployed_at",
                        "last_checked_at", "last_http_status", "last_snapshot_ref",
                    )},
                    "confirmation_recorded": bool(str(row.get("note") or "").strip()),
                }
                for row in _current_deployments(task)
            ],
            "verification": {
                "id": verification.get("id"),
                "verified_at": verification.get("verified_at"),
                "verdict": verification.get("verdict"),
                "can_close": verification.get("can_close"),
                "review_status": verification.get("review_status"),
                "evidence_chain_complete": (verification.get("evidence_chain") or {}).get("complete"),
                "checks": [
                    {key: check.get(key) for key in (
                        "id", "check", "required", "verdict", "raw_verdict",
                        "before", "after", "target",
                    )}
                    for check in verification.get("checks", []) if isinstance(check, dict)
                ],
            },
            "regression_count": len(delivery.get("regressions", [])),
        })

    artifacts = []
    if project_dir is not None:
        project_path = Path(project_dir)
        refs = [
            (delivery_cfg.get("current_baseline_ref"), "baseline"),
            (f"delivery/events/{cycle_id}.jsonl", "event_log"),
            ("deliverables/4-GEO交付追踪报告.md", "tracking_report"),
            ("deliverables/4-GEO交付追踪报告.html", "tracking_report"),
        ]
        verify_dir = project_path / "verify"
        for path in sorted(verify_dir.glob("*.json")) if verify_dir.exists() else []:
            payload = _strict_read_json(path, "verification snapshot")
            if str(payload.get("cycle_id") or "") == cycle_id:
                refs.append((f"verify/{path.name}", "verification_snapshot"))
        refs.extend((ref, "client_ledger") for ref in (ledger_refs or []))
        for ref, kind in refs:
            record = _artifact_record(project_path, str(ref or ""), kind)
            if record:
                artifacts.append(record)

    signal_summary = {}
    if project_dir is not None:
        try:
            import signals
            signal_summary = signals.build_signal_summary(
                signals.read_signal_log(Path(project_dir) / "business_signals.jsonl")
            )
        except Exception:  # noqa: BLE001
            signal_summary = {"signal_rows": 0, "unavailable": True}
    summary = {
        "task_total": len(rows),
        "by_stage": _count_values(
            [((task.get("delivery") or {}).get("stage")) for task in rows],
            DELIVERY_STAGES,
        ),
        "by_status": _count_values(
            [task.get("status") for task in rows],
            ("todo", "doing", "done", "blocked", "wontfix"),
        ),
        "by_priority": _count_values([task.get("priority") for task in rows]),
        "by_scope": _count_values([
            ((task.get("delivery") or {}).get("scope_decision") or {}).get("status")
            for task in rows
        ], tuple(sorted(SCOPE_STATUSES))),
        "asset_versions": sum(
            len((task.get("delivery") or {}).get("assets", [])) for task in rows
        ),
        "deployment_count": sum(
            len(_current_deployments(task)) for task in rows
        ),
        "close_ready_count": sum(
            ((task.get("delivery") or {}).get("verification") or {}).get("can_close") is True
            for task in rows
        ),
        "regression_count": sum(
            len((task.get("delivery") or {}).get("regressions", [])) for task in rows
        ),
        "business_signals": signal_summary,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_type": "geo_delivery_cycle_ledger",
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        "project": {
            "slug": cfg.get("slug"),
            "brand": (cfg.get("brand") or {}).get("name"),
            "site": (cfg.get("brand") or {}).get("site"),
            "industry": delivery_cfg.get("industry"),
            "target_markets": copy.deepcopy(delivery_cfg.get("target_markets") or []),
            "product_lines": copy.deepcopy(delivery_cfg.get("product_lines") or []),
            "conversion_goal": delivery_cfg.get("conversion_goal"),
        },
        "baseline_ref": delivery_cfg.get("current_baseline_ref"),
        "scope_sha256": ((delivery_cfg.get("scope_confirmation") or {}).get("scope_sha256")),
        "summary": summary,
        "event_summary": {
            "count": len(event_rows),
            "by_type": _count_values([row.get("event_type") for row in event_rows]),
            "first_at": event_rows[0].get("at") if event_rows else None,
            "last_at": event_rows[-1].get("at") if event_rows else None,
        },
        "verification_summary": {
            "verified_at": report.get("verified_at"),
            "verdict_counts": copy.deepcopy(report.get("verdict_counts") or {}),
            "regression_count": len(report.get("regressions", [])),
        },
        "tasks": tasks,
        "artifacts": artifacts,
        "next_cycle_recommendations": copy.deepcopy(report.get("next_cycle_recommendations", [])),
        "redaction": {
            "profile": "client_delivery_v1",
            "excluded": [
                "credentials", "emails", "absolute_local_paths",
                "prompts_and_answers", "page_content", "internal_notes",
            ],
        },
    }
    return redact_client_payload(manifest)


def build_current_verification_report(
    slug: str,
    cfg: dict,
    data: dict,
    at: str,
    event_log_ref: str = "",
) -> dict:
    """从 tasks.json 当前真相重建 Reviewer 决策后的周期验收摘要。"""
    results = []
    regressions = []
    cycle_id = str((cfg.get("delivery") or {}).get("current_cycle_id") or "legacy")
    for task in _cycle_tasks(data, cycle_id):
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
        latest_regression = (
            delivery.get("regressions", [])[-1]
            if isinstance(delivery.get("regressions"), list) and delivery.get("regressions")
            else None
        )
        if latest_regression:
            regressions.append(copy.deepcopy(latest_regression))
        results.append({
            "id": task.get("id"), "title": task.get("title"),
            "priority": task.get("priority"), "market": task.get("market"),
            "package": task.get("package"), "verdict": verification.get("verdict", "manual"),
            "can_close": verification.get("can_close", False),
            "review_status": verification.get("review_status", "pending"),
            "checks": copy.deepcopy(verification.get("checks", [])),
            "now": task.get("status"), "stage": delivery.get("stage"),
            "evidence_chain_complete": (verification.get("evidence_chain") or {}).get("complete", False),
            "regression": copy.deepcopy(latest_regression),
        })
    report = {
        "schema_version": SCHEMA_VERSION, "slug": slug,
        "cycle_id": cycle_id,
        "verified_at": at,
        "baseline_ref": (cfg.get("delivery") or {}).get("current_baseline_ref"),
        "event_log_ref": event_log_ref,
        "changed": 0, "results": results,
        "verdict_counts": {
            verdict: sum(row.get("verdict") == verdict for row in results)
            for verdict in ("pass", "fail", "manual", "data_missing", "error")
        },
        "regressions": regressions,
    }
    report["next_cycle_recommendations"] = _next_cycle_recommendations(results)
    return report


def _md(value: Any) -> str:
    return _client_string(
        str(value if value not in (None, "") else "—")
    ).replace("|", "\\|").replace("\n", " ")


def tracking_report_markdown(cfg: dict, data: dict, report: dict, manifest_ref: str) -> str:
    """生成客户可读台账；不输出绝对路径、密钥、内部审批备注或页面正文。"""
    brand = (cfg.get("brand") or {}).get("name") or cfg.get("slug") or "项目"
    delivery_cfg = cfg.get("delivery") if isinstance(cfg.get("delivery"), dict) else {}
    cycle_id = str(report.get("cycle_id") or delivery_cfg.get("current_cycle_id") or "legacy")
    tasks = _cycle_tasks(data, cycle_id)
    counts = report.get("verdict_counts") or {}
    closed = sum(
        task.get("status") == "done"
        and (((task.get("delivery") or {}).get("verification") or {}).get("can_close") is True)
        for task in tasks
    )
    lines = [
        f"# {brand} · GEO 交付追踪报告", "",
        f"周期：**{_md(report.get('cycle_id'))}** ｜ 复盘时间：{_md(report.get('verified_at'))}", "",
        "本报告记录本周期范围、资产、部署、验收、回归与下一周期建议。工单只有在必需审批、部署证据和 required 验收全部通过后才关闭。", "",
        "## 一、周期结论", "",
        f"- 周期执行工单：**{len(tasks)}**",
        f"- 已通过完整关闭证据链：**{closed}**",
        f"- 未达标：**{counts.get('fail', 0) + counts.get('error', 0)}**",
        f"- 待 Reviewer：**{counts.get('manual', 0)}**",
        f"- 数据不足：**{counts.get('data_missing', 0)}**",
        f"- 本次回归：**{len(report.get('regressions', []))}**",
        f"- 目标市场：**{_md(' / '.join(delivery_cfg.get('target_markets') or []))}**",
        f"- 产品线：**{_md(' / '.join(delivery_cfg.get('product_lines') or []))}**",
        f"- ICP：**{_md(' / '.join(str(row.get('name') or row.get('id') or '') if isinstance(row, dict) else str(row) for row in delivery_cfg.get('icps') or []))}**",
        f"- 转化目标：**{_md(' / '.join(delivery_cfg.get('conversion_goals') or []))}**", "",
        "## 二、业务优先级与周期范围推荐", "",
    ]
    import prioritize
    lines += prioritize.scope_report_fragment(data)
    lines += ["", "| 排名 / 工单 | 产品线 | 购买阶段 | 分数 | 建议 | 容量说明 |",
              "|---|---|---|---:|---|---|"]
    priority_rows = sorted(tasks, key=lambda task: (
        int(((task.get("delivery") or {}).get("priority") or {}).get("rank", 9999)),
        str(task.get("id") or ""),
    ))
    for task in priority_rows:
        task_delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        priority = task_delivery.get("priority") if isinstance(task_delivery.get("priority"), dict) else {}
        lines.append(
            f"| {_md(priority.get('rank'))} / {_md(task.get('id'))} · {_md(task.get('title'))} "
            f"| {_md(task.get('product_line') or task_delivery.get('product_line'))} "
            f"| {_md(task.get('buying_stage') or task_delivery.get('buying_stage'))} "
            f"| {_md(priority.get('score'))} | {_md(priority.get('recommendation'))} "
            f"| {_md(priority.get('capacity_reason'))} |"
        )
    lines += ["", "> 排序是规则型决策辅助，不等于范围批准、收入预测或商业事实结论。", ""]
    signal_summary = report.get("business_signals") if isinstance(report.get("business_signals"), dict) else {}
    lines += ["## 三、观察性业务信号", "",
        "本节只汇总用户显式导入的 GA4、Search Console、CRM、表单或销售记录；它不提供收入归因，也不证明 GEO 造成了变化。", "",
        f"- 信号行数：**{_md(signal_summary.get('signal_rows', 0))}**；观察数量：**{_md(signal_summary.get('observed_count', 0))}**",
        f"- AI referral sessions：**{_md(signal_summary.get('ai_referral_sessions', 0))}**",
        f"- 表单提交：**{_md(signal_summary.get('form_submissions', 0))}**；Demo / RFQ：**{_md(signal_summary.get('demo_rfq', 0))}**",
        f"- 销售自报 AI 来源：**{_md(signal_summary.get('sales_ai_self_report', 0))}**",
        f"- 已映射工单：**{_md(signal_summary.get('mapped_rows', 0))}**；项目级未映射：**{_md(signal_summary.get('unmapped_rows', 0))}**",
        f"- 数据完整度：**{_md(json.dumps(signal_summary.get('data_completeness', {}), ensure_ascii=False, sort_keys=True))}**", "",
        "> 这些指标是交付结果旁的方向性观察。数据不足、页面映射不唯一或来源为销售自报时，报告保留其不确定性。", "",
        "## 四、工单交付台账", "",
        "| 工单 | 阶段 | 状态 | 诊断证据 | 范围 | 资产 | 部署 | 验收 | 关闭 |",
        "|---|---|---|---:|---|---:|---:|---|---|",
    ]
    by_id = {row.get("id"): row for row in report.get("results", [])}
    for task in tasks:
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        result = by_id.get(task.get("id"), {})
        confirmed = sum(
            row.get("review_status") == "confirmed"
            for row in delivery.get("source_refs", []) if isinstance(row, dict)
        )
        lines.append(
            f"| {_md(task.get('id'))} · {_md(task.get('title'))} | {_md(delivery.get('stage'))} "
            f"| {_md(task.get('status'))} | {confirmed} "
            f"| {_md((delivery.get('scope_decision') or {}).get('status'))} "
            f"| {len(delivery.get('assets', []))} | {len(_current_deployments(task))} "
            f"| {_md(result.get('verdict'))} "
            f"| {'可关闭' if result.get('can_close') else '未满足'} |"
        )
    lines += ["", "## 五、资产版本、审批与部署", "",
              "| 工单 | 资产 / 版本 | 哈希 | 审批 | 部署 URL | 部署状态 |",
              "|---|---|---|---|---|---|"]
    asset_rows = 0
    for task in tasks:
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        deployments = _current_deployments(task)
        deployment = deployments[-1] if deployments else {}
        for asset in delivery.get("assets", []) if isinstance(delivery.get("assets"), list) else []:
            if not isinstance(asset, dict):
                continue
            asset_rows += 1
            lines.append(
                f"| {_md(task.get('id'))} | {_md(asset.get('path'))} / v{_md(asset.get('version'))} "
                f"| `{_md(str(asset.get('sha256') or '')[:12])}` "
                f"| {_md(asset.get('approval_status'))} | {_md(deployment.get('target_url'))} "
                f"| {_md(deployment.get('status'))} |"
            )
    if not asset_rows:
        lines.append("| — | 本周期尚无已绑定资产 | — | — | — | — |")
    lines += ["", "## 六、基线 → 当前 → 目标", "",
              "| 工单 / 检查 | Before | After | Target | 结论 |", "|---|---:|---:|---:|---|"]
    for result in report.get("results", []):
        for check in result.get("checks", []):
            lines.append(
                f"| {_md(result.get('id'))} / {_md(check.get('check'))} | {_md(check.get('before'))} "
                f"| {_md(check.get('after'))} | {_md(check.get('target'))} | {_md(check.get('verdict'))} |"
            )
    lines += ["", "## 七、回归记录", ""]
    regressions = report.get("regressions", [])
    if regressions:
        lines += ["| 时间 | 原验收 | 新验收 | 失败检查 |", "|---|---|---|---|"]
        for row in regressions:
            lines.append(
                f"| {_md(row.get('detected_at'))} | {_md(row.get('previous_verification_id'))} "
                f"| {_md(row.get('verification_id'))} | {_md(', '.join(row.get('failed_check_ids') or []))} |"
            )
    else:
        lines.append("本周期未发现已关闭事项回归。")
    lines += ["", "## 八、下一周期建议", ""]
    recommendations = report.get("next_cycle_recommendations", [])
    if recommendations:
        for index, row in enumerate(recommendations, 1):
            lines.append(
                f"{index}. **{_md(row.get('priority'))} · {_md(row.get('task_id'))}**："
                f"{_md(row.get('action'))}（依据：{_md(row.get('reason'))}）"
            )
    else:
        lines.append("当前 required 检查均已闭环；下一周期继续按既定节奏复跑并观察结果指标。")
    lines += ["", "## 九、签收与可回放性", "",
              f"- 周期 Manifest：`{_md(manifest_ref)}`",
              f"- 原始基线：`{_md(report.get('baseline_ref'))}`",
              f"- 验收快照目录：`verify/`",
              f"- 周期事件日志：`delivery/events/{_md(report.get('cycle_id'))}.jsonl`", "",
              "> 客户账本使用 client_delivery_v1 脱敏规则；页面正文、Prompt、AI 回答、密钥、邮箱、本机绝对路径和内部说明不会进入交付包。", ""]
    return "\n".join(lines)


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temp.write_text(value, "utf-8")
    temp.replace(path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _latest_cycle_verification(project_dir: Path, cycle_id: str) -> dict | None:
    verify_dir = Path(project_dir) / "verify"
    for path in reversed(sorted(verify_dir.glob("*.json")) if verify_dir.exists() else []):
        value = _strict_read_json(path, "verification snapshot")
        if str(value.get("cycle_id") or "legacy") == cycle_id:
            return value
    return None


def write_cycle_ledger(
    slug: str,
    cfg: dict,
    data: dict,
    report: dict,
    generated_at: str,
) -> dict:
    """写入可复制进客户交付包的脱敏周期账本和规范 Manifest。"""
    import geolib as G

    project_dir = G.project_dir(slug)
    cycle_id = _validate_cycle_id(
        (cfg.get("delivery") or {}).get("current_cycle_id")
        or report.get("cycle_id")
        or "legacy"
    )
    ledger_dir = project_dir / "delivery" / "ledger" / cycle_id
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_refs = []

    baseline_ref = str((cfg.get("delivery") or {}).get("current_baseline_ref") or "")
    baseline = _load_baseline(project_dir, baseline_ref) if baseline_ref else None
    if baseline is not None:
        baseline_path = ledger_dir / "baseline.json"
        _atomic_write_json(baseline_path, redact_client_payload(baseline))
        ledger_refs.append(baseline_path.relative_to(project_dir).as_posix())

    events = read_cycle_events(project_dir, cycle_id)
    client_events = []
    for event in events:
        client_events.append(redact_client_payload({
            key: event.get(key) for key in (
                "event_id", "cycle_id", "task_id", "event_type", "actor_role", "at", "payload",
            )
        }))
    events_path = ledger_dir / "events.jsonl"
    _atomic_write_text(
        events_path,
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in client_events),
    )
    ledger_refs.append(events_path.relative_to(project_dir).as_posix())

    verification_path = ledger_dir / "verification.json"
    _atomic_write_json(verification_path, redact_client_payload(report))
    ledger_refs.append(verification_path.relative_to(project_dir).as_posix())

    deployments = []
    for task in _cycle_tasks(data, cycle_id):
        current = _current_deployments(task)
        if current:
            deployments.append({
                "task_id": task.get("id"),
                "deployments": redact_client_payload(current),
            })
    deployments_path = ledger_dir / "deployments.json"
    _atomic_write_json(deployments_path, deployments)
    ledger_refs.append(deployments_path.relative_to(project_dir).as_posix())

    readme_path = ledger_dir / "README.md"
    _atomic_write_text(readme_path, "\n".join([
        f"# GEO 客户交付账本 · {cycle_id}", "",
        "本目录包含脱敏后的基线、完整事件时间线、验收快照和部署检查索引。", "",
        "- `manifest.json`：周期聚合、逐工单证据链与文件完整性哈希",
        "- `baseline.json`：锁定范围与原始基线的客户可见投影",
        "- `events.jsonl`：按发生顺序排列的脱敏事件",
        "- `verification.json`：最近一次周期验收结果",
        "- `deployments.json`：公开部署 URL 与检查快照索引", "",
        "已移除 Prompt、AI 回答、页面正文、密钥、邮箱、本机路径和内部说明。", "",
    ]))
    ledger_refs.append(readme_path.relative_to(project_dir).as_posix())

    manifest = build_cycle_manifest(
        cfg, data, report, generated_at,
        project_dir=project_dir, events=events, ledger_refs=ledger_refs,
    )
    manifest_ref = f"delivery/manifests/{cycle_id}.json"
    G.write_json(project_dir / manifest_ref, manifest)
    _atomic_write_json(ledger_dir / "manifest.json", manifest)
    return {
        "cycle_id": cycle_id,
        "manifest_ref": manifest_ref,
        "ledger_ref": ledger_dir.relative_to(project_dir).as_posix(),
        "files": [*ledger_refs, f"{ledger_dir.relative_to(project_dir).as_posix()}/manifest.json"],
    }


def write_tracking_report(
    slug: str,
    cfg: dict | None = None,
    data: dict | None = None,
    verification_report: dict | None = None,
) -> Path:
    import geolib as G
    import report as R
    import tasks as T

    project_dir = G.project_dir(slug)
    cfg = normalize_project_config(cfg if cfg is not None else G.load_config(slug))
    data = normalize_tasks_data(data if data is not None else T.load(slug), cfg["delivery"].get("current_cycle_id"))
    cycle_id = str(cfg["delivery"].get("current_cycle_id") or "legacy")
    if verification_report is None:
        verification_report = _latest_cycle_verification(project_dir, cycle_id)
    if (
        not isinstance(verification_report, dict)
        or str(verification_report.get("cycle_id") or "legacy") != cycle_id
    ):
        verification_report = build_current_verification_report(
            slug, cfg, data, G.now_iso(),
            event_log_ref=f"delivery/events/{cycle_id}.jsonl",
        )
    verification_report["cycle_id"] = cycle_id
    verification_report.setdefault("baseline_ref", cfg["delivery"].get("current_baseline_ref"))
    verification_report.setdefault("regressions", [])
    verification_report.setdefault("next_cycle_recommendations", [])
    try:
        import signals
        verification_report["business_signals"] = signals.build_signal_summary(
            signals.read_signal_log(project_dir / "business_signals.jsonl")
        )
    except Exception:  # noqa: BLE001
        verification_report["business_signals"] = {"signal_rows": 0, "unavailable": True}
    manifest_ref = f"delivery/manifests/{cycle_id}.json"
    verification_report["manifest_ref"] = manifest_ref
    markdown = tracking_report_markdown(cfg, data, verification_report, manifest_ref)
    out = project_dir / "deliverables"
    md_path = out / "4-GEO交付追踪报告.md"
    html_path = out / "4-GEO交付追踪报告.html"
    _atomic_write_text(md_path, markdown)
    metrics = [
        ("完整关闭", str(sum(
            row.get("can_close") is True for row in verification_report.get("results", [])
        ))),
        ("待人工", str((verification_report.get("verdict_counts") or {}).get("manual", 0))),
        ("回归", str(len(verification_report.get("regressions", [])))),
    ]
    _atomic_write_text(html_path, R.build_html(f"{(cfg.get('brand') or {}).get('name', slug)} · GEO 交付追踪报告", markdown, metrics))
    write_cycle_ledger(
        slug, cfg, data, verification_report,
        str(verification_report.get("verified_at") or G.now_iso()),
    )
    return out


def run_verification(
    slug: str,
    audit: dict,
    metrics: dict | None,
    legacy_checker: Any,
    task_id: str | None = None,
) -> dict:
    """阶段 7 主流程：重抓部署、全量检查、事件先行写入、状态原子保存并出报告。"""
    import geolib as G
    import tasks as T

    project_dir = G.project_dir(slug)
    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = cfg["delivery"].get("current_cycle_id")
        if not cycle_id:
            # 旧项目继续可验收，但不会伪造 Public Alpha 基线和证据链。
            cycle_id = "legacy"
        verified_at = G.now_iso()
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cfg["delivery"].get("current_cycle_id"))
        if not data.get("tasks"):
            raise ValueError("还没有工单，不能运行验收")
        task_ids = {str(task_id)} if task_id else None
        if task_ids and not any(str(row.get("id") or "") in task_ids for row in data["tasks"]):
            raise KeyError(f"找不到工单 {task_id}")
        for task in data["tasks"]:
            if task_ids is not None and str(task.get("id") or "") not in task_ids:
                continue
            _refresh_deployments(G, slug, task, verified_at)
        baseline = _load_baseline(project_dir, str(cfg["delivery"].get("current_baseline_ref") or ""))
        data, report, events = verify_tasks_data(
            data, cfg, project_dir, audit, metrics, baseline, legacy_checker, verified_at,
            task_ids=task_ids,
        )
        report["slug"] = slug
        existing_ids = {
            row.get("event_id") for row in read_cycle_events(project_dir, str(cycle_id))
            if isinstance(row, dict) and row.get("event_id")
        }
        milestones = [
            event for task in data["tasks"]
            if task_ids is None or str(task.get("id") or "") in task_ids
            for event in evidence_milestone_events(task, verified_at)
            if event.get("event_id") not in existing_ids
        ]
        events = milestones + events
        event_ref = _append_events(project_dir, str(cycle_id), events)
        report["event_log_ref"] = event_ref
        T.save(slug, data)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        verify_ref = f"verify/{stamp}.json"
        report["verification_snapshot_ref"] = verify_ref
        manifest_ref = f"delivery/manifests/{cycle_id}.json"
        report["manifest_ref"] = manifest_ref
        G.write_json(project_dir / verify_ref, report)
        manifest = build_cycle_manifest(cfg, data, report, verified_at)
        G.write_json(project_dir / manifest_ref, manifest)
    write_tracking_report(slug, cfg=cfg, data=data, verification_report=report)
    return report


def record_verification_review(
    slug: str,
    task_id: str,
    status: str,
    note: str,
    role: str = "reviewer",
) -> dict:
    """Reviewer 对当前验收版本给出明确结论；自动失败不能被人工覆盖。"""
    import geolib as G
    import tasks as T

    if status not in {"approved", "rejected"}:
        raise ValueError("status 必须是 approved 或 rejected")
    if str(role or "").strip() != "reviewer":
        raise ValueError("最终人工验收必须由 reviewer 完成")
    note = _validated_text(note, "Reviewer 判断说明")
    project_dir = G.project_dir(slug)
    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        cycle_id = str(cfg["delivery"].get("current_cycle_id") or "legacy")
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cfg["delivery"].get("current_cycle_id"))
        task = next((row for row in data["tasks"] if row.get("id") == task_id), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        delivery = task["delivery"]
        verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
        if not verification.get("id") or not verification.get("review_target"):
            raise ValueError("工单尚无可供 Reviewer 判断的验收快照")
        if status == "approved" and verification.get("automated_verdict") in {"fail", "error"}:
            raise ValueError("自动 required 检查失败，Reviewer 不能覆盖为通过")
        now = G.now_iso()
        approval = {
            "id": f"ap-{secrets.token_hex(6)}", "type": "final",
            "target": verification["review_target"], "task_id": task_id,
            "verification_id": verification["id"], "status": status,
            "role": "reviewer", "at": now, "note": note,
            "resolved_check_ids": [
                row.get("id") for row in verification.get("checks", [])
                if row.get("required", True) and row.get("raw_verdict") in {"manual", "data_missing"}
            ],
        }
        delivery.setdefault("approvals", []).append(approval)
        verification["review_status"] = status
        verification["review"] = copy.deepcopy(approval)
        verification["verdict"] = (
            "pass" if status == "approved" and verification.get("automated_verdict") in {"pass", "manual", "data_missing"}
            else "fail"
        )
        chain = build_evidence_chain(
            task,
            str(cfg["delivery"].get("current_baseline_ref") or ""),
            project_dir=project_dir,
        )
        verification["evidence_chain"] = chain
        strict_ready = (
            _assets_ready(task, delivery) and _deployment_stage(task, delivery) == "deployed"
        ) if delivery.get("cycle_id") else True
        verification["can_close"] = bool(
            verification["verdict"] == "pass" and strict_ready and chain.get("complete")
        )
        events = [_event(
            cycle_id, task_id, "verification_manual", now,
            {"verification_id": verification["id"], "decision": status}, actor_role="reviewer",
        )]
        if verification["can_close"]:
            task["status"] = "done"
            task["closed_at"] = now
            events.extend([
                _event(cycle_id, task_id, "verification_passed", now, {
                    "verification_id": verification["id"], "review_status": status,
                }, actor_role="reviewer"),
                _event(cycle_id, task_id, "task_closed", now, {
                    "verification_id": verification["id"],
                }, actor_role="reviewer"),
            ])
            delivery["next_action"] = "持续周期复跑"
        elif task.get("status") == "done":
            task["status"] = "todo"
            task["closed_at"] = None
            regression = {
                "id": f"reg-{secrets.token_hex(6)}",
                "detected_at": now,
                "previous_verification_id": verification.get("id"),
                "verification_id": verification.get("id"),
                "failed_check_ids": approval.get("resolved_check_ids", []),
                "reason": "Reviewer 未批准最终验收",
            }
            delivery.setdefault("regressions", []).append(regression)
            verification["regression"] = copy.deepcopy(regression)
            events.extend([
                _event(
                    cycle_id, task_id, "verification_regressed", now, regression,
                    actor_role="reviewer",
                ),
                _event(cycle_id, task_id, "task_reopened", now, {
                    "verification_id": verification["id"],
                    "reason": regression["reason"],
                    "regression_id": regression["id"],
                }, actor_role="reviewer"),
            ])
        delivery["stage"] = compute_stage(task)
        event_ref = _append_events(project_dir, cycle_id, events)
        T.save(slug, data)
        review_report = build_current_verification_report(
            slug, cfg, data, now, event_log_ref=event_ref,
        )
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        verify_ref = f"verify/{stamp}-review-{secrets.token_hex(3)}.json"
        review_report["verification_snapshot_ref"] = verify_ref
        manifest_ref = f"delivery/manifests/{cycle_id}.json"
        review_report["manifest_ref"] = manifest_ref
        G.write_json(project_dir / verify_ref, review_report)
        G.write_json(
            project_dir / manifest_ref,
            build_cycle_manifest(cfg, data, review_report, now),
        )
    write_tracking_report(slug, cfg=cfg, data=data, verification_report=review_report)
    return {
        "task": copy.deepcopy(task), "approval": approval,
        "verification": copy.deepcopy(verification),
        "report": review_report,
    }


def normalize_tasks(slug: str, save: bool = False) -> dict:
    """读取并标准化一个 GeoLook 项目。

    ``save=False`` 是懒迁移读取，不写文件。``save=True`` 是显式
    ``delivery-sync``：复用 ``G.save_config`` 和 ``tasks.save`` 的备份、
    摘要和原子写路径。
    """
    import geolib as G
    cfg = G.load_config(slug)
    delivery_cfg = normalize_config(cfg.get("delivery"))
    cycle_id = delivery_cfg.get("current_cycle_id")

    raw = _load_raw_tasks(G, slug)
    normalized = normalize_tasks_data(raw, cycle_id=cycle_id)
    normalized["delivery_migration"] = {
        "schema_version": SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "normalized_tasks": len(normalized.get("tasks", [])),
    }

    if not save:
        return normalized

    import tasks as T

    with G.project_lock(slug):
        # 锁内重新读取，避免显式同步覆盖刚发生的状态更新。
        cfg = G.load_config(slug)
        delivery_cfg = normalize_config(cfg.get("delivery"))
        cycle_id = delivery_cfg.get("current_cycle_id") or cycle_id
        cfg["delivery"] = delivery_cfg

        raw = _load_raw_tasks(G, slug)
        normalized = normalize_tasks_data(raw, cycle_id=cycle_id)
        normalized["delivery_migration"] = {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "normalized_tasks": len(normalized.get("tasks", [])),
        }
        G.save_config(slug, cfg)
        T.save(slug, normalized)
    return normalized


def delivery_sync(slug: str) -> dict:
    """显式、幂等地把兼容字段写回旧项目。"""
    return normalize_tasks(slug, save=True)


def record_external_ref(
    slug: str,
    task_id: str,
    system: str,
    external_id: str,
    url: str = "",
    *,
    external_state: str = "",
    note: str = "",
    actor_role: str = "project_owner",
) -> dict:
    """Persist an external execution reference without changing delivery truth."""
    import geolib as G
    import tasks as T

    system = str(system or "").strip().lower()
    external_id = str(external_id or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", system):
        raise ValueError("system 必须是小写适配器标识")
    if not external_id or len(external_id) > 200:
        raise ValueError("external_id 不能为空且不能超过 200 字符")
    url = str(url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("外部 URL 必须是 http(s)")
    now = G.now_iso()
    with G.project_lock(slug):
        cfg = normalize_project_config(G.load_config(slug))
        data = normalize_tasks_data(_load_raw_tasks(G, slug), cfg["delivery"].get("current_cycle_id"))
        task = next((row for row in data.get("tasks", []) if str(row.get("id")) == str(task_id)), None)
        if task is None:
            raise KeyError(f"找不到工单 {task_id}")
        refs = task.setdefault("external_refs", [])
        ref = {
            "system": system, "id": external_id, "url": url,
            "synced_at": now, "external_state": str(external_state or ""),
        }
        if note:
            ref["note"] = _validated_text(note, "同步说明", required=False)
        existing = next((row for row in refs if row.get("system") == system and row.get("id") == external_id), None)
        if existing is None:
            refs.append(ref)
        else:
            existing.update(ref)
            ref = existing
        cycle_id = str((task.get("delivery") or {}).get("cycle_id") or cfg["delivery"].get("current_cycle_id") or "legacy")
        _append_events(G.project_dir(slug), cycle_id, [_event(
            cycle_id, str(task_id), "external_sync_recorded", now,
            {"system": system, "external_id": external_id, "url": url,
             "external_state": str(external_state or "")}, actor_role=actor_role,
        )])
        T.save(slug, data)
        return copy.deepcopy(task)


# ---------------------------------------------------------------- Public Alpha 发布诊断

_DOCTOR_REPORT_SUFFIXES = {".json", ".jsonl", ".md", ".html", ".txt", ".csv"}
_DOCTOR_SECRET_PATTERNS = (
    ("credential", re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*[\"']?"
        r"(?!\[redacted)[A-Za-z0-9._~+/=-]{8,}"
    )),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")),
    ("authorization", _BEARER_RE),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("absolute_path", re.compile(
        r"(?:/Users/[^\s<>'\"]+|/home/[^\s<>'\"]+|[A-Za-z]:\\Users\\[^\s<>'\"]+)"
    )),
    ("email", _EMAIL_RE),
)


def _doctor_item(
    level: str,
    code: str,
    message: str,
    *,
    task_id: str = "",
    ref: str = "",
    next_action: str = "",
) -> dict:
    return {
        "level": level,
        "code": code,
        "message": message,
        "task_id": task_id,
        "ref": ref,
        "next_action": next_action,
    }


def _doctor_finish(slug: str, cycle_id: str | None, checks: list[dict]) -> dict:
    counts = {
        level: sum(row.get("level") == level for row in checks)
        for level in ("PASS", "WARN", "FAIL")
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "doctor_version": "public_alpha_v1",
        "slug": slug,
        "cycle_id": cycle_id,
        "ok": counts["FAIL"] == 0,
        "summary": counts,
        "checks": checks,
    }


def _doctor_local_ref(project_dir: Path, ref: Any) -> tuple[bool, str]:
    """校验项目内相对引用；空引用由上层按业务必填规则处理。"""
    text = str(ref or "").strip()
    if not text:
        return True, ""
    if text.startswith(("http://", "https://")):
        try:
            _validate_public_url(text, "URL")
        except ValueError as exc:
            return False, str(exc)
        return True, ""
    root = Path(project_dir).resolve()
    candidate = (root / text).resolve() if not Path(text).is_absolute() else Path(text).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False, "引用逃逸项目目录"
    return True, ""


def _doctor_report_files(project_dir: Path) -> list[Path]:
    roots = (
        Path(project_dir) / "deliverables",
        Path(project_dir) / "delivery" / "manifests",
        Path(project_dir) / "delivery" / "ledger",
    )
    files = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in _DOCTOR_REPORT_SUFFIXES
            and path.stat().st_size <= 8 * 1024 * 1024
        )
    return sorted(set(files))


def delivery_doctor_data(
    cfg: dict,
    data: dict,
    project_dir: Path,
    *,
    slug: str = "",
) -> dict:
    """只读检查一个项目是否满足 Public Alpha 发布门槛。"""
    project_dir = Path(project_dir)
    checks: list[dict] = []
    project = normalize_project_config(cfg)
    delivery_cfg = project["delivery"]
    cycle_id = delivery_cfg.get("current_cycle_id")

    try:
        normalized = normalize_tasks_data(data, cycle_id=None)
    except (TypeError, ValueError) as exc:
        checks.append(_doctor_item(
            "FAIL", "tasks_schema_invalid", f"任务结构无法兼容：{exc}",
            ref="tasks.json", next_action="从 .geo.bak 恢复有效 tasks.json 后重试",
        ))
        return _doctor_finish(slug, cycle_id, checks)
    checks.append(_doctor_item(
        "PASS", "tasks_schema_compatible",
        f"任务结构兼容（{len(normalized.get('tasks', []))} 条）",
        ref="tasks.json",
    ))

    scope = validate_scope(project, require_confirmation=bool(cycle_id))
    if cycle_id and not scope["valid"]:
        checks.append(_doctor_item(
            "FAIL", "active_scope_invalid",
            f"当前周期配置不完整：{'；'.join(row['message'] for row in scope['errors'])}",
            ref="geo.json", next_action="补齐范围并由 Project Owner 重新确认",
        ))
    elif not cycle_id and not scope["valid"]:
        checks.append(_doctor_item(
            "WARN", "cycle_not_ready",
            f"尚未达到新周期锁定门槛（{len(scope['errors'])} 项待补）",
            ref="geo.json", next_action="开始新周期前补齐客户画像、角色、竞品和问题库",
        ))
    else:
        checks.append(_doctor_item(
            "PASS", "project_config_complete",
            "配置完整，范围确认与当前项目一致" if cycle_id else "项目配置可用于锁定新周期",
            ref="geo.json",
        ))

    baseline_ok = not cycle_id
    if cycle_id:
        expected = f"delivery/snapshots/{cycle_id}/baseline.json"
        baseline_ref = str(delivery_cfg.get("current_baseline_ref") or "")
        baseline_path = project_dir / baseline_ref if baseline_ref else project_dir / expected
        if baseline_ref != expected:
            checks.append(_doctor_item(
                "FAIL", "baseline_ref_mismatch",
                f"当前 baseline 引用应为 {expected}", ref=baseline_ref or "geo.json",
            ))
        elif not baseline_path.is_file():
            checks.append(_doctor_item(
                "FAIL", "baseline_missing", "当前周期 baseline.json 不存在",
                ref=baseline_ref, next_action="恢复基线快照；不得用新快照覆盖历史基线",
            ))
        else:
            try:
                baseline = _strict_read_json(baseline_path, "baseline.json")
                baseline_ok = (
                    baseline.get("cycle_id") == cycle_id
                    and (baseline.get("scope") or {}).get("scope_sha256")
                    == (delivery_cfg.get("scope_confirmation") or {}).get("scope_sha256")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                baseline_ok = False
            checks.append(_doctor_item(
                "PASS" if baseline_ok else "FAIL",
                "baseline_consistent" if baseline_ok else "baseline_invalid",
                "baseline 文件存在且 cycle/scope 一致" if baseline_ok else "baseline 无法读取或 cycle/scope 不一致",
                ref=baseline_ref,
            ))

    task_rows = normalized.get("tasks", [])
    current_tasks = [
        task for task in task_rows
        if not cycle_id or (task.get("delivery") or {}).get("cycle_id") == cycle_id
    ]
    status_failures = 0
    for task in current_tasks:
        task_id = str(task.get("id") or "")
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        stored_stage = delivery.get("stage")
        computed_stage = compute_stage(task)
        if stored_stage != computed_stage:
            status_failures += 1
            checks.append(_doctor_item(
                "FAIL", "stage_state_conflict",
                f"{task_id} 存储阶段 {stored_stage}，按证据应为 {computed_stage}",
                task_id=task_id, ref="tasks.json",
                next_action="运行对应资产/部署复查或 delivery-sync，不能手工改 stage",
            ))
        verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
        required = [
            row for row in verification.get("checks", [])
            if isinstance(row, dict) and row.get("required", True)
        ]
        hard_failed = [
            row.get("id") for row in required
            if row.get("raw_verdict", row.get("verdict")) in {"fail", "error"}
        ]
        if verification.get("can_close") is True and hard_failed:
            status_failures += 1
            checks.append(_doctor_item(
                "FAIL", "required_check_bypassed",
                f"{task_id} 的 required 失败项被错误标记为可关闭：{', '.join(str(x) for x in hard_failed)}",
                task_id=task_id, ref="tasks.json",
            ))
        if task.get("status") == "done" and (
            stored_stage != "verified"
            or verification.get("can_close") is not True
            or not required
            or hard_failed
            or not baseline_ok
        ):
            status_failures += 1
            checks.append(_doctor_item(
                "FAIL", "done_without_final_verification",
                f"{task_id} 已 done 但没有完整 required 验收和关闭证据链",
                task_id=task_id, ref="tasks.json",
                next_action="重开工单并运行自动验收；自动失败不能由人工覆盖",
            ))
        if stored_stage == "verified" and task.get("status") != "done":
            status_failures += 1
            checks.append(_doctor_item(
                "FAIL", "verified_task_not_closed",
                f"{task_id} 已 verified 但主状态不是 done",
                task_id=task_id, ref="tasks.json",
            ))
        if task.get("status") == "blocked" and not str(
            delivery.get("blocker") or (delivery.get("assignment") or {}).get("blocker") or ""
        ).strip():
            status_failures += 1
            checks.append(_doctor_item(
                "FAIL", "blocked_without_reason", f"{task_id} blocked 但没有阻塞原因",
                task_id=task_id, ref="tasks.json",
            ))
    if not status_failures:
        checks.append(_doctor_item(
            "PASS", "task_status_consistent", "阶段、主状态和 required 关闭门槛一致",
            ref="tasks.json",
        ))

    if cycle_id:
        event_path = project_dir / "delivery" / "events" / f"{cycle_id}.jsonl"
        event_log = read_cycle_event_log(project_dir, str(cycle_id))
        events = event_log["events"]
        invalid_events = [
            row for row in events
            if any(key not in row for key in (
                "event_id", "cycle_id", "task_id", "event_type", "actor_role", "at", "payload",
            ))
            or row.get("event_type") not in EVENT_TYPES
            or not isinstance(row.get("payload"), dict)
        ]
        event_ids = [str(row.get("event_id") or "") for row in events]
        duplicate_ids = len(event_ids) - len(set(event_ids))
        if not event_path.is_file() or not events:
            checks.append(_doctor_item(
                "FAIL", "event_log_missing", "当前周期事件日志不存在或没有合法事件",
                ref=event_log["event_log_ref"],
            ))
        elif invalid_events or duplicate_ids:
            checks.append(_doctor_item(
                "FAIL", "event_log_invalid",
                f"事件结构错误 {len(invalid_events)} 条，重复 event_id {duplicate_ids} 条",
                ref=event_log["event_log_ref"],
            ))
        else:
            checks.append(_doctor_item(
                "PASS", "event_log_readable", f"事件日志可读取（{len(events)} 条合法事件）",
                ref=event_log["event_log_ref"],
            ))
        if event_log["warnings"]:
            checks.append(_doctor_item(
                "WARN", "event_log_damaged_lines",
                f"已跳过 {len(event_log['warnings'])} 条损坏或跨周期 JSONL 行，其他事件仍可读取",
                ref=event_log["event_log_ref"],
                next_action="从备份或原始对象补回损坏事件，不要删除整个日志",
            ))

    discovered = _discover_assets(project_dir)
    bound_paths = set()
    asset_failures = 0
    approval_failures = 0
    path_failures = 0
    deployment_failures = 0
    for task in current_tasks:
        task_id = str(task.get("id") or "")
        delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
        assets = [row for row in delivery.get("assets", []) if isinstance(row, dict)]
        current_by_id = {str(row.get("id") or ""): row for row in assets if row.get("id")}
        for asset in assets:
            try:
                rel = _normalize_asset_path(asset.get("path"))
            except ValueError as exc:
                path_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "unsafe_asset_path", f"{task_id} 资产路径不安全：{exc}",
                    task_id=task_id, ref=str(asset.get("path") or ""),
                ))
                continue
            if not rel:
                continue
            bound_paths.add(rel)
            actual = project_dir / "assets" / rel
            try:
                actual.resolve().relative_to((project_dir / "assets").resolve())
            except ValueError:
                path_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "unsafe_asset_symlink",
                    f"{task_id} 的资产文件解析后逃逸 assets/：assets/{rel}",
                    task_id=task_id, ref=f"assets/{rel}",
                ))
                continue
            if asset.get("required", True) and (not actual.is_file() or actual.stat().st_size <= 0):
                asset_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "required_asset_missing",
                    f"{task_id} 的必需资产缺失或为空：assets/{rel}",
                    task_id=task_id, ref=f"assets/{rel}", next_action="恢复资产后重新扫描、审批和部署",
                ))
            elif actual.is_file() and asset.get("sha256"):
                actual_sha, actual_size = _file_sha256(actual)
                if actual_sha != asset.get("sha256") or actual_size != asset.get("size"):
                    asset_failures += 1
                    checks.append(_doctor_item(
                        "FAIL", "asset_version_drift",
                        f"{task_id} 的资产内容与已审批版本不一致：assets/{rel}",
                        task_id=task_id, ref=f"assets/{rel}", next_action="重新扫描并重新审批当前版本",
                    ))
            recomputed_status, _ = _approval_state(asset, delivery.get("approvals", []))
            if asset.get("approval_status") != recomputed_status:
                approval_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "invalid_asset_approval",
                    f"{task_id} 资产 {asset.get('id')} 的审批聚合应为 {recomputed_status}，当前为 {asset.get('approval_status')}",
                    task_id=task_id, ref="tasks.json",
                ))
        for approval in delivery.get("approvals", []) if isinstance(delivery.get("approvals"), list) else []:
            if not isinstance(approval, dict):
                approval_failures += 1
                continue
            kind = str(approval.get("type") or "")
            valid = (
                approval.get("status") in ASSET_DECISION_STATUSES
                and bool(str(approval.get("note") or "").strip())
            )
            if kind == "asset":
                target_id = str(approval.get("target") or "").removeprefix("asset:")
                valid = valid and bool(target_id) and target_id in current_by_id
            elif kind == "deployment":
                valid = valid and approval.get("role") == "web_owner"
            elif kind == "final":
                valid = valid and approval.get("role") == "reviewer"
            else:
                valid = False
            if not valid:
                approval_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "invalid_approval_record",
                    f"{task_id} 存在无效 {kind or 'unknown'} 审批记录",
                    task_id=task_id, ref="tasks.json",
                ))
        for deployment in delivery.get("deployments", []) if isinstance(delivery.get("deployments"), list) else []:
            if not isinstance(deployment, dict):
                deployment_failures += 1
                continue
            target_url = str(deployment.get("target_url") or "").strip()
            if deployment.get("human_confirmed") is True and not target_url:
                deployment_failures += 1
                checks.append(_doctor_item(
                    "FAIL", "deployment_url_missing",
                    f"{task_id} 的人工部署确认缺少 target_url",
                    task_id=task_id, ref="tasks.json",
                ))
            elif target_url:
                try:
                    _validate_public_url(target_url)
                except ValueError as exc:
                    path_failures += 1
                    checks.append(_doctor_item(
                        "FAIL", "unsafe_deployment_url", f"{task_id} 部署 URL 无效：{exc}",
                        task_id=task_id, ref=target_url,
                    ))
            for field in ("evidence_ref", "last_snapshot_ref"):
                value = deployment.get(field)
                if not value:
                    continue
                safe, reason = _doctor_local_ref(project_dir, value)
                if not safe:
                    path_failures += 1
                    checks.append(_doctor_item(
                        "FAIL", "unsafe_project_ref",
                        f"{task_id} 的 {field} 不安全：{reason}",
                        task_id=task_id, ref=str(value),
                    ))
    if not asset_failures:
        checks.append(_doctor_item("PASS", "bound_assets_present", "已绑定资产与磁盘版本一致"))
    if not approval_failures:
        checks.append(_doctor_item("PASS", "approvals_valid", "当前资产、部署和最终审批记录有效"))
    if not deployment_failures:
        checks.append(_doctor_item("PASS", "deployment_records_complete", "部署记录均包含必要 URL 和人工确认字段"))
    if not path_failures:
        checks.append(_doctor_item("PASS", "paths_safe", "基线、资产、部署和证据引用未逃逸项目目录"))

    orphaned = sorted(set(discovered) - bound_paths)
    if orphaned:
        checks.append(_doctor_item(
            "WARN", "orphan_assets", f"{len(orphaned)} 个资产未绑定当前周期工单",
            ref="assets/", next_action="确认用途后绑定工单；无关文件不要进入客户交付包",
        ))
    else:
        checks.append(_doctor_item("PASS", "no_orphan_assets", "没有未绑定的交付资产", ref="assets/"))

    if cycle_id:
        manifest_ref = f"delivery/manifests/{cycle_id}.json"
        manifest_path = project_dir / manifest_ref
        if not manifest_path.is_file():
            checks.append(_doctor_item(
                "WARN", "manifest_missing", "当前周期 Manifest 尚未生成",
                ref=manifest_ref, next_action="运行 verify 或生成交付追踪报告即可重建",
            ))
        else:
            try:
                manifest = _strict_read_json(manifest_path, "cycle manifest")
                manifest_ok = manifest.get("cycle_id") == cycle_id
            except (OSError, ValueError, json.JSONDecodeError):
                manifest_ok = False
            checks.append(_doctor_item(
                "PASS" if manifest_ok else "FAIL",
                "manifest_readable" if manifest_ok else "manifest_invalid",
                "当前周期 Manifest 可读取并可由当前对象重建" if manifest_ok else "Manifest 无法读取或 cycle 不一致",
                ref=manifest_ref,
            ))

    report_files = _doctor_report_files(project_dir)
    report_findings = []
    report_text = []
    for path in report_files:
        try:
            path.resolve().relative_to(project_dir.resolve())
        except ValueError:
            report_findings.append((path.name, "unsafe_path"))
            continue
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(project_dir).as_posix()
        report_text.append((relative, text))
        for label, pattern in _DOCTOR_SECRET_PATTERNS:
            if pattern.search(text):
                report_findings.append((relative, label))
    factcheck_path = project_dir / "factcheck.json"
    factcheck = {}
    if factcheck_path.is_file():
        try:
            factcheck = json.loads(factcheck_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            factcheck = {}
    rejected_claims = [
        claim for claim in _factcheck_claims(factcheck)
        if claim.get("status") in {"rejected", "forbidden"}
    ]
    rejected_hits = [
        (relative, claim.get("id") or "unapproved_claim")
        for relative, text in report_text
        for claim in rejected_claims
        if claim.get("original", "").casefold() in text.casefold()
    ]
    if report_findings or rejected_hits:
        detail = ", ".join(
            f"{ref}:{kind}" for ref, kind in (report_findings + rejected_hits)[:8]
        )
        checks.append(_doctor_item(
            "FAIL", "report_sensitive_content",
            f"报告发现 {len(report_findings)} 项敏感信息、{len(rejected_hits)} 条未批准事实：{detail}",
            ref="deliverables/", next_action="重新生成脱敏报告并人工复核公开包",
        ))
    else:
        checks.append(_doctor_item(
            "PASS", "reports_redacted",
            f"报告未发现密钥、邮箱、绝对路径或未批准事实（检查 {len(report_files)} 个文件）",
            ref="deliverables/",
        ))

    return _doctor_finish(slug, cycle_id, checks)


def delivery_doctor(slug: str) -> dict:
    """从本地项目读取当前真相；检查失败也返回结构化报告，不修改文件。"""
    import geolib as G

    project_dir = G.project_dir(slug)
    checks = []
    try:
        cfg = G.load_config(slug)
    except (OSError, ValueError, json.JSONDecodeError, SystemExit) as exc:
        checks.append(_doctor_item(
            "FAIL", "config_unreadable", f"geo.json 无法读取：{type(exc).__name__}: {exc}",
            ref="geo.json",
        ))
        return _doctor_finish(slug, None, checks)
    tasks_path = project_dir / "tasks.json"
    if not tasks_path.is_file():
        data = {"tasks": []}
        checks.append(_doctor_item(
            "WARN", "tasks_missing", "项目还没有 tasks.json",
            ref="tasks.json", next_action="运行 plan 生成工单",
        ))
    else:
        try:
            data = json.loads(tasks_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            checks.append(_doctor_item(
                "FAIL", "tasks_unreadable", f"tasks.json 无法读取：{type(exc).__name__}",
                ref="tasks.json", next_action="从 .geo.bak 恢复最近一份有效备份",
            ))
            return _doctor_finish(
                slug, normalize_config(cfg.get("delivery")).get("current_cycle_id"), checks,
            )
    report = delivery_doctor_data(cfg, data, project_dir, slug=slug)
    report["checks"] = checks + report["checks"]
    return _doctor_finish(slug, report.get("cycle_id"), report["checks"])


def format_doctor_report(report: dict) -> str:
    """生成人可读、适合发布前粘贴到验证日志的 doctor 输出。"""
    lines = []
    for row in report.get("checks", []):
        suffix = f" [{row.get('task_id')}]" if row.get("task_id") else ""
        lines.append(f"{str(row.get('level') or 'FAIL'):<5} {row.get('message')}{suffix}")
        if row.get("next_action") and row.get("level") != "PASS":
            lines.append(f"      下一步：{row['next_action']}")
    summary = report.get("summary") or {}
    lines.append(
        "SUMMARY "
        f"PASS {summary.get('PASS', 0)} / WARN {summary.get('WARN', 0)} / FAIL {summary.get('FAIL', 0)}"
    )
    return "\n".join(lines)
