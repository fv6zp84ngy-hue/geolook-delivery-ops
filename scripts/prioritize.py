"""Explainable business priority and bounded cycle-scope recommendations.

The module is deliberately deterministic and standard-library only.  It writes
recommendations to existing task objects but never creates evidence, changes a scope
decision, or approves/rejects a task.
"""

from __future__ import annotations

import copy
import re
from typing import Any


SCHEMA_VERSION = "1.0"
BUYING_STAGES = (
    "problem_aware",
    "solution_exploration",
    "category_search",
    "comparison",
    "validation",
    "purchase",
    "post_purchase",
)
_STAGE_VALUE = {
    "problem_aware": 0,
    "solution_exploration": 1,
    "category_search": 2,
    "comparison": 3,
    "validation": 3,
    "purchase": 3,
    "post_purchase": 1,
}
_EFFORT_PENALTY = {"S": 0, "M": 1, "L": 3}
_EFFORT_POINTS = {"S": 1, "M": 3, "L": 5}


def _score(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(0, min(3, number))


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _delivery_config(cfg: dict | None) -> dict:
    return cfg.get("delivery") if isinstance(cfg, dict) and isinstance(cfg.get("delivery"), dict) else {}


def _profile(cfg: dict | None) -> dict:
    delivery = _delivery_config(cfg)
    return delivery.get("customer_profile") if isinstance(delivery.get("customer_profile"), dict) else {}


def _business_lists(cfg: dict | None) -> dict:
    delivery = _delivery_config(cfg)
    profile = _profile(cfg)
    product_lines = _strings(delivery.get("product_lines")) or _strings(profile.get("product_lines"))
    target_markets = _strings(delivery.get("target_markets")) or _strings(profile.get("target_markets"))
    conversion_goals = _strings(delivery.get("conversion_goals"))
    if not conversion_goals:
        goal = str(delivery.get("conversion_goal") or profile.get("conversion_goal") or "").strip()
        conversion_goals = [goal] if goal else []
    return {
        "product_lines": product_lines,
        "target_markets": target_markets,
        "conversion_goals": conversion_goals,
    }


def _first_buyer_role(cfg: dict | None) -> str:
    delivery = _delivery_config(cfg)
    icps = delivery.get("icps")
    if not isinstance(icps, list):
        icps = []
    for row in icps:
        if isinstance(row, dict):
            roles = _strings(row.get("buyer_roles"))
            role = str(row.get("buyer_role") or (roles[0] if roles else "")).strip()
            if role:
                return role
    icp = str(_profile(cfg).get("icp") or "").strip()
    return icp


def infer_buying_stage(value: Any) -> str:
    text = str(value or "").lower()
    rules = (
        ("post_purchase", ("help center", "support", "manual", "install", "repair", "maintenance", "warranty")),
        ("comparison", (" vs ", "compare", "comparison", "alternative", "competitor")),
        ("purchase", ("pricing", "price", "trial", "demo", "buy", "purchase", "dealer", "contact sales")),
        ("validation", ("security", "compliance", "privacy", "benchmark", "customer", "case study", "proof", "certif")),
        ("category_search", ("category", "definition", "what is", "entity", "disambiguation")),
        ("solution_exploration", ("integration", "capability", "solution", "how to", "use case", "compatible")),
    )
    for stage, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return stage
    return "problem_aware"


def apply_question_tags(question: dict | None, cfg: dict | None) -> dict:
    out = copy.deepcopy(question) if isinstance(question, dict) else {}
    business = _business_lists(cfg)
    text = " ".join(str(out.get(key) or "") for key in ("text", "question", "group", "type", "intent"))
    product_line = str(out.get("product_line") or "").strip()
    if not product_line and len(business["product_lines"]) == 1:
        product_line = business["product_lines"][0]
    out["product_line"] = product_line
    default_market = business["target_markets"][0] if len(business["target_markets"]) == 1 else ""
    out["market"] = str(out.get("market") or default_market or (cfg or {}).get("market") or "global").strip()
    out["buyer_role"] = str(out.get("buyer_role") or _first_buyer_role(cfg)).strip()
    stage = str(out.get("buying_stage") or "").strip()
    out["buying_stage"] = stage if stage in BUYING_STAGES else infer_buying_stage(text)
    goal = str(out.get("conversion_goal") or "").strip()
    out["conversion_goal"] = goal or (business["conversion_goals"][0] if business["conversion_goals"] else "")
    return out


def apply_question_tags_to_config(cfg: dict | None) -> dict:
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    rows = out.get("questions")
    if isinstance(rows, list):
        out["questions"] = [apply_question_tags(row, out) for row in rows if isinstance(row, dict)]
    return out


def apply_task_tags(task: dict | None, cfg: dict | None) -> dict:
    out = copy.deepcopy(task) if isinstance(task, dict) else {}
    business = _business_lists(cfg)
    delivery = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    out["delivery"] = delivery
    text = " ".join(str(out.get(key) or "") for key in ("title", "why", "action", "package"))
    product_line = str(out.get("product_line") or delivery.get("product_line") or "").strip()
    if not product_line and len(business["product_lines"]) == 1:
        product_line = business["product_lines"][0]
    default_market = business["target_markets"][0] if len(business["target_markets"]) == 1 else ""
    market = str(
        out.get("market") or delivery.get("market") or default_market
        or (cfg or {}).get("market") or "global"
    ).strip()
    buyer_role = str(out.get("buyer_role") or delivery.get("buyer_role") or _first_buyer_role(cfg)).strip()
    legacy_stage = str(delivery.get("funnel_stage") or "").strip()
    stage = str(out.get("buying_stage") or delivery.get("buying_stage") or legacy_stage).strip()
    if stage not in BUYING_STAGES:
        stage = infer_buying_stage(text)
    goal = str(out.get("conversion_goal") or delivery.get("conversion_goal") or "").strip()
    if not goal and business["conversion_goals"]:
        goal = business["conversion_goals"][0]
    for target in (out, delivery):
        target["product_line"] = product_line
        target["market"] = market
        target["buyer_role"] = buyer_role
        target["buying_stage"] = stage
        target["conversion_goal"] = goal
    delivery.pop("funnel_stage", None)
    return out


def _matches_priority(task: dict, row: dict) -> bool:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    actual = {
        "task_ids": task.get("id"),
        "product_lines": task.get("product_line") or delivery.get("product_line"),
        "markets": task.get("market") or delivery.get("market"),
        "buyer_roles": task.get("buyer_role") or delivery.get("buyer_role"),
        "buying_stages": task.get("buying_stage") or delivery.get("buying_stage"),
        "conversion_goals": task.get("conversion_goal") or delivery.get("conversion_goal"),
        "packages": task.get("package"),
        "case_types": delivery.get("case_type"),
    }
    used = False
    for key, value in actual.items():
        expected = row.get(key)
        if expected in (None, "", []):
            singular = key[:-1] if key.endswith("s") else key
            expected = row.get(singular)
        if expected in (None, "", []):
            continue
        used = True
        expected_values = expected if isinstance(expected, list) else [expected]
        if value not in expected_values and "both" not in expected_values and "all" not in expected_values:
            return False
    return used


def _business_value(task: dict, cfg: dict | None) -> tuple[int, list[dict]]:
    delivery = _delivery_config(cfg)
    rows = delivery.get("strategic_priorities")
    if not isinstance(rows, list) or not rows:
        rows = ((delivery.get("planning") or {}).get("business_priorities") or [])
    matches = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if isinstance(row, str):
            row = {"id": f"priority-{index + 1}", "name": row, "score": 2}
        if not isinstance(row, dict) or not _matches_priority(task, row):
            continue
        matches.append({
            "id": str(row.get("id") or f"priority-{index + 1}"),
            "name": str(row.get("name") or row.get("reason") or "业务优先项"),
            "score": _score(row.get("score", row.get("weight", 2)), 2),
            "reason": str(row.get("reason") or "命中项目战略优先项"),
        })
    if not matches:
        return 1, []
    return max(row["score"] for row in matches), matches


def _visibility_gap(task: dict) -> tuple[int, str]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    explicit = (delivery.get("priority_context") or {}).get("visibility_gap")
    if explicit is not None:
        return _score(explicit), "使用工单显式可见性缺口"
    case_type = str(delivery.get("case_type") or "")
    if task.get("priority") == "P0" and case_type == "technical":
        return 3, "P0 技术门票阻断抓取或机器理解"
    values = {
        "technical": (3, "技术问题影响抓取或结构化理解"),
        "fact_error": (3, "事实错误会直接污染品牌理解"),
        "outcome_metric_gap": (3, "结果指标已有明确缺口"),
        "result_metric_gap": (3, "结果指标已有明确缺口"),
        "content_gap": (2, "内容覆盖存在可定位缺口"),
        "external_evidence_gap": (2, "外部可引用证据不足"),
    }
    return values.get(case_type, (1, "尚未形成强可见性缺口信号"))


def _evidence_confidence(task: dict) -> tuple[int, str]:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    confirmed = [row for row in refs if isinstance(row, dict) and row.get("review_status") == "confirmed"]
    levels = {"low": 1, "medium": 2, "high": 3}
    if confirmed:
        value = max(levels.get(str(row.get("confidence") or "low"), 1) for row in confirmed)
        return value, f"{len(confirmed)}/{len(refs)} 条证据已人工确认"
    if refs:
        return 1, f"已有 {len(refs)} 条候选证据，但尚未人工确认"
    return 0, "尚未绑定诊断证据"


def _feasibility(task: dict, cfg: dict | None) -> tuple[int, str]:
    if task.get("status") == "blocked" or str((task.get("delivery") or {}).get("blocker") or "").strip():
        return 0, "工单当前处于阻塞状态"
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    owner = str(task.get("owner") or assignment.get("owner_role") or "").strip()
    action = str(task.get("action") or "").strip()
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    has_acceptance = bool(acceptance.get("check") or acceptance.get("checks") or acceptance.get("desc"))
    complete = sum(bool(value) for value in (owner, action, has_acceptance))
    resources = ((_delivery_config(cfg).get("planning") or {}).get("team_resources") or {})
    available = set(_strings(resources.get("available_owners")))
    if complete == 3 and (not available or owner in available):
        return 3, "负责人、动作和验收条件完整，且资源角色可用"
    if complete == 3:
        return 2, "执行规格完整，但负责人不在本周期可用角色中"
    if complete >= 2:
        return 1, "执行规格仍有缺失字段"
    return 0, "缺少负责人、动作或验收条件"


def explain_score(priority: dict) -> list[str]:
    if isinstance(priority.get("explanation"), list) and priority["explanation"]:
        return copy.deepcopy(priority["explanation"])
    return [
        f"业务重要性 {priority.get('business_value', 0)}/3",
        f"购买阶段价值 {priority.get('buyer_intent', 0)}/3",
        f"GEO 可见性缺口 {priority.get('visibility_gap', 0)}/3",
        f"证据可信度 {priority.get('evidence_confidence', 0)}/3",
        f"实施可行性 {priority.get('feasibility', 0)}/3",
        f"执行成本扣分 -{priority.get('effort_penalty', 0)}",
    ]


def score_task(task: dict | None, cfg: dict | None) -> dict:
    tagged = apply_task_tags(task, cfg)
    delivery = tagged.get("delivery") if isinstance(tagged.get("delivery"), dict) else {}
    business_value, matches = _business_value(tagged, cfg)
    stage = str(tagged.get("buying_stage") or delivery.get("buying_stage") or "problem_aware")
    buyer_intent = _STAGE_VALUE.get(stage, 0)
    visibility_gap, visibility_reason = _visibility_gap(tagged)
    evidence_confidence, evidence_reason = _evidence_confidence(tagged)
    feasibility, feasibility_reason = _feasibility(tagged, cfg)
    effort = str(tagged.get("effort") or "").upper()
    effort_penalty = _EFFORT_PENALTY.get(effort, 2)
    total = business_value + buyer_intent + visibility_gap + evidence_confidence + feasibility - effort_penalty
    p0 = tagged.get("priority") == "P0"
    recommended_priority = "P0" if p0 else ("P1" if total >= 9 else "P2" if total >= 5 else "P3")
    explanation = [
        f"业务重要性 {business_value}/3：" + (
            "；".join(row["reason"] for row in matches) if matches else "未命中显式战略优先项，使用保守默认值"
        ),
        f"购买阶段价值 {buyer_intent}/3：{stage}",
        f"GEO 可见性缺口 {visibility_gap}/3：{visibility_reason}",
        f"证据可信度 {evidence_confidence}/3：{evidence_reason}",
        f"实施可行性 {feasibility}/3：{feasibility_reason}",
        f"执行成本扣分 -{effort_penalty}：工作量 {effort or '未设置'}",
    ]
    if p0:
        explanation.insert(0, "P0 容量规则：优先占用本周期容量，但仍需证据和 Project Owner 批准")
    return {
        "schema_version": SCHEMA_VERSION,
        "score": total,
        "business_value": business_value,
        "buyer_intent": buyer_intent,
        "visibility_gap": visibility_gap,
        "evidence_confidence": evidence_confidence,
        "feasibility": feasibility,
        "effort_penalty": effort_penalty,
        "recommended_priority": recommended_priority,
        "p0_capacity_reserved": p0,
        "matched_strategic_priority_ids": [row["id"] for row in matches],
        "explanation": explanation,
        "advisory_only": True,
    }


def rank_tasks(tasks: list[dict] | None, cfg: dict | None) -> list[dict]:
    ranked = []
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        tagged = apply_task_tags(task, cfg)
        priority = score_task(tagged, cfg)
        tagged["delivery"]["priority"] = priority
        ranked.append(tagged)
    ranked.sort(key=lambda task: (
        not bool((task.get("delivery") or {}).get("priority", {}).get("p0_capacity_reserved")),
        -int((task.get("delivery") or {}).get("priority", {}).get("score", 0)),
        int((task.get("delivery") or {}).get("priority", {}).get("effort_penalty", 2)),
        str(task.get("id") or ""),
    ))
    for index, task in enumerate(ranked, 1):
        task["delivery"]["priority"]["rank"] = index
    return ranked


def apply_priority_data(data: dict | None, cfg: dict | None) -> dict:
    """Attach tags and priority details while preserving the existing task order."""
    out = copy.deepcopy(data) if isinstance(data, dict) else {"tasks": []}
    original = [row for row in out.get("tasks", []) if isinstance(row, dict)]
    ranked = rank_tasks(original, cfg)
    by_id = {str(row.get("id") or ""): row for row in ranked}
    out["tasks"] = [by_id.get(str(row.get("id") or ""), row) for row in original]
    return out


def _diagnosis_ready(task: dict) -> bool:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    diagnosis = delivery.get("diagnosis") if isinstance(delivery.get("diagnosis"), dict) else {}
    if diagnosis.get("ready_for_scope") is True:
        return True
    return any(
        isinstance(row, dict) and row.get("review_status") == "confirmed"
        for row in delivery.get("source_refs", []) if isinstance(delivery.get("source_refs"), list)
    )


def _capacity(cfg: dict | None) -> dict:
    delivery = _delivery_config(cfg)
    policy = delivery.get("policy") if isinstance(delivery.get("policy"), dict) else {}
    max_tasks = policy.get("max_cycle_tasks", policy.get("max_scoped_tasks", 12))
    max_large = policy.get("max_large_tasks", 2)
    max_tasks = max_tasks if isinstance(max_tasks, int) and max_tasks > 0 else 12
    max_large = max_large if isinstance(max_large, int) and max_large >= 0 else 2
    resources = ((delivery.get("planning") or {}).get("team_resources") or {})
    points = resources.get("capacity_points")
    points = points if isinstance(points, int) and points >= 0 else None
    owner_points = resources.get("owner_capacity_points")
    owner_points = owner_points if isinstance(owner_points, dict) else {}
    return {
        "max_tasks": min(max_tasks, 12),
        "max_large_tasks": max_large,
        "capacity_points": points,
        "available_owners": _strings(resources.get("available_owners")),
        "owner_capacity_points": {
            str(key): value for key, value in owner_points.items()
            if isinstance(value, int) and value >= 0
        },
    }


def recommend_cycle_scope(data: dict | None, cfg: dict | None, generated_at: str = "") -> dict:
    """Rank and recommend a bounded scope without making any scope decision."""
    out = copy.deepcopy(data) if isinstance(data, dict) else {"tasks": []}
    original = [row for row in out.get("tasks", []) if isinstance(row, dict)]
    ranked = rank_tasks(original, cfg)
    capacity = _capacity(cfg)
    selected: list[str] = []
    next_cycle: list[str] = []
    warnings: list[str] = []
    capacity_excluded: list[str] = []
    used_points = 0
    used_large = 0
    used_by_owner: dict[str, int] = {}
    available = set(capacity["available_owners"])

    for task in ranked:
        delivery = task["delivery"]
        priority = delivery["priority"]
        task_id = str(task.get("id") or "")
        effort = str(task.get("effort") or "").upper()
        points = _EFFORT_POINTS.get(effort, 3)
        owner = str(task.get("owner") or "").strip()
        reason = ""
        recommendation = "recommended"
        if not _diagnosis_ready(task):
            recommendation = "needs_evidence"
            reason = "尚无 confirmed 证据，不能进入执行范围"
        elif len(selected) >= capacity["max_tasks"]:
            recommendation = "next_cycle"
            reason = f"超过本周期最大工单数 {capacity['max_tasks']}"
        elif effort == "L" and used_large >= capacity["max_large_tasks"]:
            recommendation = "next_cycle"
            reason = f"超过本周期最大 L 级任务数 {capacity['max_large_tasks']}"
        elif available and owner not in available:
            recommendation = "next_cycle"
            reason = f"本周期可用角色不包含 {owner or '未设置负责人'}"
        elif capacity["capacity_points"] is not None and used_points + points > capacity["capacity_points"]:
            recommendation = "next_cycle"
            reason = "超过团队总资源点数"
        elif owner in capacity["owner_capacity_points"] and (
            used_by_owner.get(owner, 0) + points > capacity["owner_capacity_points"][owner]
        ):
            recommendation = "next_cycle"
            reason = f"超过 {owner} 的资源点数"

        if recommendation == "recommended":
            selected.append(task_id)
            used_points += points
            used_large += effort == "L"
            used_by_owner[owner] = used_by_owner.get(owner, 0) + points
            reason = "建议纳入本周期，等待 Project Owner 决策"
        elif recommendation == "next_cycle":
            next_cycle.append(task_id)
            capacity_excluded.append(task_id)
            if priority["p0_capacity_reserved"]:
                warnings.append(f"{task_id} 是 P0，但{reason}；Project Owner 必须记录调整理由")

        priority["recommended_for_cycle"] = recommendation == "recommended"
        priority["recommendation"] = recommendation
        priority["capacity_reason"] = reason
        priority["generated_at"] = generated_at
        delivery["scope_recommendation"] = {
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
            },
            "reasons": copy.deepcopy(priority["explanation"]),
            "rank": priority["rank"],
            "recommendation": recommendation,
            "recommended_for_cycle": recommendation == "recommended",
            "suggested_status": {
                "recommended": "approved", "next_cycle": "deferred",
                "needs_evidence": "needs_evidence",
            }[recommendation],
            "suggested_reason": reason,
            "capacity_reason": reason if recommendation != "recommended" else "",
            "resource_fit": recommendation == "recommended",
            "effort_points": points,
            "product_line": task.get("product_line") or "",
            "target_markets": copy.deepcopy(_business_lists(cfg)["target_markets"]),
            "generated_at": generated_at,
            "advisory_only": True,
        }

    by_id = {str(row.get("id") or ""): row for row in ranked}
    out["tasks"] = [by_id.get(str(row.get("id") or ""), row) for row in original]
    p0_ids = [
        str(row.get("id") or "") for row in ranked
        if (row.get("delivery") or {}).get("priority", {}).get("p0_capacity_reserved")
    ]
    if capacity_excluded:
        warnings.append(
            f"{len(capacity_excluded)} 条候选项超出本周期容量，已建议保留下周期："
            + "、".join(capacity_excluded)
        )
    out["scope_recommendation"] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "cycle_id": _delivery_config(cfg).get("current_cycle_id"),
        "max_tasks": capacity["max_tasks"],
        "max_large_tasks": capacity["max_large_tasks"],
        "selected_task_ids": selected,
        "selected_count": len(selected),
        "selected_large_count": used_large,
        "next_cycle_task_ids": next_cycle,
        "next_cycle_count": len(next_cycle),
        "capacity_excluded_task_ids": capacity_excluded,
        "p0_task_ids": p0_ids,
        "p0_selected_count": len([task_id for task_id in p0_ids if task_id in selected]),
        "capacity_points": capacity["capacity_points"],
        "used_capacity_points": used_points,
        "available_owners": capacity["available_owners"],
        "owner_capacity_points": capacity["owner_capacity_points"],
        "target_markets": copy.deepcopy(_business_lists(cfg)["target_markets"]),
        "product_lines": copy.deepcopy(_business_lists(cfg)["product_lines"]),
        "conversion_goals": copy.deepcopy(_business_lists(cfg)["conversion_goals"]),
        "capacity_warnings": warnings,
        "over_capacity": bool(warnings),
        "bounded": len(selected) <= capacity["max_tasks"] and used_large <= capacity["max_large_tasks"],
        "advisory_only": True,
        "note": "评分与范围均为决策辅助；Project Owner 必须逐条决定并说明人工调整",
    }
    return out


def scope_report_fragment(data: dict | None) -> list[str]:
    data = data if isinstance(data, dict) else {}
    summary = data.get("scope_recommendation") if isinstance(data.get("scope_recommendation"), dict) else {}
    if not summary:
        return ["范围推荐尚未运行；Project Owner 仍需逐条决定。"]
    lines = [
        f"- 推荐进入本周期：**{summary.get('selected_count', 0)} / {summary.get('max_tasks', 12)}**",
        f"- L 级任务：**{summary.get('selected_large_count', 0)} / {summary.get('max_large_tasks', 2)}**",
        f"- P0 已占容量：**{summary.get('p0_selected_count', 0)} / {len(summary.get('p0_task_ids') or [])}**",
        f"- 建议保留下周期：**{summary.get('next_cycle_count', 0)}**",
    ]
    for warning in summary.get("capacity_warnings") or []:
        lines.append(f"- 容量警告：{warning}")
    lines.append("- 说明：此处是规则建议，不是范围批准或商业事实结论。")
    return lines
