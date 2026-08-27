"""Reusable local task-image naming, injection, build, and validation.

WorkBuddy datasets keep Dockerfiles in each task directory.  Harbor can only
skip its per-trial ``docker compose build`` path when ``task.toml`` declares an
``[environment].docker_image`` and the job runs with ``force_build=false``.
This module owns that prebuilt-image contract so naming and freshness checks do
not drift between the runner, the builder, and task preparation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

import yaml


DEFAULT_TASK_IMAGE_TAG = "latest"
TASK_IMAGE_SOURCE_HASH_LABEL = "workbuddy-bench.task-source-sha256"
TASK_IMAGE_DATASET_LABEL = "workbuddy-bench.dataset"
TASK_IMAGE_TASK_LABEL = "workbuddy-bench.task"

_DATE_TAG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REPOSITORY_COMPONENT_RE = re.compile(r"[^a-z0-9._-]+")
_HASH_IGNORE_PARTS = frozenset({".git", "__pycache__"})
# ``docker-compose.yaml`` configures runtime services and is mutated in the
# staged copy by ``prepare_tasks`` (host-gateway injection).  It is not part of
# the task image produced from Dockerfile, so including it would make a build
# from the source dataset look stale as soon as a run stages the dataset.
_HASH_IGNORE_NAMES = frozenset({".DS_Store", "docker-compose.yaml"})


@dataclass(frozen=True)
class TaskImage:
    """Resolved reusable image identity for one task."""

    task_dir: Path
    task_name: str
    namespace: str
    tag: str
    reference: str
    source_hash: str
    build_timeout_sec: float | None = None


def validate_task_image_tag(raw: str | None) -> str:
    """Return a supported tag: ``latest`` or a real ISO date (YYYY-MM-DD)."""

    value = DEFAULT_TASK_IMAGE_TAG if raw is None else raw.strip()
    if value == DEFAULT_TASK_IMAGE_TAG:
        return value
    if not _DATE_TAG_RE.fullmatch(value):
        raise ValueError(
            f"task image tag must be 'latest' or an ISO date YYYY-MM-DD; got {value!r}"
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"task image tag is not a valid calendar date: {value!r}") from exc
    return value


def normalize_repository_component(raw: str, *, label: str) -> str:
    """Normalize a dataset/task slug into a lowercase Docker repo component."""

    normalized = _REPOSITORY_COMPONENT_RE.sub("-", raw.strip().lower()).strip("._-")
    if not normalized:
        raise ValueError(f"{label} {raw!r} cannot form a Docker repository component")
    return normalized


def _tasks_root(tasks_path: Path) -> Path:
    return tasks_path.parent if (tasks_path / "task.toml").is_file() else tasks_path


def _dataset_toml(tasks_path: Path) -> Path:
    return _tasks_root(tasks_path).parent / "dataset.toml"


def dataset_id_for_tasks(tasks_path: Path) -> str:
    """Resolve the dataset id, preferring the authoritative dataset.toml."""

    dataset_toml = _dataset_toml(tasks_path)
    if dataset_toml.is_file():
        try:
            data = tomllib.loads(dataset_toml.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid dataset TOML: {dataset_toml}: {exc}") from exc
        dataset = data.get("dataset")
        if isinstance(dataset, dict) and dataset.get("id"):
            return str(dataset["id"])
    # Dry-runs may resolve before a dataset has been fetched.  The parent of
    # ``tasks`` still carries the configured dataset directory name.
    return _tasks_root(tasks_path).parent.name


def task_image_reference(
    *,
    namespace: str,
    task_name: str,
    tag: str,
) -> str:
    """Return ``<dataset-id>/<task>:<tag>``."""

    normalized_namespace = normalize_repository_component(
        namespace, label="dataset image namespace"
    )
    normalized_task = normalize_repository_component(task_name, label="task name")
    normalized_tag = validate_task_image_tag(tag)
    return f"{normalized_namespace}/{normalized_task}:{normalized_tag}"


def task_environment_source_hash(task_dir: Path) -> str:
    """Hash build-relevant task environment content portably.

    Runtime-only ``docker-compose.yaml`` is deliberately excluded because the
    runner mutates it after staging and it does not define the built main image.
    """

    environment_dir = task_dir / "environment"
    if not environment_dir.is_dir():
        raise FileNotFoundError(f"task environment directory not found: {environment_dir}")

    entries: list[tuple[str, bytes]] = []
    for path in environment_dir.rglob("*"):
        rel = path.relative_to(environment_dir)
        if _HASH_IGNORE_PARTS & set(rel.parts) or path.name in _HASH_IGNORE_NAMES:
            continue
        if path.is_symlink():
            entries.append((rel.as_posix(), f"symlink:{path.readlink()}".encode()))
        elif path.is_file():
            entries.append((rel.as_posix(), path.read_bytes()))

    digest = hashlib.sha256()
    for relative, content in sorted(entries):
        relative_bytes = relative.encode()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def iter_task_dirs(tasks_path: Path) -> list[Path]:
    if (tasks_path / "task.toml").is_file():
        return [tasks_path]
    if not tasks_path.is_dir():
        raise FileNotFoundError(f"tasks path not found: {tasks_path}")
    return sorted(
        child
        for child in tasks_path.iterdir()
        if child.is_dir() and (child / "task.toml").is_file()
    )


def _validate_prebuilt_compose_contract(task_dir: Path) -> None:
    """Reject a task compose file that would override Harbor's prebuilt image."""

    compose_path = task_dir / "environment" / "docker-compose.yaml"
    if not compose_path.is_file():
        return
    try:
        document = yaml.safe_load(compose_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid task Docker Compose YAML: {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    main = services.get("main") if isinstance(services, dict) else None
    if isinstance(main, dict) and ("build" in main or "image" in main):
        conflicting = sorted({"build", "image"} & set(main))
        raise ValueError(
            f"{compose_path}: services.main.{conflicting[0]} overrides the reusable "
            "task image contract; keep main build/image ownership in task.toml"
        )


def resolve_task_images(
    tasks_path: Path,
    *,
    tag: str = DEFAULT_TASK_IMAGE_TAG,
    namespace: str | None = None,
    include_tasks: Iterable[str] | None = None,
    compute_source_hash: bool = True,
) -> list[TaskImage]:
    """Resolve image identities and reject normalized-name collisions."""

    normalized_tag = validate_task_image_tag(tag)
    normalized_namespace = normalize_repository_component(
        namespace or dataset_id_for_tasks(tasks_path),
        label="dataset image namespace",
    )
    include = set(include_tasks) if include_tasks is not None else None
    task_dirs = [p for p in iter_task_dirs(tasks_path) if include is None or p.name in include]
    if include is not None:
        found = {p.name for p in task_dirs}
        missing = sorted(include - found)
        if missing:
            raise ValueError(f"selected task directories not found: {missing}")
    if not task_dirs:
        raise ValueError(f"no Harbor task directories found under: {tasks_path}")

    resolved: list[TaskImage] = []
    refs: dict[str, str] = {}
    for task_dir in task_dirs:
        _validate_prebuilt_compose_contract(task_dir)
        task_toml = task_dir / "task.toml"
        try:
            task_config = tomllib.loads(task_toml.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid task TOML: {task_toml}: {exc}") from exc
        environment = task_config.get("environment") or {}
        if not isinstance(environment, dict):
            raise ValueError(f"task [environment] must be a table: {task_toml}")
        raw_timeout = environment.get("build_timeout_sec")
        build_timeout_sec = float(raw_timeout) if raw_timeout is not None else None
        if build_timeout_sec is not None and build_timeout_sec <= 0:
            raise ValueError(f"task build_timeout_sec must be positive: {task_toml}")
        reference = task_image_reference(
            namespace=normalized_namespace,
            task_name=task_dir.name,
            tag=normalized_tag,
        )
        previous = refs.get(reference)
        if previous is not None and previous != task_dir.name:
            raise ValueError(
                "task image naming collision after Docker normalization: "
                f"{previous!r} and {task_dir.name!r} both map to {reference!r}"
            )
        refs[reference] = task_dir.name
        resolved.append(
            TaskImage(
                task_dir=task_dir,
                task_name=task_dir.name,
                namespace=normalized_namespace,
                tag=normalized_tag,
                reference=reference,
                source_hash=(
                    task_environment_source_hash(task_dir)
                    if compute_source_hash
                    else ""
                ),
                build_timeout_sec=build_timeout_sec,
            )
        )
    return resolved


def _set_toml_table_string(text: str, table: str, key: str, value: str) -> str:
    """Set/add one string key in a top-level table without rewriting the TOML."""

    lines = text.splitlines(keepends=True)
    header = f"[{table}]"
    desired = f'{key} = "{value}"\n'
    header_idx = -1
    table_end = len(lines)
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped == header:
            header_idx = index
            continue
        if header_idx != -1 and stripped.startswith("[") and stripped.endswith("]"):
            table_end = index
            break

    if header_idx == -1:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.extend([f"{header}\n", desired])
        return "".join(lines)

    for index in range(header_idx + 1, table_end):
        stripped = lines[index].strip()
        if "=" in stripped and stripped.split("=", 1)[0].strip() == key:
            if stripped == desired.strip():
                return text
            lines[index] = desired
            return "".join(lines)

    insert_at = header_idx + 1
    while insert_at < table_end and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, desired)
    return "".join(lines)


def inject_task_docker_images(
    tasks_path: Path,
    *,
    tag: str = DEFAULT_TASK_IMAGE_TAG,
    namespace: str | None = None,
) -> int:
    """Inject resolved ``environment.docker_image`` values into staged tasks."""

    changed = 0
    for image in resolve_task_images(
        tasks_path, tag=tag, namespace=namespace, compute_source_hash=False
    ):
        toml_path = image.task_dir / "task.toml"
        original = toml_path.read_text()
        updated = _set_toml_table_string(
            original, "environment", "docker_image", image.reference
        )
        # Parse the result now so failures happen before Harbor sees the task.
        tomllib.loads(updated)
        if updated != original:
            toml_path.write_text(updated)
            changed += 1
    return changed


def _docker_image_labels(reference: str) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", reference],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"failed to inspect Docker image {reference}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "No such image" in stderr or "No such object" in stderr:
            return None
        raise RuntimeError(
            f"failed to inspect Docker image {reference}: {stderr or 'docker inspect failed'}"
        )
    try:
        labels = json.loads(result.stdout.strip() or "null") or {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Docker returned invalid labels for {reference}") from exc
    return {str(key): str(value) for key, value in labels.items()}


def _expected_task_image_labels(image: TaskImage) -> dict[str, str]:
    return {
        TASK_IMAGE_SOURCE_HASH_LABEL: image.source_hash,
        TASK_IMAGE_DATASET_LABEL: image.namespace,
        TASK_IMAGE_TASK_LABEL: image.task_name,
    }


def _build_task_image(image: TaskImage) -> None:
    dockerfile = image.task_dir / "environment" / "Dockerfile"
    context = image.task_dir / "environment"
    if not dockerfile.is_file():
        raise FileNotFoundError(f"task Dockerfile not found: {dockerfile}")
    command = [
        "docker",
        "build",
        "--file",
        str(dockerfile),
    ]
    for name, value in _expected_task_image_labels(image).items():
        command.extend(["--label", f"{name}={value}"])
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
        if value := os.environ.get(name):
            command.extend(["--build-arg", f"{name}={value}"])
    command.extend(["--tag", image.reference, str(context)])
    print(f"Building task image: {image.reference}")
    try:
        subprocess.run(command, check=True, timeout=image.build_timeout_sec)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"timed out building task image {image.reference} after "
            f"{image.build_timeout_sec} seconds"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"failed to build task image {image.reference}") from exc


def ensure_task_images_available(
    images: Iterable[TaskImage],
    *,
    build_missing: bool = False,
    force: bool = False,
) -> tuple[int, int]:
    """Validate local images, optionally building missing/stale mutable images.

    Date tags are immutable snapshots: a mismatching existing image is never
    overwritten unless the explicit builder ``force`` option is used.
    Returns ``(ready, built)``.
    """

    ready = 0
    built = 0
    errors: list[str] = []
    for image in images:
        labels = _docker_image_labels(image.reference)
        expected_labels = _expected_task_image_labels(image)
        mismatched_labels = {
            key: (labels or {}).get(key)
            for key, expected in expected_labels.items()
            if (labels or {}).get(key) != expected
        }
        matches = labels is not None and not mismatched_labels
        if matches and not force:
            ready += 1
            continue

        missing = labels is None
        problem = (
            "missing locally"
            if missing
            else f"stale/unmanaged (mismatched labels={mismatched_labels!r})"
        )
        can_build = build_missing or force
        if can_build and (image.tag == DEFAULT_TASK_IMAGE_TAG or missing or force):
            _build_task_image(image)
            rebuilt_labels = _docker_image_labels(image.reference)
            if any(
                (rebuilt_labels or {}).get(key) != value
                for key, value in expected_labels.items()
            ):
                errors.append(f"{image.reference}: build finished but image labels did not match")
                continue
            ready += 1
            built += 1
            continue

        if image.tag != DEFAULT_TASK_IMAGE_TAG and not missing:
            problem += "; dated tags are immutable (choose a new date or use build --force)"
        errors.append(f"{image.reference}: {problem}")

    if errors:
        hint = (
            "Build the selected images first with `python -m "
            "workbuddy_bench.runner.task_images build ...`."
        )
        raise RuntimeError("task image preflight failed:\n  " + "\n  ".join(errors) + f"\n{hint}")
    return ready, built


def _selected_tasks_from_manifest(path: Path | None) -> list[str] | None:
    if path is None:
        return None
    data = json.loads(path.read_text())
    selected = data.get("selected_tasks") or []
    return [str(name) for name in selected] if selected else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or validate reusable task images.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "build", "list"):
        sub = subparsers.add_parser(name)
        sub.add_argument("tasks_path", type=Path)
        sub.add_argument("--tag", default=DEFAULT_TASK_IMAGE_TAG)
        sub.add_argument("--manifest", type=Path, default=None)
        sub.add_argument(
            "--include-task",
            action="append",
            default=[],
            help="Limit to one task name; may be repeated (manifest selection is the fallback)",
        )
        if name == "preflight":
            sub.add_argument("--build-missing", action="store_true")
        if name == "build":
            sub.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        images = resolve_task_images(
            args.tasks_path,
            tag=args.tag,
            include_tasks=(
                args.include_task
                if args.include_task
                else _selected_tasks_from_manifest(args.manifest)
            ),
        )
        if args.command == "list":
            for image in images:
                print(f"{image.task_name}\t{image.reference}\t{image.source_hash}")
            return 0
        if args.command == "build":
            ready, built = ensure_task_images_available(
                images, build_missing=True, force=args.force
            )
        else:
            ready, built = ensure_task_images_available(
                images, build_missing=args.build_missing
            )
        print(f"Task images ready: ready={ready} built={built} tag={args.tag}")
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
