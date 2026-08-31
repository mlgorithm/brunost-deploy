"""Validated, versioned country topology configuration."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """The country topology cannot be deployed safely."""


IMAGE_DIGEST = "@sha256:"
_DIGEST_IMAGE_RE = re.compile(r".+@sha256:[0-9a-fA-F]{64}$")
_PLACEHOLDER_MARKERS = ("replace-me", "replace-with-", "<64-hex-digest>", "${")


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name} must be an integer") from exc


def _as_bool(value: Any, field_name: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}:
        return True
    if isinstance(value, str) and value.strip().lower() in {"false", "no", "0"}:
        return False
    raise ConfigError(f"{field_name} must be a boolean")


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    resource_classes: tuple[str, ...] = ("cpu",)
    queues: tuple[str, ...] = ("default",)
    region: str | None = None
    capabilities: tuple[str, ...] = ()
    node_selector: dict[str, str] = field(default_factory=dict)
    replicas: int = 1
    autoscaling: bool = False
    min_replicas: int = 1
    max_replicas: int = 10
    target_cpu_utilization: int = 70


@dataclass(frozen=True)
class StorageConfig:
    postgres: str = "bundled"
    postgres_url: str | None = None
    artifacts: str = "minio"
    artifacts_endpoint: str | None = None
    artifacts_bucket: str = "brunost-artifacts"
    postgres_image: str = "postgres:16-alpine"
    artifacts_image: str = "minio/minio@sha256:<64-hex-digest>"
    artifacts_init_image: str = "minio/mc@sha256:<64-hex-digest>"


@dataclass(frozen=True)
class CountryConfig:
    version: int
    name: str
    public_url: str
    backend: str = "compose"
    judge_replicas: int = 1
    judge_image: str = "ghcr.io/mlgorithm/brunost-judge:0.8.0"
    worker_image: str = "ghcr.io/mlgorithm/brunost-judge:0.8.0"
    # This is deliberately a generic application placeholder.  A Judge
    # deployment may serve Platform Kit, Premium, or another HTTP client;
    # strict preflight requires the operator to replace it with that
    # application's real callback hostname.
    callback_hosts: tuple[str, ...] = ("platform",)
    workers: tuple[WorkerConfig, ...] = field(default_factory=tuple)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tls: bool = True
    monitoring: bool = True
    backup_schedule: str = "0 2 * * *"
    backup_retention_days: int = 30
    worker_drain_timeout_seconds: int = 900
    worker_termination_grace_seconds: int = 900

    @classmethod
    def load(cls, path: str | Path) -> CountryConfig:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ConfigError(f"configuration file does not exist: {source}")
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> CountryConfig:
        if not isinstance(raw, dict):
            raise ConfigError("configuration must be a YAML mapping")
        if raw.get("version") != 1:
            raise ConfigError("version: 1 is required")
        cluster = raw.get("cluster") or {}
        judge = raw.get("judge") or {}
        storage_raw = raw.get("storage") or {}
        workers_raw = raw.get("workers") or []
        operations_raw = raw.get("operations") or {}
        for name, value in (("cluster", cluster), ("judge", judge), ("storage", storage_raw)):
            if not isinstance(value, dict):
                raise ConfigError(f"{name} must be a mapping")
        if not isinstance(operations_raw, dict):
            raise ConfigError("operations must be a mapping")
        if "platform" in raw:
            raise ConfigError("the deployment layer manages Judge and workers only; remove the obsolete platform section")
        for name in ("security", "observability", "backup", "integrations"):
            if raw.get(name) is not None and not isinstance(raw[name], dict):
                raise ConfigError(f"{name} must be a mapping")
        if not isinstance(workers_raw, list):
            raise ConfigError("workers must be a list")
        workers: list[WorkerConfig] = []
        for item in workers_raw:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise ConfigError("every worker needs a name")
            autoscaling = item.get("autoscaling") or {}
            if not isinstance(autoscaling, dict):
                raise ConfigError(f"worker {item['name']} autoscaling must be a mapping")
            node_selector = item.get("node_selector") or {}
            if not isinstance(node_selector, dict) or any(
                not str(key).strip() or not str(value).strip() for key, value in node_selector.items()
            ):
                raise ConfigError(f"worker {item['name']} node_selector must be a non-empty string mapping")
            workers.append(
                WorkerConfig(
                    name=str(item["name"]),
                    resource_classes=tuple(str(value) for value in item.get("resource_classes", ["cpu"])),
                    queues=tuple(str(value) for value in item.get("queues", ["default"])),
                    region=str(item["region"]) if item.get("region") else None,
                    capabilities=tuple(str(value) for value in item.get("capabilities", [])),
                    node_selector={str(key): str(value) for key, value in node_selector.items()},
                    replicas=_as_int(item.get("replicas", 1), f"worker {item['name']} replicas"),
                    autoscaling=_as_bool(autoscaling.get("enabled"), f"worker {item['name']} autoscaling.enabled", default=False),
                    min_replicas=_as_int(autoscaling.get("min_replicas", item.get("replicas", 1)), f"worker {item['name']} autoscaling.min_replicas"),
                    max_replicas=_as_int(autoscaling.get("max_replicas", max(10, _as_int(item.get("replicas", 1), f"worker {item['name']} replicas"))), f"worker {item['name']} autoscaling.max_replicas"),
                    target_cpu_utilization=_as_int(autoscaling.get("target_cpu_utilization", 70), f"worker {item['name']} autoscaling.target_cpu_utilization"),
                )
            )
        callback_hosts = judge.get("callback_hosts", (raw.get("integrations") or {}).get("judge_callback_hosts", ["platform"]))
        if not isinstance(callback_hosts, (list, tuple)):
            raise ConfigError("judge.callback_hosts must be a list")
        config = cls(
            version=1,
            name=str(cluster.get("name", "brunost-country")),
            public_url=str(cluster.get("public_url", "https://localhost")),
            backend=str(raw.get("backend", "compose")),
            judge_replicas=_as_int(judge.get("replicas", 1), "judge.replicas"),
            judge_image=str(judge.get("image", "ghcr.io/mlgorithm/brunost-judge:0.8.0")),
            worker_image=str(judge.get("worker_image", judge.get("image", "ghcr.io/mlgorithm/brunost-judge:0.8.0"))),
            callback_hosts=tuple(str(value).strip().lower() for value in callback_hosts if str(value).strip()),
            workers=tuple(workers),
            storage=StorageConfig(
                postgres=str(storage_raw.get("postgres", "bundled")),
                postgres_url=str(storage_raw["postgres_url"]) if storage_raw.get("postgres_url") else None,
                artifacts=str(storage_raw.get("artifacts", "minio")),
                artifacts_endpoint=str(storage_raw["artifacts_endpoint"]) if storage_raw.get("artifacts_endpoint") else None,
                artifacts_bucket=str(storage_raw.get("artifacts_bucket", "brunost-artifacts")),
                postgres_image=str(storage_raw.get("postgres_image", "postgres:16-alpine")),
                artifacts_image=str(storage_raw.get("artifacts_image", "minio/minio@sha256:<64-hex-digest>")),
                artifacts_init_image=str(storage_raw.get("artifacts_init_image", "minio/mc@sha256:<64-hex-digest>")),
            ),
            tls=_as_bool((raw.get("security") or {}).get("tls"), "security.tls", default=True),
            monitoring=_as_bool((raw.get("observability") or {}).get("monitoring"), "observability.monitoring", default=True),
            backup_schedule=str((raw.get("backup") or {}).get("schedule", "0 2 * * *")),
            backup_retention_days=_as_int((raw.get("backup") or {}).get("retention_days", 30), "backup.retention_days"),
            worker_drain_timeout_seconds=_as_int(operations_raw.get("worker_drain_timeout_seconds", 900), "operations.worker_drain_timeout_seconds"),
            worker_termination_grace_seconds=_as_int(operations_raw.get("worker_termination_grace_seconds", 900), "operations.worker_termination_grace_seconds"),
        )
        config.validate()
        return config

    def validate(self, *, strict: bool = False) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("cluster.name cannot be empty")
        elif len(self.name) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", self.name):
            errors.append("cluster.name must be a lowercase DNS-safe name")
        parsed = urlparse(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("cluster.public_url must be an absolute http(s) URL")
        if self.tls and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            errors.append("TLS is enabled but cluster.public_url is not HTTPS")
        if self.backend not in {"compose", "k3s"}:
            errors.append("backend must be compose or k3s")
        if self.judge_replicas < 1:
            errors.append("judge replicas must be positive")
        if not self.callback_hosts:
            errors.append("judge.callback_hosts must contain at least one callback hostname")
        for host in self.callback_hosts:
            if "/" in host or ":" in host or any(character.isspace() for character in host):
                errors.append(f"judge callback host {host!r} must be a hostname, not a URL")
        names = [worker.name for worker in self.workers]
        if len(names) != len(set(names)):
            errors.append("worker names must be unique")
        for worker in self.workers:
            if len(worker.name) > 63 or worker.name != worker.name.lower() or not worker.name.replace("-", "").isalnum() or worker.name.startswith("-") or worker.name.endswith("-"):
                errors.append(f"worker {worker.name!r} must be a DNS-safe name")
        for worker in self.workers:
            if worker.replicas < 1:
                errors.append(f"worker {worker.name} replicas must be positive")
            if not worker.resource_classes or not worker.queues or any(not value.strip() for value in worker.resource_classes + worker.queues):
                errors.append(f"worker {worker.name} needs resource_classes and queues")
            if worker.min_replicas < 1 or worker.max_replicas < 1 or worker.min_replicas > worker.max_replicas:
                errors.append(f"worker {worker.name} autoscaling replica bounds are invalid")
            if worker.autoscaling and not worker.min_replicas <= worker.replicas <= worker.max_replicas:
                errors.append(f"worker {worker.name} replicas must be within autoscaling bounds")
            if not 1 <= worker.target_cpu_utilization <= 100:
                errors.append(f"worker {worker.name} target_cpu_utilization must be between 1 and 100")
            if strict and self.backend == "k3s" and not worker.node_selector:
                errors.append(f"worker {worker.name} needs node_selector for K3s capability placement")
        if self.storage.postgres not in {"bundled", "external"}:
            errors.append("storage.postgres must be bundled or external")
        if self.storage.postgres == "external" and not self.storage.postgres_url:
            errors.append("storage.postgres_url is required for external PostgreSQL")
        if self.storage.artifacts not in {"minio", "s3", "external"}:
            errors.append("storage.artifacts must be minio, s3, or external")
        if self.storage.artifacts in {"s3", "external"} and not self.storage.artifacts_endpoint:
            errors.append("storage.artifacts_endpoint is required for external object storage")
        if self.backup_retention_days < 1:
            errors.append("backup.retention_days must be positive")
        if self.worker_drain_timeout_seconds < 1:
            errors.append("operations.worker_drain_timeout_seconds must be positive")
        if self.worker_termination_grace_seconds < 1:
            errors.append("operations.worker_termination_grace_seconds must be positive")
        if self.storage.artifacts_bucket.strip() != self.storage.artifacts_bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.storage.artifacts_bucket):
            errors.append("storage.artifacts_bucket must be a DNS-compatible object-store bucket name")
        if strict and self.backend == "k3s" and self.storage.postgres == "bundled":
            errors.append("K3s production requires external or replicated PostgreSQL")
        if strict and self.backend == "k3s" and self.storage.artifacts not in {"s3", "external"}:
            errors.append("K3s production requires S3-compatible shared artifact storage")
        if strict and self.callback_hosts in {("platform",), ("premium",)}:
            errors.append("judge.callback_hosts must be replaced with the real Platform callback hostname")
        if strict:
            for label, image in (("judge", self.judge_image), ("worker", self.worker_image)):
                if not _DIGEST_IMAGE_RE.fullmatch(image):
                    errors.append(f"{label}.image must be pinned by a 64-character sha256 digest")
            if self.storage.postgres == "bundled" and not _DIGEST_IMAGE_RE.fullmatch(self.storage.postgres_image):
                errors.append("storage.postgres_image must be digest-pinned")
            if self.storage.artifacts == "minio":
                for label, image in (("storage.artifacts_image", self.storage.artifacts_image), ("storage.artifacts_init_image", self.storage.artifacts_init_image)):
                    if not _DIGEST_IMAGE_RE.fullmatch(image):
                        errors.append(f"{label} must be digest-pinned")
        if strict and self.backend == "compose" and self.judge_replicas > 1:
            errors.append("Compose replicas require Docker Swarm; use backend: k3s for multi-node HA")
        if errors:
            raise ConfigError("; ".join(errors))
        return errors

    def as_mapping(self) -> dict[str, Any]:
        return {
            "version": 1,
            "cluster": {"name": self.name, "public_url": self.public_url},
            "backend": self.backend,
            "judge": {
                "replicas": self.judge_replicas,
                "image": self.judge_image,
                "worker_image": self.worker_image,
                "callback_hosts": list(self.callback_hosts),
            },
            "workers": [
                {
                    "name": worker.name,
                    "resource_classes": list(worker.resource_classes),
                    "queues": list(worker.queues),
                    **({"region": worker.region} if worker.region else {}),
                    **({"capabilities": list(worker.capabilities)} if worker.capabilities else {}),
                    **({"node_selector": worker.node_selector} if worker.node_selector else {}),
                    **({"replicas": worker.replicas} if worker.replicas != 1 else {}),
                    **(
                        {
                            "autoscaling": {
                                "enabled": worker.autoscaling,
                                "min_replicas": worker.min_replicas,
                                "max_replicas": worker.max_replicas,
                                "target_cpu_utilization": worker.target_cpu_utilization,
                            }
                        }
                        if worker.autoscaling
                        else {}
                    ),
                }
                for worker in self.workers
            ],
            "storage": {
                "postgres": self.storage.postgres,
                **({"postgres_url": self.storage.postgres_url} if self.storage.postgres_url else {}),
                "artifacts": self.storage.artifacts,
                **({"artifacts_endpoint": self.storage.artifacts_endpoint} if self.storage.artifacts_endpoint else {}),
                "artifacts_bucket": self.storage.artifacts_bucket,
                "postgres_image": self.storage.postgres_image,
                "artifacts_image": self.storage.artifacts_image,
                "artifacts_init_image": self.storage.artifacts_init_image,
            },
            "security": {"tls": self.tls},
            "observability": {"monitoring": self.monitoring},
            "backup": {"schedule": self.backup_schedule, "retention_days": self.backup_retention_days},
            "operations": {
                "worker_drain_timeout_seconds": self.worker_drain_timeout_seconds,
                "worker_termination_grace_seconds": self.worker_termination_grace_seconds,
            },
        }


def is_digest_pinned_image(value: str) -> bool:
    """Return whether an image reference ends in an immutable sha256 digest."""

    return bool(_DIGEST_IMAGE_RE.fullmatch(value.strip()))


def is_placeholder(value: str | None) -> bool:
    """Return whether an operator value is still an example or interpolation."""

    if value is None:
        return True
    normalized = value.strip().lower()
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def required_environment(config: CountryConfig) -> dict[str, str]:
    """Return the secret/configuration variables required to run this topology."""

    required = {
        "BRUNOST_JUDGE_API_TOKEN": "Judge API authentication",
        "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET": "signed callback authentication",
    }
    if config.workers:
        required.update(
            {
                "BRUNOST_DOCKER_SOCKET_PROXY_IMAGE": "restricted Docker socket proxy image",
                "BRUNOST_JUDGE_SANDBOX_IMAGE": "sandbox image",
                "BRUNOST_JUDGE_SANDBOX_IMAGES": "allowed sandbox image set",
                "BRUNOST_JUDGE_SANDBOX_RUNTIME": "sandbox runtime",
                "BRUNOST_JUDGE_SANDBOX_SECCOMP": "sandbox seccomp profile path",
            }
        )
    if config.storage.postgres == "bundled":
        required["POSTGRES_PASSWORD"] = "bundled PostgreSQL password"
    else:
        required["BRUNOST_POSTGRES_URL"] = "external PostgreSQL connection URL"
    if config.storage.artifacts == "minio":
        required.update(
            {
                "MINIO_ROOT_USER": "bundled object storage access key",
                "MINIO_ROOT_PASSWORD": "bundled object storage secret",
            }
        )
    else:
        required.update(
            {
                "BRUNOST_ARTIFACT_ENDPOINT": "object storage endpoint",
                "BRUNOST_ARTIFACT_ACCESS_KEY": "object storage access key",
                "BRUNOST_ARTIFACT_SECRET_KEY": "object storage secret",
            }
        )
    return required


def validate_environment(config: CountryConfig, values: dict[str, str], *, strict: bool = False) -> list[str]:
    """Validate operator-provided environment without including secret values in errors."""

    errors: list[str] = []
    for name, description in required_environment(config).items():
        value = values.get(name)
        if is_placeholder(value):
            errors.append(f"{name} is required ({description})")
    if config.workers:
        runtime = values.get("BRUNOST_JUDGE_SANDBOX_RUNTIME", "")
        if runtime and runtime not in {"runsc", "kata-runtime"}:
            errors.append("BRUNOST_JUDGE_SANDBOX_RUNTIME must be runsc or kata-runtime")
        seccomp_path = values.get("BRUNOST_JUDGE_SANDBOX_SECCOMP", "")
        if seccomp_path and not seccomp_path.startswith("/"):
            errors.append("BRUNOST_JUDGE_SANDBOX_SECCOMP must be an absolute path")
        for name in ("BRUNOST_DOCKER_SOCKET_PROXY_IMAGE", "BRUNOST_JUDGE_SANDBOX_IMAGE"):
            if values.get(name) and not is_digest_pinned_image(values[name]):
                errors.append(f"{name} must be pinned by a 64-character sha256 digest")
        raw_sandbox_images = values.get("BRUNOST_JUDGE_SANDBOX_IMAGES", "").strip()
        try:
            parsed_sandbox_images = json.loads(raw_sandbox_images) if raw_sandbox_images.startswith("{") else None
        except json.JSONDecodeError:
            parsed_sandbox_images = None
            errors.append("BRUNOST_JUDGE_SANDBOX_IMAGES must be valid JSON when configured as a mapping")
        if isinstance(parsed_sandbox_images, dict):
            sandbox_images = [str(item).strip() for item in parsed_sandbox_images.values() if str(item).strip()]
            if not sandbox_images:
                errors.append("BRUNOST_JUDGE_SANDBOX_IMAGES must contain at least one image")
        else:
            sandbox_images = [item.strip() for item in raw_sandbox_images.split(",") if item.strip()]
        if sandbox_images and any(not is_digest_pinned_image(item) for item in sandbox_images):
            errors.append("BRUNOST_JUDGE_SANDBOX_IMAGES entries must be pinned by sha256 digests")
    if strict and config.storage.postgres == "external":
        postgres_url = values.get("BRUNOST_POSTGRES_URL", "")
        if postgres_url and not postgres_url.startswith(("postgres://", "postgresql://")):
            errors.append("BRUNOST_POSTGRES_URL must be a PostgreSQL URL")
    return errors
