import json
from pathlib import Path

import pytest
import yaml

from brunostctl.cli import _snapshot_rendered, _verify_backup_checksum, _write_backup_metadata, main
from brunostctl.config import ConfigError, CountryConfig, required_environment, validate_environment
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
    assert "docker-socket-proxy" in rendered["services"]
    assert rendered["services"]["judge"]["healthcheck"]["retries"] == 12
    assert rendered["services"]["worker-gpu-1"]["stop_grace_period"] == "900s"


def test_synchronization_checks_cover_durable_signed_callback_delivery():
    config = CountryConfig.from_mapping(preset_mapping("small", cluster_name="test", public_url="https://test.example"))
    checks = synchronization_checks(config)
    assert checks["callback_hosts"] == ["premium"]
    assert checks["idempotency_header_required"] is True
    assert all(checks["callback_dispatcher"].values())
    assert checks["workers"]["socket_proxy_present"] is True
    assert checks["observability"]["worker_healthchecks"] is True


def test_strict_environment_rejects_examples_without_leaking_values():
    config = CountryConfig.from_mapping(preset_mapping("small", cluster_name="test", public_url="https://test.example"))
    errors = validate_environment(config, {name: "replace-me" for name in required_environment(config)}, strict=True)
    assert any(error.startswith("BRUNOST_JUDGE_API_TOKEN is required") for error in errors)
    assert all("replace-me" not in error for error in errors)


def test_worker_autoscaling_round_trips_to_helm_values():
    mapping = _production_mapping()
    mapping["backend"] = "k3s"
    for worker in mapping["workers"]:
        worker["node_selector"] = {"brunost.io/worker": worker["name"]}
    mapping["storage"] = {"postgres": "external", "postgres_url": "postgresql://user:pass@db/brunost", "artifacts": "external", "artifacts_endpoint": "https://objects.test"}
    mapping["workers"][0]["replicas"] = 2
    mapping["workers"][0]["autoscaling"] = {"enabled": True, "min_replicas": 2, "max_replicas": 8, "target_cpu_utilization": 65}
    config = CountryConfig.from_mapping(mapping)
    values = helm_values_mapping(config)
    worker = values["workers"][0]
    assert worker["autoscaling"] == {"enabled": True, "minReplicas": 2, "maxReplicas": 8, "targetCPUUtilization": 65}
    assert worker["nodeSelector"] == {"brunost.io/worker": "cpu-1"}
    assert worker["resources"]["requests"]["cpu"] == "100m"


def test_helm_workers_are_wired_to_the_sandbox_proxy_and_shared_paths():
    mapping = _production_mapping()
    mapping["backend"] = "k3s"
    for worker in mapping["workers"]:
        worker["node_selector"] = {"brunost.io/worker": worker["name"]}
    mapping["storage"] = {"postgres": "external", "postgres_url": "postgresql://user:pass@db/brunost", "artifacts": "external", "artifacts_endpoint": "https://objects.test"}
    config = CountryConfig.from_mapping(mapping)
    values = helm_values_mapping(config)
    assert values["judge"]["podDisruptionBudget"] == {"enabled": False, "minAvailable": 1}
    assert values["workerDefaults"]["sandbox"]["runtime"]
    assert values["workerDefaults"]["workspacePath"] == "/srv/brunost-judge/workspaces"
    assert values["workers"][0]["nodeSelector"] == {"brunost.io/worker": "cpu-1"}
    template = (Path(__file__).parents[1] / "helm" / "brunost" / "templates" / "worker-deployment.yaml").read_text()
    assert "DOCKER_HOST" in template
    assert "docker-socket-proxy" in template
    assert "type: Socket" in template
    assert "type: File" in template


def test_render_manifest_and_release_snapshot_are_deterministic(tmp_path: Path):
    assert main(["init", str(tmp_path), "--preset", "single", "--name", "demo", "--public-url", "https://demo.test"]) == 0
    manifest = tmp_path / ".brunost" / "rendered" / "release-manifest.json"
    first = manifest.read_text(encoding="utf-8")
    assert "config_sha256" in first
    snapshot = _snapshot_rendered(tmp_path, "before-upgrade")
    assert snapshot is not None
    assert (snapshot / "docker-compose.yml").read_text(encoding="utf-8") == (tmp_path / ".brunost" / "rendered" / "docker-compose.yml").read_text(encoding="utf-8")
    assert (snapshot / "release-manifest.json").read_text(encoding="utf-8") == first


def test_release_snapshot_keeps_non_secret_runtime_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert main(["init", str(tmp_path), "--preset", "single", "--name", "demo", "--public-url", "https://demo.test"]) == 0
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGE", "sandbox@sha256:" + "a" * 64)
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_RUNTIME", "runsc")
    snapshot = _snapshot_rendered(tmp_path, "before-upgrade")
    assert snapshot is not None
    runtime = (snapshot / "runtime.env").read_text(encoding="utf-8")
    assert "BRUNOST_JUDGE_SANDBOX_IMAGE=sandbox@sha256:" + "a" * 64 in runtime
    assert "BRUNOST_JUDGE_SANDBOX_RUNTIME=runsc" in runtime
    assert "CALLBACK" not in runtime


def test_release_snapshot_preserves_pins_from_the_previous_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert main(["init", str(tmp_path), "--preset", "single", "--name", "demo", "--public-url", "https://demo.test"]) == 0
    previous = "BRUNOST_JUDGE_SANDBOX_IMAGE=old@sha256:" + "a" * 64 + "\n"
    (tmp_path / ".brunost" / "rendered" / "runtime.env").write_text(previous, encoding="utf-8")
    monkeypatch.setenv("BRUNOST_JUDGE_SANDBOX_IMAGE", "new@sha256:" + "b" * 64)
    snapshot = _snapshot_rendered(tmp_path, "before-upgrade")
    assert snapshot is not None
    assert (snapshot / "runtime.env").read_text(encoding="utf-8") == previous


def test_node_drain_can_request_async_drain(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    requests: list[tuple[str, str, dict | None]] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        requests.append((url, method, payload))
        return {"status": "draining"}

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    assert main(["node", "drain", "--url", "https://judge.test", "--token", "token", "--worker-id", "cpu-1", "--no-wait"]) == 0
    assert requests == [("https://judge.test/v1/workers/cpu-1/drain", "POST", {"timeout_seconds": 900})]
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "draining"


def test_cli_backup_metadata_is_checksum_verifiable(tmp_path: Path):
    config = CountryConfig.from_mapping(_production_mapping())
    dump = tmp_path / "postgres.dump"
    dump.write_bytes(b"custom-format-test-dump")
    _write_backup_metadata(dump, config)
    assert _verify_backup_checksum(dump) is True
    dump.write_bytes(b"tampered")
    assert _verify_backup_checksum(dump) is False


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
    seccomp = tmp_path / "security" / "brunost-seccomp-v1.json"
    assert seccomp.is_file()
    assert str(seccomp.resolve()) in (tmp_path / ".env.example").read_text(encoding="utf-8")
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
        node_file = tmp_path / "nodes" / f"{name}.json"
        node_file.write_text(
            json.dumps(
                {"api_url": "https://judge.test", "worker_id": name, "worker_token": "test-token"},
            ),
            encoding="utf-8",
        )
        node_file.chmod(0o600)
    seccomp = tmp_path / "security" / "brunost-seccomp-v1.json"
    seccomp.parent.mkdir()
    seccomp.write_bytes(
        (Path(__file__).parents[1] / "src" / "brunostctl" / "security" / "brunost-seccomp-v1.json").read_bytes()
    )
    (tmp_path / ".env").write_text(
        "BRUNOST_JUDGE_API_TOKEN=judge\nBRUNOST_JUDGE_CALLBACK_SIGNING_SECRET=callback\n"
        "BRUNOST_ARTIFACT_ACCESS_KEY=access\nBRUNOST_ARTIFACT_SECRET_KEY=secret\n"
        "BRUNOST_POSTGRES_URL=postgresql://user:pass@db/brunost\n"
        "BRUNOST_ARTIFACT_ENDPOINT=https://objects.test\n"
        f"BRUNOST_DOCKER_SOCKET_PROXY_IMAGE=proxy@sha256:{'c' * 64}\n"
        f"BRUNOST_JUDGE_SANDBOX_IMAGE=sandbox@sha256:{'d' * 64}\n"
        f"BRUNOST_JUDGE_SANDBOX_IMAGES={{\"python-3.13\":\"sandbox@sha256:{'d' * 64}\"}}\n"
        "BRUNOST_JUDGE_SANDBOX_RUNTIME=runsc\n"
        f"BRUNOST_JUDGE_SANDBOX_SECCOMP={seccomp}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").chmod(0o600)
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


def test_topology_worker_enrollment_grants_runtime_and_declared_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("BRUNOST_JUDGE_SANDBOX_IMAGES", raising=False)
    mapping = _production_mapping()
    mapping["workers"] = [
        {
            "name": "gpu-1",
            "resource_classes": ["gpu", "cpu"],
            "queues": ["gpu", "default"],
            "capabilities": ["cuda"],
        }
    ]
    topology = tmp_path / "brunost.yaml"
    topology.write_text(yaml.safe_dump(mapping, sort_keys=False), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BRUNOST_JUDGE_SANDBOX_IMAGES={\"python-3.13\":\"sandbox@sha256:" + "a" * 64
        + "\",\"python-3.13-ml-v1\":\"sandbox-ml@sha256:" + "b" * 64 + "\"}\n",
        encoding="utf-8",
    )
    observed: list[dict] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        observed.append(payload or {})
        return {"join_token": "one-time-token"}

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    assert (
        main(
            [
                "node",
                "issue",
                "--url",
                "https://judge.test",
                "--token",
                "operator-token",
                "--topology",
                str(topology),
                "--worker",
                "gpu-1",
                "--node-id",
                "gpu-host-1",
            ]
        )
        == 0
    )
    payload = observed[0]
    assert payload["worker_id"] == "gpu-1"
    assert payload["resource_classes"] == ["gpu", "cpu"]
    assert {"cuda", "gpu:true", "runtime:python-3.13", "runtime:python-3.13-ml-v1"} <= set(payload["capabilities"])
    assert yaml.safe_load(capsys.readouterr().out)["join_token"] == "one-time-token"


def test_topology_enrollment_loads_judge_endpoint_after_parsing_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in ("BRUNOST_JUDGE_URL", "BRUNOST_JUDGE_API_TOKEN", "BRUNOST_JUDGE_SANDBOX_IMAGES"):
        monkeypatch.delenv(name, raising=False)
    topology = tmp_path / "brunost.yaml"
    topology.write_text(yaml.safe_dump(_production_mapping(), sort_keys=False), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BRUNOST_JUDGE_URL=https://judge.from-topology.test\n"
        "BRUNOST_JUDGE_API_TOKEN=operator-from-topology\n",
        encoding="utf-8",
    )
    observed: list[tuple[str, str | None]] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        observed.append((url, token))
        return {"join_token": "one-time-token"}

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    assert main(["node", "issue", "--topology", str(topology), "--worker", "cpu-1", "--node-id", "cpu-host-1"]) == 0
    assert observed == [("https://judge.from-topology.test/v1/nodes/enrollment-tokens", "operator-from-topology")]


def test_join_preserves_the_enrollment_grant_and_persists_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    observed: list[dict] = []

    def fake_request(url: str, *, method: str = "GET", token: str | None = None, payload: dict | None = None) -> dict:
        observed.append(payload or {})
        return {
            "cluster_id": "country",
            "node_id": "cpu-host-1",
            "worker_token": "worker-token",
            "worker": {
                "worker_id": "cpu-1",
                "capabilities": ["runtime:python-3.13"],
                "queues": ["default"],
                "resource_classes": ["cpu"],
            },
        }

    monkeypatch.setattr("brunostctl.cli._json_request", fake_request)
    output = tmp_path / "node.json"
    assert main(["node", "join", "--url", "https://judge.test", "--join-token", "join-token", "--output", str(output)]) == 0
    assert "capabilities" not in observed[0]
    assert "resource_classes" not in observed[0]
    assert json.loads(output.read_text(encoding="utf-8"))["capabilities"] == ["runtime:python-3.13"]
