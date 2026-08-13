"""One-way external execution adapters with redacted payloads."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import socket
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlparse

import export_delivery


WEBHOOK_EVENTS = {"task_approved", "task_assigned", "asset_pending_approval",
                  "deployment_pending", "verification_failed", "regression"}
_PRIVATE_KEY = re.compile(r"(?:email|e-mail|phone|mobile|contact|password|secret|token|api.?key)", re.I)


def _is_private_host(host: str) -> bool:
    """Return whether a webhook host resolves to a non-public address.

    This is a defense-in-depth check for SSRF. It rejects literal private,
    loopback, link-local and reserved addresses, common local hostnames, and
    any DNS result that resolves into those ranges. DNS lookup failures are
    left to the HTTP client so offline configuration validation remains useful.
    """
    normalized = str(host or "").strip().rstrip(".").lower()
    if not normalized or normalized in {"localhost", "localhost.localdomain"} \
            or normalized.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        addresses = {ipaddress.ip_address(normalized)}
    except ValueError:
        addresses = set()
        try:
            addresses = {
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
            }
        except (OSError, socket.gaierror):
            return False
    return any(
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_multicast or address.is_unspecified
        for address in addresses
    )


def _redact(value):
    if isinstance(value, list):
        return [_redact(row) for row in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if _PRIVATE_KEY.search(str(key)):
            continue
        if isinstance(item, str) and ("@" in item or item.startswith(("/Users/", "/home/", "C:\\Users\\"))):
            continue
        out[key] = _redact(item)
    return out


def redact_payload(payload: dict) -> dict:
    """Return a webhook/issue payload without credentials, PII or local paths."""
    return _redact(payload)


class ExportAdapter:
    system = "external"

    def validate(self, config: dict) -> dict:
        if not isinstance(config, dict):
            raise TypeError("integration config 必须是对象")
        return config

    def transform(self, task: dict, config: dict | None = None) -> dict:
        return redact_payload(export_delivery.github_issue_payload(task))

    def send(self, payload: dict, config: dict) -> dict:
        raise NotImplementedError


class WebhookAdapter(ExportAdapter):
    system = "webhook"

    def validate(self, config: dict) -> dict:
        super().validate(config)
        url = str(config.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Webhook URL 必须是有效 http(s) 地址")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Webhook URL 不得包含用户名或密码")
        if _is_private_host(parsed.hostname or ""):
            raise ValueError("Webhook URL 不得指向本机、内网或保留地址")
        return {"url": url, "timeout": min(max(int(config.get("timeout", 10)), 1), 30)}

    def send(self, payload: dict, config: dict) -> dict:
        cfg = self.validate(config)
        body = json.dumps(redact_payload(payload), ensure_ascii=False).encode("utf-8")
        request = Request(cfg["url"], data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=cfg["timeout"]) as response:
                return {"ok": 200 <= response.status < 300, "status": response.status}
        except (HTTPError, URLError, TimeoutError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class GitHubIssueAdapter(ExportAdapter):
    system = "github"

    def validate(self, config: dict) -> dict:
        super().validate(config)
        repo = str(config.get("repo") or os.environ.get("GITHUB_REPOSITORY") or "").strip()
        token = str(config.get("token") or os.environ.get("GITHUB_TOKEN") or "").strip()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            raise ValueError("GitHub repo 必须是 owner/repository")
        if not token:
            raise ValueError("缺少 GITHUB_TOKEN；创建 Issue 前必须显式配置")
        return {"repo": repo, "token": token, "timeout": min(max(int(config.get("timeout", 15)), 1), 30)}

    def send(self, payload: dict, config: dict) -> dict:
        cfg = self.validate(config)
        body = json.dumps(redact_payload(payload), ensure_ascii=False).encode("utf-8")
        request = Request(
            f"https://api.github.com/repos/{cfg['repo']}/issues", data=body,
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=cfg["timeout"]) as response:
                result = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "id": str(result.get("number") or ""), "url": result.get("html_url", "")}
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def github_config(cfg: dict | None = None) -> dict:
    delivery = (cfg or {}).get("delivery") if isinstance((cfg or {}).get("delivery"), dict) else {}
    integrations = delivery.get("integrations") if isinstance(delivery.get("integrations"), dict) else {}
    github = integrations.get("github") if isinstance(integrations.get("github"), dict) else {}
    return {"repo": github.get("repo") or os.environ.get("GITHUB_REPOSITORY", ""),
            "token": os.environ.get("GITHUB_TOKEN", ""),
            "timeout": github.get("timeout", 15)}


def sync_github_issue(slug: str, task_id: str, *, config: dict | None = None, dry_run: bool = False) -> dict:
    import geolib as G
    import delivery
    import tasks as T

    cfg = G.load_config(slug)
    data = T.load(slug)
    task = next((row for row in data.get("tasks", []) if str(row.get("id")) == str(task_id)), None)
    if task is None:
        raise KeyError(f"找不到工单 {task_id}")
    adapter = GitHubIssueAdapter()
    issue = adapter.transform(task, config)
    if dry_run:
        return {"ok": True, "dry_run": True, "payload": issue}
    result = adapter.send(issue, github_config(cfg) if config is None else config)
    if result.get("ok"):
        task = delivery.record_external_ref(slug, task_id, "github", result["id"], result.get("url", ""), actor_role="project_owner")
        result["task"] = task
    return result


def refresh_github_issue(slug: str, task_id: str, external_id: str, *, config: dict | None = None) -> dict:
    import geolib as G
    import delivery
    cfg = github_config(G.load_config(slug)) if config is None else config
    checked = dict(GitHubIssueAdapter().validate(cfg))
    request = Request(
        f"https://api.github.com/repos/{checked['repo']}/issues/{external_id}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {checked['token']}"},
    )
    try:
        with urlopen(request, timeout=checked["timeout"]) as response:
            issue = json.loads(response.read().decode("utf-8"))
        state = str(issue.get("state") or "")
        task = delivery.record_external_ref(slug, task_id, "github", str(external_id), issue.get("html_url", ""), external_state=state, actor_role="project_owner")
        return {"ok": True, "external_state": state, "task": task}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def send_webhook(slug: str, event: str, task: dict, config: dict) -> dict:
    event = str(event or "").strip()
    if event not in WEBHOOK_EVENTS:
        raise ValueError(f"不支持的 webhook 事件：{event}")
    payload = {"event": event, "sent_at": datetime.now(timezone.utc).isoformat(),
               "task": export_delivery.task_row(task)}
    return WebhookAdapter().send(payload, config)
