from pathlib import Path

import pytest
import yaml

from brunostctl.cli import main
from brunostctl.config import ConfigError, CountryConfig
from brunostctl.presets import preset_mapping
from brunostctl.render import compose_mapping, helm_values_mapping, synchronization_checks


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
    assert {"judge", "callback-dispatcher", "postgres", "minio", "worker-cpu-1", "worker-gpu-1"} <= set(rendered["services"])
    assert "platform" not in rendered["services"]
    assert rendered["services"]["judge"]["depends_on"] == ["postgres"]
    assert rendered["services"]["callback-dispatcher"]["environment"]["BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS"] == "true"
    assert "BRUNOST_JUDGE_API_TOKEN" not in rendered["services"]["callback-dispatcher"]["environment"]
    assert rendered["services"]["worker-gpu-1"]["environment"]["BRUNOST_WORKER_RESOURCE_CLASSES"] == "gpu,cpu"
    assert rendered["services"]["worker-gpu-1"]["environment"]["BRUNOST_JUDGE_REQUIRE_SIGNED_CALLBACKS"] == "true"
    assert "BRUNOST_JUDGE_CALLBACK_SIGNING_SECRET" in rendered["services"]["worker-gpu-1"]["environment"]
    assert rendered["services"]["judge"]["environment"]["BRUNOST_JUDGE_CALLBACK_HOSTS"] == "premium"
    assert "BRUNOST_JUDGE_API_TOKEN" not in rendered["services"]["worker-gpu-1"]["environment"]
    assert "BRUNOST_JUDGE_DATABASE_URL" not in rendered["services"]["worker-gpu-1"]["environment"]


def test_synchronization_checks_cover_durable_signed_callback_delivery():
    config = CountryConfig.from_mapping(preset_mapping("small", cluster_name="test", public_url="https://test.example"))
    checks = synchronization_checks(config)
    assert checks["callback_hosts"] == ["premium"]
    assert all(checks["callback_dispatcher"].values())


def test_standalone_and_packaged_helm_charts_stay_in_sync():
    root = Path(__file__).parents[1]
    standalone = root / "helm" / "brunost"
    packaged = root / "src" / "brunostctl" / "chart" / "brunost"
    standalone_files = {path.relative_to(standalone) for path in standalone.rglob("*") if path.is_file()}
    packaged_files = {path.relative_to(packaged) for path in packaged.rglob("*") if path.is_file()}
    assert standalone_files == packaged_files
    for relative in sorted(standalone_files):
        assert (standalone / relative).read_bytes() == (packaged / relative).read_bytes()


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


def test_verify_is_read_only_and_checks_both_readiness_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    mapping = _production_mapping()
    config_path = tmp_path / "brunost.yaml"
    config_path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    requests: list[tuple[str, str | None]] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        assert method == "GET"
        assert payload is None
        requests.append((url, token))
        return {"status": "ready"}

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    assert main(
        [
            "verify",
            "--config",
            str(config_path),
            "--url",
            "https://judge.test",
            "--token",
            "judge-token",
            "--premium-url",
            "https://premium.test",
        ]
    ) == 0
    assert requests == [("https://judge.test/readyz", "judge-token"), ("https://premium.test/readyz", None)]
    result = yaml.safe_load(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["synchronization"]["callback_dispatcher"]["signed_callbacks_required"] is True
    assert not (tmp_path / ".brunost").exists()


def test_verify_loads_judge_token_from_operator_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mapping = _production_mapping()
    config_path = tmp_path / "brunost.yaml"
    config_path.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    (tmp_path / ".env").write_text("BRUNOST_JUDGE_API_TOKEN=from-env-file\n", encoding="utf-8")
    monkeypatch.delenv("BRUNOST_JUDGE_API_TOKEN", raising=False)
    observed: list[str | None] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        observed.append(token)
        return {"status": "ready"}

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    assert main(["verify", "--config", str(config_path), "--url", "https://judge.test"]) == 0
    assert observed == ["from-env-file"]
