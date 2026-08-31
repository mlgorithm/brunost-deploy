"""Safe starting topologies for country operators."""

from __future__ import annotations

from typing import Any


def preset_mapping(name: str, *, cluster_name: str, public_url: str) -> dict[str, Any]:
    if name == "single":
        return {
            "version": 1,
            "cluster": {"name": cluster_name, "public_url": public_url},
            "backend": "compose",
            "judge": {"replicas": 1, "callback_hosts": ["platform"]},
            "workers": [{"name": "cpu-1", "resource_classes": ["cpu"], "queues": ["default"]}],
            "storage": {"postgres": "bundled", "artifacts": "minio"},
            "security": {"tls": public_url.startswith("https://")},
        }
    if name == "small":
        return {
            "version": 1,
            "cluster": {"name": cluster_name, "public_url": public_url},
            "backend": "compose",
            "judge": {"replicas": 1, "callback_hosts": ["platform"]},
            "workers": [
                {"name": "cpu-1", "resource_classes": ["cpu"], "queues": ["default"]},
                {"name": "cpu-2", "resource_classes": ["cpu"], "queues": ["default"]},
                {"name": "cpu-3", "resource_classes": ["cpu"], "queues": ["default"]},
                {"name": "gpu-1", "resource_classes": ["gpu", "cpu"], "queues": ["gpu", "default"]},
            ],
            "storage": {"postgres": "bundled", "artifacts": "minio"},
            "security": {"tls": True},
        }
    if name == "ha-5-node":
        return {
            "version": 1,
            "cluster": {"name": cluster_name, "public_url": public_url},
            "backend": "k3s",
            "judge": {"replicas": 2, "callback_hosts": ["platform"]},
            "workers": [
                {
                    "name": "cpu-1",
                    "resource_classes": ["cpu"],
                    "queues": ["default"],
                    "node_selector": {"brunost.io/worker": "cpu-1"},
                },
                {
                    "name": "gpu-1",
                    "resource_classes": ["gpu", "cpu"],
                    "queues": ["gpu", "default"],
                    "capabilities": ["gpu:true"],
                    "node_selector": {"brunost.io/worker": "gpu-1"},
                },
            ],
            "storage": {
                "postgres": "external",
                "postgres_url": "${BRUNOST_POSTGRES_URL}",
                "artifacts": "external",
                "artifacts_endpoint": "${BRUNOST_ARTIFACT_ENDPOINT}",
            },
            "security": {"tls": True},
            "observability": {"monitoring": True},
        }
    raise ValueError(f"unknown preset: {name}; choose single, small, or ha-5-node")
