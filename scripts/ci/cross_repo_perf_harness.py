"""Trusted payload builder and early pytest plugin for cross-repo benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Callable, Mapping, Sequence

PAYLOAD_MANIFEST_NAME = "payload-manifest.json"
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_COLLECTION_NODEIDS = 10_000
MAX_NODEID_BYTES = 16_384
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

ENV_PAIR = "CROSS_REPO_PERF_PAIR"
ENV_RUN_ID = "CROSS_REPO_PERF_RUN_ID"
ENV_RUN_ATTEMPT = "CROSS_REPO_PERF_RUN_ATTEMPT"
ENV_OUTPUT_ROOT = "CROSS_REPO_PERF_OUTPUT_ROOT"
ENV_PAYLOAD_SHA256 = "CROSS_REPO_PERF_PAYLOAD_SHA256"
ENV_MANIFEST_SHA256 = "CROSS_REPO_PERF_MANIFEST_SHA256"
ENV_HARNESS_SHA256 = "CROSS_REPO_PERF_HARNESS_SHA256"

_PAIR_PATH_NAMES = {
    "HOME": "home",
    "TMPDIR": "tmp",
    "TMP": "tmp",
    "TEMP": "tmp",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_CONFIG_HOME": "xdg-config",
    "XDG_DATA_HOME": "xdg-data",
    "XDG_STATE_HOME": "xdg-state",
    "UV_CACHE_DIR": "uv-cache",
    "PIP_CACHE_DIR": "pip-cache",
    "CCACHE_DIR": "ccache",
    "TILELANG_CACHE_DIR": "tilelang-cache",
    "TILELANG_TMP_DIR": "tilelang-tmp",
    "TRITON_CACHE_DIR": "triton-cache",
    "TORCH_EXTENSIONS_DIR": "torch-extensions",
    "TORCH_HOME": "torch-home",
    "CUDA_CACHE_PATH": "cuda-cache",
    "PYTHONPYCACHEPREFIX": "pycache",
    "NUMBA_CACHE_DIR": "numba-cache",
    "MACA_CACHE_DIR": "maca-cache",
    "MCC_CACHE_DIR": "mcc-cache",
    "MXCC_CACHE_DIR": "mxcc-cache",
}

_TOP_LEVEL_ALLOWED = {
    "benchmarks",
    "cross_repo_perf_harness.py",
    PAYLOAD_MANIFEST_NAME,
    "pyproject.toml",
    "tests",
    "trusted_manifest",
    "workloads",
}
_TEST_FILES = {
    "tests/__init__.py",
    "tests/test_base.py",
    "tests/ops/__init__.py",
    "tests/ops/test_mamba.py",
}
_POISON_FILENAMES = {"sitecustomize.py", "usercustomize.py"}


class HarnessError(RuntimeError):
    """Raised when the trusted benchmark boundary cannot be established."""


@dataclass(frozen=True)
class _PluginState:
    pair: str
    run_id: int
    run_attempt: int
    payload_root: Path
    environment_root: Path
    collection_path: Path
    payload_sha256: str
    manifest_sha256: str
    harness_sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise HarnessError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HarnessError(f"{context} must be a positive integer")
    return value


def _strict_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _assert_no_symlink_components(path: Path) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise HarnessError(f"cannot inspect path component {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise HarnessError(f"path contains a symlink component: {current}")
    return absolute


def _load_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> dict[str, object]:
    path = _assert_no_symlink_components(path)
    flags = os.O_RDONLY
    for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HarnessError(f"cannot open JSON file {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HarnessError(f"JSON file must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise HarnessError(f"JSON file exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > max_bytes:
            raise HarnessError(f"JSON file exceeds {max_bytes} bytes: {path}")
    except OSError as exc:
        raise HarnessError(f"cannot read JSON file {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_strict_json_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise HarnessError(f"invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"JSON file must contain an object: {path}")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise HarnessError(f"JSON output exceeds {MAX_JSON_BYTES} bytes: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _checked_directory(path: Path, context: str) -> Path:
    path = _assert_no_symlink_components(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise HarnessError(f"required {context} is missing: {path}") from exc
    if stat.S_ISLNK(mode):
        raise HarnessError(f"{context} must not be a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise HarnessError(f"required {context} must be a directory: {path}")
    return path.resolve()


def _checked_regular_file(path: Path, context: str) -> Path:
    path = _assert_no_symlink_components(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise HarnessError(f"required {context} is missing: {path}") from exc
    if stat.S_ISLNK(mode):
        raise HarnessError(f"{context} must not be a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise HarnessError(f"required {context} must be a regular file: {path}")
    return path


def _copy_regular_file(source: Path, destination: Path, context: str) -> None:
    source = _checked_regular_file(source, context)
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise HarnessError(f"cannot open trusted source file {source}: {exc}") from exc
    try:
        mode = os.fstat(source_fd).st_mode
        if not stat.S_ISREG(mode):
            raise HarnessError(f"{context} must remain a regular file: {source}")
        with (
            os.fdopen(source_fd, "rb", closefd=False) as input_stream,
            destination.open("xb") as output_stream,
        ):
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        os.chmod(destination, 0o644)
    finally:
        os.close(source_fd)


def _copy_tree(source: Path, destination: Path, context: str) -> None:
    source = _checked_directory(source, context)
    destination.mkdir(parents=True, exist_ok=False)
    for entry in sorted(os.scandir(source), key=lambda item: item.name):
        entry_path = Path(entry.path)
        target = destination / entry.name
        mode = entry.stat(follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            raise HarnessError(f"{context} contains a symlink: {entry_path}")
        if stat.S_ISDIR(mode):
            _copy_tree(entry_path, target, context)
        elif stat.S_ISREG(mode):
            _copy_regular_file(entry_path, target, context)
        else:
            raise HarnessError(
                f"{context} entries must be a regular file or directory: {entry_path}"
            )


def _relative_path(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise HarnessError(f"{context} must be a safe relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise HarnessError(f"{context} must be a safe relative path")
    return pure.as_posix()


def _file_records(root: Path, *, exclude: set[str] | None = None) -> list[dict[str, object]]:
    root = _checked_directory(root, "tree root")
    excluded = exclude or set()
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise HarnessError(f"tree contains a symlink: {path}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise HarnessError(f"tree entries must be a regular file or directory: {path}")
        if relative in excluded:
            continue
        records.append(
            {"path": relative, "size": path.stat().st_size, "sha256": _sha256_file(path)}
        )
    return records


def _records_sha256(records: Sequence[Mapping[str, object]]) -> str:
    encoded = json.dumps(
        list(records), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def canonical_tree_sha256(root: Path | str) -> str:
    """Return a deterministic digest of relative paths, sizes, and file bytes."""

    return _records_sha256(_file_records(Path(root)))


def audit_payload_root(root: Path | str) -> None:
    """Reject import shadowing, startup hooks, metadata, and non-regular nodes."""

    root = _checked_directory(Path(root), "payload root")
    for top_level in root.iterdir():
        if top_level.name not in _TOP_LEVEL_ALLOWED:
            raise HarnessError(f"unexpected payload files include top-level path: {top_level.name}")
    for poison in ("tileops", "tileops.py", "tilelang", "tilelang.py"):
        if (root / poison).exists() or (root / poison).is_symlink():
            raise HarnessError(f"payload contains importable source package: {poison}")
    actual_test_files: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise HarnessError(f"payload contains a symlink: {relative}")
        if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
            raise HarnessError(f"payload entries must be a regular file or directory: {relative}")
        if path.name == ".git":
            raise HarnessError(f"payload contains repository metadata: {relative}")
        if stat.S_ISREG(mode):
            if path.suffix.lower() == ".pth" or path.name in _POISON_FILENAMES:
                raise HarnessError(f"payload contains a Python startup hook: {relative}")
            if relative.startswith("tests/"):
                actual_test_files.add(relative)
    if (root / "tests").exists() and actual_test_files != _TEST_FILES:
        raise HarnessError("payload tests tree differs from the trusted helper allowlist")


def _copy_manifest_snapshot(source: Path, destination: Path) -> None:
    source = _checked_directory(source, "TileOps manifest directory")
    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    for path in sorted(source.iterdir(), key=lambda item: item.name):
        if path.name.endswith(".yaml"):
            _copy_regular_file(path, destination / path.name, "TileOps manifest YAML")
            copied += 1
    if copied == 0:
        raise HarnessError("required TileOps manifest directory has no YAML files")


def build_trusted_payload(
    baseline_root: Path | str,
    payload_root: Path | str,
    *,
    expected_harness_sha256: str | None = None,
) -> dict[str, object]:
    """Copy the fixed baseline-only benchmark payload into a neutral directory."""

    baseline_root = _checked_directory(Path(baseline_root), "baseline checkout")
    payload_root = Path(payload_root)
    if payload_root.exists() or payload_root.is_symlink():
        raise HarnessError(f"payload destination must not already exist: {payload_root}")
    if expected_harness_sha256 is not None:
        expected_harness_sha256 = _require_sha256(
            expected_harness_sha256, "expected harness SHA-256"
        )
    payload_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{payload_root.name}.", dir=payload_root.parent))
    try:
        _copy_tree(baseline_root / "benchmarks", temporary / "benchmarks", "benchmarks")
        _copy_tree(baseline_root / "workloads", temporary / "workloads", "workloads")
        for relative in sorted(_TEST_FILES):
            _copy_regular_file(
                baseline_root / relative, temporary / relative, f"trusted helper {relative}"
            )
        _copy_manifest_snapshot(baseline_root / "tileops/manifest", temporary / "trusted_manifest")
        _copy_regular_file(
            baseline_root / "pyproject.toml",
            temporary / "pyproject.toml",
            "pytest configuration",
        )
        _copy_regular_file(
            baseline_root / "scripts/ci/cross_repo_perf_harness.py",
            temporary / "cross_repo_perf_harness.py",
            "trusted pytest harness",
        )
        audit_payload_root(temporary)
        harness_sha256 = _sha256_file(temporary / "cross_repo_perf_harness.py")
        if expected_harness_sha256 is not None and harness_sha256 != expected_harness_sha256:
            raise HarnessError("trusted harness SHA-256 does not match the resolver value")
        records = _file_records(temporary)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "payload_sha256": _records_sha256(records),
            "manifest_sha256": canonical_tree_sha256(temporary / "trusted_manifest"),
            "harness_sha256": harness_sha256,
            "files": records,
        }
        _atomic_write_json(temporary / PAYLOAD_MANIFEST_NAME, manifest)
        audit_payload_root(temporary)
        temporary.rename(payload_root)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _validate_payload_manifest(value: dict[str, object]) -> list[dict[str, object]]:
    expected_keys = {
        "schema_version",
        "payload_sha256",
        "manifest_sha256",
        "harness_sha256",
        "files",
    }
    if set(value) != expected_keys:
        raise HarnessError("payload manifest keys do not match the schema")
    if value["schema_version"] != 1:
        raise HarnessError("payload manifest schema_version must be 1")
    _require_sha256(value["payload_sha256"], "payload manifest payload_sha256")
    _require_sha256(value["manifest_sha256"], "payload manifest manifest_sha256")
    _require_sha256(value["harness_sha256"], "payload manifest harness_sha256")
    files = value["files"]
    if not isinstance(files, list) or len(files) > 20_000:
        raise HarnessError("payload manifest files must be a bounded list")
    validated: list[dict[str, object]] = []
    previous = ""
    for index, record in enumerate(files):
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise HarnessError(f"payload manifest file record {index} is invalid")
        relative = _relative_path(record["path"], f"payload manifest files[{index}].path")
        size = record["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise HarnessError(f"payload manifest files[{index}].size is invalid")
        digest = _require_sha256(record["sha256"], f"payload manifest files[{index}].sha256")
        if relative <= previous:
            raise HarnessError("payload manifest file paths must be unique and sorted")
        previous = relative
        validated.append({"path": relative, "size": size, "sha256": digest})
    return validated


def verify_trusted_payload(
    payload_root: Path | str, *, expected_harness_sha256: str | None = None
) -> dict[str, object]:
    """Revalidate a completed payload and return its strict manifest."""

    payload_root = _checked_directory(Path(payload_root), "payload root")
    audit_payload_root(payload_root)
    manifest = _load_json(payload_root / PAYLOAD_MANIFEST_NAME)
    expected_records = _validate_payload_manifest(manifest)
    harness_digest = _sha256_file(payload_root / "cross_repo_perf_harness.py")
    if harness_digest != manifest["harness_sha256"]:
        raise HarnessError("harness SHA-256 does not match the payload manifest")
    if expected_harness_sha256 is not None:
        expected = _require_sha256(expected_harness_sha256, "expected harness SHA-256")
        if harness_digest != expected:
            raise HarnessError("harness SHA-256 does not match the resolver value")
    actual_records = _file_records(payload_root, exclude={PAYLOAD_MANIFEST_NAME})
    if actual_records != expected_records:
        raise HarnessError("unexpected payload files or file content")
    if _records_sha256(actual_records) != manifest["payload_sha256"]:
        raise HarnessError("payload SHA-256 does not match the payload manifest")
    manifest_digest = canonical_tree_sha256(payload_root / "trusted_manifest")
    if manifest_digest != manifest["manifest_sha256"]:
        raise HarnessError("manifest SHA-256 does not match the payload manifest")
    return manifest


def install_manifest_override(
    manifest_root: Path | str, *, manifest_module: ModuleType | object | None = None
) -> str:
    """Force ``tileops.manifest`` to read only the trusted YAML snapshot."""

    manifest_root = _checked_directory(Path(manifest_root), "trusted manifest directory")
    files: list[Path] = []
    for path in sorted(manifest_root.iterdir(), key=lambda item: item.name):
        if path.name.endswith(".yaml"):
            files.append(_checked_regular_file(path, "trusted manifest YAML"))
    if not files:
        raise HarnessError("trusted manifest directory must contain YAML files")
    if manifest_module is None:
        manifest_module = importlib.import_module("tileops.manifest")
    load_manifest = getattr(manifest_module, "load_manifest", None)
    cache_clear = getattr(load_manifest, "cache_clear", None)
    if not callable(cache_clear):
        raise HarnessError("tileops.manifest.load_manifest must expose cache_clear()")
    manifest_module.manifest_files = lambda: list(files)
    cache_clear()
    return canonical_tree_sha256(manifest_root)


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError):
        return False
    return True


def audit_import_origins(
    origins: Mapping[str, Path | str],
    payload_root: Path | str,
    environment_root: Path | str,
) -> None:
    """Verify trusted helpers come from payload and code packages from the venv."""

    payload_root = _checked_directory(Path(payload_root), "payload root")
    environment_root = _checked_directory(Path(environment_root), "environment root")
    expected = {
        "benchmarks": payload_root,
        "workloads": payload_root,
        "tests": payload_root,
        "tileops": environment_root,
        "tilelang": environment_root,
    }
    if set(origins) != set(expected):
        raise HarnessError("import-origin audit received an incomplete module set")
    for name, required_root in expected.items():
        origin = Path(origins[name])
        if not _path_within(origin, required_root):
            raise HarnessError(f"{name} import resolved outside {required_root}: {origin}")


def _module_origin(name: str) -> Path:
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or not origin:
        raise HarnessError(f"{name} must resolve to a file-backed package")
    return Path(origin)


def _audit_runtime_imports(payload_root: Path, environment_root: Path) -> None:
    origins = {
        name: _module_origin(name)
        for name in ("benchmarks", "workloads", "tests", "tileops", "tilelang")
    }
    audit_import_origins(origins, payload_root, environment_root)


def _install_benchmark_report_redirect(state: _PluginState) -> None:
    module = importlib.import_module("benchmarks.benchmark_base")
    origin = _module_origin("benchmarks.benchmark_base")
    if not _path_within(origin, state.payload_root):
        raise HarnessError(f"benchmark report module resolved outside payload: {origin}")
    report = getattr(module, "BenchmarkReport", None)
    current_dump = getattr(report, "dump", None)
    if not callable(current_dump):
        raise HarnessError("benchmarks.benchmark_base.BenchmarkReport.dump must be callable")
    original_dump = getattr(current_dump, "_cross_repo_perf_original", current_dump)
    report_path = state.collection_path.parent / "profile_run.log"

    def trusted_dump(path: str) -> None:
        requested = Path(path)
        if requested.is_absolute() or requested.parts != ("profile_run.log",):
            raise HarnessError("BenchmarkReport.dump target must be profile_run.log")
        original_dump(str(report_path))

    trusted_dump._cross_repo_perf_original = original_dump
    report.dump = staticmethod(trusted_dump)


def _nodeids_sha256(nodeids: Sequence[str]) -> str:
    return _sha256_bytes(("\n".join(nodeids) + "\n").encode("utf-8"))


def collection_fingerprint(
    payload_sha256: str, manifest_sha256: str, nodeids: Sequence[str]
) -> str:
    """Hash the comparison inputs used to establish collection equivalence."""

    payload_sha256 = _require_sha256(payload_sha256, "payload_sha256")
    manifest_sha256 = _require_sha256(manifest_sha256, "manifest_sha256")
    sorted_nodeids = _validated_nodeids(nodeids)
    value = {
        "payload_sha256": payload_sha256,
        "manifest_sha256": manifest_sha256,
        "nodeids_sha256": _nodeids_sha256(sorted_nodeids),
    }
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _validated_nodeids(nodeids: Sequence[str]) -> list[str]:
    if len(nodeids) > MAX_COLLECTION_NODEIDS:
        raise HarnessError("collection exceeds the 10000 node limit")
    validated: list[str] = []
    for index, nodeid in enumerate(nodeids):
        if not isinstance(nodeid, str) or not nodeid:
            raise HarnessError(f"node ID {index} must be a non-empty string")
        if len(nodeid.encode("utf-8")) > MAX_NODEID_BYTES:
            raise HarnessError(f"node ID {index} exceeds {MAX_NODEID_BYTES} bytes")
        validated.append(nodeid)
    if len(set(validated)) != len(validated):
        raise HarnessError("collection contains duplicate node IDs")
    return sorted(validated)


def _validate_pair_path_environment(pair_root: Path | str, environment: Mapping[str, str]) -> None:
    pair_root = _checked_directory(Path(pair_root), "pair root")
    for name, relative in _PAIR_PATH_NAMES.items():
        value = environment.get(name)
        if not value:
            raise HarnessError(f"missing required pair path environment: {name}")
        path = _checked_directory(Path(value), f"pair environment path {name}")
        expected = (pair_root / relative).resolve()
        if path != expected:
            raise HarnessError(f"pair environment path escapes pair root: {name}={path}")


def collection_document(
    *,
    pair: str,
    run_id: int,
    run_attempt: int,
    payload_sha256: str,
    manifest_sha256: str,
    harness_sha256: str,
    nodeids: Sequence[str],
) -> dict[str, object]:
    """Build the strict Task 2 ``collection.json`` document."""

    if pair not in {"baseline", "candidate"}:
        raise HarnessError("pair must be baseline or candidate")
    run_id = _require_positive_int(run_id, "run_id")
    run_attempt = _require_positive_int(run_attempt, "run_attempt")
    payload_sha256 = _require_sha256(payload_sha256, "payload_sha256")
    manifest_sha256 = _require_sha256(manifest_sha256, "manifest_sha256")
    harness_sha256 = _require_sha256(harness_sha256, "harness_sha256")
    sorted_nodeids = _validated_nodeids(nodeids)
    return {
        "schema_version": 1,
        "pair": pair,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "payload_sha256": payload_sha256,
        "manifest_sha256": manifest_sha256,
        "harness_sha256": harness_sha256,
        "nodeids_sha256": _nodeids_sha256(sorted_nodeids),
        "count": len(sorted_nodeids),
        "nodeids": sorted_nodeids,
    }


def _required_environment() -> _PluginState:
    missing = [
        name
        for name in (
            ENV_PAIR,
            ENV_RUN_ID,
            ENV_RUN_ATTEMPT,
            ENV_OUTPUT_ROOT,
            ENV_PAYLOAD_SHA256,
            ENV_MANIFEST_SHA256,
            ENV_HARNESS_SHA256,
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise HarnessError(f"missing required harness environment: {', '.join(missing)}")
    try:
        run_id_value = int(os.environ[ENV_RUN_ID])
        run_attempt_value = int(os.environ[ENV_RUN_ATTEMPT])
    except ValueError as exc:
        raise HarnessError("run ID and attempt must be decimal integers") from exc
    pair = os.environ[ENV_PAIR]
    if pair not in {"baseline", "candidate"}:
        raise HarnessError("pair must be baseline or candidate")
    payload_root = _checked_directory(Path.cwd(), "payload root")
    environment_root = _checked_directory(Path(sys.prefix), "environment root")
    _checked_regular_file(environment_root / "pyvenv.cfg", "environment pyvenv.cfg")
    executable = Path(sys.executable).absolute()
    try:
        executable.relative_to(environment_root / "bin")
    except ValueError as exc:
        raise HarnessError("pytest must run from the pair virtual environment") from exc
    pair_root = _checked_directory(environment_root.parent, "pair root")
    output_root = _checked_directory(Path(os.environ[ENV_OUTPUT_ROOT]), "output root")
    if output_root.parent != pair_root:
        raise HarnessError("output root must be a direct child of the pair root")
    if payload_root == environment_root or _path_within(payload_root, environment_root):
        raise HarnessError("payload root must remain outside the pair virtual environment")
    if payload_root == output_root or _path_within(payload_root, output_root):
        raise HarnessError("payload root must remain outside the pair output root")
    _validate_pair_path_environment(pair_root, os.environ)
    return _PluginState(
        pair=pair,
        run_id=_require_positive_int(run_id_value, "run ID"),
        run_attempt=_require_positive_int(run_attempt_value, "run attempt"),
        payload_root=payload_root,
        environment_root=environment_root,
        collection_path=output_root / "collection.json",
        payload_sha256=_require_sha256(os.environ[ENV_PAYLOAD_SHA256], "payload SHA-256"),
        manifest_sha256=_require_sha256(os.environ[ENV_MANIFEST_SHA256], "manifest SHA-256"),
        harness_sha256=_require_sha256(os.environ[ENV_HARNESS_SHA256], "harness SHA-256"),
    )


def _verify_plugin_state(state: _PluginState) -> None:
    manifest = verify_trusted_payload(
        state.payload_root, expected_harness_sha256=state.harness_sha256
    )
    for key, expected in (
        ("payload_sha256", state.payload_sha256),
        ("manifest_sha256", state.manifest_sha256),
        ("harness_sha256", state.harness_sha256),
    ):
        if manifest[key] != expected:
            raise HarnessError(f"{key} differs from the trusted workflow input")
    module_path = Path(__file__).resolve()
    expected_module_path = (state.payload_root / "cross_repo_perf_harness.py").resolve()
    if module_path != expected_module_path:
        raise HarnessError(f"pytest loaded the harness from an untrusted path: {module_path}")


def _install_verified_plugin_state(state: _PluginState) -> None:
    _verify_plugin_state(state)
    digest = install_manifest_override(state.payload_root / "trusted_manifest")
    if digest != state.manifest_sha256:
        raise HarnessError("installed manifest override has an unexpected SHA-256")
    _audit_runtime_imports(state.payload_root, state.environment_root)
    _install_benchmark_report_redirect(state)


def pytest_load_initial_conftests(early_config, parser, args) -> None:
    """Install the manifest override before pytest imports initial conftests."""

    del parser, args
    state = _required_environment()
    _install_verified_plugin_state(state)
    early_config._cross_repo_perf_state = state


def pytest_configure(config) -> None:
    """Reverify the payload and restore the manifest after conftest imports."""

    state = _required_environment()
    existing = getattr(config, "_cross_repo_perf_state", None)
    if existing is not None and existing != state:
        raise HarnessError("harness environment changed during pytest startup")
    _install_verified_plugin_state(state)
    config._cross_repo_perf_state = state


def pytest_collection_finish(session) -> None:
    """Record the sorted collection and its trusted-input digests atomically."""

    state = getattr(session.config, "_cross_repo_perf_state", None)
    if state is None:
        raise HarnessError("cross-repo performance harness was not configured")
    _audit_runtime_imports(state.payload_root, state.environment_root)
    document = collection_document(
        pair=state.pair,
        run_id=state.run_id,
        run_attempt=state.run_attempt,
        payload_sha256=state.payload_sha256,
        manifest_sha256=state.manifest_sha256,
        harness_sha256=state.harness_sha256,
        nodeids=[item.nodeid for item in session.items],
    )
    _atomic_write_json(state.collection_path, document)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Detect mutation of any trusted payload input during execution."""

    del exitstatus
    state = getattr(session.config, "_cross_repo_perf_state", None)
    if state is None:
        return
    try:
        manifest = verify_trusted_payload(
            state.payload_root, expected_harness_sha256=state.harness_sha256
        )
        if (
            manifest["payload_sha256"] != state.payload_sha256
            or manifest["manifest_sha256"] != state.manifest_sha256
            or manifest["harness_sha256"] != state.harness_sha256
        ):
            raise HarnessError("trusted benchmark input digests changed during pytest execution")
    except HarnessError:
        session.exitstatus = 3
        raise


def _resolved_context(path: Path) -> dict[str, object]:
    value = _load_json(path)
    expected_keys = {
        "schema_version",
        "disposition",
        "reason",
        "run_id",
        "run_attempt",
        "trigger_comment_id",
        "trigger_actor",
        "harness_sha256",
        "tileops",
        "tilelang",
    }
    if set(value) != expected_keys:
        raise HarnessError("resolved.json keys do not match the schema")
    if value["schema_version"] != 1 or value["disposition"] != "run":
        raise HarnessError("resolved.json does not authorize a benchmark run")
    _require_positive_int(value["run_id"], "resolved run ID")
    _require_positive_int(value["run_attempt"], "resolved run attempt")
    _require_positive_int(value["trigger_comment_id"], "resolved trigger comment ID")
    _require_sha256(value["harness_sha256"], "resolved harness_sha256")
    if not isinstance(value["reason"], str) or not isinstance(value["trigger_actor"], str):
        raise HarnessError("resolved.json string fields are invalid")
    if not value["trigger_actor"]:
        raise HarnessError("resolved trigger actor must not be empty")
    if not isinstance(value["tileops"], dict) or not isinstance(value["tilelang"], dict):
        raise HarnessError("resolved repository identities must be objects")
    return value


def _resolved_harness_sha256(path: Path) -> str:
    return str(_resolved_context(path)["harness_sha256"])


def _validated_system_path(value: str) -> str:
    if not value:
        raise HarnessError("system PATH must not be empty")
    allowed_roots = tuple(Path(root).resolve() for root in ("/usr", "/bin", "/sbin", "/opt"))
    entries: list[str] = []
    for text in value.split(os.pathsep):
        path = Path(text)
        if not text or not path.is_absolute():
            raise HarnessError(f"system PATH contains a non-absolute entry: {text or '<empty>'}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise HarnessError(f"system PATH entry cannot be resolved: {path}: {exc}") from exc
        if not resolved.is_dir() or not any(
            resolved == root or resolved.is_relative_to(root) for root in allowed_roots
        ):
            raise HarnessError(f"system PATH entry is outside trusted roots: {resolved}")
        lowered = resolved.as_posix().lower()
        if any(
            marker in lowered for marker in ("/conda", "/mamba", "/miniforge", "/venv", "/.venv")
        ):
            raise HarnessError(f"system PATH contains a Python environment: {resolved}")
        normalized = str(resolved)
        if normalized not in entries:
            entries.append(normalized)
    return os.pathsep.join(entries)


def _checked_python_executable(path: Path) -> tuple[Path, Path]:
    absolute = path.absolute()
    environment_root = _checked_directory(absolute.parent.parent, "environment root")
    if absolute.parent != environment_root / "bin":
        raise HarnessError("Python executable must be inside environment_root/bin")
    _checked_regular_file(environment_root / "pyvenv.cfg", "environment pyvenv.cfg")
    try:
        mode = absolute.lstat().st_mode
    except OSError as exc:
        raise HarnessError(f"cannot inspect Python executable {absolute}: {exc}") from exc
    try:
        resolved_is_file = absolute.resolve(strict=True).is_file()
    except OSError as exc:
        raise HarnessError(f"cannot resolve Python executable {absolute}: {exc}") from exc
    if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)) or not resolved_is_file:
        raise HarnessError("Python executable must resolve to a regular file")
    return absolute, environment_root


def _benchmark_environment(
    *,
    python_executable: Path,
    pair_root: Path,
    output_root: Path,
    pair: str,
    run_id: int,
    run_attempt: int,
    manifest: Mapping[str, object],
    system_path: str,
    maca_path: Path,
    maca_library_path: str,
    tilelang_mxcc_flags: str,
) -> dict[str, str]:
    maca_path = _checked_directory(maca_path, "MACA installation")
    library_directories: list[str] = []
    for value in maca_library_path.split(os.pathsep):
        if not value:
            raise HarnessError("MACA library path contains an empty entry")
        directory = _checked_directory(Path(value), "MACA library directory")
        canonical = str(directory)
        if canonical not in library_directories:
            library_directories.append(canonical)
    if not re.fullmatch(r"-gcc-version [1-9][0-9]*", tilelang_mxcc_flags):
        raise HarnessError("TileLang mxcc flags must contain one fixed GCC major version")
    environment = {
        key: value for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ") if (value := os.environ.get(key))
    }
    environment["PATH"] = os.pathsep.join(
        (str(python_executable.parent), _validated_system_path(system_path))
    )
    created: dict[str, Path] = {}
    for name, relative in _PAIR_PATH_NAMES.items():
        path = created.setdefault(relative, pair_root / relative)
        path.mkdir(parents=True, exist_ok=True)
        checked = _checked_directory(path, f"pair environment path {name}")
        if checked.parent != pair_root and not checked.is_relative_to(pair_root):
            raise HarnessError(f"pair environment path escapes pair root: {name}={checked}")
        environment[name] = str(checked)
    environment.update(
        {
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TILELANG_DISABLE_CACHE": "1",
            "PIP_NO_INDEX": "1",
            "USE_MACA": "ON",
            "MACA_PATH": str(maca_path),
            "LD_LIBRARY_PATH": os.pathsep.join(library_directories),
            "TILELANG_MXCC_FLAGS": tilelang_mxcc_flags,
            ENV_PAIR: pair,
            ENV_RUN_ID: str(run_id),
            ENV_RUN_ATTEMPT: str(run_attempt),
            ENV_OUTPUT_ROOT: str(output_root),
            ENV_PAYLOAD_SHA256: str(manifest["payload_sha256"]),
            ENV_MANIFEST_SHA256: str(manifest["manifest_sha256"]),
            ENV_HARNESS_SHA256: str(manifest["harness_sha256"]),
        }
    )
    _validate_pair_path_environment(pair_root, environment)
    return environment


def run_verified_pytest(
    payload_root: Path | str,
    resolved_path: Path | str,
    python_executable: Path | str,
    output_root: Path | str,
    *,
    pair: str,
    run_id: int,
    run_attempt: int,
    maca_path: Path | str,
    maca_library_path: str,
    tilelang_mxcc_flags: str,
    system_path: str = DEFAULT_SYSTEM_PATH,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> subprocess.CompletedProcess:
    """Run the one trusted pytest command in a pair-local environment."""

    payload_root = _checked_directory(Path(payload_root), "payload root")
    python_executable, environment_root = _checked_python_executable(Path(python_executable))
    pair_root = _checked_directory(environment_root.parent, "pair root")
    output_root = _checked_directory(Path(output_root), "output root")
    if output_root.parent != pair_root:
        raise HarnessError("output root must be a direct child of the pair root")
    if pair not in {"baseline", "candidate"}:
        raise HarnessError("pair must be baseline or candidate")
    run_id = _require_positive_int(run_id, "run ID")
    run_attempt = _require_positive_int(run_attempt, "run attempt")
    resolved = _resolved_context(Path(resolved_path))
    if resolved["run_id"] != run_id or resolved["run_attempt"] != run_attempt:
        raise HarnessError("run identity differs from resolved.json")
    resolved_digest = str(resolved["harness_sha256"])
    manifest = verify_trusted_payload(payload_root, expected_harness_sha256=resolved_digest)
    environment = _benchmark_environment(
        python_executable=python_executable,
        pair_root=pair_root,
        output_root=output_root,
        pair=pair,
        run_id=run_id,
        run_attempt=run_attempt,
        manifest=manifest,
        system_path=system_path,
        maca_path=Path(maca_path),
        maca_library_path=maca_library_path,
        tilelang_mxcc_flags=tilelang_mxcc_flags,
    )
    command = [
        str(python_executable),
        "-m",
        "pytest",
        "-p",
        "cross_repo_perf_harness",
        "-p",
        "pytest_timeout",
        "-p",
        "no:cacheprovider",
        "-q",
        "benchmarks/ops",
        "--timeout=900",
        "--timeout-method=thread",
        f"--junit-xml={output_root / 'bench_results.xml'}",
    ]
    final_manifest = verify_trusted_payload(payload_root, expected_harness_sha256=resolved_digest)
    if final_manifest != manifest:
        raise HarnessError("trusted payload changed before pytest execution")
    return runner(command, cwd=payload_root, env=environment, check=False)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted cross-repository pytest harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-payload")
    build.add_argument("--baseline-root", type=Path, required=True)
    build.add_argument("--payload-root", type=Path, required=True)
    build.add_argument("--resolved", type=Path, required=True)
    build.add_argument("--result", type=Path, required=True)

    run = subparsers.add_parser("run-pytest")
    run.add_argument("--payload-root", type=Path, required=True)
    run.add_argument("--resolved", type=Path, required=True)
    run.add_argument("--python", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--pair", choices=("baseline", "candidate"), required=True)
    run.add_argument("--run-id", type=int, required=True)
    run.add_argument("--run-attempt", type=int, required=True)
    run.add_argument("--system-path", required=True)
    run.add_argument("--maca-path", type=Path, required=True)
    run.add_argument("--maca-library-path", required=True)
    run.add_argument("--tilelang-mxcc-flags", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "build-payload":
            resolved = _resolved_context(arguments.resolved)
            manifest = build_trusted_payload(
                arguments.baseline_root,
                arguments.payload_root,
                expected_harness_sha256=str(resolved["harness_sha256"]),
            )
            _atomic_write_json(
                arguments.result,
                {
                    "schema_version": 1,
                    "payload_sha256": manifest["payload_sha256"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "harness_sha256": manifest["harness_sha256"],
                },
            )
            return 0
        if arguments.command == "run-pytest":
            completed = run_verified_pytest(
                arguments.payload_root,
                arguments.resolved,
                arguments.python,
                arguments.output_root,
                pair=arguments.pair,
                run_id=arguments.run_id,
                run_attempt=arguments.run_attempt,
                system_path=arguments.system_path,
                maca_path=arguments.maca_path,
                maca_library_path=arguments.maca_library_path,
                tilelang_mxcc_flags=arguments.tilelang_mxcc_flags,
            )
            return int(completed.returncode)
        parser.error(f"unknown command: {arguments.command}")
    except HarnessError as error:
        print(f"cross-repo-perf harness error: {error}", file=sys.stderr)
        return 2
    return 2


__all__ = [
    "ENV_HARNESS_SHA256",
    "ENV_MANIFEST_SHA256",
    "ENV_OUTPUT_ROOT",
    "ENV_PAIR",
    "ENV_PAYLOAD_SHA256",
    "ENV_RUN_ATTEMPT",
    "ENV_RUN_ID",
    "HarnessError",
    "audit_import_origins",
    "audit_payload_root",
    "build_trusted_payload",
    "canonical_tree_sha256",
    "collection_document",
    "collection_fingerprint",
    "install_manifest_override",
    "main",
    "run_verified_pytest",
    "verify_trusted_payload",
]


if __name__ == "__main__":
    raise SystemExit(main())
