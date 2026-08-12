"""Render deployment artifacts from a country topology."""

from __future__ import annotations

from typing import Any

import yaml

from brunostctl.config import CountryConfig


def _environment(config: CountryConfig, *, service: str) -> dict[str, str]:
    postgres = (
        "${BRUNOST_POSTGRES_URL}"
        if config.storage.postgres == "external"
        else "postgresql://brunost:${POSTGRES_PASSWORD}@postgres:5432/brunost"
    )
    artifacts_endpoint = config.storage.artifacts_endpoint or "http://minio:9000"
    artifact_backend = "s3" if config.storage.artifacts in {"minio", "s3", "external"} else "filesystem"
    values = {
        "BRUNOST_CLUSTER_NAME": config.name,
        "BRUNOST_JUDGE_DATABASE_URL": postgres,
        "BRUNOST_JUDGE_API_TOKEN": "${BRUNOST_JUDGE_API_TOKEN}",
        "BRUNOST_JUDGE_REQUIRE_API_TOKEN": "true",
        "BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN": "true",
        "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET": "${BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET}",
        "BRUNOST_JUDGE_CLUSTER_ID": config.name,
        "BRUNOST_JUDGE_ARTIFACT_BACKEND": artifact_backend,
        "BRUNOST_JUDGE_ARTIFACT_ENDPOINT": artifacts_endpoint,
        "BRUNOST_JUDGE_ARTIFACT_BUCKET": config.storage.artifacts_bucket,
        "BRUNOST_JUDGE_ARTIFACT_ACCESS_KEY": "${MINIO_ROOT_USER}" if config.storage.artifacts == "minio" else "${BRUNOST_ARTIFACT_ACCESS_KEY}",
        "BRUNOST_JUDGE_ARTIFACT_SECRET_KEY": "${MINIO_ROOT_PASSWORD}" if config.storage.artifacts == "minio" else "${BRUNOST_ARTIFACT_SECRET_KEY}",
        "BRUNOST_JUDGE_CALLBACK_HOSTS": "platform",
    }
    if service == "platform":
        values.update(
            {
                "BRUNOST_JUDGE_URL": "http://judge:8787",
                "BRUNOST_PLATFORM_CALLBACK_SECRET": "${BRUNOST_PLATFORM_CALLBACK_SECRET}",
                "BRUNOST_PLATFORM_PUBLIC_URL": config.public_url,
            }
        )
    return values


def compose_mapping(config: CountryConfig) -> dict[str, Any]:
    services: dict[str, Any] = {
        "platform": {
            "image": config.platform_image,
            "environment": _environment(config, service="platform"),
            "depends_on": ["judge"],
            "restart": "unless-stopped",
            "deploy": {"replicas": config.platform_replicas},
        },
        "judge": {
            "image": config.judge_image,
            "command": ["brunost", "server", "--host", "0.0.0.0", "--port", "8787"],
            "environment": _environment(config, service="judge"),
            "ports": ["8787:8787"],
            "depends_on": ["postgres"] if config.storage.postgres == "bundled" else [],
            "restart": "unless-stopped",
            "deploy": {"replicas": config.judge_replicas},
        },
    }
    if config.storage.postgres == "bundled":
        services["postgres"] = {
            "image": config.storage.postgres_image,
            "environment": {
                "POSTGRES_DB": "brunost",
                "POSTGRES_USER": "brunost",
                "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
            },
            "volumes": ["postgres-data:/var/lib/postgresql/data"],
            "healthcheck": {"test": ["CMD-SHELL", "pg_isready -U brunost -d brunost"], "interval": "5s", "timeout": "5s", "retries": 12},
            "restart": "unless-stopped",
        }
    if config.storage.artifacts == "minio":
        services["minio"] = {
            "image": config.storage.artifacts_image,
            "command": ["server", "/data", "--console-address", ":9001"],
            "environment": {"MINIO_ROOT_USER": "${MINIO_ROOT_USER}", "MINIO_ROOT_PASSWORD": "${MINIO_ROOT_PASSWORD}"},
            "volumes": ["artifact-data:/data"],
            "restart": "unless-stopped",
        }
        services["artifact-init"] = {
            "image": config.storage.artifacts_init_image,
            "entrypoint": ["/bin/sh", "-c"],
            "command": ["mc alias set local http://minio:9000 $${MINIO_ROOT_USER} $${MINIO_ROOT_PASSWORD} && mc mb --ignore-existing local/" + config.storage.artifacts_bucket],
            "environment": {"MINIO_ROOT_USER": "${MINIO_ROOT_USER}", "MINIO_ROOT_PASSWORD": "${MINIO_ROOT_PASSWORD}"},
            "depends_on": ["minio"],
            "restart": "on-failure",
        }
    for worker in config.workers:
        services[f"worker-{worker.name}"] = {
            "image": config.worker_image,
            "command": [
                "brunost",
                "worker",
                "--config",
                "/etc/brunost/node.json",
                "--poll-seconds",
                "1",
            ],
            "environment": {
                **_environment(config, service="worker"),
                "BRUNOST_JUDGE_URL": "http://judge:8787",
                "BRUNOST_WORKER_QUEUES": ",".join(worker.queues),
                "BRUNOST_WORKER_RESOURCE_CLASSES": ",".join(worker.resource_classes),
                "BRUNOST_WORKER_REGION": worker.region or "",
            },
            # The generated Compose file lives in .brunost/rendered; resolve
            # credentials back to the operator-owned project nodes directory.
            "volumes": [f"../../nodes/{worker.name}.json:/etc/brunost/node.json:ro"],
            "depends_on": ["judge"],
            "restart": "unless-stopped",
            "deploy": {"replicas": worker.replicas, "resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}} if "gpu" in worker.resource_classes else {"replicas": worker.replicas},
        }
    volumes: dict[str, Any] = {}
    if config.storage.postgres == "bundled":
        volumes["postgres-data"] = {}
    if config.storage.artifacts == "minio":
        volumes["artifact-data"] = {}
    return {"name": config.name, "services": services, **({"volumes": volumes} if volumes else {})}


def render_compose(config: CountryConfig) -> str:
    return yaml.safe_dump(compose_mapping(config), sort_keys=False, default_flow_style=False)


def render_env(config: CountryConfig) -> str:
    return "\n".join(
        [
            f"BRUNOST_CLUSTER_NAME={config.name}",
            f"BRUNOST_PUBLIC_URL={config.public_url}",
            "BRUNOST_JUDGE_API_TOKEN=replace-with-a-long-random-token",
            "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=replace-with-a-long-random-secret",
            "BRUNOST_PLATFORM_CALLBACK_SECRET=replace-with-a-long-random-secret",
            "POSTGRES_PASSWORD=replace-with-a-long-random-password",
            "MINIO_ROOT_USER=brunost",
            "MINIO_ROOT_PASSWORD=replace-with-a-long-random-password",
            "BRUNOST_POSTGRES_URL=postgresql://user:password@db.example:5432/brunost",
            "",
        ]
    )
