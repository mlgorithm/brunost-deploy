"""Validated, versioned country topology configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    """The country topology cannot be deployed safely."""


IMAGE_DIGEST = "@sha256:"


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    resource_classes: tuple[str, ...] = ("cpu",)
    queues: tuple[str, ...] = ("default",)
    region: str | None = None
    capabilities: tuple[str, ...] = ()
    replicas: int = 1


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
    platform_replicas: int = 1
    judge_replicas: int = 1
    platform_image: str = "ghcr.io/mlgorithm/brunost-platform:stable"
    judge_image: str = "ghcr.io/mlgorithm/brunost-judge:0.8.0"
    worker_image: str = "ghcr.io/mlgorithm/brunost-judge:0.8.0"
    workers: tuple[WorkerConfig, ...] = field(default_factory=tuple)
    storage: StorageConfig = field(default_factory=StorageConfig)
    tls: bool = True
    monitoring: bool = True
    backup_schedule: str = "0 2 * * *"

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
        platform = raw.get("platform") or {}
        judge = raw.get("judge") or {}
        storage_raw = raw.get("storage") or {}
        workers_raw = raw.get("workers") or []
        if not isinstance(cluster, dict) or not isinstance(platform, dict) or not isinstance(judge, dict):
            raise ConfigError("cluster, platform, and judge must be mappings")
        if not isinstance(workers_raw, list):
            raise ConfigError("workers must be a list")
        workers: list[WorkerConfig] = []
        for item in workers_raw:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise ConfigError("every worker needs a name")
            workers.append(
                WorkerConfig(
                    name=str(item["name"]),
                    resource_classes=tuple(str(value) for value in item.get("resource_classes", ["cpu"])),
                    queues=tuple(str(value) for value in item.get("queues", ["default"])),
                    region=str(item["region"]) if item.get("region") else None,
                    capabilities=tuple(str(value) for value in item.get("capabilities", [])),
                    replicas=int(item.get("replicas", 1)),
                )
            )
        config = cls(
            version=1,
            name=str(cluster.get("name", "brunost-country")),
            public_url=str(cluster.get("public_url", "https://localhost")),
            backend=str(raw.get("backend", "compose")),
            platform_replicas=int(platform.get("replicas", 1)),
            judge_replicas=int(judge.get("replicas", 1)),
            platform_image=str(platform.get("image", "ghcr.io/mlgorithm/brunost-platform:stable")),
            judge_image=str(judge.get("image", "ghcr.io/mlgorithm/brunost-judge:0.8.0")),
            worker_image=str(judge.get("worker_image", judge.get("image", "ghcr.io/mlgorithm/brunost-judge:0.8.0"))),
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
            tls=bool((raw.get("security") or {}).get("tls", True)),
            monitoring=bool((raw.get("observability") or {}).get("monitoring", True)),
            backup_schedule=str((raw.get("backup") or {}).get("schedule", "0 2 * * *")),
        )
        config.validate()
        return config

    def validate(self, *, strict: bool = False) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("cluster.name cannot be empty")
        parsed = urlparse(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("cluster.public_url must be an absolute http(s) URL")
        if self.tls and parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            errors.append("TLS is enabled but cluster.public_url is not HTTPS")
        if self.backend not in {"compose", "k3s"}:
            errors.append("backend must be compose or k3s")
        if self.platform_replicas < 1 or self.judge_replicas < 1:
            errors.append("platform and judge replicas must be positive")
        names = [worker.name for worker in self.workers]
        if len(names) != len(set(names)):
            errors.append("worker names must be unique")
        for worker in self.workers:
            if worker.replicas < 1:
                errors.append(f"worker {worker.name} replicas must be positive")
            if not worker.resource_classes or not worker.queues:
                errors.append(f"worker {worker.name} needs resource_classes and queues")
        if self.storage.postgres not in {"bundled", "external"}:
            errors.append("storage.postgres must be bundled or external")
        if self.storage.postgres == "external" and not self.storage.postgres_url:
            errors.append("storage.postgres_url is required for external PostgreSQL")
        if self.storage.artifacts not in {"minio", "s3", "external"}:
            errors.append("storage.artifacts must be minio, s3, or external")
        if self.storage.artifacts in {"s3", "external"} and not self.storage.artifacts_endpoint:
            errors.append("storage.artifacts_endpoint is required for external object storage")
        if strict and self.backend == "k3s" and self.storage.postgres == "bundled":
            errors.append("K3s production requires external or replicated PostgreSQL")
        if strict and self.backend == "k3s" and self.storage.artifacts not in {"s3", "external"}:
            errors.append("K3s production requires S3-compatible shared artifact storage")
        if strict:
            for label, image in (("platform", self.platform_image), ("judge", self.judge_image), ("worker", self.worker_image)):
                digest = image.rsplit(IMAGE_DIGEST, 1)[-1] if IMAGE_DIGEST in image else ""
                if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
                    errors.append(f"{label}.image must be pinned by a 64-character sha256 digest")
            if self.storage.postgres == "bundled" and "@sha256:" not in self.storage.postgres_image:
                errors.append("storage.postgres_image must be digest-pinned")
            if self.storage.artifacts == "minio":
                for label, image in (("storage.artifacts_image", self.storage.artifacts_image), ("storage.artifacts_init_image", self.storage.artifacts_init_image)):
                    if "@sha256:" not in image:
                        errors.append(f"{label} must be digest-pinned")
        if strict and self.backend == "compose" and (self.platform_replicas > 1 or self.judge_replicas > 1):
            errors.append("Compose replicas require Docker Swarm; use backend: k3s for multi-node HA")
        if errors:
            raise ConfigError("; ".join(errors))
        return errors

    def as_mapping(self) -> dict[str, Any]:
        return {
            "version": 1,
            "cluster": {"name": self.name, "public_url": self.public_url},
            "backend": self.backend,
            "platform": {"replicas": self.platform_replicas, "image": self.platform_image},
            "judge": {"replicas": self.judge_replicas, "image": self.judge_image, "worker_image": self.worker_image},
            "workers": [
                {
                    "name": worker.name,
                    "resource_classes": list(worker.resource_classes),
                    "queues": list(worker.queues),
                    **({"region": worker.region} if worker.region else {}),
                    **({"capabilities": list(worker.capabilities)} if worker.capabilities else {}),
                    **({"replicas": worker.replicas} if worker.replicas != 1 else {}),
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
            "backup": {"schedule": self.backup_schedule},
        }
