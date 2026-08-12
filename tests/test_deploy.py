from pathlib import Path

import pytest

from brunostctl.cli import main
from brunostctl.config import ConfigError, CountryConfig
from brunostctl.presets import preset_mapping
from brunostctl.render import compose_mapping


def _production_mapping() -> dict:
    digest = "a" * 64
    mapping = preset_mapping("small", cluster_name="test", public_url="https://contest.test")
    mapping["platform"]["image"] = f"ghcr.io/example/platform@sha256:{digest}"
    mapping["judge"]["image"] = f"ghcr.io/example/judge@sha256:{digest}"
    mapping["judge"]["worker_image"] = f"ghcr.io/example/judge@sha256:{digest}"
    mapping["storage"]["postgres_image"] = f"postgres@sha256:{digest}"
    mapping["storage"]["artifacts_image"] = f"minio/minio@sha256:{digest}"
    mapping["storage"]["artifacts_init_image"] = f"minio/mc@sha256:{digest}"
    return mapping


def test_country_config_round_trips_and_strict_checks_images():
    config = CountryConfig.from_mapping(_production_mapping())
    assert config.name == "test"
    assert config.workers[-1].resource_classes == ("gpu", "cpu")
    assert config.validate(strict=True) == []

    unsafe = dict(_production_mapping())
    unsafe["judge"] = {"image": "ghcr.io/example/judge:latest"}
    with pytest.raises(ConfigError, match="judge.image"):
        CountryConfig.from_mapping(unsafe).validate(strict=True)


def test_compose_contains_control_plane_workers_and_shared_postgres():
    config = CountryConfig.from_mapping(preset_mapping("small", cluster_name="test", public_url="https://test.example"))
    rendered = compose_mapping(config)
    assert {"platform", "judge", "postgres", "minio", "worker-cpu-1", "worker-gpu-1"} <= set(rendered["services"])
    assert rendered["services"]["judge"]["depends_on"] == ["postgres"]
    assert rendered["services"]["worker-gpu-1"]["environment"]["BRUNOST_WORKER_RESOURCE_CLASSES"] == "gpu,cpu"


def test_init_renders_operator_bundle(tmp_path: Path):
    assert main(["init", str(tmp_path), "--preset", "small", "--name", "demo", "--public-url", "https://demo.test"]) == 0
    assert (tmp_path / "brunost.yaml").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / ".brunost" / "rendered" / "docker-compose.yml").is_file()
    assert main(["preflight", "--config", str(tmp_path / "brunost.yaml")]) == 0
