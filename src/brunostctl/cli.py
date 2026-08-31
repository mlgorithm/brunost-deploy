"""The country operator command line interface.

The CLI is intentionally an orchestration layer: it validates topology,
renders immutable deployment artifacts, and calls the existing Judge HTTP
enrollment API. It never asks a country to write application integration code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from brunostctl.config import ConfigError, CountryConfig, is_placeholder, validate_environment
from brunostctl.presets import preset_mapping
from brunostctl.render import helm_values_mapping, render_compose, render_env, synchronization_checks

_SECCOMP_PROFILE_NAME = "brunost-seccomp-v1.json"
_MAX_HTTP_RESPONSE_BYTES = 1_000_000
_RUNTIME_SNAPSHOT_VARIABLES = (
    "BRUNOST_JUDGE_SANDBOX_IMAGE",
    "BRUNOST_JUDGE_SANDBOX_IMAGES",
    "BRUNOST_DOCKER_SOCKET_PROXY_IMAGE",
    "BRUNOST_JUDGE_SANDBOX_RUNTIME",
    "BRUNOST_JUDGE_SANDBOX_SECCOMP",
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Do not forward an operator or worker credential to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_HTTP_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _json_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("HTTP endpoint must be an absolute http(s) URL")
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _HTTP_OPENER.open(request, timeout=15) as response:
            body = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            if len(body) > _MAX_HTTP_RESPONSE_BYTES:
                raise RuntimeError(f"{method} {url} returned a response larger than {_MAX_HTTP_RESPONSE_BYTES} bytes")
            decoded = json.loads(body.decode()) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read(_MAX_HTTP_RESPONSE_BYTES + 1).decode(errors="replace")
        if len(detail) > _MAX_HTTP_RESPONSE_BYTES:
            detail = detail[:_MAX_HTTP_RESPONSE_BYTES] + "…"
        raise RuntimeError(f"{method} {url} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"could not reach {url}: {exc.reason}") from exc
    if not isinstance(decoded, dict):
        raise TypeError(f"{method} {url} returned a non-object response")
    return decoded


def _write_private(path: Path, content: str, *, force: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise ConfigError(f"file already exists: {path} (use --force to replace it)")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_config(path: Path, *, strict: bool = False) -> CountryConfig:
    config = CountryConfig.load(path)
    config.validate(strict=strict)
    return config


def _load_dotenv(root: Path) -> None:
    """Load simple KEY=VALUE pairs without adding a dotenv dependency."""
    path = root / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _judge_url(value: str | None) -> str:
    """Resolve a Judge URL after an operator bundle's environment is loaded."""

    return (value or os.environ.get("BRUNOST_JUDGE_URL") or "http://127.0.0.1:8787").rstrip("/")


def _platform_callback_host(platform_url: str) -> str:
    """Return the hostname that a Judge callback allowlist needs to permit."""

    parsed = urlparse(platform_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("--platform-url must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("--platform-url must not include credentials, a query, or a fragment")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ConfigError("--platform-url must use HTTPS outside local development")
    return parsed.hostname.lower()


def _platform_connection_env(
    config: CountryConfig,
    *,
    platform_url: str,
    judge_url: str | None = None,
    callback_path: str = "/api/judge/callback",
) -> str:
    """Build a non-secret Platform Kit environment template for this Judge.

    The function intentionally never reads or copies the Judge API token or
    callback signing secret.  Operators place those two shared credentials in
    their secret manager, then inject them into the independently deployed
    Platform Kit application.
    """

    host = _platform_callback_host(platform_url)
    if host not in config.callback_hosts:
        allowed = ", ".join(config.callback_hosts)
        raise ConfigError(
            f"platform hostname {host!r} is not in judge.callback_hosts ({allowed}); "
            "declare it in brunost.yaml before connecting the Platform Kit"
        )
    if not callback_path.startswith("/") or ".." in callback_path.split("/"):
        raise ConfigError("--callback-path must be an absolute, normalized path")
    selected_judge_url = (judge_url or config.public_url).rstrip("/")
    parsed_judge = urlparse(selected_judge_url)
    if parsed_judge.scheme not in {"http", "https"} or not parsed_judge.netloc:
        raise ConfigError("--judge-url must be an absolute http(s) URL")
    callback_url = platform_url.rstrip("/") + callback_path
    return "\n".join(
        [
            "# Generated by brunostctl platform-env. This file contains no secrets.",
            "# Inject the two shared Judge credentials from a secret manager; do not copy the Judge .env file.",
            f"BRUNOST_JUDGE_URL={selected_judge_url}",
            "BRUNOST_JUDGE_API_TOKEN=<platform-service-token>",
            "BRUNOST_JUDGE_CALLBACK_SECRET=<same-value-as-BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET>",
            f"BRUNOST_PLATFORM_CALLBACK_URL={callback_url}",
            "BRUNOST_PLATFORM_CALLBACK_TOKEN=<random-platform-callback-token>",
            "BRUNOST_PLATFORM_EDITION=standalone",
            "BRUNOST_PLATFORM_FEATURES=",
            "BRUNOST_PLATFORM_SESSION_COOKIE_SECURE=true",
            "",
        ]
    )


def _config_path(value: str | None) -> Path:
    return Path(value or "brunost.yaml").expanduser().resolve()


def _command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _render(root: Path, config: CountryConfig, *, force: bool = False) -> list[Path]:
    generated = root / ".brunost" / "rendered"
    generated.mkdir(parents=True, exist_ok=True)
    _materialize_chart(root, force=force)
    seccomp_path = _materialize_seccomp_profile(root, force=force)
    files = {
        generated / "docker-compose.yml": render_compose(config),
        generated / ".env.example": render_env(config, seccomp_path=str(seccomp_path)),
        generated / "topology.json": json.dumps(config.as_mapping(), indent=2, sort_keys=True) + "\n",
        generated / "helm-values.yaml": yaml.safe_dump(helm_values_mapping(config), sort_keys=False),
        generated / "release-manifest.json": _release_manifest(config),
    }
    written: list[Path] = []
    for path, content in files.items():
        if path.exists() and not force:
            # Render output is reproducible; do not overwrite an operator's
            # edited deployment without an explicit flag.
            if path.read_text(encoding="utf-8") != content:
                raise ConfigError(f"render output differs: {path} (use --force to replace it)")
        else:
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
        written.append(path)
    return written


def _materialize_chart(root: Path, *, force: bool) -> Path:
    """Put the packaged chart in the operator bundle.

    This keeps ``brunostctl init`` self-contained: a country operator can
    install from a wheel without cloning this repository or knowing where the
    chart source lives.
    """
    destination = root / ".brunost" / "chart"
    if destination.is_dir() and not force:
        return destination
    source = Path(__file__).resolve().parents[2] / "helm" / "brunost"
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return destination
    packaged = resources.files("brunostctl").joinpath("chart", "brunost")
    with resources.as_file(packaged) as unpacked:
        shutil.copytree(unpacked, destination, dirs_exist_ok=True)
    return destination


def _seccomp_profile_path(root: Path) -> Path:
    return root / "security" / _SECCOMP_PROFILE_NAME


def _materialize_seccomp_profile(root: Path, *, force: bool) -> Path:
    """Write the reviewed, versioned Docker seccomp profile into a country bundle."""

    destination = _seccomp_profile_path(root)
    payload = resources.files("brunostctl").joinpath("security", _SECCOMP_PROFILE_NAME).read_bytes()
    if destination.is_file() and destination.read_bytes() == payload:
        return destination.resolve()
    if destination.exists() and not force:
        raise ConfigError(
            f"seccomp profile differs from the bundled {_SECCOMP_PROFILE_NAME}: {destination} (use --force after review)"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    try:
        destination.chmod(0o644)
    except OSError:
        pass
    return destination.resolve()


def _preflight(
    config: CountryConfig,
    *,
    strict: bool,
    backend: str | None = None,
    root: Path | None = None,
    enforce_tools: bool = False,
) -> list[str]:
    config.validate(strict=strict)
    selected = backend or config.backend
    missing: list[str] = []
    for command in ("docker", "docker compose") if selected == "compose" else ("kubectl", "helm"):
        executable = command.split()[0]
        if not _command_available(executable):
            missing.append(executable)
    warnings: list[str] = []
    if missing:
        warnings.append(f"missing local command(s): {', '.join(sorted(set(missing)))}")
    if not config.workers:
        warnings.append("no worker nodes are configured")
    node_root = root or Path.cwd()
    node_problems = _validate_node_files(config, node_root, strict=strict)
    warnings.extend(node_problems)
    env_problems = validate_environment(config, dict(os.environ), strict=strict)
    warnings.extend(env_problems)
    if strict and config.workers:
        seccomp_path = os.environ.get("BRUNOST_JUDGE_SANDBOX_SECCOMP", "")
        if seccomp_path and not is_placeholder(seccomp_path) and not Path(seccomp_path).is_file():
            warnings.append("BRUNOST_JUDGE_SANDBOX_SECCOMP must reference a readable versioned seccomp profile")
    env_file = node_root / ".env"
    if env_file.is_file() and strict and env_file.stat().st_mode & 0o077:
        warnings.append(".env must be mode 0600")
    if config.storage.postgres == "bundled" and selected == "k3s":
        warnings.append("bundled PostgreSQL is not HA; use external or replicated PostgreSQL for country production")
    if strict and (enforce_tools and missing or any(problem for problem in warnings if "missing local command" not in problem)):
        raise ConfigError("preflight failed: " + "; ".join(warnings))
    return warnings


def _run(command: list[str], *, cwd: Path, dry_run: bool) -> int:
    print("$ " + _display_command(command))
    if dry_run:
        return 0
    completed = subprocess.run(command, cwd=cwd, check=False)
    return completed.returncode


def _run_to_file(command: list[str], *, cwd: Path, output: Path, dry_run: bool) -> int:
    print("$ " + _display_command(command) + f" > {output}")
    if dry_run:
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("wb") as stream:
        completed = subprocess.run(command, cwd=cwd, stdout=stream, check=False)
    if completed.returncode == 0:
        temporary.replace(output)
        try:
            output.chmod(0o600)
        except OSError:
            pass
    else:
        temporary.unlink(missing_ok=True)
    return completed.returncode


def _run_with_input(command: list[str], *, cwd: Path, input_path: Path, dry_run: bool) -> int:
    print("$ " + _display_command(command) + f" < {input_path}")
    if dry_run:
        return 0
    with input_path.open("rb") as stream:
        completed = subprocess.run(command, cwd=cwd, stdin=stream, check=False)
    return completed.returncode


def _display_command(command: list[str]) -> str:
    """Render a command for logs without exposing operator secret values."""

    display = " ".join(command)
    for name, value in os.environ.items():
        if value and ("TOKEN" in name or "PASSWORD" in name or "SECRET" in name or name.endswith("_URL")):
            display = display.replace(value, "<redacted>")
    return display


def _compose_check(root: Path, *, dry_run: bool, compose_file: str = ".brunost/rendered/docker-compose.yml") -> int:
    return _run(
        ["docker", "compose", "--env-file", ".env", "-f", compose_file, "config", "-q"],
        cwd=root,
        dry_run=dry_run,
    )


def _helm_check(root: Path, config: CountryConfig, *, dry_run: bool) -> int:
    values_file = ".brunost/rendered/helm-values.yaml"
    lint = _run(["helm", "lint", ".brunost/chart", "-f", values_file], cwd=root, dry_run=dry_run)
    if lint:
        return lint
    command = ["helm", "template", config.name, ".brunost/chart", "--namespace", config.name, "-f", values_file]
    for worker in config.workers:
        command.extend(["--set-file", f"nodeConfigs.{worker.name}=nodes/{worker.name}.json"])
    return _run(command, cwd=root, dry_run=dry_run)


def _config_digest(config: CountryConfig) -> str:
    canonical = json.dumps(config.as_mapping(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _release_manifest(config: CountryConfig) -> str:
    images = sorted({config.judge_image, config.worker_image, config.storage.postgres_image, config.storage.artifacts_image, config.storage.artifacts_init_image})
    return json.dumps(
        {
            "schema_version": 1,
            "cluster": config.name,
            "backend": config.backend,
            "config_sha256": _config_digest(config),
            "images": images,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


def _snapshot_rendered(root: Path, release: str, *, config_path: Path | None = None) -> Path | None:
    rendered = root / ".brunost" / "rendered"
    if not rendered.is_dir():
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", release):
        raise ConfigError("release must contain only letters, numbers, dots, underscores, or hyphens")
    destination = root / ".brunost" / "releases" / release
    destination.mkdir(parents=True, exist_ok=True)
    for source in rendered.iterdir():
        if source.is_file():
            target = destination / source.name
            if target.is_file() and target.read_bytes() != source.read_bytes():
                raise ConfigError(f"release snapshot already exists with different content: {destination}")
            shutil.copy2(source, target)
    config_source = config_path or root / "brunost.yaml"
    if config_source.is_file():
        target = destination / config_source.name
        if target.is_file() and target.read_bytes() != config_source.read_bytes():
            raise ConfigError(f"release snapshot already exists with different configuration: {destination}")
        shutil.copy2(config_source, target)
    # New releases carry the pins recorded after the previous successful
    # install/upgrade.  Older generated bundles do not have this state, so
    # bootstrap one from the current non-secret environment once.
    if not (destination / "runtime.env").is_file():
        _snapshot_runtime_environment(destination)
    return destination


def _snapshot_runtime_environment(destination: Path) -> None:
    """Bootstrap non-secret runtime pins for a legacy rendered release.

    Secrets intentionally remain in the operator's current ``.env``. The
    snapshot contains only image/runtime/profile settings and is safe to retain
    beside rendered release artifacts with the same restrictive permissions.
    """

    values = {
        name: value
        for name in _RUNTIME_SNAPSHOT_VARIABLES
        if (value := os.environ.get(name)) and not is_placeholder(value)
    }
    _write_runtime_environment(destination / "runtime.env", values)


def _record_runtime_environment(root: Path) -> None:
    """Record the pins used by a successfully applied release."""

    values = {
        name: value
        for name in _RUNTIME_SNAPSHOT_VARIABLES
        if (value := os.environ.get(name)) and not is_placeholder(value)
    }
    _write_runtime_environment(root / ".brunost" / "rendered" / "runtime.env", values)


def _write_runtime_environment(target: Path, values: dict[str, str]) -> None:
    content = "".join(f"{name}={values[name]}\n" for name in sorted(values))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass


def _validate_node_files(config: CountryConfig, root: Path, *, strict: bool) -> list[str]:
    problems: list[str] = []
    for worker in config.workers:
        path = root / "nodes" / f"{worker.name}.json"
        if not path.is_file():
            problems.append(f"worker credentials not enrolled: {worker.name}")
            continue
        if strict and path.stat().st_mode & 0o077:
            problems.append(f"worker credential file must be mode 0600: nodes/{worker.name}.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"invalid worker credential file nodes/{worker.name}.json: {exc}")
            continue
        if not isinstance(data, dict) or not data.get("api_url") or not data.get("worker_id") or not data.get("worker_token"):
            problems.append(f"worker credential file is incomplete: nodes/{worker.name}.json")
        else:
            parsed = urlparse(str(data["api_url"]))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                problems.append(f"worker credential file has an invalid api_url: nodes/{worker.name}.json")
    return problems


def _database_environment_problems(config: CountryConfig, values: dict[str, str]) -> list[str]:
    if config.storage.postgres == "bundled":
        return ["POSTGRES_PASSWORD is required for the bundled PostgreSQL backup/restore"] if is_placeholder(values.get("POSTGRES_PASSWORD")) else []
    postgres_url = values.get("BRUNOST_POSTGRES_URL") or config.storage.postgres_url
    if is_placeholder(postgres_url):
        return ["BRUNOST_POSTGRES_URL is required for the external PostgreSQL backup/restore"]
    if not postgres_url.startswith(("postgres://", "postgresql://")):
        return ["BRUNOST_POSTGRES_URL must be a PostgreSQL URL"]
    return []


def _write_backup_metadata(output: Path, config: CountryConfig) -> None:
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(f"{output.name}.sha256")
    manifest = output.with_name(f"{output.name}.manifest.json")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cluster": config.name,
                "backend": config.backend,
                "database": "brunost",
                "dump": output.name,
                "sha256": digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for path in (checksum, manifest):
        try:
            path.chmod(0o600)
        except OSError:
            pass


def _verify_backup_checksum(backup: Path) -> bool:
    checksum = backup.with_name(f"{backup.name}.sha256")
    if not checksum.is_file():
        return True
    fields = checksum.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != backup.name or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
        return False
    return hashlib.sha256(backup.read_bytes()).hexdigest().lower() == fields[0].lower()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunostctl", description="Install and operate a distributed Brunost country cluster")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a topology without writing application code")
    init.add_argument("path", nargs="?", type=Path, default=Path("."))
    init.add_argument("--preset", choices=("single", "small", "ha-5-node"), default="single")
    init.add_argument("--name", default="country-2026")
    init.add_argument("--public-url", default="https://contest.example.org")
    init.add_argument("--platform-url", help="optional Platform Kit URL; its hostname is allowlisted for Judge callbacks")
    init.add_argument("--force", action="store_true")

    preflight = sub.add_parser("preflight", help="validate topology and local infrastructure")
    preflight.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    preflight.add_argument("--strict", action="store_true", help="apply production image and topology checks")

    render = sub.add_parser("render", help="render Compose and environment artifacts")
    render.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    render.add_argument("--force", action="store_true")

    install = sub.add_parser("install", help="render and start the selected backend")
    install.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    install.add_argument("--backend", choices=("compose", "k3s"))
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--force", action="store_true")

    for name, help_text in (("upgrade", "apply the topology's pinned image/configuration"), ("rollback", "roll back to a previous release")):
        lifecycle = sub.add_parser(name, help=help_text)
        lifecycle.add_argument("--config", type=Path, default=Path("brunost.yaml"))
        lifecycle.add_argument("--release", required=True, help="immutable release identifier (or Helm revision when rolling back K3s)")
        lifecycle.add_argument("--dry-run", action="store_true")

    backup = sub.add_parser("backup", help="create a consistent Judge database backup")
    backup.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    backup.add_argument("--output", type=Path, help="backup dump path (default: backups/<cluster>/postgres.dump)")
    backup.add_argument("--dry-run", action="store_true")

    restore = sub.add_parser("restore", help="restore a Judge database backup")
    restore.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    restore.add_argument("--backup", type=Path, required=True, help="custom-format PostgreSQL dump")
    restore.add_argument("--confirm", action="store_true", help="confirm that existing Judge data may be replaced")
    restore.add_argument("--dry-run", action="store_true")

    dr_check = sub.add_parser("dr-check", help="validate a backup and print the disaster-recovery checks")
    dr_check.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    dr_check.add_argument("--backup", type=Path, required=True, help="custom-format PostgreSQL dump to inspect")

    status = sub.add_parser("status", help="inspect Judge readiness")
    status.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL or http://127.0.0.1:8787)")
    status.add_argument("--token", help="Judge API token (default: BRUNOST_JUDGE_API_TOKEN)")

    verify = sub.add_parser("verify", help="read-only verify Platform/Judge synchronization prerequisites")
    verify.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    verify.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL)")
    verify.add_argument("--token", help="Judge API token (default: BRUNOST_JUDGE_API_TOKEN)")
    verify.add_argument("--platform-url", help="optional Platform base URL to check /readyz")
    verify.add_argument("--platform-token", help="optional Platform readiness token")
    # Kept as hidden aliases so existing country runbooks do not break.
    verify.add_argument("--premium-url", dest="platform_url", help=argparse.SUPPRESS)
    verify.add_argument("--premium-token", dest="platform_token", help=argparse.SUPPRESS)

    platform_env = sub.add_parser("platform-env", help="generate a non-secret Platform Kit-to-Judge connection template")
    platform_env.add_argument("--config", type=Path, default=Path("brunost.yaml"))
    platform_env.add_argument("--platform-url", required=True, help="public Platform Kit base URL")
    platform_env.add_argument("--judge-url", help="Judge base URL (default: cluster.public_url)")
    platform_env.add_argument("--callback-path", default="/api/judge/callback")
    platform_env.add_argument("--output", type=Path, help="write the template to this private environment file")
    platform_env.add_argument("--force", action="store_true")

    node = sub.add_parser("node", help="enroll, inspect, or revoke a worker node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    issue = node_sub.add_parser("issue", help="issue a one-time join token")
    issue.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL)")
    issue.add_argument("--token", help="Judge API token (default: BRUNOST_JUDGE_API_TOKEN)")
    issue.add_argument("--topology", type=Path, help="brunost.yaml used to derive a declared worker's queue, class, and grant")
    issue.add_argument("--worker", help="worker name from --topology")
    issue.add_argument("--node-id", required=True)
    issue.add_argument("--worker-id")
    issue.add_argument("--region")
    issue.add_argument("--queue", action="append", default=[])
    issue.add_argument("--resource-class", action="append", dest="resource_classes", default=[])
    issue.add_argument("--capability", action="append", default=[])
    issue.add_argument("--ttl-seconds", default=900, type=int)
    join = node_sub.add_parser("join", help="consume a token and save worker credentials")
    join.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL)")
    join.add_argument("--join-token", help="join token (default: BRUNOST_JUDGE_JOIN_TOKEN)")
    join.add_argument("--node-id")
    join.add_argument("--worker-id")
    join.add_argument("--region")
    join.add_argument("--queue", action="append", default=[])
    join.add_argument("--resource-class", action="append", dest="resource_classes", default=[])
    join.add_argument("--capability", action="append", default=[])
    join.add_argument("--output", type=Path, default=Path("brunost-node.json"))
    join.add_argument("--force", action="store_true")
    doctor = node_sub.add_parser("doctor", help="check one enrolled worker")
    doctor.add_argument("--config", type=Path, default=Path("brunost-node.json"))
    revoke = node_sub.add_parser("revoke", help="revoke one worker credential")
    revoke.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL)")
    revoke.add_argument("--token", help="Judge API token (default: BRUNOST_JUDGE_API_TOKEN)")
    revoke.add_argument("--worker-id", required=True)
    drain = node_sub.add_parser("drain", help="stop assigning work and wait for a worker to finish")
    drain.add_argument("--url", help="Judge base URL (default: BRUNOST_JUDGE_URL)")
    drain.add_argument("--token", help="Judge API token (default: BRUNOST_JUDGE_API_TOKEN)")
    drain.add_argument("--worker-id", required=True)
    drain.add_argument("--timeout-seconds", default=900, type=int)
    drain.add_argument("--poll-seconds", default=5, type=int)
    drain.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "config"):
        args.config = args.config.expanduser().resolve()
    try:
        if args.command == "init":
            root = args.path.expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            config_path = root / "brunost.yaml"
            if config_path.exists() and not args.force:
                raise ConfigError(f"file already exists: {config_path} (use --force to replace it)")
            mapping = preset_mapping(args.preset, cluster_name=args.name, public_url=args.public_url)
            if args.platform_url:
                mapping["judge"]["callback_hosts"] = [_platform_callback_host(args.platform_url)]
            config_path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
            config = CountryConfig.from_mapping(mapping)
            _render(root, config, force=True)
            (root / "nodes").mkdir(exist_ok=True)
            (root / ".env.example").write_text(
                render_env(config, seccomp_path=str(_seccomp_profile_path(root).resolve())), encoding="utf-8"
            )
            print(f"created {config_path}")
            print("next: brunostctl preflight && brunostctl install --dry-run")
            if args.platform_url:
                print(
                    "then: brunostctl platform-env --config "
                    f"{config_path} --platform-url {args.platform_url}"
                )
            return 0
        if args.command == "preflight":
            _load_dotenv(args.config.expanduser().resolve().parent)
            config = _load_config(args.config, strict=args.strict)
            warnings = _preflight(config, strict=args.strict, root=args.config.parent, enforce_tools=args.strict)
            print(json.dumps({"status": "ok" if not warnings else "warning", "cluster": config.name, "backend": config.backend, "warnings": warnings}, indent=2))
            return 0
        if args.command == "render":
            config = _load_config(args.config)
            paths = _render(args.config.parent, config, force=args.force)
            print(json.dumps([str(path) for path in paths], indent=2))
            return 0
        if args.command == "install":
            _load_dotenv(args.config.parent)
            config = _load_config(args.config, strict=True)
            backend = args.backend or config.backend
            if args.backend and args.backend != config.backend:
                raise ConfigError(f"--backend {args.backend} conflicts with backend: {config.backend} in {args.config}")
            warnings = _preflight(config, strict=True, backend=backend, root=args.config.parent, enforce_tools=not args.dry_run)
            missing_nodes = [worker.name for worker in config.workers if not (args.config.parent / "nodes" / f"{worker.name}.json").is_file()]
            if missing_nodes:
                raise ConfigError(
                    "enroll worker nodes before install: "
                    + ", ".join(f"brunostctl node join --output nodes/{name}.json" for name in missing_nodes)
                )
            if warnings:
                print("preflight warnings: " + "; ".join(warnings), file=sys.stderr)
            _render(args.config.parent, config, force=args.force)
            if backend == "compose":
                checked = _compose_check(args.config.parent, dry_run=args.dry_run)
                if checked:
                    return checked
                result = _run(
                    ["docker", "compose", "--env-file", ".env", "-f", ".brunost/rendered/docker-compose.yml", "up", "-d", "--wait"],
                    cwd=args.config.parent,
                    dry_run=args.dry_run,
                )
                if result == 0 and not args.dry_run:
                    _record_runtime_environment(args.config.parent)
                return result
            checked = _helm_check(args.config.parent, config, dry_run=args.dry_run)
            if checked:
                return checked
            command = [
                "helm",
                "upgrade",
                "--install",
                config.name,
                ".brunost/chart",
                "--namespace",
                config.name,
                "--create-namespace",
                "--atomic",
                "--wait",
                "--timeout",
                "10m",
                "--history-max",
                "20",
            ]
            for worker in config.workers:
                command.extend(["--set-file", f"nodeConfigs.{worker.name}=nodes/{worker.name}.json"])
            command.extend(["-f", ".brunost/rendered/helm-values.yaml"])
            result = _run(command, cwd=args.config.parent, dry_run=args.dry_run)
            if result == 0 and not args.dry_run:
                _record_runtime_environment(args.config.parent)
            return result
        if args.command in {"upgrade", "rollback", "backup"}:
            _load_dotenv(args.config.parent)
            config = _load_config(args.config, strict=args.command != "backup")
            if args.command == "backup":
                root = args.config.parent
                output = (args.output or root / "backups" / config.name / "postgres.dump").expanduser()
                if not output.is_absolute():
                    output = root / output
                database_errors = _database_environment_problems(config, dict(os.environ))
                if database_errors:
                    raise ConfigError("backup preflight failed: " + "; ".join(database_errors))
                if config.storage.postgres == "external":
                    if not args.dry_run and not _command_available("pg_dump"):
                        raise ConfigError("pg_dump is required for an external PostgreSQL backup")
                    postgres_url = os.environ.get("BRUNOST_POSTGRES_URL") or config.storage.postgres_url
                    if not postgres_url or postgres_url.startswith("${"):
                        raise ConfigError("set BRUNOST_POSTGRES_URL before backing up external PostgreSQL")
                    command = ["pg_dump", postgres_url, "--format=custom"]
                    result = _run_to_file(command, cwd=root, output=output, dry_run=args.dry_run)
                    if result == 0 and not args.dry_run:
                        _write_backup_metadata(output, config)
                    return result
                else:
                    command = [
                        "docker",
                        "compose",
                        "--env-file",
                        ".env",
                        "-f",
                        ".brunost/rendered/docker-compose.yml",
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "-U",
                        "brunost",
                        "--format=custom",
                        "brunost",
                    ]
                    result = _run_to_file(command, cwd=root, output=output, dry_run=args.dry_run)
                    if result == 0 and not args.dry_run:
                        _write_backup_metadata(output, config)
                    return result
            elif args.command == "upgrade":
                _preflight(config, strict=True, root=args.config.parent, enforce_tools=not args.dry_run)
                _snapshot_rendered(args.config.parent, f"pre-{args.release}", config_path=args.config)
                _render(args.config.parent, config, force=True)
                if config.backend == "k3s":
                    checked = _helm_check(args.config.parent, config, dry_run=args.dry_run)
                    if checked:
                        return checked
                    command = [
                        "helm",
                        "upgrade",
                        config.name,
                        ".brunost/chart",
                        "--namespace",
                        config.name,
                        "--atomic",
                        "--wait",
                        "--timeout",
                        "10m",
                        "--history-max",
                        "20",
                    ]
                    for worker in config.workers:
                        command.extend(["--set-file", f"nodeConfigs.{worker.name}=nodes/{worker.name}.json"])
                    command.extend(["-f", ".brunost/rendered/helm-values.yaml"])
                else:
                    checked = _compose_check(args.config.parent, dry_run=args.dry_run)
                    if checked:
                        return checked
                    command = [
                        "docker",
                        "compose",
                        "--env-file",
                        ".env",
                        "-f",
                        ".brunost/rendered/docker-compose.yml",
                        "up",
                        "-d",
                        "--pull",
                        "always",
                        "--wait",
                    ]
            else:
                if config.backend == "k3s":
                    command = [
                        "helm",
                        "rollback",
                        config.name,
                        args.release,
                        "--namespace",
                        config.name,
                        "--wait",
                        "--cleanup-on-fail",
                        "--timeout",
                        "10m",
                    ]
                else:
                    snapshot = args.config.parent / ".brunost" / "releases" / args.release
                    compose_file = snapshot / "docker-compose.yml"
                    if not compose_file.is_file():
                        raise ConfigError(f"Compose release snapshot does not exist: {snapshot}")
                    runtime_file = snapshot / "runtime.env"
                    if not runtime_file.is_file():
                        raise ConfigError(
                            f"Compose release snapshot is missing runtime pins: {runtime_file}; create a new snapshot before upgrading"
                        )
                    checked = _run(
                        [
                            "docker",
                            "compose",
                            "--project-directory",
                            ".",
                            "--env-file",
                            ".env",
                            "--env-file",
                            str(runtime_file),
                            "-f",
                            str(compose_file),
                            "config",
                            "-q",
                        ],
                        cwd=args.config.parent,
                        dry_run=args.dry_run,
                    )
                    if checked:
                        return checked
                    command = [
                        "docker",
                        "compose",
                        "--project-directory",
                        ".",
                        "--env-file",
                        ".env",
                        "--env-file",
                        str(runtime_file),
                        "-f",
                        str(compose_file),
                        "up",
                        "-d",
                        "--wait",
                    ]
            result = _run(command, cwd=args.config.parent, dry_run=args.dry_run)
            if args.command == "upgrade" and result == 0 and not args.dry_run:
                _record_runtime_environment(args.config.parent)
            return result
        if args.command == "restore":
            root = args.config.parent
            backup = args.backup.expanduser().resolve()
            if not backup.is_file():
                raise ConfigError(f"backup does not exist: {backup}")
            if not _verify_backup_checksum(backup):
                raise ConfigError(f"backup checksum verification failed: {backup}")
            config = _load_config(args.config, strict=True)
            environment_errors = _database_environment_problems(config, dict(os.environ))
            if environment_errors:
                raise ConfigError("restore preflight failed: " + "; ".join(environment_errors))
            if not args.confirm and not args.dry_run:
                raise ConfigError("restore replaces Judge data; repeat with --confirm")
            if config.storage.postgres == "external":
                if not args.dry_run and not _command_available("pg_restore"):
                    raise ConfigError("pg_restore is required for an external PostgreSQL restore")
                postgres_url = os.environ.get("BRUNOST_POSTGRES_URL") or config.storage.postgres_url
                if not postgres_url or postgres_url.startswith("${"):
                    raise ConfigError("set BRUNOST_POSTGRES_URL before restoring external PostgreSQL")
                command = ["pg_restore", "--clean", "--if-exists", "--no-owner", "--exit-on-error", "--dbname", postgres_url, str(backup)]
                return _run(command, cwd=root, dry_run=args.dry_run)
            command = [
                "docker",
                "compose",
                "--env-file",
                ".env",
                "-f",
                ".brunost/rendered/docker-compose.yml",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--exit-on-error",
                "-U",
                "brunost",
                "-d",
                "brunost",
            ]
            return _run_with_input(command, cwd=root, input_path=backup, dry_run=args.dry_run)
        if args.command == "dr-check":
            backup = args.backup.expanduser().resolve()
            if not backup.is_file():
                raise ConfigError(f"backup does not exist: {backup}")
            checks = {
                "backup_file": True,
                "checksum_verified": _verify_backup_checksum(backup),
                "custom_format_dump": _command_available("pg_restore"),
                "restore_command": "brunostctl restore --config " + str(args.config) + " --backup " + str(backup) + " --confirm",
                "runbook": "deployments/nrec-bgo-production/DR.md",
            }
            if checks["custom_format_dump"]:
                result = subprocess.run(["pg_restore", "--list", str(backup)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                checks["custom_format_dump"] = result.returncode == 0
                if result.returncode:
                    checks["error"] = result.stderr.decode(errors="replace").strip()
            print(json.dumps({"status": "ok" if all(value is True for key, value in checks.items() if key not in {"restore_command", "runbook"}) else "failed", "checks": checks}, indent=2, sort_keys=True))
            return 0 if checks["custom_format_dump"] else 2
        if args.command == "verify":
            _load_dotenv(args.config.parent)
            config = _load_config(args.config, strict=True)
            judge_token = args.token or os.environ.get("BRUNOST_JUDGE_API_TOKEN")
            platform_token = args.platform_token or os.environ.get("BRUNOST_PLATFORM_API_TOKEN") or os.environ.get("BRUNOST_PREMIUM_API_TOKEN")
            checks = synchronization_checks(config)
            dispatcher_checks = checks["callback_dispatcher"]
            failed_checks = [name for name, passed in dispatcher_checks.items() if not passed]
            if failed_checks:
                raise ConfigError("callback synchronization configuration failed: " + ", ".join(failed_checks))
            result: dict[str, Any] = {
                "status": "ok",
                "cluster": config.name,
                "synchronization": checks,
                "judge": {
                    "url": _judge_url(args.url),
                    "ready": _json_request(_judge_url(args.url) + "/readyz", token=judge_token),
                },
            }
            if args.platform_url:
                result["platform"] = {
                    "url": args.platform_url.rstrip("/"),
                    "ready": _json_request(args.platform_url.rstrip("/") + "/readyz", token=platform_token),
                }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "platform-env":
            config = _load_config(args.config, strict=False)
            template = _platform_connection_env(
                config,
                platform_url=args.platform_url,
                judge_url=args.judge_url,
                callback_path=args.callback_path,
            )
            if args.output:
                _write_private(args.output.expanduser().resolve(), template, force=args.force)
                print(f"wrote {args.output.expanduser().resolve()}")
            else:
                print(template, end="")
            return 0
        if args.command == "status":
            result = _json_request(_judge_url(args.url) + "/readyz", token=args.token or os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "node":
            return _node_command(args)
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"brunostctl: {exc}", file=sys.stderr)
        return 2
    return 2


def _node_command(args: argparse.Namespace) -> int:
    _load_dotenv(Path.cwd())
    if args.node_command == "issue":
        if args.ttl_seconds < 1:
            raise ConfigError("--ttl-seconds must be positive")
        queues = args.queue or ["default"]
        resource_classes = args.resource_classes or ["cpu"]
        capabilities = list(args.capability)
        worker_id = args.worker_id
        region = args.region
        if args.topology or args.worker:
            if not args.topology or not args.worker:
                raise ConfigError("--topology and --worker must be used together")
            topology_path = args.topology.expanduser().resolve()
            _load_dotenv(topology_path.parent)
            topology = _load_config(topology_path, strict=True)
            configured_worker = next((item for item in topology.workers if item.name == args.worker), None)
            if configured_worker is None:
                raise ConfigError(f"worker {args.worker!r} is not declared in {topology_path}")
            queues = args.queue or list(configured_worker.queues)
            resource_classes = args.resource_classes or list(configured_worker.resource_classes)
            capabilities = sorted(
                set(capabilities)
                | set(configured_worker.capabilities)
                | _runtime_capability_grant()
                | ({"gpu:true"} if "gpu" in resource_classes else set())
            )
            worker_id = worker_id or configured_worker.name
            region = region or configured_worker.region
        base = _judge_url(args.url)
        token = args.token or os.environ.get("BRUNOST_JUDGE_API_TOKEN")
        payload = {
            "node_id": args.node_id,
            "worker_id": worker_id,
            "role": "worker",
            "region": region,
            "queues": queues,
            "resource_classes": resource_classes,
            "capabilities": capabilities,
            "ttl_seconds": args.ttl_seconds,
        }
        print(json.dumps(_json_request(base + "/v1/nodes/enrollment-tokens", method="POST", token=token, payload=payload), indent=2, sort_keys=True))
        return 0
    if args.node_command == "join":
        join_token = args.join_token or os.environ.get("BRUNOST_JUDGE_JOIN_TOKEN")
        if not join_token:
            raise ConfigError("--join-token or BRUNOST_JUDGE_JOIN_TOKEN is required")
        base = _judge_url(args.url)
        payload = {
            "join_token": join_token,
            "hostname": args.node_id or platform.node(),
            "metadata": {"region": args.region} if args.region else {},
        }
        # Omit undeclared inventory fields rather than turning a scoped grant
        # into an empty capability/resource set. The Judge returns the granted
        # inventory in the persisted node configuration.
        if args.capability:
            payload["capabilities"] = args.capability
        if args.resource_classes:
            payload["resource_classes"] = args.resource_classes
        response = _json_request(base + "/v1/nodes/enroll", method="POST", payload=payload)
        worker = response.get("worker") or {}
        config = {
            "version": 1,
            "api_url": base,
            "cluster_id": response.get("cluster_id", "local"),
            "node_id": response.get("node_id"),
            "worker_id": args.worker_id or worker.get("worker_id"),
            "worker_token": response.get("worker_token"),
            "capabilities": worker.get("capabilities", []),
            "queues": args.queue or worker.get("queues", ["default"]),
            "resource_classes": args.resource_classes or worker.get("resource_classes", ["cpu"]),
            "region": args.region,
            "path_map": [],
        }
        if not config["worker_id"] or not config["worker_token"]:
            raise RuntimeError("control plane returned incomplete worker credentials")
        _write_private(args.output.expanduser().resolve(), json.dumps(config, indent=2, sort_keys=True) + "\n", force=args.force)
        print(f"worker joined: {config['worker_id']}")
        print(f"credentials saved: {args.output.expanduser().resolve()}")
        return 0
    if args.node_command == "doctor":
        config_path = args.config.expanduser().resolve()
        if config_path.stat().st_mode & 0o077:
            raise ConfigError(f"worker credential file must be mode 0600: {config_path}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        required = ("api_url", "worker_id", "worker_token")
        if not isinstance(config, dict) or any(not config.get(name) for name in required):
            raise ConfigError(f"worker credential file is incomplete: {config_path}")
        parsed = urlparse(str(config["api_url"]))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"worker credential file has an invalid api_url: {config_path}")
        health = _json_request(config["api_url"].rstrip("/") + "/healthz")
        worker = _json_request(config["api_url"].rstrip("/") + f"/v1/workers/{config['worker_id']}/status", token=config["worker_token"])
        print(json.dumps({"health": health, "worker": worker}, indent=2, sort_keys=True))
        return 0 if worker.get("status") in {"ready", "busy"} else 1
    if args.node_command == "revoke":
        base = _judge_url(args.url)
        print(json.dumps(_json_request(base + f"/v1/workers/{args.worker_id}/credential/revoke", method="POST", token=args.token or os.environ.get("BRUNOST_JUDGE_API_TOKEN")), indent=2, sort_keys=True))
        return 0
    if args.node_command == "drain":
        if args.timeout_seconds < 1 or args.poll_seconds < 1:
            raise ConfigError("drain timeout and poll intervals must be positive")
        base = _judge_url(args.url)
        response = _json_request(
            base + f"/v1/workers/{args.worker_id}/drain",
            method="POST",
            token=args.token or os.environ.get("BRUNOST_JUDGE_API_TOKEN"),
            payload={"timeout_seconds": args.timeout_seconds},
        )
        if not args.wait:
            print(json.dumps(response, indent=2, sort_keys=True))
            return 0
        deadline = time.monotonic() + args.timeout_seconds
        latest = response
        while time.monotonic() < deadline:
            status = str(latest.get("status", "")).lower()
            if status in {"drained", "idle", "offline", "stopped"} or latest.get("drained") is True:
                print(json.dumps(latest, indent=2, sort_keys=True))
                return 0
            time.sleep(args.poll_seconds)
            latest = _json_request(base + f"/v1/workers/{args.worker_id}/status", token=args.token)
        raise RuntimeError(f"worker {args.worker_id} did not drain within {args.timeout_seconds} seconds")
    raise ConfigError(f"unknown node command: {args.node_command}")


def _runtime_capability_grant() -> set[str]:
    """Return the Judge runtime labels a deployed remote worker will report."""

    capabilities = {"runtime:python-3.13"}
    raw_images = os.environ.get("BRUNOST_JUDGE_SANDBOX_IMAGES", "").strip()
    if raw_images.startswith("{"):
        try:
            mapping = json.loads(raw_images)
        except json.JSONDecodeError as exc:
            raise ConfigError("BRUNOST_JUDGE_SANDBOX_IMAGES must be valid JSON") from exc
        if isinstance(mapping, dict):
            capabilities.update(f"runtime:{name}" for name in mapping if isinstance(name, str) and name.strip())
    return capabilities


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
