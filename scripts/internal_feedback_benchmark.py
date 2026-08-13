"""Deterministic internal-feedback benchmark preparation and analysis.

This module is deliberately independent from the dashboard.  It reads the
existing controlled-case ledger, normalizes it into a benchmark snapshot, and
derives truth from verification snapshots, delivery state and event evidence.
It does not edit a source case and does not call an LLM or MatrAIx.  The local
dummy runner exists only to validate the benchmark plumbing before a real
fixed-cohort persona runner is connected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "benchmark.case.v1"
TRIAL_SCHEMA_VERSION = "benchmark.trial.v1"
PERSONA_REQUEST_SCHEMA_VERSION = "benchmark.persona_request.v1"
SUMMARY_SCHEMA_VERSION = "benchmark.summary.v1"
# Formal GEO design has 32 planned persona-task pairs:
# 8 fixed synthetic personas × 4 deterministic tasks.
# Gate is pre-registered before formal external-model execution.
FORMAL_MIN_PAIRS = 30
MIN_PAIR_COVERAGE = 0.90
MIN_GROUND_TRUTH_COVERAGE = 1.0
CASE_FILE_NAMES = {
    "geo.json",
    "tasks.json",
    "audit.json",
    "diagnosis.json",
    "factcheck.json",
    "execution-provenance.json",
    "execution-summary.json",
}
ALLOWED_ROLES = {
    "project_owner", "geo_operator", "fact_approver", "content_owner",
    "web_owner", "reviewer",
}

# P1 keeps the cohort fixed and reviewable.  These are prompts, not ground
# truth: the verifier still owns all scored labels.  A real MatrAIx/Persona
# runner may use the same profile ids and replace the prompt text, but must
# preserve the cohort metadata in every trial.
PERSONA_PROFILES = {
    "founder": {
        "id": "founder",
        "label": "Founder / GM",
        "goal": "decide whether this is worth a focused buying conversation",
        "constraints": ["time constrained", "needs a clear business outcome", "skeptical of vague claims"],
        "instruction": "Ask whether the offer is clearly defined, credible, and worth a next conversation.",
    },
    "technical_buyer": {
        "id": "technical_buyer",
        "label": "Technical buyer",
        "goal": "validate implementation fit and operational risk",
        "constraints": ["needs concrete technical evidence", "checks integrations and deployment constraints"],
        "instruction": "Look for precise capabilities, dependencies, integration details, and evidence.",
    },
    "security_reviewer": {
        "id": "security_reviewer",
        "label": "Security reviewer",
        "goal": "identify security, privacy, and compliance gaps",
        "constraints": ["does not infer unverified certifications", "requires explicit data handling details"],
        "instruction": "Reject unsupported security or compliance claims and call out missing evidence.",
    },
    "developer": {
        "id": "developer",
        "label": "Developer",
        "goal": "determine whether the product can be used in a real build",
        "constraints": ["prefers examples and API-level clarity", "needs actionable next steps"],
        "instruction": "Check whether the information is specific enough to implement or test.",
    },
    "marketing_operator": {
        "id": "marketing_operator",
        "label": "Marketing operator",
        "goal": "understand positioning, audience, and conversion path",
        "constraints": ["needs a crisp category definition", "compares alternatives and proof"],
        "instruction": "Assess whether the message is easy to repeat, compare, and turn into a qualified action.",
    },
    "procurement": {
        "id": "procurement",
        "label": "Procurement",
        "goal": "check purchase readiness and vendor risk",
        "constraints": ["needs stable facts", "looks for ownership, terms, and deployment proof"],
        "instruction": "Look for verifiable vendor, deployment, pricing, and support information.",
    },
    "customer_success": {
        "id": "customer_success",
        "label": "Customer success",
        "goal": "judge whether the product can be adopted and supported",
        "constraints": ["needs setup and support clarity", "cares about realistic expectations"],
        "instruction": "Check onboarding, limitations, supportability, and whether claims set the right expectations.",
    },
    "skeptical_researcher": {
        "id": "skeptical_researcher",
        "label": "Skeptical researcher",
        "goal": "find contradictions and unsupported conclusions",
        "constraints": ["checks source quality", "does not reward confident wording without evidence"],
        "instruction": "Look for contradictions, ambiguity, missing citations, and competitor-context errors.",
    },
}
DEFAULT_PERSONA_COHORT = tuple(PERSONA_PROFILES)


def persona_profile(persona_id: str) -> dict:
    """Return a frozen, serializable persona profile.

    Unknown ids are allowed for compatibility with existing P0 smoke runs, but
    are explicitly generic and therefore not a formal cohort profile.
    """
    profile = PERSONA_PROFILES.get(str(persona_id))
    if profile is None:
        return {
            "id": str(persona_id),
            "label": "Unspecified operator",
            "goal": "inspect the supplied product information",
            "constraints": ["profile_not_in_formal_cohort"],
            "instruction": "Inspect the supplied information and state only what is supported.",
            "formal": False,
        }
    return {**profile, "formal": True}


def _cohort_profiles(persona_id: str, persona_profiles: Iterable[str] | None) -> list[dict]:
    ids = list(persona_profiles or [persona_id])
    if not ids:
        raise ValueError("persona cohort cannot be empty")
    seen = set()
    profiles = []
    for value in ids:
        pid = str(value).strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        profiles.append(persona_profile(pid))
    if not profiles:
        raise ValueError("persona cohort cannot be empty")
    return profiles


def _json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha(value: Any) -> str:
    return _sha256_bytes(_canonical(value).encode("utf-8"))


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_case_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-")
    if not text:
        raise ValueError("case_id 不能为空")
    return text[:80]


def _load_events(case_dir: Path, cycle_id: str) -> tuple[list[dict], list[dict], str]:
    candidates = [
        case_dir / "delivery" / "events" / f"{cycle_id}.jsonl",
        case_dir / "delivery" / "ledger" / cycle_id / "events.jsonl",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        events: list[dict] = []
        warnings: list[dict] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
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
            if row.get("cycle_id") and row.get("cycle_id") != cycle_id:
                warnings.append({"code": "event_cycle_mismatch", "line": line_no})
                continue
            events.append(row)
        return events, warnings, _rel(path, case_dir)
    return [], [{"code": "event_log_missing"}], ""


def _verification_files(case_dir: Path) -> list[Path]:
    verify_dir = case_dir / "verify"
    if not verify_dir.is_dir():
        return []
    return sorted(path for path in verify_dir.glob("*.json") if path.is_file())


def _find_baseline(case_dir: Path, cycle_id: str) -> tuple[Path | None, dict | None]:
    candidates = [
        case_dir / "delivery" / "snapshots" / cycle_id / "baseline.json",
        case_dir / "delivery" / "ledger" / cycle_id / "baseline.json",
    ]
    for path in candidates:
        if path.is_file():
            return path, _json(path, {})
    return None, None


def _source_files(case_dir: Path) -> Iterable[Path]:
    for path in sorted(case_dir.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in {".json", ".jsonl", ".md", ".html", ".txt"}:
            yield path


def _task_projection(task: dict) -> dict:
    delivery = task.get("delivery") if isinstance(task.get("delivery"), dict) else {}
    assignment = delivery.get("assignment") if isinstance(delivery.get("assignment"), dict) else {}
    verification = delivery.get("verification") if isinstance(delivery.get("verification"), dict) else {}
    refs = delivery.get("source_refs") if isinstance(delivery.get("source_refs"), list) else []
    return {
        "task_id": str(task.get("id") or ""),
        "title": task.get("title"),
        "status": task.get("status"),
        "stage": delivery.get("stage"),
        "owner_role": assignment.get("owner_role"),
        "next_action": delivery.get("next_action") or "",
        "source_refs": [
            {key: row.get(key) for key in ("id", "type", "ref", "review_status", "confidence")}
            for row in refs if isinstance(row, dict)
        ],
        "scope_status": (delivery.get("scope_decision") or {}).get("status"),
        "assignment_status": assignment.get("status"),
        "assets": [
            {key: row.get(key) for key in ("id", "path", "version", "sha256", "required", "missing", "approval_status")}
            for row in delivery.get("assets", []) if isinstance(row, dict)
        ],
        "approvals": [
            {key: row.get(key) for key in ("id", "type", "target", "status", "role", "requirement_id")}
            for row in delivery.get("approvals", []) if isinstance(row, dict)
        ],
        "deployments": [
            {key: row.get(key) for key in ("id", "target_url", "status", "deployment_complete", "human_confirmed", "deployed_by_role", "last_snapshot_ref")}
            for row in delivery.get("deployments", []) if isinstance(row, dict)
        ],
        "verification": {
            "id": verification.get("id"),
            "verdict": verification.get("verdict"),
            "can_close": verification.get("can_close"),
            "evidence_chain_complete": (verification.get("evidence_chain") or {}).get("complete"),
        },
    }


def load_case(case_dir: str | Path, case_id: str | None = None) -> dict:
    """Read one controlled case without modifying it."""
    root = Path(case_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"case directory not found: {case_dir}")
    geo = _json(root / "geo.json", {})
    tasks_data = _json(root / "tasks.json", {})
    delivery = geo.get("delivery") if isinstance(geo.get("delivery"), dict) else {}
    cycle_id = str(delivery.get("current_cycle_id") or "")
    if not cycle_id:
        baseline_dirs = list((root / "delivery" / "snapshots").glob("*/baseline.json"))
        if len(baseline_dirs) == 1:
            cycle_id = baseline_dirs[0].parent.name
    if not cycle_id:
        raise ValueError(f"case has no deterministic cycle_id: {root.name}")
    cid = _safe_case_id(case_id or root.name)
    baseline_path, baseline = _find_baseline(root, cycle_id)
    events, event_warnings, event_ref = _load_events(root, cycle_id)
    reports = []
    for path in _verification_files(root):
        payload = _json(path)
        if isinstance(payload, dict) and payload.get("cycle_id") in {None, cycle_id}:
            reports.append({"ref": _rel(path, root), "sha256": _sha256_file(path), "data": payload})
    final = next((row["data"] for row in reports if row["ref"].endswith("final.json")), None)
    if final is None and reports:
        final = reports[-1]["data"]
    scope_confirmation = delivery.get("scope_confirmation") if isinstance(delivery.get("scope_confirmation"), dict) else {}
    baseline_scope = (baseline or {}).get("scope") if isinstance((baseline or {}).get("scope"), dict) else {}
    mapped_scope_sha = baseline_scope.get("scope_sha256") or scope_confirmation.get("scope_sha256")
    source_hashes = {}
    for path in _source_files(root):
        source_hashes[_rel(path, root)] = _sha256_file(path)
    adapter_errors = []
    if baseline_path is None:
        adapter_errors.append("baseline_missing")
    if baseline is not None and baseline.get("cycle_id") not in {None, cycle_id}:
        adapter_errors.append("baseline_cycle_mismatch")
    if not scope_confirmation.get("scope_sha256"):
        adapter_errors.append("project_scope_sha_missing")
    if baseline is not None and not mapped_scope_sha:
        adapter_errors.append("baseline_scope_sha_missing")
    if not tasks_data.get("tasks") or not isinstance(tasks_data.get("tasks"), list):
        adapter_errors.append("tasks_missing")
    if not events:
        adapter_errors.append("event_log_missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": cid,
        "industry": delivery.get("industry") or geo.get("industry"),
        "source_case_name": root.name,
        "cycle_id": cycle_id,
        "snapshot_id": "final",
        "source_refs": [
            {"kind": "case_file", "ref": name, "sha256": digest}
            for name, digest in sorted(source_hashes.items())
            if name in CASE_FILE_NAMES
        ],
        "runtime_baseline": {
            "cycle_id": cycle_id,
            "baseline_ref": _rel(baseline_path, root) if baseline_path else "",
            "exists": baseline_path is not None,
            "locked": bool((baseline or {}).get("locked", True)) if baseline else False,
            "source_cycle_id": (baseline or {}).get("cycle_id"),
            "source_scope_sha256": baseline_scope.get("scope_sha256"),
            "mapped_scope_sha256": mapped_scope_sha,
            "scope_matches_project": bool(mapped_scope_sha and mapped_scope_sha == scope_confirmation.get("scope_sha256")),
            "sha256": _sha256_file(baseline_path) if baseline_path else None,
        },
        "delivery_state": {
            "config": {
                "current_cycle_id": cycle_id,
                "current_baseline_ref": delivery.get("current_baseline_ref"),
                "scope_confirmation": {
                    key: scope_confirmation.get(key)
                    for key in ("status", "confirmed_by_role", "confirmed_by_role_name", "scope_sha256", "confirmed_at")
                },
            },
            "tasks": [
                _task_projection(row) for row in tasks_data.get("tasks", []) if isinstance(row, dict)
            ],
        },
        "verification_snapshots": [
            {"ref": row["ref"], "sha256": row["sha256"], "data": row["data"]}
            for row in reports
        ],
        "event_log": {
            "ref": event_ref,
            "sha256": _sha256_file(root / event_ref) if event_ref else None,
            "events": events,
            "warnings": event_warnings,
        },
        "source_hashes": source_hashes,
        "adapter_valid": not adapter_errors,
        "adapter_errors": adapter_errors,
        "adapter_warnings": (["baseline_scope_sha256_mapped_from_project_scope"]
                             if baseline and not baseline_scope.get("scope_sha256") and mapped_scope_sha else []),
        "final_report_ref": next((row["ref"] for row in reports if row["data"] is final), ""),
    }


def case_adapter(case_dir: str | Path, case_id: str | None = None) -> dict:
    """Named adapter entry point; source cases remain read-only."""
    return load_case(case_dir, case_id)


def _provenance(kind: str, ref: str, **extra: Any) -> dict:
    row = {"source": kind, "source_ref": ref}
    row.update(extra)
    return row


def _report_rows(snapshot: dict, filename: str) -> list[dict]:
    for row in snapshot.get("verification_snapshots", []):
        if str(row.get("ref") or "").endswith(filename):
            return [item for item in (row.get("data") or {}).get("results", []) if isinstance(item, dict)]
    return []


def build_truth(snapshot: dict) -> dict:
    """Build deterministic Ground Truth with provenance for every scored field."""
    final_rows = {str(row.get("id")): row for row in _report_rows(snapshot, "final.json")}
    if not final_rows:
        final_rows = {str(row.get("id")): row for row in _report_rows(snapshot, "04-current-live-pass.json")}
    tasks = {str(row.get("task_id")): row for row in (snapshot.get("delivery_state") or {}).get("tasks", [])}
    events = (snapshot.get("event_log") or {}).get("events", [])
    all_reports = [row.get("data") or {} for row in snapshot.get("verification_snapshots", [])]
    truth_tasks = []
    consistency_errors = []
    for task_id in sorted(set(tasks) | set(final_rows)):
        task = tasks.get(task_id, {})
        result = final_rows.get(task_id, {})
        checks = [row for row in result.get("checks", []) if isinstance(row, dict)]
        required = [row for row in checks if row.get("required", True)]
        regression_rows = [
            row for report in all_reports
            for row in report.get("results", [])
            if isinstance(row, dict) and str(row.get("id")) == task_id and row.get("regression")
        ]
        task_events = [row for row in events if str(row.get("task_id") or "") == task_id]
        regression_detected = bool(regression_rows or any(
            row.get("event_type") == "verification_regressed" for row in task_events
        ))
        reopen_required = any(row.get("event_type") == "task_reopened" for row in task_events)
        owner_role = task.get("owner_role")
        observed_next = task.get("next_action") or ""
        action_provenance = _provenance("delivery_state", "tasks.json", field="delivery.next_action")
        next_action = {
            "scorable": False,
            "action": None,
            "observed_text": observed_next,
            "owner_role": owner_role if owner_role in ALLOWED_ROLES else None,
            "owner_role_scorable": owner_role in ALLOWED_ROLES,
            "reason": "runtime_has_no_canonical_next_action_code",
            "provenance": [action_provenance],
        }
        evidence_complete = result.get("evidence_chain_complete")
        if evidence_complete is None:
            evidence_complete = (task.get("verification") or {}).get("evidence_chain_complete")
        truth_tasks.append({
            "task_id": task_id,
            "close_ready": {
                "value": result.get("can_close") is True,
                "provenance": [_provenance("current_verifier", snapshot.get("final_report_ref") or "verify/final.json", field="results[].can_close")],
            },
            "required_checks": {
                "value": [
                    {key: row.get(key) for key in ("id", "check", "required", "raw_verdict", "verdict", "before", "after", "target")}
                    for row in checks
                ],
                "all_required_pass": bool(required) and all(row.get("raw_verdict", row.get("verdict")) == "pass" for row in required),
                "provenance": [_provenance("current_verifier", snapshot.get("final_report_ref") or "verify/final.json", field="results[].checks")],
            },
            "evidence_status": {
                "complete": evidence_complete is True,
                "source_refs": len(task.get("source_refs", [])),
                "confirmed_source_refs": sum(row.get("review_status") == "confirmed" for row in task.get("source_refs", [])),
                "provenance": [_provenance("evidence_chain", snapshot.get("final_report_ref") or "verify/final.json", field="evidence_chain_complete")],
            },
            "regression_detected": {
                "value": regression_detected,
                "provenance": [_provenance("delivery_event" if any(row.get("event_type") == "verification_regressed" for row in task_events) else "verification_snapshot", snapshot.get("event_log", {}).get("ref") or "", event_type="verification_regressed")],
            },
            "reopen_required": {
                "value": reopen_required,
                "provenance": [_provenance("delivery_event", snapshot.get("event_log", {}).get("ref") or "", event_type="task_reopened")],
            },
            "role": {
                "value": owner_role,
                "provenance": [_provenance("delivery_state", "tasks.json", field="delivery.assignment.owner_role")],
            },
            "next_action": next_action,
        })
        if result.get("now") is not None and task.get("status") is not None and result.get("now") != task.get("status"):
            consistency_errors.append({"task_id": task_id, "field": "status", "report": result.get("now"), "state": task.get("status")})
        if result.get("stage") is not None and task.get("stage") is not None and result.get("stage") != task.get("stage"):
            consistency_errors.append({"task_id": task_id, "field": "stage", "report": result.get("stage"), "state": task.get("stage")})
    baseline = snapshot.get("runtime_baseline") or {}
    baseline_truth = {
        "exists": baseline.get("exists") is True,
        "cycle_match": baseline.get("source_cycle_id") in {None, baseline.get("cycle_id")},
        "scope_match": baseline.get("scope_matches_project") is True,
        "locked": baseline.get("locked") is True,
        "provenance": [_provenance("baseline_snapshot", baseline.get("baseline_ref") or "")],
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": snapshot.get("case_id"),
        "cycle_id": snapshot.get("cycle_id"),
        "deterministic": True,
        "adapter_valid": snapshot.get("adapter_valid") is True,
        "adapter_errors": list(snapshot.get("adapter_errors") or []),
        "state_consistent": not consistency_errors,
        "consistency_errors": consistency_errors,
        "baseline": baseline_truth,
        "event_log": {
            "event_count": len(events),
            "warnings": (snapshot.get("event_log") or {}).get("warnings", []),
            "provenance": [_provenance("event_log", (snapshot.get("event_log") or {}).get("ref") or "")],
        },
        "tasks": truth_tasks,
        "truth_sources": [
            _provenance("current_verifier", snapshot.get("final_report_ref") or "verify/final.json"),
            _provenance("delivery_state", "tasks.json"),
            _provenance("event_log", (snapshot.get("event_log") or {}).get("ref") or ""),
            _provenance("deployment_evidence", "delivery/snapshots/*/deployments"),
        ],
    }


def truth_adapter(snapshot: dict) -> dict:
    """Named deterministic truth entry point for benchmark callers."""
    return build_truth(snapshot)


def _raw_parity_payload(snapshot: dict, task: dict) -> dict:
    return {
        "case_id": snapshot.get("case_id"),
        "cycle_id": snapshot.get("cycle_id"),
        "task_id": task.get("task_id"),
        "source_refs": task.get("source_refs", []),
        "assets": task.get("assets", []),
        "deployments": task.get("deployments", []),
        "baseline": snapshot.get("runtime_baseline", {}),
    }


def _stimulus(snapshot: dict, task: dict, arm: str, persona: dict, model: str, seed: int) -> dict:
    raw = _raw_parity_payload(snapshot, task)
    parity_hash = _canonical_sha(raw)
    derived = {
        "stage": task.get("stage"),
        "status": task.get("status"),
        "verification": task.get("verification"),
    } if arm == "treatment" else {}
    payload = {
        "arm": arm,
        "persona_id": persona["id"],
        "persona_model": model,
        "seed": seed,
        "persona_profile": persona,
        "case_id": snapshot.get("case_id"),
        "task_id": task.get("task_id"),
        "raw_facts": raw,
        "derived_agent_fields": derived,
        "information_parity_hash": parity_hash,
    }
    return {**payload, "stimulus_hash": _canonical_sha(payload)}


def _dummy_response(truth: dict, arm: str, persona: dict) -> dict:
    if arm == "treatment":
        return {
            "close_ready": truth["close_ready"]["value"],
            "reopen_required": truth["reopen_required"]["value"],
            "evidence_complete": truth["evidence_status"]["complete"],
            "owner_role": truth["role"]["value"],
            "next_action": truth["next_action"].get("observed_text"),
            "persona_id": persona["id"],
        }
    return {
        "close_ready": False,
        "reopen_required": False,
        "evidence_complete": False,
        "owner_role": None,
        "next_action": None,
        "persona_id": persona["id"],
    }


def _persona_prompt(pair: dict, arm: str) -> str:
    """Build the only prompt a real persona runner needs to receive.

    The runner is deliberately out-of-process.  It may be MatrAIx, a local
    Persona 8B wrapper, or a test executable, but it cannot change the case
    truth or trial metadata.  Baseline and treatment are separate calls.
    """
    stimulus = pair[arm]["stimulus"]
    persona = stimulus["persona_profile"]
    return (
        f"You are the fixed persona {persona['label']} (id={persona['id']}).\n"
        f"Goal: {persona['goal']}.\n"
        f"Constraints: {', '.join(persona['constraints'])}.\n"
        f"Instruction: {persona['instruction']}\n\n"
        "Review only the supplied product information. Do not invent facts. "
        "Return a JSON object with keys close_ready, reopen_required, "
        "evidence_complete, owner_role, next_action, and rationale.\n\n"
        f"Arm: {arm}\n"
        f"Product information:\n{json.dumps(stimulus, ensure_ascii=False, sort_keys=True, indent=2)}"
    )


def _invoke_persona_runner(command: str, request: dict, timeout: float) -> tuple[dict, float]:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("persona runner command is empty")
    started = time.monotonic()
    completed = subprocess.run(
        argv,
        input=json.dumps(request, ensure_ascii=False, sort_keys=True),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    elapsed = round(time.monotonic() - started, 4)
    if completed.returncode != 0:
        raise RuntimeError(f"persona runner exited {completed.returncode}: {completed.stderr[-500:]}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("persona runner stdout must be a JSON object") from exc
    if isinstance(value, dict) and isinstance(value.get("response"), dict):
        value = value["response"]
    if not isinstance(value, dict):
        raise ValueError("persona runner response must be a JSON object")
    return value, elapsed


def run_trials(manifest_path: str | Path, runner_command: str, output_path: str | Path | None = None, *, timeout: float = 120.0) -> dict:
    """Run a fixed cohort through an external Persona/MatrAIx-compatible command.

    Protocol: one JSON request on stdin, one JSON object on stdout per process.
    The command is invoked once per arm, so baseline/treatment cannot share
    mutable conversation state.  Errors are recorded as invalid trials and
    never silently converted into model feedback.
    """
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest_dir = manifest_file.parent
    manifest = _json(manifest_file, {})
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid benchmark manifest")
    pairs = _read_jsonl(manifest_dir / str(manifest.get("pairs_ref") or "pairs.jsonl"))
    if not pairs:
        raise ValueError("manifest contains no paired trials")
    results = []
    for pair in pairs:
        for arm in ("baseline", "treatment"):
            stimulus = pair[arm]["stimulus"]
            request = {
                "schema_version": PERSONA_REQUEST_SCHEMA_VERSION,
                "trial_id": f"{pair['pair_id']}:{arm}",
                "pair_id": pair["pair_id"],
                "run_id": pair[arm]["run_id"],
                "arm": arm,
                "case_id": pair["case_id"],
                "task_id": pair["task_id"],
                "cohort_id": pair["cohort_id"],
                "persona_id": pair["persona_id"],
                "persona_model": pair["persona_model"],
                "seed": pair["seed"],
                "information_parity_hash": pair["information_parity_hash"],
                "persona": stimulus["persona_profile"],
                "prompt": _persona_prompt(pair, arm),
                "stimulus": stimulus,
            }
            base = {
                key: request[key]
                for key in ("trial_id", "pair_id", "run_id", "arm", "case_id", "task_id", "cohort_id", "persona_id", "persona_model", "seed", "information_parity_hash")
            }
            base.update({"schema_version": TRIAL_SCHEMA_VERSION, "runner": "external-command", "runner_command": runner_command})
            try:
                response, elapsed = _invoke_persona_runner(runner_command, request, timeout)
                base.update({"runner_status": "ok", "latency_seconds": elapsed, "response": response})
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                base.update({"runner_status": "error", "runner_error": str(exc), "response": {}})
            results.append(base)
    target = Path(output_path).expanduser().resolve() if output_path else manifest_dir / "persona_results.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    manifest["results_ref"] = target.relative_to(manifest_dir).as_posix()
    manifest["runner"] = {**(manifest.get("runner") or {}), "type": "external-command", "command": runner_command, "timeout_seconds": timeout}
    _write_json(manifest_file, manifest)
    return {"results_ref": manifest["results_ref"], "trial_count": len(results), "error_count": sum(row.get("runner_status") != "ok" for row in results)}


def prepare(cases: list[tuple[str, str | Path]], output_dir: str | Path, *, cohort_id: str = "p0-local-smoke", persona_id: str = "operator-01", persona_model: str = "dummy-local", seed: int = 0, dummy: bool = True, persona_profiles: Iterable[str] | None = None) -> dict:
    out = Path(output_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    cohort = _cohort_profiles(persona_id, persona_profiles)
    snapshots = []
    truths = []
    pairs = []
    results = []
    for case_id, case_dir in cases:
        snapshot = load_case(case_dir, case_id)
        truth = build_truth(snapshot)
        snapshot_path = out / "cases" / f"{snapshot['case_id']}.json"
        truth_path = out / "truth" / f"{snapshot['case_id']}.json"
        _write_json(snapshot_path, snapshot)
        _write_json(truth_path, truth)
        snapshots.append({"case_id": snapshot["case_id"], "ref": snapshot_path.relative_to(out).as_posix(), "sha256": _sha256_file(snapshot_path)})
        truths.append({"case_id": truth["case_id"], "ref": truth_path.relative_to(out).as_posix(), "sha256": _sha256_file(truth_path)})
        by_id = {row["task_id"]: row for row in snapshot["delivery_state"]["tasks"]}
        truth_by_id = {row["task_id"]: row for row in truth["tasks"]}
        for task_id in sorted(set(by_id) & set(truth_by_id)):
            task = by_id[task_id]
            for persona in cohort:
                pair_id = f"{snapshot['case_id']}:{task_id}:{persona['id']}"
                baseline = _stimulus(snapshot, task, "baseline", persona, persona_model, seed)
                treatment = _stimulus(snapshot, task, "treatment", persona, persona_model, seed)
                pair = {
                    "pair_id": pair_id,
                    "case_id": snapshot["case_id"],
                    "task_id": task_id,
                    "cohort_id": cohort_id,
                    "persona_id": persona["id"],
                    "persona_model": persona_model,
                    "seed": seed,
                    "information_parity_hash": baseline["information_parity_hash"],
                    "baseline": {"run_id": f"{pair_id}:baseline", "stimulus": baseline},
                    "treatment": {"run_id": f"{pair_id}:treatment", "stimulus": treatment},
                }
                pairs.append(pair)
                if dummy:
                    for arm in ("baseline", "treatment"):
                        results.append({
                            "schema_version": TRIAL_SCHEMA_VERSION,
                            "trial_id": f"{pair_id}:{arm}",
                            "pair_id": pair_id,
                            "run_id": pair[arm]["run_id"],
                            "arm": arm,
                            "case_id": snapshot["case_id"],
                            "task_id": task_id,
                            "cohort_id": cohort_id,
                            "persona_id": persona["id"],
                            "persona_model": persona_model,
                            "seed": seed,
                            "information_parity_hash": pair["information_parity_hash"],
                            "runner": "dummy-local",
                            "runner_status": "ok",
                            "response": _dummy_response(truth_by_id[task_id], arm, persona),
                        })
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_type": "internal_feedback",
        "title": "内测用户反馈",
        "created_at": "2026-01-01T00:00:00Z",
        "runner": {"type": "dummy-local" if dummy else "external", "persona_id": cohort[0]["id"], "persona_model": persona_model, "seed": seed, "cohort_id": cohort_id, "persona_cohort": cohort},
        "cases": snapshots,
        "truth": truths,
        "pairs_ref": "pairs.jsonl",
        "results_ref": "dummy_results.jsonl" if dummy else None,
        "formal_min_pairs": FORMAL_MIN_PAIRS,
        "publishable": False,
        "publishability_reason": "analysis_required",
    }
    _write_json(out / "manifest.json", manifest)
    with (out / "pairs.jsonl").open("w", encoding="utf-8") as handle:
        for row in pairs:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    if dummy:
        with (out / "dummy_results.jsonl").open("w", encoding="utf-8") as handle:
            for row in results:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"manifest": manifest, "pair_count": len(pairs), "result_count": len(results), "output_dir": str(out)}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} line {line_no} is invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {line_no} is not an object")
        rows.append(row)
    return rows


def _truth_index(manifest_dir: Path, manifest: dict) -> dict[tuple[str, str], dict]:
    out = {}
    for row in manifest.get("truth", []):
        payload = _json(manifest_dir / row["ref"], {})
        for task in payload.get("tasks", []):
            if isinstance(task, dict):
                out[(payload.get("case_id"), task.get("task_id"))] = task
    return out


def analyze(manifest_path: str | Path, results_path: str | Path | None = None, output_path: str | Path | None = None) -> dict:
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest_dir = manifest_file.parent
    manifest = _json(manifest_file, {})
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid benchmark manifest")
    result_file = Path(results_path).expanduser().resolve() if results_path else manifest_dir / str(manifest.get("results_ref") or "dummy_results.jsonl")
    results = _read_jsonl(result_file)
    truth = _truth_index(manifest_dir, manifest)
    truth_documents = {
        row.get("case_id"): _json(manifest_dir / row["ref"], {})
        for row in manifest.get("truth", [])
    }
    pairs = {row.get("pair_id"): row for row in _read_jsonl(manifest_dir / str(manifest.get("pairs_ref") or "pairs.jsonl"))}
    manifest_runner = manifest.get("runner") or {}
    expected_cohort = {str(row.get("id")) for row in manifest_runner.get("persona_cohort", []) if isinstance(row, dict)}
    valid = []
    invalid = []
    metrics = {"close_ready": {"baseline": [], "treatment": []}, "reopen_required": {"baseline": [], "treatment": []}, "evidence_complete": {"baseline": [], "treatment": []}, "owner_role": {"baseline": [], "treatment": []}}
    by_pair: dict[str, dict[str, dict]] = {}
    for row in results:
        required = ("trial_id", "pair_id", "run_id", "arm", "case_id", "task_id", "cohort_id", "persona_id", "persona_model", "seed", "information_parity_hash", "response")
        errors = [key for key in required if key not in row]
        if row.get("runner_status", "ok") != "ok":
            errors.append("runner_status")
        pair = pairs.get(row.get("pair_id"))
        if row.get("arm") not in {"baseline", "treatment"}:
            errors.append("arm")
        if pair is None:
            errors.append("pair_id")
        else:
            if row.get("information_parity_hash") != pair.get("information_parity_hash"):
                errors.append("information_parity_hash")
            if any(row.get(key) != pair.get(key) for key in ("case_id", "task_id", "cohort_id", "persona_id", "persona_model", "seed")):
                errors.append("pair_metadata")
            if expected_cohort and row.get("persona_id") not in expected_cohort:
                errors.append("persona_cohort")
        if (row.get("case_id"), row.get("task_id")) not in truth:
            errors.append("ground_truth")
        if errors:
            invalid.append({"trial_id": row.get("trial_id"), "errors": sorted(set(errors))})
            continue
        valid.append(row)
        by_pair.setdefault(row["pair_id"], {})[row["arm"]] = row
        gt = truth[(row["case_id"], row["task_id"])]
        response = row.get("response") or {}
        checks = {
            "close_ready": response.get("close_ready") is gt["close_ready"]["value"],
            "reopen_required": response.get("reopen_required") is gt["reopen_required"]["value"],
            "evidence_complete": response.get("evidence_complete") is gt["evidence_status"]["complete"],
        }
        for key, score in checks.items():
            metrics[key][row["arm"]].append(bool(score))
        if gt["role"]["value"] in ALLOWED_ROLES:
            metrics["owner_role"][row["arm"]].append(response.get("owner_role") == gt["role"]["value"])
    paired = []
    pair_invalid = []
    for pair_id, arms in by_pair.items():
        if set(arms) != {"baseline", "treatment"}:
            pair_invalid.append({"pair_id": pair_id, "arms": sorted(arms)})
        else:
            paired.append(pair_id)
    def mean(rows: list[bool]) -> float | None:
        return round(sum(rows) / len(rows), 4) if rows else None
    metric_summary = {}
    for key, arms in metrics.items():
        b, t = mean(arms["baseline"]), mean(arms["treatment"])
        metric_summary[key] = {"baseline_accuracy": b, "treatment_accuracy": t, "paired_delta": round(t - b, 4) if b is not None and t is not None else None, "scorable": bool(arms["baseline"] and arms["treatment"])}
    by_persona = {}
    for persona_id in sorted({row.get("persona_id") for row in valid}):
        persona_pairs = [pair_id for pair_id in paired if pairs.get(pair_id, {}).get("persona_id") == persona_id]
        persona_row = {"pair_count": len(persona_pairs)}
        for key, arms in metrics.items():
            # Metrics are already arm-specific; filter by trial metadata for
            # a true paired cohort view instead of averaging across personas.
            values = {arm: [] for arm in ("baseline", "treatment")}
            for row in valid:
                if row.get("persona_id") not in {persona_id} or row.get("arm") not in values:
                    continue
                gt = truth.get((row.get("case_id"), row.get("task_id")))
                if not gt:
                    continue
                response = row.get("response") or {}
                if key == "owner_role":
                    score = response.get("owner_role") == gt["role"]["value"]
                elif key == "evidence_complete":
                    score = response.get("evidence_complete") is gt["evidence_status"]["complete"]
                else:
                    score = response.get(key) is gt[key]["value"]
                values[row["arm"]].append(bool(score))
            b, t = mean(values["baseline"]), mean(values["treatment"])
            persona_row[key] = {"baseline_accuracy": b, "treatment_accuracy": t, "paired_delta": round(t - b, 4) if b is not None and t is not None else None, "scorable": bool(values["baseline"] and values["treatment"])}
        by_persona[persona_id] = persona_row
    valid_pairs = len(paired)
    parity_valid = not invalid and not pair_invalid and all(
        pairs[pair_id].get("baseline", {}).get("stimulus", {}).get("information_parity_hash") == pairs[pair_id].get("treatment", {}).get("stimulus", {}).get("information_parity_hash")
        for pair_id in paired if pair_id in pairs
    )
    pair_coverage = (valid_pairs / len(pairs)) if pairs else 0.0
    ground_truth_coverage = (len({(row.get("case_id"), row.get("task_id")) for row in valid}) / len(truth)) if truth else 0.0
    publishable = bool(
        valid_pairs >= int(manifest.get("formal_min_pairs") or FORMAL_MIN_PAIRS)
        and pair_coverage >= MIN_PAIR_COVERAGE
        and ground_truth_coverage >= MIN_GROUND_TRUTH_COVERAGE
        and parity_valid
        and not invalid
        and not pair_invalid
        and valid_pairs > 0
        and all(doc.get("adapter_valid") is True and doc.get("state_consistent") is True for doc in truth_documents.values())
    )
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "benchmark_type": "internal_feedback",
        "title": "内测用户反馈",
        "manifest_ref": manifest_file.name,
        "runner": manifest.get("runner"),
        "trial_count": len(results),
        "valid_trial_count": len(valid),
        "invalid_trial_count": len(invalid),
        "valid_pairs": valid_pairs,
        "pair_coverage": round(pair_coverage, 4),
        "parity_valid": parity_valid,
        "ground_truth_deterministic": True,
        "ground_truth_coverage": round(ground_truth_coverage, 4),
        "adapter_valid": all(doc.get("adapter_valid") is True for doc in truth_documents.values()),
        "state_consistent": all(doc.get("state_consistent") is True for doc in truth_documents.values()),
        "adapter_errors": {
            case_id: doc.get("adapter_errors", [])
            for case_id, doc in truth_documents.items()
            if doc.get("adapter_errors")
        },
        "formal_gates": {
            "min_valid_pairs": int(manifest.get("formal_min_pairs") or FORMAL_MIN_PAIRS),
            "min_pair_coverage": MIN_PAIR_COVERAGE,
            "min_ground_truth_coverage": MIN_GROUND_TRUTH_COVERAGE,
        },
        "metrics": metric_summary,
        "paired_by_persona": by_persona,
        "next_action": {"scorable": False, "reason": "runtime_has_no_canonical_next_action_code"},
        "invalid_trials": invalid,
        "invalid_pairs": pair_invalid,
        "publishable": publishable,
        "publishability_reason": "formal_gates_passed" if publishable else "smoke_or_data_quality_gates_not_met",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    target = Path(output_path).expanduser().resolve() if output_path else manifest_dir / "summary.json"
    _write_json(target, summary)
    return summary


def publish(summary_path: str | Path, output_path: str | Path | None = None) -> dict:
    """Publish a projection only; all scoring is already frozen in summary.json."""
    source = Path(summary_path).expanduser().resolve()
    summary = _json(source)
    if not isinstance(summary, dict) or summary.get("schema_version") != SUMMARY_SCHEMA_VERSION:
        raise ValueError("invalid benchmark summary")
    publication = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "title": summary.get("title") if summary.get("publishable") else "内测用户反馈数据（未达到正式发布门槛）",
        "publishable": summary.get("publishable") is True,
        "reason": summary.get("publishability_reason"),
        "summary_ref": source.name,
        "valid_pairs": summary.get("valid_pairs"),
        "metrics": summary.get("metrics"),
        "next_action": summary.get("next_action"),
    }
    target = Path(output_path).expanduser().resolve() if output_path else source.parent / "publication.json"
    _write_json(target, publication)
    return publication


def _parse_case_specs(values: list[str]) -> list[tuple[str, str]]:
    rows = []
    for value in values:
        if "=" not in value:
            raise ValueError("--case 格式必须是 case_id=/path/to/case")
        cid, path = value.split("=", 1)
        rows.append((_safe_case_id(cid), path))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Internal feedback benchmark prepare/analyze/publish")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--case", action="append", required=True, help="case_id=/path/to/controlled-case")
    p.add_argument("--output", required=True)
    p.add_argument("--cohort-id", default="p0-local-smoke")
    p.add_argument("--persona-id", default="operator-01")
    p.add_argument("--persona-model", default="dummy-local")
    p.add_argument("--persona-profile", action="append", dest="persona_profiles", help="固定 cohort persona id；可重复传入")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--no-dummy", action="store_true")
    p = sub.add_parser("analyze")
    p.add_argument("--manifest", required=True)
    p.add_argument("--results")
    p.add_argument("--output")
    p = sub.add_parser("run")
    p.add_argument("--manifest", required=True)
    p.add_argument("--runner", required=True, help="外部 Persona/MatrAIx runner 命令；JSON stdin/stdout 协议")
    p.add_argument("--output")
    p.add_argument("--timeout", type=float, default=120.0)
    p = sub.add_parser("publish")
    p.add_argument("--summary", required=True)
    p.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(_parse_case_specs(args.case), args.output, cohort_id=args.cohort_id, persona_id=args.persona_id, persona_model=args.persona_model, seed=args.seed, dummy=not args.no_dummy, persona_profiles=args.persona_profiles)
        elif args.command == "run":
            result = run_trials(args.manifest, args.runner, args.output, timeout=args.timeout)
        elif args.command == "analyze":
            result = analyze(args.manifest, args.results, args.output)
        else:
            result = publish(args.summary, args.output)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
