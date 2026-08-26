from pathlib import Path

import pytest
import yaml

from brunostctl.cli import main
from brunostctl.config import ConfigError, CountryConfig
from brunostctl.presets import preset_mapping
from brunostctl.render import compose_mapping, helm_values_mapping


def _production_mapping() -> dict:
    digest = "a" * 64
    mapping = preset_mapping("small", cluster_name="test", public_url="https://contest.test")
    mapping["judge"]["callback_hosts"] = ["premium.example.org"]
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
    assert {"judge", "postgres", "minio", "worker-cpu-1", "worker-gpu-1"} <= set(rendered["services"])
    assert "platform" not in rendered["services"]
    assert rendered["services"]["judge"]["depends_on"] == ["postgres"]
    assert rendered["services"]["worker-gpu-1"]["environment"]["BRUNOST_WORKER_RESOURCE_CLASSES"] == "gpu,cpu"
    assert rendered["services"]["judge"]["environment"]["BRUNOST_JUDGE_CALLBACK_HOSTS"] == "premium"
    assert "BRUNOST_JUDGE_API_TOKEN" not in rendered["services"]["worker-gpu-1"]["environment"]
    assert "BRUNOST_JUDGE_DATABASE_URL" not in rendered["services"]["worker-gpu-1"]["environment"]


def test_helm_values_preserve_topology_images_and_replicas(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRUNOST_JUDGE_API_TOKEN", "judge-token")
    monkeypatch.setenv("BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET", "callback-secret")
    config = CountryConfig.from_mapping(_production_mapping())
    values = helm_values_mapping(config)
    assert "platform" not in values
    assert values["judge"]["replicas"] == 1
    assert values["judge"]["image"] == config.judge_image
    assert values["judge"]["apiToken"] == "judge-token"
    assert values["judge"]["callbackHosts"] == ["premium.example.org"]
    assert next(worker for worker in values["workers"] if worker["name"] == "gpu-1")["resourceClasses"] == ["gpu", "cpu"]


def test_init_renders_operator_bundle(tmp_path: Path):
    assert main(["init", str(tmp_path), "--preset", "small", "--name", "demo", "--public-url", "https://demo.test"]) == 0
    assert (tmp_path / "brunost.yaml").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / ".brunost" / "rendered" / "docker-compose.yml").is_file()
    assert (tmp_path / ".brunost" / "chart" / "Chart.yaml").is_file()
    assert main(["preflight", "--config", str(tmp_path / "brunost.yaml")]) == 0


def test_k3s_install_uses_bundled_chart_and_node_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    digest = "b" * 64
    mapping = preset_mapping("ha-5-node", cluster_name="ha-test", public_url="https://ha.test")
    mapping["judge"]["callback_hosts"] = ["premium.example.org"]
    mapping["judge"]["image"] = f"ghcr.io/example/judge@sha256:{digest}"
    mapping["judge"]["worker_image"] = f"ghcr.io/example/judge@sha256:{digest}"
    (tmp_path / "brunost.yaml").write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    (tmp_path / "nodes").mkdir()
    for name in ("cpu-1", "gpu-1"):
        (tmp_path / "nodes" / f"{name}.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BRUNOST_JUDGE_API_TOKEN=judge\nBRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=callback\n"
        "BRUNOST_ARTIFACT_ACCESS_KEY=access\nBRUNOST_ARTIFACT_SECRET_KEY=secret\n"
        "BRUNOST_POSTGRES_URL=postgresql://user:pass@db/brunost\n"
        "BRUNOST_ARTIFACT_ENDPOINT=https://objects.test\n",
        encoding="utf-8",
    )
    assert main(["install", "--config", str(tmp_path / "brunost.yaml"), "--dry-run"]) == 0
    assert ".brunost/chart" in capsys.readouterr().out
