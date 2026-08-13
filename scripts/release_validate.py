#!/usr/bin/env python3
"""Reproducible Public Alpha RC validation and Tester Kit builder.

The validator deliberately keeps release work in a temporary directory. It
does not touch a user's ``work/`` tree, call external URLs, or require API
keys. CI installs the pinned runtime dependencies before invoking it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PREFIX = "geo-delivery-ops-tester-kit"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret|webhook[_-]?key)\s*[:=]\s*[^\s,;]+"
)
ABSOLUTE_PATH = re.compile(r"(?:/Users/|/home/|/private/var/|[A-Za-z]:\\Users\\)")
FORBIDDEN_NAME_PARTS = {".env", "work", "__pycache__", "real-cases", "customer-materials"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path, directory: str) -> Iterable[tuple[Path, str]]:
    base = root / directory
    if not base.exists():
        return []
    return ((path, path.relative_to(root).as_posix()) for path in base.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and ".DS_Store" not in path.parts)


def tester_kit_members(root: Path = ROOT) -> list[tuple[Path, str]]:
    """Return the explicit Tester Kit allow-list, never the whole repository."""
    root_files = (
        ("TESTER_KIT_README.md", "README.md"),
        ("LICENSE", "LICENSE"),
        ("NOTICE.md", "NOTICE.md"),
        ("VERSION", "VERSION"),
        ("KNOWN_LIMITATIONS.md", "KNOWN_LIMITATIONS.md"),
        ("TESTER_GUIDE.md", "TESTER_GUIDE.md"),
        ("TESTING_GUIDE.md", "TESTING_GUIDE.md"),
        ("SMOKE_TEST.md", "SMOKE_TEST.md"),
        ("TESTER_FEEDBACK_TEMPLATE.md", "TESTER_FEEDBACK_TEMPLATE.md"),
        ("run_tester_smoke.sh", "run_tester_smoke.sh"),
        ("serve_public_demo.sh", "serve_public_demo.sh"),
        ("docs/index.html", "docs/index.html"),
    )
    script_files = ("scripts/rc.py", "scripts/version.py")
    result: list[tuple[Path, str]] = []
    for source_name, archive_name in root_files:
        source = root / source_name
        if source.is_file():
            result.append((source, archive_name))
    for source_name in script_files:
        source = root / source_name
        if source.is_file():
            result.append((source, source_name))
    result.extend(_iter_files(root, "docs/delivery-ops"))
    for path, archive_name in _iter_files(root, "docs"):
        relative = Path(archive_name)
        if relative.parts[:2] == ("docs", "delivery-ops"):
            continue
        if path.suffix.lower() in {".mp4", ".gif", ".png", ".jpg", ".jpeg", ".webm"}:
            result.append((path, archive_name))
    result.extend(_iter_files(root, "examples"))
    return sorted(result, key=lambda item: item[1])


def _validate_relative_member(name: str) -> None:
    path = Path(name.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise ValueError(f"unsafe archive path: {name}")
    if any(part in FORBIDDEN_NAME_PARTS for part in path.parts) or path.suffix.lower() in {".pyc", ".pyo"}:
        raise ValueError(f"forbidden archive member: {name}")
    if path.name == ".DS_Store":
        raise ValueError(f"forbidden archive member: {name}")


def scan_archive(archive_path: Path) -> dict:
    """Scan names and text payloads before an artifact is published."""
    findings: list[dict] = []
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            name = info.filename
            try:
                _validate_relative_member(name.removeprefix(PACKAGE_PREFIX + "/"))
            except ValueError as exc:
                findings.append({"code": "forbidden_member", "path": name, "detail": str(exc)})
                continue
            if info.is_dir():
                continue
            payload = archive.read(info)
            if len(payload) > 8 * 1024 * 1024:
                continue
            text = payload.decode("utf-8", errors="replace")
            if SENSITIVE_ASSIGNMENT.search(text):
                findings.append({"code": "secret_like_text", "path": name})
            if ABSOLUTE_PATH.search(text):
                findings.append({"code": "absolute_path", "path": name})
    return {"ok": not findings, "findings": findings}


def build_tester_kit(root: Path = ROOT, output_dir: Path | None = None) -> dict:
    """Build a deterministic, offline-only Tester Kit ZIP and checksum."""
    root = root.resolve()
    output_dir = (output_dir or root / "release").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    archive_path = output_dir / f"geo-delivery-ops-v{version}-tester-kit.zip"
    members = tester_kit_members(root)
    required = {"README.md", "VERSION", "run_tester_smoke.sh", "scripts/rc.py", "docs/index.html"}
    missing = sorted(required - {archive_name for _, archive_name in members})
    if missing:
        raise FileNotFoundError(f"Tester Kit allow-list is incomplete: {', '.join(missing)}")

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, archive_name in members:
            _validate_relative_member(archive_name)
            info = zipfile.ZipInfo(f"{PACKAGE_PREFIX}/{archive_name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if source.suffix == ".sh" else 0o644
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, source.read_bytes())

    scan = scan_archive(archive_path)
    if not scan["ok"]:
        raise ValueError(f"Tester Kit scan failed: {scan['findings']}")
    digest = sha256_file(archive_path)
    return {
        "artifact": archive_path.name,
        "path": str(archive_path),
        "sha256": digest,
        "file_count": len(members),
        "members": [archive_name for _, archive_name in members],
        "scan": scan,
    }


def _sanitized(text: str, root: Path) -> str:
    text = text.replace(str(root), "<repo>")
    text = re.sub(r"/private/var/folders/[^\s/:]+/[^\s]+", "<temp>", text)
    text = re.sub(r"/var/folders/[^\s/:]+/[^\s]+", "<temp>", text)
    return text


class ValidationRun:
    def __init__(self, root: Path, output: Path, python: str):
        self.root = root.resolve()
        self.output = output.resolve()
        self.python = python
        self.lines: list[str] = []
        self.results: list[dict] = []

    def log(self, message: str) -> None:
        self.lines.append(message.rstrip())

    def command(self, label: str, args: list[str], *, cwd: Path | None = None,
                env: dict[str, str] | None = None, check: bool = True,
                timeout: int = 300) -> subprocess.CompletedProcess[str]:
        self.log(f"$ {label}: {' '.join(args)}")
        merged = os.environ.copy()
        if env:
            merged.update(env)
        result = subprocess.run(args, cwd=str(cwd or self.root), env=merged,
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
        if result.stdout:
            self.lines.append(_sanitized(result.stdout, self.root).rstrip())
        status = "PASS" if result.returncode == 0 else "FAIL"
        self.log(f"{status} {label} (exit {result.returncode})")
        if check and result.returncode != 0:
            raise RuntimeError(f"{label} failed with exit {result.returncode}")
        return result

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append({"name": name, "ok": bool(ok), "detail": detail})
        self.log(f"{'PASS' if ok else 'FAIL'} {name}{(': ' + detail) if detail else ''}")

    def run(self) -> dict:
        self.output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="geo-delivery-validate-") as temp_name:
            temp = Path(temp_name)
            self.command("unit tests", [self.python, "-m", "unittest", "discover", "-s", "tests", "-p", "test*.py"])
            self.record("unit tests", True)
            self.command("integration tests", [self.python, "-m", "unittest", "tests.test_delivery_e2e", "tests.test_release_e2e", "tests.test_version_consistency"])
            self.record("integration tests", True)

            kit = build_tester_kit(self.root, self.output)
            self.record("build Tester Kit", True, f"{kit['artifact']} ({kit['file_count']} files)")
            extract_root = temp / "tester-kit"
            extract_root.mkdir()
            with zipfile.ZipFile(kit["path"]) as archive:
                archive.extractall(extract_root)
            kit_root = extract_root / PACKAGE_PREFIX
            self.command("Tester Smoke", ["bash", "run_tester_smoke.sh"], cwd=kit_root)
            self.record("Tester Smoke", True)
            self.record("Tester Kit extraction", True, kit["artifact"])

            agent_root = temp / "agent"
            agent_work = agent_root / "work"
            env = {"GEO_WORK": str(agent_work)}
            self.command("create source demo", [self.python, "scripts/rc.py", "demo", "--root", str(agent_root), "--slug", "validation-demo"], env=env)
            self.record("create source demo", True)
            self.command("verify source demo", [self.python, "scripts/rc.py", "verify-demo", "--root", str(agent_root), "--slug", "validation-demo"], env=env)
            self.record("verify source demo", True)

            # A new, incomplete project must still be diagnosable without editing JSON.
            self.command("create doctor project", [self.python, "scripts/geo.py", "init", "--url", "https://example.invalid", "--name", "Release Validation", "--slug", "doctor-check", "--market", "global"], env=env)
            self.record("create doctor project", True)
            self.command("delivery-doctor", [self.python, "scripts/geo.py", "delivery-doctor", "--slug", "doctor-check", "--json"], env=env)
            self.record("delivery-doctor", True, "FAIL 0; WARN allowed for an intentionally incomplete new project")

            port = _free_port()
            ui = subprocess.Popen([self.python, "scripts/geo.py", "ui", "--port", str(port), "--no-open"],
                                  cwd=str(self.root), env={**os.environ, **env},
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                _wait_http(f"http://127.0.0.1:{port}/", ui)
                self.record("source UI HTTP smoke", True, f"127.0.0.1:{port}")
            finally:
                ui.terminate()
                try:
                    output, _ = ui.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    ui.kill()
                    output, _ = ui.communicate()
                if output:
                    self.lines.append(_sanitized(output, self.root).rstrip())

        manifest = {
            "distribution_version": (self.root / "VERSION").read_text(encoding="utf-8").strip(),
            "product_stage": "public_alpha",
            "artifact": kit["artifact"],
            "artifact_sha256": kit["sha256"],
            "file_count": kit["file_count"],
            "members": kit["members"],
            "checks": self.results,
        }
        (self.output / "BUILD_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (self.output / "SHA256SUMS.txt").write_text(f"{kit['sha256']}  {kit['artifact']}\n", encoding="utf-8")
        (self.output / "RELEASE_NOTES.md").write_text(
            f"# Public Alpha RC {manifest['distribution_version']}\n\n"
            "This artifact was validated in a clean temporary directory.\n\n"
            f"- Tester Kit: `{kit['artifact']}`\n- SHA-256: `{kit['sha256']}`\n"
            "- Runtime dependencies are installed by the caller or CI workflow.\n"
            "- Windows is not part of the first-release support matrix.\n",
            encoding="utf-8",
        )
        (self.output / "release-validation.log").write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        all_ok = all(row["ok"] for row in self.results)
        return {"ok": all_ok, "manifest": manifest, "results": self.results, "output": str(self.output)}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, process: subprocess.Popen[str], timeout: float = 15) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"UI exited early ({process.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"UI did not respond: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and package the Public Alpha RC")
    parser.add_argument("--root", default=str(ROOT), help="source repository root")
    parser.add_argument("--output", default="", help="release output directory")
    parser.add_argument("--python", default=sys.executable, help="Python executable for checks")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    output = Path(args.output).expanduser().resolve() if args.output else root / "release"
    try:
        result = ValidationRun(root, output, args.python).run()
    except Exception as exc:  # CLI must leave a useful failure artifact when possible.
        print(f"FAIL release validation: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
