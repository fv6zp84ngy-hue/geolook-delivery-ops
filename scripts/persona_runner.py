#!/usr/bin/env python3
"""JSONL-compatible fixed-persona runner for P1 benchmark trials.

The process protocol is intentionally tiny:

    one JSON request on stdin -> one JSON response on stdout

The request is produced by ``internal_feedback_benchmark run``.  This runner
uses an OpenAI-compatible chat-completions endpoint, which also makes it
usable with a self-hosted Persona 8B gateway or a MatrAIx adapter without
adding a Python SDK dependency.  Configuration is supplied by flags or env:

    PERSONA_API_URL / MATRAIX_API_URL
    PERSONA_API_KEY / MATRAIX_API_KEY
    PERSONA_MODEL / MATRAIX_MODEL

No prompt, API key, or model response is written to stdout except the single
validated JSON result.  Diagnostics go to stderr and failures exit non-zero;
the benchmark runner then records an invalid trial instead of scoring it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


REQUEST_SCHEMA = "benchmark.persona_request.v1"
DEFAULT_TIMEOUT = 120.0


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def _json_object(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("model content is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("model content must be a JSON object")
    return parsed


def _validate_response(value: dict) -> dict:
    required = ("close_ready", "reopen_required", "evidence_complete", "owner_role", "next_action")
    missing = [key for key in required if key not in value]
    if missing:
        raise ValueError(f"model response missing keys: {', '.join(missing)}")
    for key in ("close_ready", "reopen_required", "evidence_complete"):
        if not isinstance(value[key], bool):
            raise ValueError(f"{key} must be boolean")
    if value["owner_role"] is not None and not isinstance(value["owner_role"], str):
        raise ValueError("owner_role must be a string or null")
    if value["next_action"] is not None and not isinstance(value["next_action"], str):
        raise ValueError("next_action must be a string or null")
    return value


def _system_prompt(request: dict) -> str:
    persona = request.get("persona") or {}
    constraints = ", ".join(str(x) for x in persona.get("constraints", []))
    return (
        "You are a controlled benchmark persona. Follow the supplied persona "
        "profile exactly. Do not invent facts, citations, deployment status, "
        "or task evidence. Return ONLY a JSON object with exactly these keys: "
        "close_ready (boolean), reopen_required (boolean), "
        "evidence_complete (boolean), owner_role (string or null), "
        "next_action (string or null), rationale (short string). "
        f"Persona: {persona.get('label', persona.get('id', 'unknown'))}. "
        f"Goal: {persona.get('goal', '')}. Constraints: {constraints}."
    )


def _request_payload(request: dict, model: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt(request)},
            {"role": "user", "content": str(request.get("prompt") or "")},
        ],
        "temperature": 0,
        "seed": request.get("seed"),
        "response_format": {"type": "json_object"},
    }


def _extract_content(payload: dict) -> str | dict:
    # OpenAI-compatible response shape.
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("provider response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, dict):
        return content
    if isinstance(content, str) and content.strip():
        return content
    raise ValueError("provider response has no message content")


def call_provider(request: dict, *, url: str, api_key: str, model: str, timeout: float) -> dict:
    if request.get("schema_version") != REQUEST_SCHEMA:
        raise ValueError("unsupported persona request schema")
    if not url:
        raise ValueError("PERSONA_API_URL or MATRAIX_API_URL is required")
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("persona provider URL must use http or https")
    if not model:
        raise ValueError("PERSONA_MODEL or MATRAIX_MODEL is required")
    payload = _request_payload(request, model)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024).decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"provider HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"provider unavailable: {exc.reason}") from exc
    try:
        provider_response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("provider response is not JSON") from exc
    content = _extract_content(provider_response)
    response = content if isinstance(content, dict) else _json_object(content)
    return _validate_response(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one fixed Persona/MatrAIx benchmark request")
    parser.add_argument("--url", default="", help="OpenAI-compatible chat completions URL")
    parser.add_argument("--api-key", default="", help="provider key; prefer environment variables")
    parser.add_argument("--model", default="", help="provider model name")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    url = args.url or _env("PERSONA_API_URL", "MATRAIX_API_URL")
    api_key = args.api_key or _env("PERSONA_API_KEY", "MATRAIX_API_KEY")
    model = args.model or _env("PERSONA_MODEL", "MATRAIX_MODEL")
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("stdin must contain one JSON object")
        response = call_provider(request, url=url, api_key=api_key, model=model, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"persona_runner: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
