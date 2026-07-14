#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import hashlib
import http.client
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_STRING_BYTES = 4096
MAX_NODEID_BYTES = 16384
MAX_COLLECTION_NODEIDS = 10000
MAX_GENERIC_ARRAY = 256
MAX_NESTING_DEPTH = 8
MAX_PYPI_JSON_BYTES = 8 * 1024 * 1024
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_NORMALIZER_RE = re.compile(r"[-_.]+")

UV_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.11.16/uv-x86_64-unknown-linux-gnu.tar.gz"
)
PYTHON_URL = (
    "https://releases.astral.sh/github/python-build-standalone/releases/download/20251007/"
    "cpython-3.10.18%2B20251007-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
)
TORCH_URL = (
    "https://repos.metax-tech.com/r/maca-pypi/packages/torch/2.8.0+metax3.7.1.3/"
    "torch-2.8.0+metax3.7.1.3-cp310-cp310-linux_x86_64.whl"
)
TRITON_URL = (
    "https://files.pythonhosted.org/packages/fd/6e/676ab5019b4dde8b9b7bab71245102fc02778ef3df48218b298686b9ffd6/"
    "triton-3.5.1-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
)
TORCH_SHA256 = "81775cdc54b870f9c18c01266cdf1bf2f462a22e9609b6f7b35fd33aef1292da"
TORCH_SIZE = 368585264
TRITON_SHA256 = "5fc53d849f879911ea13f4a877243afc513187bc7ee92d1f2c0f1ba3169e3c94"
TRITON_SIZE = 170320692
SETUPTOOLS_DISTUTILS_PTH = (
    b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == 'local'; "
    b"enabled and __import__('_distutils_hack').add_shim(); \n"
)

PAIR_PATH_VARIABLES = (
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "UV_CACHE_DIR",
    "PIP_CACHE_DIR",
    "CCACHE_DIR",
    "TILELANG_CACHE_DIR",
    "TILELANG_TMP_DIR",
    "TRITON_CACHE_DIR",
    "TORCH_EXTENSIONS_DIR",
    "TORCH_HOME",
    "CUDA_CACHE_PATH",
    "PYTHONPYCACHEPREFIX",
    "NUMBA_CACHE_DIR",
    "MACA_CACHE_DIR",
    "MCC_CACHE_DIR",
    "MXCC_CACHE_DIR",
)

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


class EvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Toolchain:
    uv: Path
    python: Path
    include: Path


@dataclass(frozen=True)
class WheelAudit:
    name: str
    version: str
    filename: str
    sha256: str
    size: int


@dataclass(frozen=True)
class CompiledRequirement:
    name: str
    version: str | None
    url: str | None
    hashes: frozenset[str]


def normalize_name(name: str) -> str:
    return NAME_NORMALIZER_RE.sub("-", name).lower()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expect_dict(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{context} must be an object")
    return value


def _expect_exact_keys(value: Mapping[str, object], expected: set[str], context: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        raise EvidenceError(f"{context} missing required keys: {', '.join(missing)}")
    if unexpected:
        raise EvidenceError(f"{context} has unexpected keys: {', '.join(unexpected)}")


def _require_string(value: object, context: str, max_bytes: int = MAX_STRING_BYTES) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{context} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise EvidenceError(f"{context} exceeds the string size limit")
    return value


def _require_optional_string(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError(f"{context} must be a string")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise EvidenceError(f"{context} exceeds the string size limit")
    return value


def _require_positive_int(value: object, context: str) -> int:
    if not _is_int(value) or value <= 0:
        raise EvidenceError(f"{context} must be a positive integer")
    return value


def _require_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise EvidenceError(f"{context} must be a lowercase SHA-256")
    return value


def _require_git_sha(value: object, context: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise EvidenceError(f"{context} must be a lowercase 40-character git SHA")
    return value


def _safe_relative_path(value: object, context: str) -> PurePosixPath:
    text = _require_string(value, context)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text.startswith("/")
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise EvidenceError(f"{context} is an unsafe relative path")
    return path


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise EvidenceError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _read_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> object:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EvidenceError(f"cannot stat JSON file {path}: {error}") from error
    if size > max_bytes:
        raise EvidenceError(f"JSON file {path} exceeds the {max_bytes} byte size limit")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"invalid JSON file {path}: {error}") from error


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                hasher.update(block)
    except OSError as error:
        raise EvidenceError(f"cannot hash {path}: {error}") from error
    return hasher.hexdigest()


def verify_sha256(path: Path, expected: str, expected_size: int | None = None) -> None:
    _require_sha256(expected, f"expected hash for {path}")
    if not path.is_file() or path.is_symlink():
        raise EvidenceError(f"artifact is not a regular file: {path}")
    if expected_size is not None and path.stat().st_size != expected_size:
        raise EvidenceError(
            f"size mismatch for {path}: expected {expected_size}, found {path.stat().st_size}"
        )
    actual = sha256_file(path)
    if actual != expected:
        raise EvidenceError(f"sha256 mismatch for {path}: expected {expected}, found {actual}")


def _validate_https_url(value: object, context: str) -> str:
    url = _require_string(value, context)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise EvidenceError(f"{context} must be an https URL without credentials")
    return url


def load_toolchain_lock(path: Path) -> dict[str, object]:
    value = _expect_dict(_read_json(path, 64 * 1024), "toolchain lock")
    _expect_exact_keys(value, {"schema_version", "platform", "uv", "python"}, "toolchain lock")
    if value["schema_version"] != 1:
        raise EvidenceError("toolchain lock schema_version must be 1")
    if value["platform"] != "x86_64-unknown-linux-gnu":
        raise EvidenceError("toolchain lock platform must be x86_64-unknown-linux-gnu")

    uv = _expect_dict(value["uv"], "toolchain lock uv")
    _expect_exact_keys(uv, {"version", "url", "sha256", "size", "executable"}, "toolchain lock uv")
    if uv["version"] != "0.11.16":
        raise EvidenceError("toolchain lock uv version must be 0.11.16")
    if _validate_https_url(uv["url"], "toolchain lock uv url") != UV_URL:
        raise EvidenceError("toolchain lock uv URL is not the pinned artifact")
    _require_sha256(uv["sha256"], "toolchain lock uv sha256")
    _require_positive_int(uv["size"], "toolchain lock uv size")
    _safe_relative_path(uv["executable"], "toolchain lock uv executable path")

    python = _expect_dict(value["python"], "toolchain lock python")
    _expect_exact_keys(
        python,
        {"version", "url", "sha256", "size", "executable", "include"},
        "toolchain lock python",
    )
    if python["version"] != "3.10.18":
        raise EvidenceError("toolchain lock Python version must be 3.10.18")
    if _validate_https_url(python["url"], "toolchain lock python url") != PYTHON_URL:
        raise EvidenceError("toolchain lock Python URL is not the pinned artifact")
    _require_sha256(python["sha256"], "toolchain lock python sha256")
    _require_positive_int(python["size"], "toolchain lock python size")
    _safe_relative_path(python["executable"], "toolchain lock python executable path")
    _safe_relative_path(python["include"], "toolchain lock python include path")
    return value


def _empty_directory(path: Path, context: str) -> None:
    _assert_no_symlink_components(path)
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise EvidenceError(f"{context} must be an empty directory")
    else:
        path.mkdir(parents=True)


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise EvidenceError(f"path contains a symlink component: {current}")
            except OSError as error:
                raise EvidenceError(f"cannot inspect path component {current}: {error}") from error


def _archive_relative(name: str) -> PurePosixPath:
    normalized = name.removeprefix("./")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise EvidenceError(f"unsafe archive traversal path: {name}")
    return path


def _safe_symlink_target(member_path: PurePosixPath, target: str) -> None:
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or target.startswith("/"):
        raise EvidenceError(f"unsafe absolute archive link: {member_path} -> {target}")
    normalized = posixpath.normpath(str(member_path.parent / target_path))
    if normalized == ".." or normalized.startswith("../"):
        raise EvidenceError(f"unsafe escaping archive link: {member_path} -> {target}")


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    _empty_directory(destination, "archive extraction destination")
    symlinks: list[tuple[Path, str]] = []
    total_size = 0
    count = 0
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive:
                count += 1
                if count > 250000:
                    raise EvidenceError("archive contains too many members")
                relative = _archive_relative(member.name)
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    os.chmod(target, member.mode & 0o777)
                    continue
                if member.issym():
                    _safe_symlink_target(relative, member.linkname)
                    symlinks.append((target, member.linkname))
                    continue
                if not member.isreg():
                    raise EvidenceError(
                        f"archive member is not a regular file/directory/safe link: {member.name}"
                    )
                total_size += member.size
                if total_size > 4 * 1024 * 1024 * 1024:
                    raise EvidenceError("archive expanded size exceeds 4 GiB")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise EvidenceError(f"cannot read archive member: {member.name}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                os.chmod(target, member.mode & 0o777)
        for target, linkname in symlinks:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(linkname, target)
    except EvidenceError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise EvidenceError(f"failed to safely extract {archive_path}: {error}") from error


def _download_with_retries(
    url: str,
    destination: Path,
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    attempts: int = 3,
    sleep: Callable[[int], object] = time.sleep,
) -> str:
    if attempts < 1 or attempts > 5:
        raise EvidenceError("download attempts must be between 1 and 5")
    last_error: BaseException | None = None
    for attempt in range(attempts):
        destination.unlink(missing_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "cross-repo-perf/1"})
        try:
            with opener(request, timeout=60) as response, destination.open("xb") as output:
                final_url = response.geturl()
                shutil.copyfileobj(response, output, length=1024 * 1024)
            return str(final_url)
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            last_error = error
            destination.unlink(missing_ok=True)
            if attempt + 1 < attempts:
                sleep(2**attempt)
    raise EvidenceError(
        f"failed to download {url} after {attempts} attempts: {last_error}"
    ) from last_error


def _default_downloader(url: str, destination: Path) -> str:
    return _download_with_retries(url, destination)


def _final_toolchain_url_allowed(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    parsed = urllib.parse.urlsplit(actual)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return parsed.hostname in {
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
        "github.com",
        "releases.astral.sh",
    }


def _run_version(executable: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"cannot execute pinned tool {executable}: {error}") from error
    return (result.stdout + result.stderr).strip()


def _compile_extension_probe(python: Path, include: Path, root: Path) -> dict[str, str]:
    probe_root = root / "extension-probe"
    probe_root.mkdir()
    source = probe_root / "probe.c"
    source.write_text(
        "#include <Python.h>\n"
        'static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "_cross_repo_perf_probe", 0, -1, 0};\n'
        "PyMODINIT_FUNC PyInit__cross_repo_perf_probe(void) { return PyModule_Create(&module); }\n",
        encoding="ascii",
    )
    suffix_result = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    suffix = suffix_result.stdout.strip()
    if not suffix.startswith("."):
        raise EvidenceError("pinned Python returned an invalid extension suffix")
    extension = probe_root / f"_cross_repo_perf_probe{suffix}"
    compiler = os.environ.get("CC", "cc")
    try:
        subprocess.run(
            [compiler, "-shared", "-fPIC", f"-I{include}", str(source), "-o", str(extension)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.util, pathlib; "
                    f"p=pathlib.Path({str(extension)!r}).resolve(); "
                    "s=importlib.util.spec_from_file_location('_cross_repo_perf_probe', p); "
                    "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(p)"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"pinned Python C-extension probe failed: {error}") from error
    result = {
        "extension_sha256": sha256_file(extension),
        "extension_path": str(extension.resolve()),
    }
    shutil.rmtree(probe_root)
    return result


def bootstrap_toolchain(
    lock_path: Path,
    root: Path,
    provenance_path: Path,
    *,
    downloader: Callable[[str, Path], str] = _default_downloader,
    extension_probe: Callable[[Path, Path, Path], dict[str, str]] = _compile_extension_probe,
) -> Toolchain:
    lock = load_toolchain_lock(lock_path)
    _empty_directory(root, "toolchain root")
    downloads = root / "downloads"
    downloads.mkdir()
    extracted: dict[str, Path] = {}
    archive_hashes: dict[str, str] = {}
    for name in ("uv", "python"):
        record = _expect_dict(lock[name], f"toolchain {name}")
        archive = downloads / f"{name}.tar.gz"
        final_url = downloader(str(record["url"]), archive)
        if not _final_toolchain_url_allowed(str(record["url"]), final_url):
            raise EvidenceError(
                f"toolchain download final URL is an untrusted redirect: {final_url}"
            )
        verify_sha256(archive, str(record["sha256"]), int(record["size"]))
        destination = root / f"{name}-root"
        safe_extract_tar(archive, destination)
        extracted[name] = destination
        archive_hashes[name] = sha256_file(archive)

    uv_record = _expect_dict(lock["uv"], "toolchain uv")
    python_record = _expect_dict(lock["python"], "toolchain python")
    uv = extracted["uv"].joinpath(*PurePosixPath(str(uv_record["executable"])).parts)
    python = extracted["python"].joinpath(*PurePosixPath(str(python_record["executable"])).parts)
    include = extracted["python"].joinpath(*PurePosixPath(str(python_record["include"])).parts)
    if not uv.is_file() or uv.is_symlink():
        raise EvidenceError("pinned uv executable is missing or not a regular file")
    if not python.is_file() or python.is_symlink():
        raise EvidenceError("pinned Python executable is missing or not a regular file")
    if not (include / "Python.h").is_file() or (include / "Python.h").is_symlink():
        raise EvidenceError("pinned Python include directory is missing Python.h")
    if "uv 0.11.16" not in _run_version(uv):
        raise EvidenceError("pinned uv executable reports the wrong version")
    if "Python 3.10.18" not in _run_version(python):
        raise EvidenceError("pinned Python executable reports the wrong version")
    probe = extension_probe(python, include, root)
    provenance = {
        "schema_version": 1,
        "platform": lock["platform"],
        "uv": {
            "version": uv_record["version"],
            "url": uv_record["url"],
            "archive_sha256": archive_hashes["uv"],
            "executable": str(uv.resolve()),
            "executable_sha256": sha256_file(uv),
        },
        "python": {
            "version": python_record["version"],
            "url": python_record["url"],
            "archive_sha256": archive_hashes["python"],
            "executable": str(python.resolve()),
            "executable_sha256": sha256_file(python),
            "include": str(include.resolve()),
        },
        "extension_probe": probe,
    }
    write_json_atomic(provenance_path, provenance)
    return Toolchain(uv=uv.resolve(), python=python.resolve(), include=include.resolve())


def _wheel_tags(filename: str) -> tuple[str, str, str]:
    if not filename.endswith(".whl"):
        raise EvidenceError(f"runtime artifact is not a wheel: {filename}")
    parts = filename[:-4].rsplit("-", 3)
    if len(parts) != 4:
        raise EvidenceError(f"invalid wheel filename: {filename}")
    return parts[1], parts[2], parts[3]


def _manylinux_floor(tag: str) -> int | None:
    aliases = {
        "manylinux1_x86_64": 5,
        "manylinux2010_x86_64": 12,
        "manylinux2014_x86_64": 17,
    }
    if tag in aliases:
        return aliases[tag]
    match = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", tag)
    if not match or int(match.group(1)) != 2:
        return None
    floor = int(match.group(2))
    return floor if floor <= 28 else None


def _wheel_rank_py310(filename: str, *, allow_linux: bool = False) -> tuple[int, int] | None:
    python_tags, abi_tags, platform_tags = _wheel_tags(filename)
    python_values = python_tags.split(".")
    abi_values = abi_tags.split(".")
    platform_values = platform_tags.split(".")
    python_rank = 0
    if "cp310" in python_values and "cp310" in abi_values:
        python_rank = 4
    elif "cp310" in python_values and "abi3" in abi_values:
        python_rank = 3
    elif "abi3" in abi_values and any(
        tag.startswith("cp") and tag[2:].isdigit() and int(tag[2:]) <= 310 for tag in python_values
    ):
        python_rank = 2
    elif "none" in abi_values and any(tag in {"py3", "py310"} for tag in python_values):
        python_rank = 1
    if not python_rank:
        return None

    if "any" in platform_values:
        return python_rank, 0
    floors = [floor for tag in platform_values if (floor := _manylinux_floor(tag)) is not None]
    if floors:
        return python_rank, max(floors)
    if allow_linux and "linux_x86_64" in platform_values:
        return python_rank, 29
    return None


def _wheel_compatible_with_py310(filename: str, *, allow_linux: bool = False) -> bool:
    return _wheel_rank_py310(filename, allow_linux=allow_linux) is not None


def _validate_package_record(value: object, index: int) -> dict[str, object]:
    package = _expect_dict(value, f"runtime package {index}")
    expected = {"name", "version", "filename", "url", "source_host", "size", "sha256"}
    _expect_exact_keys(package, expected, f"runtime package {index}")
    name = _require_string(package["name"], f"runtime package {index} name")
    normalized = normalize_name(name)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise EvidenceError(f"runtime package {index} has an invalid normalized name")
    version = _require_string(package["version"], f"runtime package {name} version")
    if any(character.isspace() for character in version):
        raise EvidenceError(f"runtime package {name} version contains whitespace")
    filename = _require_string(package["filename"], f"runtime package {name} filename")
    if not _wheel_compatible_with_py310(filename, allow_linux=normalized == "torch"):
        raise EvidenceError(f"runtime package {name} wheel is not compatible with Python 3.10")
    url = _validate_https_url(package["url"], f"runtime package {name} url")
    parsed = urllib.parse.urlsplit(url)
    source_host = _require_string(package["source_host"], f"runtime package {name} source_host")
    if source_host != parsed.hostname:
        raise EvidenceError(f"runtime package {name} source_host does not match its URL")
    if urllib.parse.unquote(PurePosixPath(parsed.path).name) != filename:
        raise EvidenceError(f"runtime package {name} filename does not match its URL")
    size = _require_positive_int(package["size"], f"runtime package {name} size")
    sha256 = _require_sha256(package["sha256"], f"runtime package {name} sha256")
    if normalized == "torch":
        if (url, version, size, sha256) != (
            TORCH_URL,
            "2.8.0+metax3.7.1.3",
            TORCH_SIZE,
            TORCH_SHA256,
        ):
            raise EvidenceError("torch source is not the exact pinned MetaX artifact")
    elif normalized == "triton":
        if (url, version, size, sha256) != (
            TRITON_URL,
            "3.5.1",
            TRITON_SIZE,
            TRITON_SHA256,
        ):
            raise EvidenceError("triton source is not the exact pinned artifact")
    elif source_host != "files.pythonhosted.org":
        raise EvidenceError(f"runtime package {name} must use files.pythonhosted.org")
    return package


def load_runtime_lock(path: Path) -> dict[str, object]:
    value = _expect_dict(_read_json(path), "runtime lock")
    _expect_exact_keys(value, {"schema_version", "python", "platform", "packages"}, "runtime lock")
    if value["schema_version"] != 1:
        raise EvidenceError("runtime lock schema_version must be 1")
    if value["python"] != "3.10":
        raise EvidenceError("runtime lock Python version must be 3.10")
    if value["platform"] != "x86_64-manylinux_2_28":
        raise EvidenceError("runtime lock platform must be x86_64-manylinux_2_28")
    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise EvidenceError("runtime lock packages must be a non-empty array")
    if len(packages) > 256:
        raise EvidenceError("runtime lock contains too many packages")
    normalized_names: set[str] = set()
    validated = []
    for index, package_value in enumerate(packages):
        package = _validate_package_record(package_value, index)
        normalized = normalize_name(str(package["name"]))
        if normalized in normalized_names:
            raise EvidenceError(
                f"runtime lock contains duplicate normalized distribution: {normalized}"
            )
        normalized_names.add(normalized)
        validated.append(package)
    return {**value, "packages": validated}


def _compiled_requirement_lines(path: Path) -> list[str]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise EvidenceError("compiled requirements exceed the 2 MiB size limit")
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise EvidenceError(f"cannot read compiled requirements {path}: {error}") from error

    logical: list[str] = []
    current: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continues = stripped.endswith("\\")
        segment = stripped[:-1].rstrip() if continues else stripped
        if not current and segment.startswith("--hash="):
            raise EvidenceError("compiled requirements contain an orphan hash")
        current.append(segment)
        if not continues:
            logical.append(" ".join(current))
            current = []
    if current:
        raise EvidenceError("compiled requirements end with an incomplete continuation")
    if not logical:
        raise EvidenceError("compiled requirements are empty")
    return logical


def _parse_compiled_requirements(path: Path) -> list[CompiledRequirement]:
    parsed: list[CompiledRequirement] = []
    names: set[str] = set()
    hash_pattern = re.compile(r"(?:^|\s)--hash=sha256:([0-9a-f]{64})(?=\s|$)")
    for index, line in enumerate(_compiled_requirement_lines(path), start=1):
        hashes = set(hash_pattern.findall(line))
        requirement = hash_pattern.sub("", line).strip()
        direct = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s+@\s+(\S+)", requirement)
        pinned = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", requirement)
        if direct:
            raw_name, raw_url = direct.groups()
            base_url, fragment = urllib.parse.urldefrag(raw_url)
            url = _validate_https_url(base_url, f"compiled requirement {index} URL")
            if fragment:
                values = urllib.parse.parse_qs(fragment, strict_parsing=True)
                if set(values) != {"sha256"} or len(values["sha256"]) != 1:
                    raise EvidenceError(f"compiled requirement {index} has an invalid URL hash")
                hashes.add(
                    _require_sha256(values["sha256"][0], f"compiled requirement {index} URL hash")
                )
            version = None
        elif pinned:
            raw_name, version = pinned.groups()
            url = None
        else:
            raise EvidenceError(f"compiled requirement {index} is not an exact pin or direct URL")

        name = normalize_name(raw_name)
        if name in names:
            raise EvidenceError(
                f"compiled requirements contain duplicate normalized distribution: {name}"
            )
        names.add(name)
        if not hashes:
            raise EvidenceError(f"compiled requirement {name} has no SHA-256 hash")
        if any(not HASH_RE.fullmatch(value) for value in hashes):
            raise EvidenceError(f"compiled requirement {name} has an invalid SHA-256 hash")
        parsed.append(
            CompiledRequirement(
                name=name,
                version=version,
                url=url,
                hashes=frozenset(hashes),
            )
        )
    return parsed


def _load_pypi_release(name: str, version: str) -> Mapping[str, object]:
    quoted_name = urllib.parse.quote(name, safe="")
    quoted_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{quoted_name}/{quoted_version}/json"
    request = urllib.request.Request(url, headers={"User-Agent": "tileops-cross-repo-perf/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname != "pypi.org":
                raise EvidenceError(
                    f"PyPI metadata redirected to an untrusted URL: {response.geturl()}"
                )
            payload = response.read(MAX_PYPI_JSON_BYTES + 1)
    except OSError as error:
        raise EvidenceError(
            f"cannot download PyPI metadata for {name}=={version}: {error}"
        ) from error
    if len(payload) > MAX_PYPI_JSON_BYTES:
        raise EvidenceError(f"PyPI metadata for {name}=={version} exceeds 8 MiB")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeError, ValueError) as error:
        raise EvidenceError(f"PyPI metadata for {name}=={version} is invalid JSON") from error
    return _expect_dict(value, f"PyPI metadata for {name}=={version}")


def _fixed_direct_record(requirement: CompiledRequirement) -> dict[str, object]:
    fixed = {
        "torch": (
            "2.8.0+metax3.7.1.3",
            TORCH_URL,
            TORCH_SIZE,
            TORCH_SHA256,
        ),
        "triton": ("3.5.1", TRITON_URL, TRITON_SIZE, TRITON_SHA256),
    }
    version, url, size, sha256 = fixed[requirement.name]
    if requirement.version is not None or requirement.url != url or requirement.hashes != {sha256}:
        raise EvidenceError(f"{requirement.name} must use its exact pinned direct URL artifact")
    parsed = urllib.parse.urlsplit(url)
    return {
        "name": requirement.name,
        "version": version,
        "filename": urllib.parse.unquote(PurePosixPath(parsed.path).name),
        "url": url,
        "source_host": str(parsed.hostname),
        "size": size,
        "sha256": sha256,
    }


def _select_pypi_wheel(
    requirement: CompiledRequirement,
    release: Mapping[str, object],
) -> dict[str, object]:
    if requirement.url is not None or requirement.version is None:
        raise EvidenceError(
            f"{requirement.name} may not use a direct URL outside the pinned artifacts"
        )
    info = _expect_dict(release.get("info"), f"PyPI metadata for {requirement.name} info")
    if info.get("version") != requirement.version:
        raise EvidenceError(f"PyPI metadata version mismatch for {requirement.name}")
    files = release.get("urls")
    if not isinstance(files, list) or len(files) > 4096:
        raise EvidenceError(f"PyPI metadata file list is invalid for {requirement.name}")

    candidates: list[tuple[tuple[int, int], dict[str, object]]] = []
    for index, raw_file in enumerate(files):
        file = _expect_dict(raw_file, f"PyPI metadata {requirement.name} file {index}")
        if file.get("packagetype") != "bdist_wheel" or file.get("yanked") is True:
            continue
        filename = _require_string(
            file.get("filename"), f"PyPI metadata {requirement.name} filename"
        )
        digests = _expect_dict(file.get("digests"), f"PyPI metadata {requirement.name} digests")
        sha256 = _require_sha256(digests.get("sha256"), f"PyPI metadata {requirement.name} sha256")
        if sha256 not in requirement.hashes:
            continue
        url = _validate_https_url(file.get("url"), f"PyPI metadata {requirement.name} URL")
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.hostname != "files.pythonhosted.org":
            raise EvidenceError(
                f"PyPI wheel for {requirement.name} must use files.pythonhosted.org"
            )
        if urllib.parse.unquote(PurePosixPath(parsed_url.path).name) != filename:
            raise EvidenceError(f"PyPI wheel URL filename mismatch for {requirement.name}")
        rank = _wheel_rank_py310(filename)
        if rank is None:
            continue
        record = {
            "name": requirement.name,
            "version": requirement.version,
            "filename": filename,
            "url": url,
            "source_host": "files.pythonhosted.org",
            "size": _require_positive_int(
                file.get("size"), f"PyPI metadata {requirement.name} size"
            ),
            "sha256": sha256,
        }
        candidates.append((rank, record))

    if not candidates:
        raise EvidenceError(
            f"no compatible wheel with a compiled hash exists for {requirement.name}=={requirement.version}"
        )
    best_rank = max(rank for rank, _ in candidates)
    best = [record for rank, record in candidates if rank == best_rank]
    if len(best) != 1:
        filenames = ", ".join(sorted(str(record["filename"]) for record in best))
        raise EvidenceError(f"ambiguous compatible wheels for {requirement.name}: {filenames}")
    return best[0]


def normalize_runtime_lock(
    requirements_path: Path,
    output_path: Path,
    *,
    metadata_loader: Callable[[str, str], Mapping[str, object]] = _load_pypi_release,
) -> dict[str, object]:
    requirements = _parse_compiled_requirements(requirements_path)
    packages: list[dict[str, object]] = []
    for requirement in requirements:
        if requirement.name in {"torch", "triton"}:
            packages.append(_fixed_direct_record(requirement))
            continue
        if requirement.url is not None:
            raise EvidenceError(
                f"direct URL requirement for {requirement.name} is forbidden; use files.pythonhosted.org metadata"
            )
        assert requirement.version is not None
        try:
            release = metadata_loader(requirement.name, requirement.version)
        except EvidenceError:
            raise
        except Exception as error:
            raise EvidenceError(
                f"cannot load PyPI metadata for {requirement.name}=={requirement.version}: {error}"
            ) from error
        packages.append(_select_pypi_wheel(requirement, release))

    names = {str(package["name"]) for package in packages}
    missing = sorted({"torch", "triton"} - names)
    if missing:
        raise EvidenceError(
            f"compiled requirements are missing pinned packages: {', '.join(missing)}"
        )
    packages.sort(key=lambda item: normalize_name(str(item["name"])))
    value = {
        "schema_version": 1,
        "python": "3.10",
        "platform": "x86_64-manylinux_2_28",
        "packages": packages,
    }
    for index, package in enumerate(packages):
        _validate_package_record(package, index)
    write_json_atomic(output_path, value)
    return value


def make_pair_environment(pair_root: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    _empty_directory(pair_root, "pair root")
    blocked = {
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
        "UV_PROJECT_ENVIRONMENT",
    }
    environment = {key: value for key, value in inherited.items() if key not in blocked}
    created: dict[str, Path] = {}
    for name in PAIR_PATH_VARIABLES:
        relative = _PAIR_PATH_NAMES[name]
        path = created.setdefault(relative, pair_root / relative)
        path.mkdir(parents=True, exist_ok=True)
        environment[name] = str(path.resolve())
    environment.update(
        {
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TILELANG_DISABLE_CACHE": "1",
            "PIP_NO_INDEX": "1",
        }
    )
    root = pair_root.resolve()
    for name in PAIR_PATH_VARIABLES:
        value = Path(environment[name]).resolve()
        if not value.is_relative_to(root):
            raise EvidenceError(f"pair environment path escapes pair root: {name}={value}")
    return environment


def _safe_zip_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise EvidenceError(f"wheel contains an unsafe traversal path: {name}")
    return path


def audit_wheel(path: Path, expected_name: str) -> WheelAudit:
    if path.is_symlink() or not path.is_file():
        raise EvidenceError(f"wheel is not a regular file: {path}")
    metadata_entries: list[zipfile.ZipInfo] = []
    entry_points_entries: list[zipfile.ZipInfo] = []
    pth_entries: list[zipfile.ZipInfo] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                member = _safe_zip_name(info.filename)
                basename = member.name.lower()
                if basename.endswith(".pth"):
                    pth_entries.append(info)
                if basename == "sitecustomize.py":
                    raise EvidenceError(
                        f"wheel contains forbidden sitecustomize.py: {info.filename}"
                    )
                if basename == "usercustomize.py":
                    raise EvidenceError(
                        f"wheel contains forbidden usercustomize.py: {info.filename}"
                    )
                top_level_dist_info = len(member.parts) == 2 and member.parts[0].lower().endswith(
                    ".dist-info"
                )
                if top_level_dist_info and basename == "metadata":
                    metadata_entries.append(info)
                if top_level_dist_info and basename == "entry_points.txt":
                    entry_points_entries.append(info)
            if len(metadata_entries) != 1:
                raise EvidenceError("wheel must contain exactly one METADATA file")
            metadata_text = archive.read(metadata_entries[0]).decode("utf-8")
            metadata = Parser().parsestr(metadata_text)
            name = metadata.get("Name")
            version = metadata.get("Version")
            if not name or normalize_name(name) != normalize_name(expected_name):
                raise EvidenceError(
                    f"wheel distribution mismatch: expected {expected_name}, found {name or '<missing>'}"
                )
            if not version:
                raise EvidenceError("wheel metadata has no version")
            if pth_entries:
                allowed_setuptools_pth = (
                    len(pth_entries) == 1
                    and normalize_name(name) == "setuptools"
                    and version == "80.9.0"
                    and pth_entries[0].filename == "distutils-precedence.pth"
                    and archive.read(pth_entries[0]) == SETUPTOOLS_DISTUTILS_PTH
                )
                if not allowed_setuptools_pth:
                    raise EvidenceError("wheel contains a forbidden .pth startup hook")
            if len(entry_points_entries) > 1:
                raise EvidenceError("wheel must contain at most one entry_points.txt file")
            for entry in entry_points_entries:
                parser = configparser.ConfigParser(interpolation=None, strict=True)
                parser.read_string(archive.read(entry).decode("utf-8"))
                if parser.has_section("pytest11"):
                    pytest_plugins = dict(parser.items("pytest11"))
                    allowed_timeout_plugin = (
                        normalize_name(name) == "pytest-timeout"
                        and version == "2.4.0"
                        and pytest_plugins == {"timeout": "pytest_timeout"}
                    )
                    if not allowed_timeout_plugin:
                        raise EvidenceError("wheel declares a forbidden pytest11 entry point")
    except EvidenceError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, configparser.Error) as error:
        raise EvidenceError(f"invalid wheel {path}: {error}") from error
    return WheelAudit(
        name=normalize_name(name),
        version=version,
        filename=path.name,
        sha256=sha256_file(path),
        size=path.stat().st_size,
    )


def select_wheel(paths: Iterable[Path], expected_name: str) -> WheelAudit:
    matches: list[WheelAudit] = []
    errors: list[EvidenceError] = []
    for path in paths:
        try:
            audit = audit_wheel(path, expected_name)
        except EvidenceError as error:
            errors.append(error)
            continue
        matches.append(audit)
    if len(matches) != 1:
        detail = f" ({errors[0]})" if errors and not matches else ""
        raise EvidenceError(
            f"expected exactly one wheel for {expected_name}, found {len(matches)}{detail}"
        )
    return matches[0]


def _walk_regular_files(root: Path, excluded: set[Path] | None = None) -> list[Path]:
    excluded = excluded or set()
    output: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for name in list(dirnames):
            child = directory_path / name
            if child.is_symlink():
                raise EvidenceError(f"artifact contains a symlink directory: {child}")
        for name in filenames:
            child = directory_path / name
            if child in excluded:
                continue
            if child.is_symlink() or not child.is_file():
                raise EvidenceError(f"artifact contains a non-regular file: {child}")
            output.append(child)
    return sorted(output, key=lambda path: path.relative_to(root).as_posix())


def _validate_artifact_identity(value: object) -> dict[str, object]:
    identity = _expect_dict(value, "artifact identity")
    expected = {"repository", "run_id", "run_attempt", "pair"}
    _expect_exact_keys(identity, expected, "artifact identity")
    repository = _require_string(identity["repository"], "artifact identity repository")
    if repository.count("/") != 1:
        raise EvidenceError("artifact identity repository must be owner/repo")
    _require_positive_int(identity["run_id"], "artifact identity run_id")
    _require_positive_int(identity["run_attempt"], "artifact identity run_attempt")
    if identity["pair"] not in {"baseline", "candidate", "resolution", "report"}:
        raise EvidenceError("artifact identity pair is invalid")
    return identity


def write_artifact_manifest(root: Path, output: Path, identity: Mapping[str, object]) -> None:
    root = root.resolve()
    output_parent = output.parent.resolve()
    if not output_parent.is_relative_to(root):
        raise EvidenceError("artifact manifest output must be inside the artifact root")
    validated_identity = _validate_artifact_identity(dict(identity))
    files = []
    for path in _walk_regular_files(root, {output}):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    write_json_atomic(
        output,
        {"schema_version": 1, "identity": validated_identity, "files": files},
    )


def validate_artifact_manifest(
    root: Path, manifest_path: Path, expected: Mapping[str, object]
) -> None:
    root = root.resolve()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise EvidenceError("artifact manifest is not a regular file")
    value = _expect_dict(_read_json(manifest_path), "artifact manifest")
    _expect_exact_keys(value, {"schema_version", "identity", "files"}, "artifact manifest")
    if value["schema_version"] != 1:
        raise EvidenceError("artifact manifest schema_version must be 1")
    identity = _validate_artifact_identity(value["identity"])
    expected_identity = _validate_artifact_identity(dict(expected))
    if identity != expected_identity:
        raise EvidenceError("artifact manifest identity mismatch")
    entries = value["files"]
    if not isinstance(entries, list) or len(entries) > 10000:
        raise EvidenceError("artifact manifest files must be a bounded array")
    recorded: dict[str, dict[str, object]] = {}
    for index, entry_value in enumerate(entries):
        entry = _expect_dict(entry_value, f"artifact manifest file {index}")
        _expect_exact_keys(entry, {"path", "size", "sha256"}, f"artifact manifest file {index}")
        relative = _safe_relative_path(entry["path"], f"artifact manifest file {index} path")
        path_text = relative.as_posix()
        if path_text in recorded:
            raise EvidenceError(f"artifact manifest contains duplicate path: {path_text}")
        _require_positive_int(entry["size"], f"artifact manifest file {path_text} size")
        _require_sha256(entry["sha256"], f"artifact manifest file {path_text} sha256")
        recorded[path_text] = entry
    actual_files = _walk_regular_files(root, {manifest_path})
    actual_names = {path.relative_to(root).as_posix() for path in actual_files}
    if actual_names != set(recorded):
        raise EvidenceError("artifact manifest file set mismatch")
    for path in actual_files:
        name = path.relative_to(root).as_posix()
        entry = recorded[name]
        verify_sha256(path, str(entry["sha256"]), int(entry["size"]))


def validate_service_artifact(
    record_value: Mapping[str, object], expected_value: Mapping[str, object]
) -> None:
    expected_keys = {
        "schema_version",
        "repository",
        "run_id",
        "run_attempt",
        "name",
        "artifact_id",
        "artifact_digest",
        "expired",
    }
    record = _expect_dict(dict(record_value), "service artifact")
    expected = _expect_dict(dict(expected_value), "expected service artifact")
    _expect_exact_keys(record, expected_keys, "service artifact")
    _expect_exact_keys(expected, expected_keys, "expected service artifact")
    if record["schema_version"] != 1:
        raise EvidenceError("service artifact schema_version must be 1")
    _require_string(record["repository"], "service artifact repository")
    _require_positive_int(record["run_id"], "service artifact run id")
    _require_positive_int(record["run_attempt"], "service artifact run attempt")
    _require_string(record["name"], "service artifact name")
    _require_positive_int(record["artifact_id"], "service artifact artifact id")
    digest_value = _require_string(record["artifact_digest"], "service artifact artifact digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value):
        raise EvidenceError("service artifact digest must be sha256:<64 lowercase hex>")
    if not isinstance(record["expired"], bool):
        raise EvidenceError("service artifact expired must be a boolean")
    if record["expired"]:
        raise EvidenceError("service artifact is expired")
    if record != expected:
        differing = next(
            (key for key in sorted(expected_keys) if record[key] != expected[key]), "identity"
        )
        raise EvidenceError(f"service artifact {differing.replace('_', ' ')} mismatch")


def _validate_limits(value: object, context: str, depth: int = 0) -> None:
    if depth > MAX_NESTING_DEPTH:
        raise EvidenceError(f"{context} exceeds the maximum nesting depth")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise EvidenceError(f"{context} string exceeds {MAX_STRING_BYTES} bytes")
    elif isinstance(value, list):
        if len(value) > MAX_COLLECTION_NODEIDS:
            raise EvidenceError(f"{context} array exceeds {MAX_COLLECTION_NODEIDS} entries")
        for index, item in enumerate(value):
            _validate_limits(item, f"{context}[{index}]", depth + 1)
    elif isinstance(value, dict):
        if len(value) > 64:
            raise EvidenceError(f"{context} object contains too many keys")
        for key, item in value.items():
            if not isinstance(key, str):
                raise EvidenceError(f"{context} contains a non-string key")
            _validate_limits(item, f"{context}.{key}", depth + 1)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise EvidenceError(f"{context} contains an unsupported JSON value")


def _validate_repository_identity(value: object, context: str) -> None:
    repository = _expect_dict(value, context)
    keys = {
        "repository",
        "pr_number",
        "pr_url",
        "author",
        "default_branch",
        "default_sha",
        "base_ref",
        "base_sha",
        "head_ref",
        "head_sha",
        "merge_sha",
    }
    _expect_exact_keys(repository, keys, context)
    _require_string(repository["repository"], f"{context} repository")
    _require_positive_int(repository["pr_number"], f"{context} pr_number")
    _validate_https_url(repository["pr_url"], f"{context} pr_url")
    for key in ("author", "default_branch", "base_ref", "head_ref"):
        _require_string(repository[key], f"{context} {key}")
    for key in ("default_sha", "base_sha", "head_sha", "merge_sha"):
        _require_git_sha(repository[key], f"{context} {key}")


def _validate_resolved(value: dict[str, object]) -> None:
    keys = {
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
    _expect_exact_keys(value, keys, "resolved")
    if value["schema_version"] != 1:
        raise EvidenceError("resolved schema_version must be 1")
    if value["disposition"] not in {"run", "reject", "ignore"}:
        raise EvidenceError("resolved disposition is invalid")
    _require_optional_string(value["reason"], "resolved reason")
    for key in ("run_id", "run_attempt", "trigger_comment_id"):
        _require_positive_int(value[key], f"resolved {key}")
    _require_string(value["trigger_actor"], "resolved trigger_actor")
    _require_sha256(value["harness_sha256"], "resolved harness_sha256")
    _validate_repository_identity(value["tileops"], "resolved tileops")
    _validate_repository_identity(value["tilelang"], "resolved tilelang")


def _validate_status(value: dict[str, object]) -> None:
    keys = {
        "schema_version",
        "pair",
        "state",
        "phase",
        "exit_code",
        "reason",
        "started_at",
        "finished_at",
        "run_id",
        "run_attempt",
        "tileops_sha",
        "tilelang_sha",
        "payload_sha256",
    }
    _expect_exact_keys(value, keys, "status")
    if value["schema_version"] != 1:
        raise EvidenceError("status schema_version must be 1")
    if value["pair"] not in {"baseline", "candidate"}:
        raise EvidenceError("status pair is invalid")
    if value["state"] not in {"success", "failed", "skipped"}:
        raise EvidenceError("status state is invalid")
    if value["phase"] not in {
        "setup",
        "toolchain",
        "build",
        "install",
        "import",
        "payload",
        "collection",
        "benchmark",
        "artifact",
        "complete",
        "suppressed",
    }:
        raise EvidenceError("status phase is invalid")
    if value["exit_code"] is not None and not _is_int(value["exit_code"]):
        raise EvidenceError("status exit_code must be an integer or null")
    for key in ("reason", "started_at", "finished_at"):
        _require_optional_string(value[key], f"status {key}")
    for key in ("run_id", "run_attempt"):
        _require_positive_int(value[key], f"status {key}")
    for key in ("tileops_sha", "tilelang_sha"):
        _require_git_sha(value[key], f"status {key}")
    _require_sha256(value["payload_sha256"], "status payload_sha256")


def _validate_wheel_provenance(value: object, context: str) -> None:
    wheel = _expect_dict(value, context)
    keys = {"distribution", "version", "filename", "sha256", "source_sha", "import_path"}
    _expect_exact_keys(wheel, keys, context)
    for key in ("distribution", "version", "filename", "import_path"):
        _require_string(wheel[key], f"{context} {key}")
    _require_sha256(wheel["sha256"], f"{context} sha256")
    _require_git_sha(wheel["source_sha"], f"{context} source_sha")


def _validate_provenance(value: dict[str, object]) -> None:
    keys = {
        "schema_version",
        "pair",
        "run_id",
        "run_attempt",
        "python",
        "runtime_lock_sha256",
        "wheelhouse_manifest_sha256",
        "payload_sha256",
        "manifest_sha256",
        "harness_sha256",
        "companion",
        "tilelang",
        "tileops",
    }
    _expect_exact_keys(value, keys, "provenance")
    if value["schema_version"] != 1:
        raise EvidenceError("provenance schema_version must be 1")
    if value["pair"] not in {"baseline", "candidate"}:
        raise EvidenceError("provenance pair is invalid")
    for key in ("run_id", "run_attempt"):
        _require_positive_int(value[key], f"provenance {key}")
    python = _expect_dict(value["python"], "provenance python")
    _expect_exact_keys(
        python, {"version", "executable", "executable_sha256", "include"}, "provenance python"
    )
    for key in ("version", "executable", "include"):
        _require_string(python[key], f"provenance python {key}")
    _require_sha256(python["executable_sha256"], "provenance python executable_sha256")
    for key in (
        "runtime_lock_sha256",
        "wheelhouse_manifest_sha256",
        "payload_sha256",
        "manifest_sha256",
        "harness_sha256",
    ):
        _require_sha256(value[key], f"provenance {key}")
    for key in ("companion", "tilelang", "tileops"):
        _validate_wheel_provenance(value[key], f"provenance {key}")


def _nodeids_digest(nodeids: Sequence[str]) -> str:
    return hashlib.sha256(("\n".join(nodeids) + "\n").encode()).hexdigest()


def _validate_collection(value: dict[str, object]) -> None:
    keys = {
        "schema_version",
        "pair",
        "run_id",
        "run_attempt",
        "payload_sha256",
        "manifest_sha256",
        "harness_sha256",
        "nodeids_sha256",
        "count",
        "nodeids",
    }
    _expect_exact_keys(value, keys, "collection")
    if value["schema_version"] != 1:
        raise EvidenceError("collection schema_version must be 1")
    if value["pair"] not in {"baseline", "candidate"}:
        raise EvidenceError("collection pair is invalid")
    for key in ("run_id", "run_attempt"):
        _require_positive_int(value[key], f"collection {key}")
    for key in ("payload_sha256", "manifest_sha256", "harness_sha256", "nodeids_sha256"):
        _require_sha256(value[key], f"collection {key}")
    nodeids = value["nodeids"]
    if not isinstance(nodeids, list) or len(nodeids) > MAX_COLLECTION_NODEIDS:
        raise EvidenceError("collection nodeids exceeds the 10000 node limit")
    if not _is_int(value["count"]) or value["count"] != len(nodeids):
        raise EvidenceError("collection count does not match nodeids")
    validated: list[str] = []
    for index, nodeid in enumerate(nodeids):
        validated.append(_require_string(nodeid, f"collection nodeids[{index}]", MAX_NODEID_BYTES))
    if len(set(validated)) != len(validated):
        raise EvidenceError("collection contains duplicate node IDs")
    if validated != sorted(validated):
        raise EvidenceError("collection node IDs must be sorted")
    if value["nodeids_sha256"] != _nodeids_digest(validated):
        raise EvidenceError("collection nodeids_sha256 digest mismatch")


def load_bounded_json(
    path: Path, schema: str, max_bytes: int = MAX_JSON_BYTES
) -> dict[str, object]:
    value = _expect_dict(_read_json(path, max_bytes), schema)
    _validate_limits(value, schema)
    validators = {
        "resolved": _validate_resolved,
        "status": _validate_status,
        "provenance": _validate_provenance,
        "collection": _validate_collection,
    }
    try:
        validator = validators[schema]
    except KeyError as error:
        raise EvidenceError(f"unknown bounded JSON schema: {schema}") from error
    validator(value)
    return value


def _validate_download_final_url(expected: str, actual: str) -> None:
    if actual != expected:
        raise EvidenceError(
            f"wheel download final URL mismatch: expected {expected}, found {actual}"
        )


def materialize_wheelhouse(
    lock_path: Path,
    wheelhouse: Path,
    *,
    downloader: Callable[[str, Path], str] = _default_downloader,
) -> None:
    lock = load_runtime_lock(lock_path)
    _empty_directory(wheelhouse, "wheelhouse")
    packages = []
    for package in sorted(lock["packages"], key=lambda item: normalize_name(str(item["name"]))):
        destination = wheelhouse / str(package["filename"])
        final_url = downloader(str(package["url"]), destination)
        _validate_download_final_url(str(package["url"]), final_url)
        verify_sha256(destination, str(package["sha256"]), int(package["size"]))
        audit = audit_wheel(destination, str(package["name"]))
        if audit.version != package["version"]:
            raise EvidenceError(
                f"wheel version mismatch for {package['name']}: {audit.version} != {package['version']}"
            )
        packages.append(dict(package))
    write_json_atomic(
        wheelhouse / "wheelhouse-manifest.json",
        {
            "schema_version": 1,
            "runtime_lock_sha256": sha256_file(lock_path),
            "packages": packages,
        },
    )


def freeze_wheelhouse(wheelhouse: Path) -> None:
    _assert_no_symlink_components(wheelhouse)
    if not wheelhouse.is_dir():
        raise EvidenceError("wheelhouse is not a directory")
    for directory, dirnames, filenames in os.walk(wheelhouse, topdown=False, followlinks=False):
        directory_path = Path(directory)
        for name in filenames:
            path = directory_path / name
            if path.is_symlink() or not path.is_file():
                raise EvidenceError(f"wheelhouse contains a non-regular file: {path}")
            os.chmod(path, 0o444)
        for name in dirnames:
            path = directory_path / name
            if path.is_symlink() or not path.is_dir():
                raise EvidenceError(f"wheelhouse contains a non-directory: {path}")
            os.chmod(path, 0o555)
        os.chmod(directory_path, 0o555)


def verify_wheelhouse(lock_path: Path, wheelhouse: Path) -> None:
    lock = load_runtime_lock(lock_path)
    _assert_no_symlink_components(wheelhouse)
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise EvidenceError("wheelhouse is not a regular directory")
    manifest_path = wheelhouse / "wheelhouse-manifest.json"
    manifest = _expect_dict(_read_json(manifest_path), "wheelhouse manifest")
    _expect_exact_keys(
        manifest,
        {"schema_version", "runtime_lock_sha256", "packages"},
        "wheelhouse manifest",
    )
    if manifest["schema_version"] != 1:
        raise EvidenceError("wheelhouse manifest schema_version must be 1")
    if manifest["runtime_lock_sha256"] != sha256_file(lock_path):
        raise EvidenceError("wheelhouse manifest runtime lock hash mismatch")
    expected_packages = sorted(lock["packages"], key=lambda item: normalize_name(str(item["name"])))
    if manifest["packages"] != expected_packages:
        raise EvidenceError("wheelhouse manifest package identity mismatch")
    expected_files = {str(package["filename"]) for package in expected_packages}
    expected_files.add("wheelhouse-manifest.json")
    actual_files = {path.name for path in wheelhouse.iterdir() if path.is_file()}
    if actual_files != expected_files:
        raise EvidenceError("wheelhouse file set mismatch")
    if any(path.is_dir() for path in wheelhouse.iterdir()):
        raise EvidenceError("wheelhouse contains an unexpected directory")
    for path in [wheelhouse, *wheelhouse.iterdir()]:
        if path.is_symlink():
            raise EvidenceError(f"wheelhouse contains a symlink: {path}")
        if path.stat().st_mode & 0o222:
            raise EvidenceError(f"wheelhouse path is writable: {path}")
    for package in expected_packages:
        path = wheelhouse / str(package["filename"])
        verify_sha256(path, str(package["sha256"]), int(package["size"]))
        audit = audit_wheel(path, str(package["name"]))
        if audit.version != package["version"]:
            raise EvidenceError(f"wheelhouse wheel version mismatch for {package['name']}")


def emit_requirements(lock_path: Path, output: Path) -> None:
    lock = load_runtime_lock(lock_path)
    lines = []
    for package in sorted(lock["packages"], key=lambda item: normalize_name(str(item["name"]))):
        lines.append(
            f"{normalize_name(str(package['name']))}=={package['version']} "
            f"--hash=sha256:{package['sha256']}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary, output)


def _toolchain_json(toolchain: Toolchain) -> dict[str, str]:
    return {
        "uv": str(toolchain.uv),
        "python": str(toolchain.python),
        "include": str(toolchain.include),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted cross-repository performance utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap = subparsers.add_parser("bootstrap-toolchain")
    bootstrap.add_argument("--lock", type=Path, required=True)
    bootstrap.add_argument("--root", type=Path, required=True)
    bootstrap.add_argument("--provenance", type=Path, required=True)

    validate_toolchain = subparsers.add_parser("validate-toolchain-lock")
    validate_toolchain.add_argument("--lock", type=Path, required=True)

    validate_runtime = subparsers.add_parser("validate-runtime-lock")
    validate_runtime.add_argument("--lock", type=Path, required=True)

    normalize_runtime = subparsers.add_parser("normalize-runtime-lock")
    normalize_runtime.add_argument("--requirements", type=Path, required=True)
    normalize_runtime.add_argument("--output", type=Path, required=True)

    materialize = subparsers.add_parser("materialize-wheelhouse")
    materialize.add_argument("--lock", type=Path, required=True)
    materialize.add_argument("--wheelhouse", type=Path, required=True)

    freeze = subparsers.add_parser("freeze-wheelhouse")
    freeze.add_argument("--wheelhouse", type=Path, required=True)

    verify = subparsers.add_parser("verify-wheelhouse")
    verify.add_argument("--lock", type=Path, required=True)
    verify.add_argument("--wheelhouse", type=Path, required=True)

    requirements = subparsers.add_parser("emit-requirements")
    requirements.add_argument("--lock", type=Path, required=True)
    requirements.add_argument("--output", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "bootstrap-toolchain":
            toolchain = bootstrap_toolchain(arguments.lock, arguments.root, arguments.provenance)
            print(json.dumps(_toolchain_json(toolchain), sort_keys=True))
        elif arguments.command == "validate-toolchain-lock":
            load_toolchain_lock(arguments.lock)
        elif arguments.command == "validate-runtime-lock":
            load_runtime_lock(arguments.lock)
        elif arguments.command == "normalize-runtime-lock":
            normalize_runtime_lock(arguments.requirements, arguments.output)
        elif arguments.command == "materialize-wheelhouse":
            materialize_wheelhouse(arguments.lock, arguments.wheelhouse)
        elif arguments.command == "freeze-wheelhouse":
            freeze_wheelhouse(arguments.wheelhouse)
        elif arguments.command == "verify-wheelhouse":
            verify_wheelhouse(arguments.lock, arguments.wheelhouse)
        elif arguments.command == "emit-requirements":
            emit_requirements(arguments.lock, arguments.output)
        else:
            parser.error(f"unknown command: {arguments.command}")
    except EvidenceError as error:
        print(f"cross-repo-perf evidence error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
