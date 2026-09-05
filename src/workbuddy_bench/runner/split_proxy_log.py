"""Split a run-level proxy request log into per-trial files after a run.

The proxy cannot know Harbor's final trial directories while requests are in
flight, so it writes one canonical run-level JSONL file. This independently
callable finalizer attributes those records by ``trial_id`` and writes
``agent/requests.jsonl`` below each matching trial.

Only ``model_connection: local_proxy`` runs with ``record_full_io`` enabled are
applicable. Unattributed records remain in the source log. Successful records
are removed from the source only after every destination commit succeeds.
The caller must stop the proxy writer before invoking this finalizer.

    python3 -m workbuddy_bench.runner.split_proxy_log --manifest <manifest.json>
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, TextIO

import yaml

from workbuddy_bench.proxy.interceptors.logger import proxy_log_filename
from workbuddy_bench.runner.sharded_eval import find_harbor_job_dirs


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def legacy_proxy_log_filename(instance_id: str) -> str:
    """Return the filename emitted by LogInterceptor before the hashed scheme."""

    if not instance_id:
        return "proxy_requests.jsonl"
    safe = "".join(
        character if (character.isalnum() or character in "._-") else "_"
        for character in instance_id
    )
    return f"{safe}.jsonl"


def proxy_log_candidates(log_dir: Path, instance_id: str) -> list[Path]:
    """Return canonical then legacy source candidates, without path traversal.

    The final raw-name candidate preserves compatibility with the original
    splitter, which used ``<instance_id>.jsonl`` directly.  It is included only
    when it is a genuine single path segment.
    """

    names = [proxy_log_filename(instance_id)]
    legacy_name = legacy_proxy_log_filename(instance_id)
    if _fits_name_limit(log_dir, legacy_name):
        names.append(legacy_name)
    raw_name = f"{instance_id}.jsonl"
    if (
        instance_id
        and Path(raw_name).name == raw_name
        and "\x00" not in raw_name
        and _fits_name_limit(log_dir, raw_name)
    ):
        names.append(raw_name)

    candidates: list[Path] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            candidates.append(log_dir / name)
            seen.add(name)
    return candidates


def _fits_name_limit(directory: Path, name: str) -> bool:
    """Return whether ``name`` can be probed as one entry below ``directory``."""

    try:
        name_max = os.pathconf(directory, "PC_NAME_MAX")
    except (OSError, ValueError):
        # Linux filesystems used by WorkBuddy conventionally expose NAME_MAX=255.
        # Falling back keeps canonical discovery usable when pathconf itself is
        # unavailable while still excluding pathological legacy candidates.
        name_max = 255
    if name_max < 0:
        return True
    try:
        return len(os.fsencode(name)) <= name_max
    except UnicodeEncodeError:
        return False


def proxy_log_source_lock_filename(source: Path) -> str:
    """Return a stable sidecar lock name shared by every claimant of ``source``.

    Locking the source inode itself is insufficient because finalization replaces
    or unlinks that inode.  Hash the normalized path and keep the lock on a stable
    sidecar instead.
    """

    normalized = str(source.resolve(strict=False))
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    return f".split-proxy-source.{digest}.lock"


def _job_root_from_runtime_config(config_path: Path) -> Path | None:
    """Return the Harbor jobs root declared by a generated runtime config."""

    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(config, dict):
        raise ValueError(f"runtime config must contain a mapping: {config_path}")
    jobs_dir = config.get("jobs_dir")
    if not jobs_dir:
        return None
    root = Path(str(jobs_dir))
    return root if root.is_absolute() else (_repo_root() / root)


def _trial_instance_id(trial_dir: Path) -> str | None:
    """Read the run identity recorded by Harbor in one trial config."""

    config_path = trial_dir / "config.json"
    try:
        config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        return None
    agent = config.get("agent")
    if not isinstance(agent, dict):
        return None
    kwargs = agent.get("kwargs")
    if not isinstance(kwargs, dict) or not kwargs.get("instance_id"):
        return None
    return str(kwargs["instance_id"])


def _trial_roots(job_root: Path) -> Iterator[Path]:
    """Visit experiments and only their directly associated attempt-history rounds."""

    job_root = job_root.resolve()
    history_suffix = ".attempt-history"
    if (job_root / "config.json").is_file() or (
        job_root.parent / (job_root.name + history_suffix)
    ).is_dir():
        experiments = [job_root]
    else:
        experiments = find_harbor_job_dirs(job_root)
        # An experiment can be empty if resume stopped just after archiving.
        if job_root.is_dir():
            experiments.extend(
                history.with_name(history.name.removesuffix(history_suffix))
                for history in job_root.glob(f"*{history_suffix}")
                if history.is_dir()
            )
        if not experiments:
            experiments = [job_root]

    for experiment in sorted(set(experiments)):
        if experiment.is_dir():
            yield experiment
        history = experiment.parent / (experiment.name + history_suffix)
        if history.is_dir():
            yield from (round_dir for round_dir in sorted(history.iterdir()) if round_dir.is_dir())


def _trial_dirs_by_name(job_root: Path, instance_id: str) -> dict[str, Path]:
    """Return unambiguous trial directories belonging to ``instance_id``.

    A configured jobs root can contain many historical experiments. Trial
    basenames are normally unique, but silently letting the last directory win
    would cross-contaminate runs if artifacts were copied or a suffix collided.
    Duplicate names therefore require exactly one matching ``config.json``.
    Unique legacy trials without identity metadata remain supported; a known
    mismatch is never accepted.
    """

    candidates: dict[str, list[Path]] = defaultdict(list)
    for run_dir in _trial_roots(job_root):
        for sub in sorted(run_dir.iterdir()):
            if sub.is_dir() and "__" in sub.name:
                candidates[sub.name].append(sub)

    resolved: dict[str, Path] = {}
    for trial_name, paths in sorted(candidates.items()):
        identities = [(path, _trial_instance_id(path)) for path in paths]
        matches = [path for path, identity in identities if identity == instance_id]

        if len(paths) == 1:
            path, identity = identities[0]
            if identity is None or identity == instance_id:
                resolved[trial_name] = path
            else:
                print(
                    f"[split-proxy-log] ignoring {path}: recorded instance_id "
                    f"{identity!r} does not match {instance_id!r}.",
                    file=sys.stderr,
                )
            continue

        if len(matches) == 1:
            resolved[trial_name] = matches[0]
            continue

        detail = ", ".join(
            f"{path} (instance_id={identity!r})" for path, identity in identities
        )
        reason = "multiple current-instance matches" if matches else "no current-instance match"
        print(
            f"[split-proxy-log] ambiguous trial {trial_name!r}: {reason}; "
            f"leaving its records unattributed. Candidates: {detail}",
            file=sys.stderr,
        )
    return resolved


@contextmanager
def _bounded_file_lock(
    lock_path: Path,
    *,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    """Acquire one advisory lock with a bounded wait."""

    with lock_path.open("a+") as lock_file:
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"timed out after {timeout_seconds:g}s waiting for "
                        f"proxy-log split lock {lock_path}"
                    ) from exc
                time.sleep(min(max(poll_seconds, 0.001), remaining))
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _source_locks(
    sources: list[Path],
    *,
    timeout_seconds: float = 10.0,
    poll_seconds: float = 0.05,
) -> Iterator[None]:
    """Lock every possible source path in deterministic order.

    Legacy filename mappings are not injective, so two different instance ids
    can claim the same source.  Stable path-sidecar locks serialize those claims
    across instances. Locking the canonical candidate also serializes splitters
    of the same instance. The timeout is shared by the full lock set.
    """

    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    with ExitStack() as stack:
        for source in sorted({source.resolve() for source in sources}):
            lock_path = source.parent / proxy_log_source_lock_filename(source)
            stack.enter_context(
                _bounded_file_lock(
                    lock_path,
                    timeout_seconds=max(deadline - time.monotonic(), 0.0),
                    poll_seconds=poll_seconds,
                )
            )
        yield


def _record_identity(raw_line: str) -> tuple[str, str]:
    """Return a stable key used to make destination commits retry-safe."""

    line = raw_line.rstrip("\r\n")
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        record = None
    if isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"]:
        return ("id", record["id"])
    return ("line", hashlib.sha256(line.encode("utf-8")).hexdigest())


def _record_belongs_to_instance(
    record: dict[str, object], instance_id: str, model_route: str
) -> tuple[bool, str]:
    """Validate record ownership without trusting a non-injective filename."""

    if "instance_id" in record:
        recorded_instance = record.get("instance_id")
        if isinstance(recorded_instance, str) and recorded_instance == instance_id:
            return True, ""
        return False, "instance_id mismatch"

    recorded_route = record.get("route")
    if model_route and isinstance(recorded_route, str) and recorded_route == model_route:
        return True, ""
    if not isinstance(recorded_route, str) or not recorded_route:
        return False, "missing instance_id/route ownership"
    if not model_route:
        return False, "manifest missing model_route for legacy ownership"
    return False, "route mismatch"


def _existing_record_index(path: Path) -> tuple[set[str], Counter[str]]:
    ids: set[str] = set()
    fallback_counts: Counter[str] = Counter()
    if not path.exists():
        return ids, fallback_counts
    if not path.is_file():
        raise OSError(f"destination is not a regular file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            kind, value = _record_identity(raw_line)
            if kind == "id":
                ids.add(value)
            else:
                fallback_counts[value] += 1
    return ids, fallback_counts


def _copy_bytes(source: Path, destination: BinaryIO) -> bool:
    """Copy ``source`` and return whether its last byte was a newline."""

    size = source.stat().st_size
    with source.open("rb") as handle:
        shutil.copyfileobj(handle, destination, length=1024 * 1024)
        if not size:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def _fsync_directory(path: Path) -> None:
    """Best-effort durability for a completed rename or unlink."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _atomic_output(destination: Path, mode_source: Path) -> Iterator[BinaryIO]:
    """Commit a complete file on success, preserving append-style permissions."""

    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            yield output
            shutil.copymode(mode_source, temporary)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _commit_trial_records(destination: Path, spool: Path) -> tuple[int, int]:
    """Atomically merge records into any destination, skipping earlier commits."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_ids, fallback_remaining = _existing_record_index(destination)
    destination_existed = destination.is_file()
    newly_written = already_present = 0
    with _atomic_output(destination, destination if destination_existed else spool) as output:
        ends_with_newline = not destination_existed or _copy_bytes(destination, output)
        with spool.open("r", encoding="utf-8") as records:
            for raw_line in records:
                kind, value = _record_identity(raw_line)
                if kind == "id":
                    if value in existing_ids:
                        already_present += 1
                        continue
                    existing_ids.add(value)
                elif fallback_remaining[value] > 0:
                    fallback_remaining[value] -= 1
                    already_present += 1
                    continue

                if not ends_with_newline:
                    output.write(b"\n")
                output.write(raw_line.rstrip("\n").encode("utf-8") + b"\n")
                ends_with_newline = True
                newly_written += 1
    return newly_written, already_present


def _atomic_replace_from_file(source: Path, destination: Path) -> None:
    with _atomic_output(destination, destination if destination.is_file() else source) as output:
        with source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)


def _source_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _record_destination(
    record: dict[str, object], trial_dirs: dict[str, Path], instance_id: str
) -> Path | None:
    """Resolve the output separately from ownership checks and atomic commits.

    Only agent output is supported today. Future request purposes can select
    another file here without changing spooling, locking or retry semantics.
    """

    meta = record.get("meta")
    trial_id = record.get("trial_id") or (meta.get("trial_id") if isinstance(meta, dict) else "")
    if not isinstance(trial_id, str) or not trial_id or trial_id == instance_id:
        return None
    trial_dir = trial_dirs.get(trial_id)
    return trial_dir / "agent" / "requests.jsonl" if trial_dir is not None else None


def _split_locked(
    src: Path,
    trial_dirs: dict[str, Path],
    instance_id: str,
    model_route: str,
) -> int:
    source_signature = _source_signature(src)
    attributed = unattributed = 0
    rejected_ownership: Counter[str] = Counter()

    with tempfile.TemporaryDirectory(prefix=".split-proxy-log.", dir=src.parent) as temp_name:
        temp_dir = Path(temp_name)
        unattributed_spool = temp_dir / "_unattributed.jsonl"
        spools: dict[Path, Path] = {}
        with ExitStack() as stack:
            handles: dict[Path, TextIO] = {}
            source = stack.enter_context(src.open("r", encoding="utf-8"))
            unknown = stack.enter_context(unattributed_spool.open("w", encoding="utf-8"))
            for raw_line in source:
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                destination = None
                if isinstance(record, dict):
                    owned, error = _record_belongs_to_instance(record, instance_id, model_route)
                    if owned:
                        destination = _record_destination(record, trial_dirs, instance_id)
                    else:
                        rejected_ownership[error] += 1
                if destination is None:
                    unknown.write(line + "\n")
                    unattributed += 1
                    continue
                if destination not in handles:
                    spool = temp_dir / f"{len(spools)}.jsonl"
                    spools[destination] = spool
                    handles[destination] = stack.enter_context(spool.open("w", encoding="utf-8"))
                handles[destination].write(line + "\n")
                attributed += 1

        if rejected_ownership:
            detail = ", ".join(
                f"{reason}: {count}" for reason, count in sorted(rejected_ownership.items())
            )
            print(
                f"[split-proxy-log] WARNING: retained {sum(rejected_ownership.values())} "
                f"record(s) with unverifiable or foreign ownership in {src}: {detail}",
                file=sys.stderr,
            )

        newly_written = already_present = 0
        for destination, spool in sorted(spools.items()):
            added, existing = _commit_trial_records(destination, spool)
            newly_written += added
            already_present += existing

        # The caller stops the writer; this check catches accidental live use.
        # Committed destinations are retry-safe if a concurrent append is detected.
        if _source_signature(src) != source_signature:
            raise RuntimeError(
                f"source proxy log changed while splitting: {src}; stop the proxy and retry"
            )
        if unattributed:
            _atomic_replace_from_file(unattributed_spool, src)
            source_action = f"{unattributed} unattributed record(s) kept in {src}"
        else:
            src.unlink()
            _fsync_directory(src.parent)
            source_action = f"removed {src}"

    print(
        f"[split-proxy-log] attributed {attributed} record(s) to "
        f"{len(spools)} file(s): wrote {newly_written}, "
        f"already present {already_present}; {source_action}."
    )
    return 0


def split_proxy_log(
    manifest_path: Path,
    log_dir: Path | None = None,
    results_root: Path | None = None,
    job_root: Path | None = None,
    runtime_config: Path | None = None,
) -> int:
    """Split one run log; retain the public signature used by existing callers."""

    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise ValueError(f"manifest must contain a JSON object: {manifest_path}")

    if str(manifest.get("model_connection") or "") != "local_proxy":
        print("[split-proxy-log] not a local_proxy run; nothing to split.")
        return 0
    if not manifest.get("record_full_io"):
        print("[split-proxy-log] record_full_io off; no per-request log to split.")
        return 0

    instance_id = str(manifest.get("instance_id") or "")
    job_slug = str(manifest.get("job_slug") or "")
    if not instance_id or not job_slug:
        raise ValueError("manifest missing required instance_id/job_slug")

    log_dir = log_dir or (_repo_root() / "scripts" / "logs" / "proxy")
    if not log_dir.is_dir():
        expected = log_dir / proxy_log_filename(instance_id)
        print(f"[split-proxy-log] no proxy log at {expected}; nothing to split.")
        return 0

    if job_root is None and runtime_config is not None:
        job_root = _job_root_from_runtime_config(runtime_config)
    if job_root is None:
        job_root = (results_root or (_repo_root() / "results")) / job_slug

    connection = manifest.get("connection")
    connection_route = (
        connection.get("model_route") if isinstance(connection, dict) else ""
    )
    model_route = str(manifest.get("model_route") or connection_route or "")

    candidates = proxy_log_candidates(log_dir, instance_id)
    with _source_locks(candidates):
        # Discover after locking: upgraded resumes can leave both old and new
        # sources, and different instances may share a legacy filename.
        sources = [candidate for candidate in candidates if candidate.is_file()]
        if not sources:
            print("[split-proxy-log] proxy log was already consumed; nothing to split.")
            return 0
        trial_dirs = _trial_dirs_by_name(job_root, instance_id)
        if not trial_dirs:
            print(
                f"[split-proxy-log] no matching trial dirs under {job_root}; "
                "leaving log in place."
            )
            return 0
        for source in sources:
            _split_locked(source, trial_dirs, instance_id, model_route)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="Resolved run manifest JSON.")
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="Proxy log dir (default: <repo>/scripts/logs/proxy).",
    )
    parser.add_argument(
        "--job-dir", type=Path, default=None,
        help="Harbor experiment dir holding the trials (in-place resume passes this).",
    )
    parser.add_argument(
        "--runtime-config", type=Path, default=None,
        help="Generated Harbor runtime YAML; its jobs_dir locates the trials.",
    )
    args = parser.parse_args()
    try:
        return split_proxy_log(
            args.manifest,
            args.log_dir,
            job_root=args.job_dir,
            runtime_config=args.runtime_config,
        )
    except Exception as exc:  # caller decides whether finalization is run-fatal
        print(f"[split-proxy-log] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
