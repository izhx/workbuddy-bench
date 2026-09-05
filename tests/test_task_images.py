from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

from harbor.environments.definition import should_use_prebuilt_docker_image
import pytest
import yaml

from workbuddy_bench.runner import prepare_tasks, task_images
from workbuddy_bench.runner.prepare_job import compose_job_config


def _make_dataset(tmp_path: Path, *task_names: str) -> Path:
    root = tmp_path / "wb-bench-office-v1.0"
    tasks = root / "tasks"
    tasks.mkdir(parents=True)
    (root / "dataset.toml").write_text(
        '[dataset]\nid = "wb-bench-office-v1.0"\n'
    )
    for task_name in task_names:
        task = tasks / task_name
        (task / "environment").mkdir(parents=True)
        (task / "task.toml").write_text(
            '[environment]\nnetwork_mode = "public"\n\n[agent]\ntimeout_sec = 10\n'
        )
        (task / "environment" / "Dockerfile").write_text("FROM alpine:3.20\n")
    return tasks


@pytest.mark.parametrize(
    "value",
    ["latest", "2026-08-27", "2024-02-29"],
)
def test_validate_task_image_tag_accepts_latest_or_real_date(value: str) -> None:
    assert task_images.validate_task_image_tag(value) == value


@pytest.mark.parametrize(
    "value",
    ["nightly", "20260827", "2026-02-30", "Latest", ""],
)
def test_validate_task_image_tag_rejects_other_values(value: str) -> None:
    if value == "":
        # None means default, but an explicitly empty external value is a typo.
        with pytest.raises(ValueError):
            task_images.validate_task_image_tag(value)
    else:
        with pytest.raises(ValueError):
            task_images.validate_task_image_tag(value)


def test_injects_normalized_image_without_rewriting_other_task_config(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "Report-L4-001")

    changed = task_images.inject_task_docker_images(
        tasks, tag="2026-08-27"
    )

    assert changed == 1
    config = tomllib.loads((tasks / "Report-L4-001" / "task.toml").read_text())
    assert config["environment"]["network_mode"] == "public"
    assert config["environment"]["docker_image"] == (
        "wb-bench-office-v1.0/report-l4-001:2026-08-27"
    )
    assert config["agent"]["timeout_sec"] == 10
    assert task_images.inject_task_docker_images(tasks, tag="2026-08-27") == 0


def test_prepare_tasks_does_not_inject_image_without_explicit_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _make_dataset(tmp_path, "Report-L4-001")
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_tasks", str(tasks)],
    )

    assert prepare_tasks.main() == 0
    config = tomllib.loads((tasks / "Report-L4-001" / "task.toml").read_text())
    assert "docker_image" not in config["environment"]


def test_prepare_tasks_injects_explicit_tag_without_resolving_force_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _make_dataset(tmp_path, "Report-L4-001")
    monkeypatch.setattr(
        sys,
        "argv",
        ["prepare_tasks", str(tasks), "--task-image-tag", "latest"],
    )

    assert prepare_tasks.main() == 0
    config = tomllib.loads((tasks / "Report-L4-001" / "task.toml").read_text())
    assert config["environment"]["docker_image"] == (
        "wb-bench-office-v1.0/report-l4-001:latest"
    )


def test_prepare_tasks_limits_image_injection_to_manifest_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _make_dataset(tmp_path, "selected", "unselected")
    (tasks / "unselected" / "environment" / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n"
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"selected_tasks": ["selected"]}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_tasks",
            str(tasks),
            "--task-image-tag",
            "latest",
            "--manifest",
            str(manifest),
        ],
    )

    assert prepare_tasks.main() == 0
    selected = tomllib.loads((tasks / "selected" / "task.toml").read_text())
    unselected = tomllib.loads((tasks / "unselected" / "task.toml").read_text())
    assert selected["environment"]["docker_image"] == (
        "wb-bench-office-v1.0/selected:latest"
    )
    assert "docker_image" not in unselected["environment"]


def test_rejects_task_names_that_collide_after_normalization(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "Foo Bar", "foo-bar")
    with pytest.raises(ValueError, match="naming collision"):
        task_images.resolve_task_images(tasks, compute_source_hash=False)


def test_source_hash_ignores_runtime_compose_mutation(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "one")
    task = tasks / "one"
    compose = task / "environment" / "docker-compose.yaml"
    compose.write_text("services: {}\n")
    before = task_images.task_environment_source_hash(task)
    compose.write_text("services:\n  main:\n    extra_hosts: [host.docker.internal:host-gateway]\n")
    after = task_images.task_environment_source_hash(task)
    assert before == after


def test_source_hash_changes_when_file_mode_changes(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "one")
    task = tasks / "one"
    script = task / "environment" / "entrypoint.sh"
    script.write_text("#!/bin/sh\nexec true\n")
    script.chmod(0o644)
    before = task_images.task_environment_source_hash(task)

    script.chmod(0o755)
    after = task_images.task_environment_source_hash(task)

    assert before != after


def test_rejects_compose_that_overrides_prebuilt_main_image(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "one")
    (tasks / "one" / "environment" / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n"
    )
    with pytest.raises(ValueError, match="overrides the reusable task image contract"):
        task_images.resolve_task_images(tasks, compute_source_hash=False)


def test_preflight_rebuilds_stale_latest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "one")
    image = task_images.resolve_task_images(tasks)[0]
    labels = iter(
        [
            {
                **task_images._expected_task_image_labels(image),
                task_images.TASK_IMAGE_SOURCE_HASH_LABEL: "old",
            },
            task_images._expected_task_image_labels(image),
        ]
    )
    built: list[str] = []
    monkeypatch.setattr(task_images, "_docker_image_labels", lambda _ref: next(labels))
    monkeypatch.setattr(task_images, "_build_task_image", lambda item: built.append(item.reference))

    assert task_images.ensure_task_images_available([image], build_missing=True) == (1, 1)
    assert built == [image.reference]


def test_builder_tags_labels_forwards_proxy_and_honors_task_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tasks = _make_dataset(tmp_path, "one")
    task_toml = tasks / "one" / "task.toml"
    task_toml.write_text(
        '[environment]\nbuild_timeout_sec = 42.0\n\n[agent]\ntimeout_sec = 10\n'
    )
    image = task_images.resolve_task_images(tasks)[0]
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:1080")
    calls: list[tuple[list[str], bool, float | None]] = []

    def fake_run(command: list[str], *, check: bool, timeout: float | None):
        calls.append((command, check, timeout))

    monkeypatch.setattr(task_images.subprocess, "run", fake_run)
    task_images._build_task_image(image)

    command, check, timeout = calls[0]
    assert check is True
    assert timeout == 42.0
    assert command[:2] == ["docker", "build"]
    assert ["--build-arg", "HTTPS_PROXY=http://proxy.example:1080"] == command[
        command.index("--build-arg") : command.index("--build-arg") + 2
    ]
    assert command[command.index("--tag") + 1] == "wb-bench-office-v1.0/one:latest"
    assert f"{task_images.TASK_IMAGE_SOURCE_HASH_LABEL}={image.source_hash}" in command


def test_preflight_does_not_overwrite_stale_date_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tasks = _make_dataset(tmp_path, "one")
    image = task_images.resolve_task_images(tasks, tag="2026-08-27")[0]
    monkeypatch.setattr(
        task_images,
        "_docker_image_labels",
        lambda _ref: {
            **task_images._expected_task_image_labels(image),
            task_images.TASK_IMAGE_SOURCE_HASH_LABEL: "old",
        },
    )
    with pytest.raises(RuntimeError, match="dated tags are immutable"):
        task_images.ensure_task_images_available([image], build_missing=True)


def test_image_name_uses_full_dataset_id_as_namespace(tmp_path: Path) -> None:
    tasks = _make_dataset(tmp_path, "one")
    image = task_images.resolve_task_images(tasks, compute_source_hash=False)[0]
    assert image.namespace == "wb-bench-office-v1.0"
    assert image.reference == "wb-bench-office-v1.0/one:latest"


def test_harbor_force_build_decides_whether_injected_image_is_used(
    tmp_path: Path,
) -> None:
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "Dockerfile").write_text("FROM alpine:3.20\n")

    assert should_use_prebuilt_docker_image(
        environment,
        docker_image="wb-bench-office-v1.0/one:latest",
        force_build=False,
    )
    assert not should_use_prebuilt_docker_image(
        environment,
        docker_image="wb-bench-office-v1.0/one:latest",
        force_build=True,
    )


def test_runtime_job_keeps_force_build_configurable(tmp_path: Path) -> None:
    manifest = {
        "instance_id": "test-instance",
        "dataset": "datasets/wb-bench-office-v1.0/tasks",
        "harness_backend": "local",
        "model_route": "glm-5.2",
        "backend_model_name": "glm-5.2",
        "connection": {"effective": "local_proxy", "proxy_url": "http://127.0.0.1:3456"},
        "model_connection": "local_proxy",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    job = yaml.safe_load(Path("configs/jobs/glm-5.2.cc.office.yaml").read_text())
    job["environment_override"] = {"force_build": True}
    job_path = tmp_path / "build.yaml"
    job_path.write_text(yaml.safe_dump(job, sort_keys=False))
    runtime_path = compose_job_config(
        job_path,
        tmp_path / "generated",
        configs_dir=Path("configs"),
        manifest_path=manifest_path,
    )
    runtime = yaml.safe_load(runtime_path.read_text())
    assert runtime["environment"]["force_build"] is True

    job["environment_override"] = {"force_build": False}
    job_path = tmp_path / "reuse.yaml"
    job_path.write_text(yaml.safe_dump(job, sort_keys=False))
    reuse_runtime_path = compose_job_config(
        job_path,
        tmp_path / "generated-reuse",
        configs_dir=Path("configs"),
        manifest_path=manifest_path,
    )
    reuse_runtime = yaml.safe_load(reuse_runtime_path.read_text())
    assert reuse_runtime["environment"]["force_build"] is False
