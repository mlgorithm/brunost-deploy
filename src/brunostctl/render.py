"""Render deployment artifacts from a country topology."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import yaml

from brunostctl.config import CountryConfig


def _environment(config: CountryConfig, *, control_plane: bool) -> dict[str, str]:
    artifacts_endpoint = config.storage.artifacts_endpoint or "http://minio:9000"
    artifact_backend = "s3" if config.storage.artifacts in {"minio", "s3", "external"} else "filesystem"
    values = {
        "BRUNOST_CLUSTER_NAME": config.name,
        "BRUNOST_JUDGE_REQUIRE_WORKER_TOKEN": "true",
        "BRUNOST_JUDGE_ENV": "production",
        "BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS": "true",
        "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET": "${BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET}",
        "BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS": "true",
        "BRUNOST_JUDGE_CALLBACK_HOSTS": ",".join(config.callback_hosts),
        "BRUNOST_JUDGE_CLUSTER_ID": config.name,
        "BRUNOST_JUDGE_ARTIFACT_BACKEND": artifact_backend,
        "BRUNOST_JUDGE_ARTIFACT_ENDPOINT": artifacts_endpoint,
        "BRUNOST_JUDGE_ARTIFACT_BUCKET": config.storage.artifacts_bucket,
        "BRUNOST_JUDGE_ARTIFACT_ACCESS_KEY": "${MINIO_ROOT_USER}" if config.storage.artifacts == "minio" else "${BRUNOST_ARTIFACT_ACCESS_KEY}",
        "BRUNOST_JUDGE_ARTIFACT_SECRET_KEY": "${MINIO_ROOT_PASSWORD}" if config.storage.artifacts == "minio" else "${BRUNOST_ARTIFACT_SECRET_KEY}",
    }
    if control_plane:
        postgres = (
            "${BRUNOST_POSTGRES_URL}"
            if config.storage.postgres == "external"
            else "postgresql://brunost:${POSTGRES_PASSWORD}@postgres:5432/brunost"
        )
        values.update(
            {
                "BRUNOST_JUDGE_DATABASE_URL": postgres,
                "BRUNOST_JUDGE_API_TOKEN": "${BRUNOST_JUDGE_API_TOKEN}",
                "BRUNOST_JUDGE_REQUIRE_API_TOKEN": "true",
                "BRUNOST_JUDGE_REQUIRE_IDEMPOTENCY_HEADER": "true",
                "BRUNOST_JUDGE_CALLBACK_HOSTS": ",".join(config.callback_hosts),
            }
        )
    return values


def compose_mapping(config: CountryConfig) -> dict[str, Any]:
    services: dict[str, Any] = {
        "judge": {
            "image": config.judge_image,
            # The Judge image declares ``brunost`` as its entrypoint.
            "command": ["server", "--host", "0.0.0.0", "--port", "8787"],
            "environment": _environment(config, control_plane=True),
            "ports": ["${BRUNOST_JUDGE_BIND:-127.0.0.1}:8787:8787"],
            "depends_on": ["postgres"] if config.storage.postgres == "bundled" else [],
            "healthcheck": {
                "test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/readyz', timeout=5).read()"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 12,
                "start_period": "20s",
            },
            "stop_grace_period": f"{config.worker_termination_grace_seconds}s",
            "init": True,
            "stop_signal": "SIGTERM",
            "logging": {"driver": "json-file", "options": {"max-size": "50m", "max-file": "5"}},
            "restart": "unless-stopped",
            "deploy": {
                "replicas": config.judge_replicas,
                "update_config": {"parallelism": 1, "order": "start-first", "failure_action": "rollback"},
                "rollback_config": {"parallelism": 1, "order": "stop-first"},
                "restart_policy": {"condition": "on-failure", "max_attempts": 3},
            },
        },
    }
    postgres = (
        "${BRUNOST_POSTGRES_URL}"
        if config.storage.postgres == "external"
        else "postgresql://brunost:${POSTGRES_PASSWORD}@postgres:5432/brunost"
    )
    services["callback-dispatcher"] = {
        "image": config.judge_image,
        # Callback delivery is a control-plane responsibility. Keeping it in
        # its own process makes retries survive worker loss without granting
        # the dispatcher sandbox or API credentials.
        "command": ["callback-dispatcher", "--poll-seconds", "1"],
        "healthcheck": {
            "test": ["CMD-SHELL", "kill -0 1"],
            "interval": "15s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "20s",
        },
        "environment": {
            "BRUNOST_JUDGE_DATABASE_URL": postgres,
            "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET": "${BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET}",
            "BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS": "true",
            "BRUNOST_JUDGE_ENV": "production",
        },
        "read_only": True,
        "tmpfs": ["/tmp"],
        "security_opt": ["no-new-privileges:true"],
        "depends_on": ["judge"],
        "stop_grace_period": f"{config.worker_termination_grace_seconds}s",
        "init": True,
        "stop_signal": "SIGTERM",
        "logging": {"driver": "json-file", "options": {"max-size": "25m", "max-file": "5"}},
        "restart": "unless-stopped",
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
    if config.workers:
        services["docker-socket-proxy"] = {
            "image": "${BRUNOST_DOCKER_SOCKET_PROXY_IMAGE:?set BRUNOST_DOCKER_SOCKET_PROXY_IMAGE to a digest-pinned image}",
            "environment": {
                "CONTAINERS": "1",
                "IMAGES": "1",
                "INFO": "1",
                "VERSION": "1",
                "POST": "1",
                "NETWORKS": "0",
                "VOLUMES": "0",
                "EXEC": "0",
                "AUTH": "0",
                "SECRETS": "0",
                "SWARM": "0",
            },
            "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"],
            "restart": "unless-stopped",
        }
    for worker in config.workers:
        services[f"worker-{worker.name}"] = {
            "image": config.worker_image,
            "command": [
                "worker",
                "--config",
                "/etc/brunost/node.json",
                "--poll-seconds",
                "1",
            ],
            "environment": {
                **_environment(config, control_plane=False),
                "BRUNOST_JUDGE_URL": "http://judge:8787",
                # This is an explicit, allowlisted Docker-network exception;
                # all external Premium/control-plane traffic remains HTTPS.
                "BRUNOST_JUDGE_ALLOW_INSECURE_HTTP": "true",
                "BRUNOST_JUDGE_INTERNAL_HTTP_HOSTS": "judge",
                "BRUNOST_WORKER_QUEUES": ",".join(worker.queues),
                "BRUNOST_WORKER_RESOURCE_CLASSES": ",".join(worker.resource_classes),
                "BRUNOST_WORKER_REGION": worker.region or "",
                "BRUNOST_JUDGE_SANDBOX_IMAGE": "${BRUNOST_JUDGE_SANDBOX_IMAGE}",
                "BRUNOST_JUDGE_SANDBOX_IMAGES": "${BRUNOST_JUDGE_SANDBOX_IMAGES}",
                "BRUNOST_JUDGE_SANDBOX_RUNTIME": "${BRUNOST_JUDGE_SANDBOX_RUNTIME}",
                "BRUNOST_JUDGE_REQUIRE_SECCOMP": "true",
                "BRUNOST_JUDGE_SANDBOX_SECCOMP": "${BRUNOST_JUDGE_SANDBOX_SECCOMP}",
                "BRUNOST_JUDGE_SANDBOX_TIMEOUT_SECONDS": "900",
                "BRUNOST_JUDGE_SANDBOX_MEMORY": "4g",
                "BRUNOST_JUDGE_SANDBOX_CPUS": "2",
                "BRUNOST_JUDGE_SANDBOX_PIDS_LIMIT": "256",
                "BRUNOST_JUDGE_REQUIRE_IMMUTABLE_ARTIFACTS": "true",
                "DOCKER_HOST": "tcp://docker-socket-proxy:2375",
                "TMPDIR": "/srv/brunost-judge/workspaces",
            },
            # The generated Compose file lives in .brunost/rendered; resolve
            # credentials back to the operator-owned project nodes directory.
            "volumes": [
                f"../../nodes/{worker.name}.json:/etc/brunost/node.json:ro",
                f"../../workspaces/{worker.name}:/srv/brunost-judge/workspaces:rw",
                "${BRUNOST_JUDGE_SANDBOX_SECCOMP}:${BRUNOST_JUDGE_SANDBOX_SECCOMP}:ro",
            ],
            "depends_on": ["judge", "docker-socket-proxy"],
            "stop_grace_period": f"{config.worker_termination_grace_seconds}s",
            "init": True,
            "stop_signal": "SIGTERM",
            "healthcheck": {
                "test": ["CMD-SHELL", "test -s /etc/brunost/node.json && kill -0 1"],
                "interval": "15s",
                "timeout": "5s",
                "retries": 3,
                "start_period": "20s",
            },
            "logging": {"driver": "json-file", "options": {"max-size": "25m", "max-file": "5"}},
            "restart": "unless-stopped",
            "deploy": {
                "replicas": worker.replicas,
                "update_config": {"parallelism": 1, "order": "start-first", "failure_action": "rollback"},
                "rollback_config": {"parallelism": 1, "order": "stop-first"},
                **({"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}} if "gpu" in worker.resource_classes else {}),
            },
        }
    volumes: dict[str, Any] = {}
    if config.storage.postgres == "bundled":
        volumes["postgres-data"] = {}
    if config.storage.artifacts == "minio":
        volumes["artifact-data"] = {}
    return {
        "name": config.name,
        "x-brunost": {"schema-version": 1, "cluster": config.name},
        "services": services,
        **({"volumes": volumes} if volumes else {}),
    }


def synchronization_checks(config: CountryConfig) -> dict[str, Any]:
    """Return non-secret checks for the Premium callback delivery path.

    This inspects the rendered Compose model in memory. It deliberately does
    not contact either application or mutate the deployment, which makes it
    suitable for the operator's pre-cutover verification workflow.
    """
    services = compose_mapping(config)["services"]
    judge = services.get("judge", {})
    dispatcher = services.get("callback-dispatcher", {})
    judge_environment = judge.get("environment", {})
    dispatcher_environment = dispatcher.get("environment", {})
    dispatcher_command = dispatcher.get("command", [])
    return {
        "callback_hosts": list(config.callback_hosts),
        "idempotency_header_required": judge_environment.get("BRUNOST_JUDGE_REQUIRE_IDEMPOTENCY_HEADER") == "true",
        "callback_dispatcher": {
            "present": bool(dispatcher),
            "command_is_dispatcher": dispatcher_command[:1] == ["callback-dispatcher"],
            "image_matches_judge": dispatcher.get("image") == judge.get("image"),
            "shared_database": dispatcher_environment.get("BRUNOST_JUDGE_DATABASE_URL")
            == judge_environment.get("BRUNOST_JUDGE_DATABASE_URL"),
            "signed_callbacks_required": judge_environment.get("BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS") == "true"
            and dispatcher_environment.get("BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS") == "true",
            "restart_policy": dispatcher.get("restart") == "unless-stopped",
        },
        "workers": {
            "configured": bool(config.workers),
            "drain_grace_seconds": config.worker_termination_grace_seconds,
            "socket_proxy_present": not config.workers or "docker-socket-proxy" in services,
            "credentials_mounted_read_only": all(
                any(str(volume).endswith(":/etc/brunost/node.json:ro") for volume in services[f"worker-{worker.name}"].get("volumes", []))
                for worker in config.workers
            ),
        },
        "observability": {
            "judge_healthcheck": bool(judge.get("healthcheck")),
            "worker_healthchecks": all(bool(services[f"worker-{worker.name}"].get("healthcheck")) for worker in config.workers),
        },
    }


def render_compose(config: CountryConfig) -> str:
    return yaml.safe_dump(compose_mapping(config), sort_keys=False, default_flow_style=False)


def render_env(
    config: CountryConfig,
    *,
    seccomp_path: str = "/srv/brunost-judge/security/brunost-seccomp-v1.json",
) -> str:
    return "\n".join(
        [
            f"BRUNOST_CLUSTER_NAME={config.name}",
            f"BRUNOST_PUBLIC_URL={config.public_url}",
            "BRUNOST_JUDGE_API_TOKEN=replace-with-a-long-random-token",
            "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=replace-with-a-long-random-secret",
            f"BRUNOST_JUDGE_CALLBACK_HOSTS={','.join(config.callback_hosts)}",
            "BRUNOST_JUDGE_BIND=127.0.0.1",
            "POSTGRES_PASSWORD=replace-with-a-long-random-password",
            "MINIO_ROOT_USER=brunost",
            "MINIO_ROOT_PASSWORD=replace-with-a-long-random-password",
            "BRUNOST_ARTIFACT_ACCESS_KEY=replace-with-object-storage-access-key",
            "BRUNOST_ARTIFACT_SECRET_KEY=replace-with-object-storage-secret-key",
            "BRUNOST_ARTIFACT_ENDPOINT=https://s3.example.org",
            "BRUNOST_POSTGRES_URL=postgresql://user:password@db.example:5432/brunost",
            "BRUNOST_DOCKER_SOCKET_PROXY_IMAGE=tecnativa/docker-socket-proxy@sha256:<64-hex-digest>",
            "BRUNOST_JUDGE_SANDBOX_IMAGE=ghcr.io/mlgorithm/brunost-sandbox@sha256:<64-hex-digest>",
            "BRUNOST_JUDGE_SANDBOX_IMAGES={\"python-3.13\":\"ghcr.io/mlgorithm/brunost-sandbox@sha256:<64-hex-digest>\"}",
            "BRUNOST_JUDGE_SANDBOX_RUNTIME=runsc",
            f"BRUNOST_JUDGE_SANDBOX_SECCOMP={seccomp_path}",
            "",
        ]
    )


def helm_values_mapping(config: CountryConfig) -> dict[str, Any]:
    """Translate the country topology into the Helm chart's value schema."""

    def resolve(value: str) -> str:
        """Resolve a deliberate ``${ENV_NAME}`` placeholder when available.

        Render remains safe before an operator has created ``.env`` (the
        placeholder is retained), while ``install`` can load the operator's
        secrets before rendering a usable Kubernetes Secret manifest.
        """
        if value.startswith("${") and value.endswith("}"):
            return os.environ.get(value[2:-1], value)
        return value

    return {
        "judge": {
            "image": config.judge_image,
            "replicas": config.judge_replicas,
            "port": 8787,
            "apiToken": resolve("${BRUNOST_JUDGE_API_TOKEN}"),
            "callbackSigningSecret": resolve("${BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET}"),
            "callbackHosts": list(config.callback_hosts),
            "podDisruptionBudget": {"enabled": config.judge_replicas > 1, "minAvailable": 1},
        },
        "workers": [
            {
                "name": worker.name,
                "image": config.worker_image,
                "replicas": worker.replicas,
                "queues": list(worker.queues),
                "resourceClasses": list(worker.resource_classes),
                "region": worker.region or "",
                "nodeSelector": dict(worker.node_selector),
                "autoscaling": {
                    "enabled": worker.autoscaling,
                    "minReplicas": worker.min_replicas,
                    "maxReplicas": worker.max_replicas,
                    "targetCPUUtilization": worker.target_cpu_utilization,
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "2", "memory": "4Gi"},
                },
            }
            for worker in config.workers
        ],
        "nodeConfigs": {},
        "postgres": {
            "externalUrl": resolve(config.storage.postgres_url or ""),
            "image": config.storage.postgres_image,
            "password": resolve("${POSTGRES_PASSWORD}"),
        },
        "artifacts": {
            "endpoint": resolve(config.storage.artifacts_endpoint or ""),
            "bucket": config.storage.artifacts_bucket,
            "backend": "s3",
            "accessKey": resolve("${BRUNOST_ARTIFACT_ACCESS_KEY}"),
            "secretKey": resolve("${BRUNOST_ARTIFACT_SECRET_KEY}"),
        },
        "ingress": {
            "enabled": config.tls,
            "className": "nginx",
            "host": urlparse(config.public_url).hostname or "judge.example.org",
            "tlsSecret": "brunost-tls",
        },
        "monitoring": {"enabled": config.monitoring, "path": "/metrics", "port": 8787},
        "workerDefaults": {
            "terminationGraceSeconds": config.worker_termination_grace_seconds,
            "socketProxyImage": resolve("${BRUNOST_DOCKER_SOCKET_PROXY_IMAGE}"),
            "dockerSocketPath": "/var/run/docker.sock",
            "workspacePath": "/srv/brunost-judge/workspaces",
            "sandbox": {
                "image": resolve("${BRUNOST_JUDGE_SANDBOX_IMAGE}"),
                "images": resolve("${BRUNOST_JUDGE_SANDBOX_IMAGES}"),
                "runtime": resolve("${BRUNOST_JUDGE_SANDBOX_RUNTIME}"),
                "requireSeccomp": "true",
                "seccompProfilePath": resolve("${BRUNOST_JUDGE_SANDBOX_SECCOMP}"),
            },
            "podDisruptionBudget": {"enabled": bool(config.workers), "maxUnavailable": 0},
        },
    }
