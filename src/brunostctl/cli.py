"""The country operator command line interface.

The CLI is intentionally an orchestration layer: it validates topology,
renders immutable deployment artifacts, and calls the existing Judge HTTP
enrollment API. It never asks a country to write application integration code.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from brunostctl.config import ConfigError, CountryConfig
from brunostctl.presets import preset_mapping
from brunostctl.render import helm_values_mapping, render_compose, render_env


def _json_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            decoded = json.loads(body.decode()) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
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


def _config_path(value: str | None) -> Path:
    return Path(value or "brunost.yaml").expanduser().resolve()


def _command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _render(root: Path, config: CountryConfig, *, force: bool = False) -> list[Path]:
    generated = root / ".brunost" / "rendered"
    generated.mkdir(parents=True, exist_ok=True)
    _materialize_chart(root, force=force)
    files = {
        generated / "docker-compose.yml": render_compose(config),
        generated / ".env.example": render_env(config),
        generated / "topology.json": json.dumps(config.as_mapping(), indent=2, sort_keys=True) + "\n",
        generated / "helm-values.yaml": yaml.safe_dump(helm_values_mapping(config), sort_keys=False),
    }
    written: list[Path] = []
    for path, content in files.items():
        if path.exists() and not force:
            # Render output is reproducible; do not overwrite an operator's
            # edited deployment without an explicit flag.
            if path.read_text(encoding="utf-8") != content:
                raise ConfigError(f"render output differs: {path} (use --force to replace it)")
        else:
            path.write_text(content, encoding="utf-8")
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


def _preflight(config: CountryConfig, *, strict: bool, backend: str | None = None, root: Path | None = None) -> list[str]:
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
    missing_nodes = [worker.name for worker in config.workers if not (node_root / "nodes" / f"{worker.name}.json").is_file()]
    if missing_nodes:
        warnings.append("worker credentials not enrolled: " + ", ".join(missing_nodes))
    if config.storage.postgres == "bundled" and selected == "k3s":
        warnings.append("bundled PostgreSQL is not HA; use external or replicated PostgreSQL for country production")
    return warnings


def _run(command: list[str], *, cwd: Path, dry_run: bool) -> int:
    print("$ " + " ".join(command))
    if dry_run:
        return 0
    completed = subprocess.run(command, cwd=cwd, check=False)
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brunostctl", description="Install and operate a distributed Brunost country cluster")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a topology without writing application code")
    init.add_argument("path", nargs="?", type=Path, default=Path("."))
    init.add_argument("--preset", choices=("single", "small", "ha-5-node"), default="single")
    init.add_argument("--name", default="country-2026")
    init.add_argument("--public-url", default="https://contest.example.org")
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

    for name, help_text in (("upgrade", "apply a new immutable image/configuration"), ("rollback", "roll back to a previous release"), ("backup", "run the configured database/artifact backup")):
        lifecycle = sub.add_parser(name, help=help_text)
        lifecycle.add_argument("--config", type=Path, default=Path("brunost.yaml"))
        lifecycle.add_argument("--release", help="release identifier for upgrade/rollback")
        lifecycle.add_argument("--dry-run", action="store_true")

    status = sub.add_parser("status", help="inspect control-plane health")
    status.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    status.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))

    node = sub.add_parser("node", help="enroll, inspect, or revoke a worker node")
    node_sub = node.add_subparsers(dest="node_command", required=True)
    issue = node_sub.add_parser("issue", help="issue a one-time join token")
    issue.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    issue.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
    issue.add_argument("--node-id", required=True)
    issue.add_argument("--worker-id")
    issue.add_argument("--region")
    issue.add_argument("--queue", action="append", default=[])
    issue.add_argument("--resource-class", action="append", dest="resource_classes", default=[])
    issue.add_argument("--capability", action="append", default=[])
    issue.add_argument("--ttl-seconds", default=900, type=int)
    join = node_sub.add_parser("join", help="consume a token and save worker credentials")
    join.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    join.add_argument("--join-token", default=os.environ.get("BRUNOST_JUDGE_JOIN_TOKEN"))
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
    revoke.add_argument("--url", default=os.environ.get("BRUNOST_JUDGE_URL", "http://127.0.0.1:8787"))
    revoke.add_argument("--token", default=os.environ.get("BRUNOST_JUDGE_API_TOKEN"))
    revoke.add_argument("--worker-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            root = args.path.expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            config_path = root / "brunost.yaml"
            if config_path.exists() and not args.force:
                raise ConfigError(f"file already exists: {config_path} (use --force to replace it)")
            mapping = preset_mapping(args.preset, cluster_name=args.name, public_url=args.public_url)
            config_path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
            config = CountryConfig.from_mapping(mapping)
            _render(root, config, force=True)
            (root / "nodes").mkdir(exist_ok=True)
            (root / ".env.example").write_text(render_env(config), encoding="utf-8")
            print(f"created {config_path}")
            print("next: brunostctl preflight && brunostctl install --dry-run")
            return 0
        if args.command == "preflight":
            config = _load_config(args.config, strict=args.strict)
            warnings = _preflight(config, strict=args.strict, root=args.config.parent)
            print(json.dumps({"status": "ok", "cluster": config.name, "backend": config.backend, "warnings": warnings}, indent=2))
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
            warnings = _preflight(config, strict=True, backend=backend, root=args.config.parent)
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
                return _run(["docker", "compose", "--env-file", ".env", "-f", ".brunost/rendered/docker-compose.yml", "up", "-d"], cwd=args.config.parent, dry_run=args.dry_run)
            command = ["helm", "upgrade", "--install", config.name, ".brunost/chart", "--namespace", config.name, "--create-namespace"]
            for worker in config.workers:
                command.extend(["--set-file", f"nodeConfigs.{worker.name}=nodes/{worker.name}.json"])
            command.extend(["-f", ".brunost/rendered/helm-values.yaml"])
            return _run(command, cwd=args.config.parent, dry_run=args.dry_run)
        if args.command in {"upgrade", "rollback", "backup"}:
            _load_dotenv(args.config.parent)
            config = _load_config(args.config, strict=args.command != "backup")
            if args.command == "backup":
                if config.storage.postgres == "external":
                    postgres_url = os.environ.get("BRUNOST_POSTGRES_URL") or config.storage.postgres_url
                    if not postgres_url or postgres_url.startswith("${"):
                        raise ConfigError("set BRUNOST_POSTGRES_URL before backing up external PostgreSQL")
                    command = ["pg_dump", postgres_url, "--format=custom", "--file", f"brunost-{config.name}.dump"]
                else:
                    command = ["docker", "compose", "-f", ".brunost/rendered/docker-compose.yml", "exec", "-T", "postgres", "pg_dump", "-U", "brunost", "brunost"]
            elif args.command == "upgrade":
                _render(args.config.parent, config, force=True)
                if config.backend == "k3s":
                    command = ["helm", "upgrade", config.name, ".brunost/chart", "--namespace", config.name, "-f", ".brunost/rendered/helm-values.yaml"]
                    for worker in config.workers:
                        command.extend(["--set-file", f"nodeConfigs.{worker.name}=nodes/{worker.name}.json"])
                else:
                    command = ["docker", "compose", "--env-file", ".env", "-f", ".brunost/rendered/docker-compose.yml", "up", "-d", "--pull", "always"]
            else:
                if not args.release:
                    raise ConfigError("rollback requires --release")
                command = ["helm", "rollback", config.name, args.release, "--namespace", config.name] if config.backend == "k3s" else ["docker", "compose", "--env-file", ".env", "-f", ".brunost/rendered/docker-compose.yml", "up", "-d"]
            return _run(command, cwd=args.config.parent, dry_run=args.dry_run)
        if args.command == "status":
            result = _json_request(args.url.rstrip("/") + "/healthz", token=args.token)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "node":
            return _node_command(args)
    except (ConfigError, OSError, RuntimeError, ValueError) as exc:
        print(f"brunostctl: {exc}", file=sys.stderr)
        return 2
    return 2


def _node_command(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    if args.node_command == "issue":
        payload = {
            "node_id": args.node_id,
            "worker_id": args.worker_id,
            "role": "worker",
            "region": args.region,
            "queues": args.queue or ["default"],
            "resource_classes": args.resource_classes or ["cpu"],
            "capabilities": args.capability,
            "ttl_seconds": args.ttl_seconds,
        }
        print(json.dumps(_json_request(base + "/v1/nodes/enrollment-tokens", method="POST", token=args.token, payload=payload), indent=2, sort_keys=True))
        return 0
    if args.node_command == "join":
        if not args.join_token:
            raise ConfigError("--join-token or BRUNOST_JUDGE_JOIN_TOKEN is required")
        payload = {
            "join_token": args.join_token,
            "hostname": args.node_id or platform.node(),
            "capabilities": args.capability,
            "resource_classes": args.resource_classes or ["cpu"],
            "metadata": {"region": args.region} if args.region else {},
        }
        response = _json_request(base + "/v1/nodes/enroll", method="POST", payload=payload)
        worker = response.get("worker") or {}
        config = {
            "version": 1,
            "api_url": base,
            "cluster_id": response.get("cluster_id", "local"),
            "node_id": response.get("node_id"),
            "worker_id": args.worker_id or worker.get("worker_id"),
            "worker_token": response.get("worker_token"),
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
        config = json.loads(args.config.expanduser().read_text(encoding="utf-8"))
        health = _json_request(config["api_url"].rstrip("/") + "/healthz")
        worker = _json_request(config["api_url"].rstrip("/") + f"/v1/workers/{config['worker_id']}/status", token=config["worker_token"])
        print(json.dumps({"health": health, "worker": worker}, indent=2, sort_keys=True))
        return 0 if worker.get("status") in {"ready", "busy"} else 1
    if args.node_command == "revoke":
        print(json.dumps(_json_request(base + f"/v1/workers/{args.worker_id}/credential/revoke", method="POST", token=args.token), indent=2, sort_keys=True))
        return 0
    raise ConfigError(f"unknown node command: {args.node_command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
