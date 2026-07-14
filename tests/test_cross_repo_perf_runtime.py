from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts import cross_repo_perf_runtime as runtime

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


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def toolchain_lock(uv_data: bytes = b"uv", python_data: bytes = b"python") -> dict[str, object]:
    return {
        "schema_version": 1,
        "platform": "x86_64-unknown-linux-gnu",
        "uv": {
            "version": "0.11.16",
            "url": UV_URL,
            "sha256": digest(uv_data),
            "size": len(uv_data),
            "executable": "uv-x86_64-unknown-linux-gnu/uv",
        },
        "python": {
            "version": "3.10.18",
            "url": PYTHON_URL,
            "sha256": digest(python_data),
            "size": len(python_data),
            "executable": "python/bin/python3.10",
            "include": "python/include/python3.10",
        },
    }


def package_record(
    name: str = "demo",
    version: str = "1.0",
    filename: str | None = None,
    url: str | None = None,
    data: bytes = b"wheel",
) -> dict[str, object]:
    filename = filename or f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    url = url or f"https://files.pythonhosted.org/packages/00/00/{filename}"
    return {
        "name": name,
        "version": version,
        "filename": filename,
        "url": url,
        "source_host": url.split("/", 3)[2],
        "size": len(data),
        "sha256": digest(data),
    }


def runtime_lock(*packages: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "python": "3.10",
        "platform": "x86_64-manylinux_2_28",
        "packages": list(packages or (package_record(),)),
    }


def pypi_file(
    filename: str,
    data: bytes,
    *,
    host: str = "files.pythonhosted.org",
    packagetype: str = "bdist_wheel",
) -> dict[str, object]:
    return {
        "filename": filename,
        "url": f"https://{host}/packages/00/00/{filename}",
        "size": len(data),
        "packagetype": packagetype,
        "digests": {"sha256": digest(data)},
        "yanked": False,
    }


def pypi_release(version: str, *files: dict[str, object]) -> dict[str, object]:
    return {"info": {"version": version}, "urls": list(files)}


def make_wheel(
    path: Path,
    name: str = "demo-package",
    version: str = "1.0",
    extra_files: dict[str, bytes] | None = None,
    entry_points: str | None = None,
) -> Path:
    dist = name.replace("-", "_")
    dist_info = f"{dist}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist}/__init__.py", b"")
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", wheel)
        archive.writestr(f"{dist_info}/RECORD", b"")
        if entry_points is not None:
            archive.writestr(f"{dist_info}/entry_points.txt", entry_points.encode())
        for filename, contents in (extra_files or {}).items():
            archive.writestr(filename, contents)
    return path


def tar_bytes(files: dict[str, tuple[bytes, int]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, (contents, mode) in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = mode
            archive.addfile(info, io.BytesIO(contents))
    return output.getvalue()


def valid_resolved() -> dict[str, object]:
    repository = {
        "repository": "owner/repo",
        "pr_number": 1,
        "pr_url": "https://github.com/owner/repo/pull/1",
        "author": "maintainer",
        "default_branch": "dev",
        "default_sha": "1" * 40,
        "base_ref": "dev",
        "base_sha": "1" * 40,
        "head_ref": "feature",
        "head_sha": "2" * 40,
        "merge_sha": "3" * 40,
    }
    return {
        "schema_version": 1,
        "disposition": "run",
        "reason": "",
        "run_id": 10,
        "run_attempt": 2,
        "trigger_comment_id": 99,
        "trigger_actor": "maintainer",
        "harness_sha256": "a" * 64,
        "tileops": repository,
        "tilelang": {**repository, "repository": "tile-ai/tilelang-metax"},
    }


def valid_status() -> dict[str, object]:
    return {
        "schema_version": 1,
        "pair": "baseline",
        "state": "success",
        "phase": "complete",
        "exit_code": 0,
        "reason": "",
        "started_at": "2026-07-14T00:00:00Z",
        "finished_at": "2026-07-14T01:00:00Z",
        "run_id": 10,
        "run_attempt": 2,
        "tileops_sha": "1" * 40,
        "tilelang_sha": "2" * 40,
        "payload_sha256": "3" * 64,
    }


def wheel_provenance(name: str) -> dict[str, object]:
    return {
        "distribution": name,
        "version": "1.0",
        "filename": f"{name}-1.0-py3-none-any.whl",
        "sha256": "4" * 64,
        "source_sha": "5" * 40,
        "import_path": f"/tmp/pair/venv/lib/python3.10/site-packages/{name}/__init__.py",
    }


def valid_provenance() -> dict[str, object]:
    return {
        "schema_version": 1,
        "pair": "baseline",
        "run_id": 10,
        "run_attempt": 2,
        "python": {
            "version": "3.10.18",
            "executable": "/tmp/toolchain/python/bin/python3.10",
            "executable_sha256": "6" * 64,
            "include": "/tmp/toolchain/python/include/python3.10",
        },
        "runtime_lock_sha256": "7" * 64,
        "wheelhouse_manifest_sha256": "8" * 64,
        "payload_sha256": "9" * 64,
        "manifest_sha256": "a" * 64,
        "harness_sha256": "b" * 64,
        "companion": wheel_provenance("apache-tvm-ffi"),
        "tilelang": wheel_provenance("tilelang"),
        "tileops": wheel_provenance("tileops"),
    }


def valid_collection() -> dict[str, object]:
    nodeids = ["benchmarks/ops/bench_a.py::test_a", "benchmarks/ops/bench_b.py::test_b"]
    return {
        "schema_version": 1,
        "pair": "baseline",
        "run_id": 10,
        "run_attempt": 2,
        "payload_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "harness_sha256": "3" * 64,
        "nodeids_sha256": digest(("\n".join(nodeids) + "\n").encode()),
        "count": 2,
        "nodeids": nodeids,
    }


def test_verify_sha256_rejects_modified_file(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"first")
    runtime.verify_sha256(artifact, digest(b"first"), expected_size=5)
    artifact.write_bytes(b"second")
    with pytest.raises(runtime.EvidenceError, match="sha256|size"):
        runtime.verify_sha256(artifact, digest(b"first"), expected_size=5)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("platform"), "platform"),
        (lambda value: value["uv"].update(url="http://example.test/uv.tar.gz"), "https"),
        (lambda value: value["uv"].update(sha256="bad"), "sha256"),
        (lambda value: value["python"].update(version="3.12.0"), "3.10"),
        (lambda value: value["python"].update(executable="../python"), "path"),
        (lambda value: value.update(extra=True), "unexpected"),
    ],
)
def test_load_toolchain_lock_rejects_invalid_schema(tmp_path: Path, mutation, message: str) -> None:
    value = toolchain_lock()
    mutation(value)
    path = tmp_path / "toolchain.lock"
    write_json(path, value)
    with pytest.raises(runtime.EvidenceError, match=message):
        runtime.load_toolchain_lock(path)


def test_safe_extract_tar_rejects_traversal_and_special_members(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.tar.gz"
    with tarfile.open(traversal, "w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(runtime.EvidenceError, match="unsafe|traversal"):
        runtime.safe_extract_tar(traversal, tmp_path / "out-traversal")

    symlink = tmp_path / "symlink.tar.gz"
    with tarfile.open(symlink, "w:gz") as archive:
        info = tarfile.TarInfo("python/bin/python")
        info.type = tarfile.SYMTYPE
        info.linkname = "/usr/bin/python3"
        archive.addfile(info)
    with pytest.raises(runtime.EvidenceError, match="link|regular"):
        runtime.safe_extract_tar(symlink, tmp_path / "out-symlink")

    hardlink = tmp_path / "hardlink.tar.gz"
    with tarfile.open(hardlink, "w:gz") as archive:
        info = tarfile.TarInfo("python/bin/python")
        info.type = tarfile.LNKTYPE
        info.linkname = "python/bin/python3.10"
        archive.addfile(info)
    with pytest.raises(runtime.EvidenceError, match="regular|link"):
        runtime.safe_extract_tar(hardlink, tmp_path / "out-hardlink")


def test_safe_extract_tar_allows_in_root_relative_symlink(tmp_path: Path) -> None:
    archive_path = tmp_path / "safe-symlink.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        contents = b"python"
        target = tarfile.TarInfo("python/bin/python3.10")
        target.size = len(contents)
        archive.addfile(target, io.BytesIO(contents))
        link = tarfile.TarInfo("python/bin/python")
        link.type = tarfile.SYMTYPE
        link.linkname = "python3.10"
        archive.addfile(link)

    output = tmp_path / "out"
    runtime.safe_extract_tar(archive_path, output)

    assert (output / "python/bin/python").is_symlink()
    assert (output / "python/bin/python").read_bytes() == b"python"


def test_download_with_retries_is_bounded_and_cleans_partial_files(tmp_path: Path) -> None:
    destination = tmp_path / "artifact"
    calls = 0
    sleeps: list[int] = []

    class Response(io.BytesIO):
        def geturl(self) -> str:
            return "https://files.example.test/final"

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            destination.write_bytes(b"partial")
            raise TimeoutError("first timeout")
        if calls == 2:
            raise TimeoutError("second timeout")
        return Response(b"complete")

    final_url = runtime._download_with_retries(
        "https://files.example.test/artifact",
        destination,
        opener=opener,
        attempts=3,
        sleep=sleeps.append,
    )

    assert calls == 3
    assert sleeps == [1, 2]
    assert destination.read_bytes() == b"complete"
    assert final_url == "https://files.example.test/final"


def test_bootstrap_toolchain_uses_only_verified_archives(tmp_path: Path) -> None:
    uv_archive = tar_bytes(
        {
            "uv-x86_64-unknown-linux-gnu/uv": (
                b"#!/bin/sh\necho 'uv 0.11.16 (x86_64-unknown-linux-gnu)'\n",
                0o755,
            )
        }
    )
    python_archive = tar_bytes(
        {
            "python/bin/python3.10": (b"#!/bin/sh\necho 'Python 3.10.18'\n", 0o755),
            "python/include/python3.10/Python.h": (b"/* fixture */\n", 0o644),
        }
    )
    lock = toolchain_lock(uv_archive, python_archive)
    lock_path = tmp_path / "toolchain.lock"
    write_json(lock_path, lock)
    downloads = {UV_URL: uv_archive, PYTHON_URL: python_archive}
    seen: list[tuple[str, Path]] = []

    def downloader(url: str, destination: Path) -> str:
        seen.append((url, destination))
        destination.write_bytes(downloads[url])
        return url

    probed: list[tuple[Path, Path]] = []

    def probe(python: Path, include: Path, root: Path) -> dict[str, str]:
        probed.append((python, include))
        return {"extension_sha256": "e" * 64, "extension_path": str(root / "probe.so")}

    provenance = tmp_path / "toolchain.json"
    result = runtime.bootstrap_toolchain(
        lock_path,
        tmp_path / "bootstrap",
        provenance,
        downloader=downloader,
        extension_probe=probe,
    )
    assert [url for url, _ in seen] == [UV_URL, PYTHON_URL]
    assert result.python.name == "python3.10"
    assert result.uv.name == "uv"
    assert result.include.joinpath("Python.h").is_file()
    assert probed == [(result.python, result.include)]
    data = json.loads(provenance.read_text(encoding="utf-8"))
    assert data["python"]["archive_sha256"] == digest(python_archive)
    assert data["uv"]["archive_sha256"] == digest(uv_archive)


def test_bootstrap_toolchain_rejects_redirected_download(tmp_path: Path) -> None:
    uv_archive = tar_bytes({"uv-x86_64-unknown-linux-gnu/uv": (b"uv", 0o755)})
    python_archive = tar_bytes(
        {
            "python/bin/python3.10": (b"python", 0o755),
            "python/include/python3.10/Python.h": (b"header", 0o644),
        }
    )
    path = tmp_path / "lock"
    write_json(path, toolchain_lock(uv_archive, python_archive))

    def downloader(url: str, destination: Path) -> str:
        destination.write_bytes(uv_archive if url == UV_URL else python_archive)
        return "https://attacker.example/artifact"

    with pytest.raises(runtime.EvidenceError, match="redirect|final url"):
        runtime.bootstrap_toolchain(
            path,
            tmp_path / "root",
            tmp_path / "provenance.json",
            downloader=downloader,
            extension_probe=lambda *args: {},
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["packages"][0].pop("sha256"), "sha256"),
        (lambda value: value["packages"][0].update(url="http://example.test/demo.whl"), "https"),
        (lambda value: value["packages"][0].update(filename="demo-1.0.tar.gz"), "wheel"),
        (
            lambda value: value["packages"][0].update(
                filename="demo-1.0-cp312-cp312-manylinux_2_28_x86_64.whl"
            ),
            "python 3.10|compatible",
        ),
        (
            lambda value: value["packages"].append({**value["packages"][0], "name": "DEMO"}),
            "duplicate",
        ),
        (lambda value: value["packages"][0].update(source_host="example.test"), "source_host"),
        (lambda value: value.update(extra=True), "unexpected"),
    ],
)
def test_load_runtime_lock_rejects_invalid_entries(tmp_path: Path, mutation, message: str) -> None:
    value = runtime_lock()
    mutation(value)
    path = tmp_path / "runtime.lock"
    write_json(path, value)
    with pytest.raises(runtime.EvidenceError, match=message):
        runtime.load_runtime_lock(path)


def test_runtime_lock_enforces_torch_and_triton_sources(tmp_path: Path) -> None:
    torch = package_record(
        "torch",
        "2.8.0+metax3.7.1.3",
        TORCH_URL.rsplit("/", 1)[1],
        TORCH_URL,
        b"torch",
    )
    triton = package_record("triton", "3.5.1", TRITON_URL.rsplit("/", 1)[1], TRITON_URL, b"triton")
    torch.update(size=TORCH_SIZE, sha256=TORCH_SHA256)
    triton.update(size=TRITON_SIZE, sha256=TRITON_SHA256)
    path = tmp_path / "runtime.lock"
    write_json(path, runtime_lock(torch, triton))
    runtime.load_runtime_lock(path)

    torch["url"] = f"https://files.pythonhosted.org/packages/{torch['filename']}"
    torch["source_host"] = "files.pythonhosted.org"
    write_json(path, runtime_lock(torch, triton))
    with pytest.raises(runtime.EvidenceError, match="torch.*source|pinned"):
        runtime.load_runtime_lock(path)


def test_normalize_runtime_lock_selects_compatible_wheel_and_pinned_sources(tmp_path: Path) -> None:
    native = pypi_file(
        "demo_pkg-1.2.3-cp310-cp310-manylinux_2_28_x86_64.whl",
        b"native",
    )
    older = pypi_file(
        "demo_pkg-1.2.3-cp310-cp310-manylinux_2_17_x86_64.whl",
        b"older",
    )
    pure = pypi_file("demo_pkg-1.2.3-py3-none-any.whl", b"pure")
    incompatible = pypi_file(
        "demo_pkg-1.2.3-cp311-cp311-manylinux_2_28_x86_64.whl",
        b"cp311",
    )
    requirements = tmp_path / "resolved.txt"
    requirements.write_text(
        f"torch @ {TORCH_URL}#sha256={TORCH_SHA256}\n"
        f"triton @ {TRITON_URL}#sha256={TRITON_SHA256}\n"
        "demo-pkg==1.2.3 \\\n"
        f"    --hash=sha256:{native['digests']['sha256']} \\\n"
        f"    --hash=sha256:{older['digests']['sha256']} \\\n"
        f"    --hash=sha256:{pure['digests']['sha256']} \\\n"
        f"    --hash=sha256:{incompatible['digests']['sha256']}\n",
        encoding="utf-8",
    )
    output = tmp_path / "runtime.lock"
    metadata = {("demo-pkg", "1.2.3"): pypi_release("1.2.3", pure, older, incompatible, native)}

    value = runtime.normalize_runtime_lock(
        requirements,
        output,
        metadata_loader=lambda name, version: metadata[(name, version)],
    )

    assert value == json.loads(output.read_text(encoding="utf-8"))
    assert output.read_bytes().endswith(b"\n")
    packages = {item["name"]: item for item in value["packages"]}
    assert list(packages) == ["demo-pkg", "torch", "triton"]
    assert packages["demo-pkg"]["filename"] == native["filename"]
    assert packages["demo-pkg"]["url"] == native["url"]
    assert packages["torch"] == {
        "name": "torch",
        "version": "2.8.0+metax3.7.1.3",
        "filename": TORCH_URL.rsplit("/", 1)[1],
        "url": TORCH_URL,
        "source_host": "repos.metax-tech.com",
        "size": TORCH_SIZE,
        "sha256": TORCH_SHA256,
    }
    assert packages["triton"]["url"] == TRITON_URL
    runtime.load_runtime_lock(output)


@pytest.mark.parametrize(
    ("requirements_text", "metadata", "message"),
    [
        (
            "torch==2.8.0 --hash=sha256:" + "a" * 64 + "\n",
            {("torch", "2.8.0"): pypi_release("2.8.0")},
            "torch.*direct|pinned",
        ),
        (
            "demo @ https://private.example/demo-1.0-py3-none-any.whl#sha256=" + "a" * 64 + "\n",
            {},
            "direct URL|files.pythonhosted",
        ),
        (
            "demo==1.0 --hash=sha256:" + digest(b"sdist") + "\n",
            {
                ("demo", "1.0"): pypi_release(
                    "1.0",
                    pypi_file("demo-1.0.tar.gz", b"sdist", packagetype="sdist"),
                )
            },
            "compatible wheel",
        ),
        (
            "demo==1.0 --hash=sha256:" + digest(b"listed") + "\n",
            {
                ("demo", "1.0"): pypi_release(
                    "1.0",
                    pypi_file("demo-1.0-py3-none-any.whl", b"other"),
                )
            },
            "hash|compatible wheel",
        ),
        (
            "demo==1.0 --hash=sha256:" + digest(b"new-glibc") + "\n",
            {
                ("demo", "1.0"): pypi_release(
                    "1.0",
                    pypi_file(
                        "demo-1.0-cp310-cp310-manylinux_2_31_x86_64.whl",
                        b"new-glibc",
                    ),
                )
            },
            "compatible wheel",
        ),
        (
            "demo_pkg==1.0 --hash=sha256:"
            + digest(b"first")
            + "\ndemo-pkg==1.0 --hash=sha256:"
            + digest(b"second")
            + "\n",
            {},
            "duplicate normalized",
        ),
    ],
)
def test_normalize_runtime_lock_rejects_untrusted_or_incompatible_sources(
    tmp_path: Path,
    requirements_text: str,
    metadata: dict[tuple[str, str], dict[str, object]],
    message: str,
) -> None:
    requirements = tmp_path / "resolved.txt"
    requirements.write_text(requirements_text, encoding="utf-8")
    with pytest.raises(runtime.EvidenceError, match=message):
        runtime.normalize_runtime_lock(
            requirements,
            tmp_path / "runtime.lock",
            metadata_loader=lambda name, version: metadata[(name, version)],
        )


def test_normalize_runtime_lock_rejects_ambiguous_equal_rank_wheels(tmp_path: Path) -> None:
    first = pypi_file("demo-1.0-cp310-cp310-manylinux_2_28_x86_64.whl", b"first")
    second = pypi_file(
        "demo-1.0-cp310-cp310-manylinux_2_28_x86_64.alt.whl",
        b"second",
    )
    requirements = tmp_path / "resolved.txt"
    requirements.write_text(
        "demo==1.0 \\\n"
        f"    --hash=sha256:{first['digests']['sha256']} \\\n"
        f"    --hash=sha256:{second['digests']['sha256']}\n",
        encoding="utf-8",
    )
    release = pypi_release("1.0", first, second)
    with pytest.raises(runtime.EvidenceError, match="ambiguous"):
        runtime.normalize_runtime_lock(
            requirements,
            tmp_path / "runtime.lock",
            metadata_loader=lambda name, version: release,
        )


def test_make_pair_environment_replaces_inherited_paths(tmp_path: Path) -> None:
    pair_root = tmp_path / "pair"
    inherited = {
        "HOME": "/home/runner",
        "PYTHONPATH": "/ci-cache/site-packages",
        "TILELANG_CACHE_DIR": "/ci-cache/tilelang",
        "TRITON_CACHE_DIR": "/ci-cache/triton",
        "CONDA_PREFIX": "/opt/conda",
        "PATH": "/usr/bin:/bin",
    }
    environment = runtime.make_pair_environment(pair_root, inherited)
    path_variables = runtime.PAIR_PATH_VARIABLES
    for name in path_variables:
        value = Path(environment[name]).resolve()
        assert value.is_relative_to(pair_root.resolve()), (name, value)
        assert value.is_dir()
    assert environment["PYTHONPATH"] == ""
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["TILELANG_DISABLE_CACHE"] == "1"
    assert "CONDA_PREFIX" not in environment
    assert "/ci-cache" not in json.dumps(environment)


def test_make_pair_environment_rejects_nonempty_or_symlink_roots(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "stale").write_text("stale", encoding="utf-8")
    with pytest.raises(runtime.EvidenceError, match="empty"):
        runtime.make_pair_environment(nonempty, {})

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(runtime.EvidenceError, match="symlink"):
        runtime.make_pair_environment(link, {})


def test_audit_wheel_reads_metadata_and_hash(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path / "demo_package-1.0-py3-none-any.whl")
    audit = runtime.audit_wheel(wheel, "demo-package")
    assert audit.name == "demo-package"
    assert audit.version == "1.0"
    assert audit.sha256 == digest(wheel.read_bytes())
    assert audit.size == wheel.stat().st_size


@pytest.mark.parametrize(
    ("extra_files", "entry_points", "message"),
    [
        ({"escape.pth": b"/tmp"}, None, "pth"),
        ({"sitecustomize.py": b""}, None, "sitecustomize"),
        ({"pkg/usercustomize.py": b""}, None, "usercustomize"),
        ({"../escape": b""}, None, "unsafe|traversal"),
        ({}, "[pytest11]\nplugin = pkg.plugin\n", "pytest11"),
    ],
)
def test_audit_wheel_rejects_startup_hooks(
    tmp_path: Path, extra_files: dict[str, bytes], entry_points: str | None, message: str
) -> None:
    wheel = make_wheel(
        tmp_path / "demo_package-1.0-py3-none-any.whl",
        extra_files=extra_files,
        entry_points=entry_points,
    )
    with pytest.raises(runtime.EvidenceError, match=message):
        runtime.audit_wheel(wheel, "demo-package")


def test_audit_wheel_allows_only_the_pinned_timeout_plugin_entry_point(tmp_path: Path) -> None:
    expected = make_wheel(
        tmp_path / "pytest_timeout-2.4.0-py3-none-any.whl",
        name="pytest-timeout",
        version="2.4.0",
        entry_points="[pytest11]\ntimeout = pytest_timeout\n",
    )
    assert runtime.audit_wheel(expected, "pytest-timeout").version == "2.4.0"

    tampered = make_wheel(
        tmp_path / "pytest_timeout-2.4.0-1-py3-none-any.whl",
        name="pytest-timeout",
        version="2.4.0",
        entry_points="[pytest11]\ntimeout = attacker.plugin\n",
    )
    with pytest.raises(runtime.EvidenceError, match="pytest11"):
        runtime.audit_wheel(tampered, "pytest-timeout")


def test_audit_wheel_allows_only_the_pinned_setuptools_startup_hook(tmp_path: Path) -> None:
    expected_pth = (
        b"import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local') == 'local'; "
        b"enabled and __import__('_distutils_hack').add_shim(); \n"
    )
    expected = make_wheel(
        tmp_path / "setuptools-80.9.0-py3-none-any.whl",
        name="setuptools",
        version="80.9.0",
        extra_files={
            "distutils-precedence.pth": expected_pth,
            "setuptools/_vendor/demo-1.0.dist-info/METADATA": b"vendored",
            "setuptools/_vendor/demo-1.0.dist-info/entry_points.txt": b"[pytest11]\nevil = demo\n",
        },
    )
    assert runtime.audit_wheel(expected, "setuptools").version == "80.9.0"

    tampered = make_wheel(
        tmp_path / "setuptools-80.9.0-1-py3-none-any.whl",
        name="setuptools",
        version="80.9.0",
        extra_files={"distutils-precedence.pth": b"import attacker\n"},
    )
    with pytest.raises(runtime.EvidenceError, match="pth"):
        runtime.audit_wheel(tampered, "setuptools")


def test_audit_wheel_rejects_wrong_distribution_and_duplicate_selection(tmp_path: Path) -> None:
    first = make_wheel(tmp_path / "demo_package-1.0-py3-none-any.whl")
    with pytest.raises(runtime.EvidenceError, match="distribution"):
        runtime.audit_wheel(first, "other")
    second = make_wheel(tmp_path / "demo_package-1.1-py3-none-any.whl", version="1.1")
    with pytest.raises(runtime.EvidenceError, match="exactly one|multiple"):
        runtime.select_wheel([first, second], "demo-package")


def test_artifact_manifest_round_trip_and_determinism(tmp_path: Path) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "nested").mkdir()
    (root / "nested" / "b.bin").write_bytes(b"b")
    manifest = root / "artifact-manifest.json"
    identity = {"repository": "owner/repo", "run_id": 10, "run_attempt": 2, "pair": "baseline"}
    runtime.write_artifact_manifest(root, manifest, identity)
    first = manifest.read_bytes()
    runtime.validate_artifact_manifest(root, manifest, identity)
    runtime.write_artifact_manifest(root, manifest, identity)
    assert manifest.read_bytes() == first


@pytest.mark.parametrize("failure", ["tamper", "extra", "identity", "traversal", "symlink"])
def test_artifact_manifest_rejects_untrusted_contents(tmp_path: Path, failure: str) -> None:
    root = tmp_path / "artifact"
    root.mkdir()
    target = root / "data.txt"
    target.write_text("data", encoding="utf-8")
    manifest = root / "artifact-manifest.json"
    identity = {"repository": "owner/repo", "run_id": 10, "run_attempt": 2, "pair": "baseline"}
    runtime.write_artifact_manifest(root, manifest, identity)
    expected = identity
    if failure == "tamper":
        target.write_text("changed", encoding="utf-8")
    elif failure == "extra":
        (root / "extra").write_text("extra", encoding="utf-8")
    elif failure == "identity":
        expected = {**identity, "run_attempt": 3}
    elif failure == "traversal":
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["files"][0]["path"] = "../escape"
        write_json(manifest, value)
    else:
        target.unlink()
        target.symlink_to("/etc/passwd")
    with pytest.raises(runtime.EvidenceError):
        runtime.validate_artifact_manifest(root, manifest, expected)


def test_validate_service_artifact_requires_exact_identity() -> None:
    record = {
        "schema_version": 1,
        "repository": "owner/repo",
        "run_id": 10,
        "run_attempt": 2,
        "name": "baseline-10-2",
        "artifact_id": 123,
        "artifact_digest": f"sha256:{'a' * 64}",
        "expired": False,
    }
    runtime.validate_service_artifact(record, record)
    for key, bad in [
        ("repository", "other/repo"),
        ("run_id", 11),
        ("run_attempt", 3),
        ("name", "candidate-10-2"),
        ("artifact_id", 124),
        ("artifact_digest", f"sha256:{'b' * 64}"),
        ("expired", True),
    ]:
        with pytest.raises(
            runtime.EvidenceError, match=key.replace("_", " ") + "|mismatch|expired"
        ):
            runtime.validate_service_artifact({**record, key: bad}, record)


@pytest.mark.parametrize(
    ("schema", "factory"),
    [
        ("resolved", valid_resolved),
        ("status", valid_status),
        ("provenance", valid_provenance),
        ("collection", valid_collection),
    ],
)
def test_load_bounded_json_accepts_exact_schemas(tmp_path: Path, schema: str, factory) -> None:
    path = tmp_path / f"{schema}.json"
    write_json(path, factory())
    assert runtime.load_bounded_json(path, schema) == factory()


def test_load_bounded_json_rejects_size_duplicates_unknown_keys_and_limits(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
    with pytest.raises(runtime.EvidenceError, match="2 MiB|size"):
        runtime.load_bounded_json(oversized, "status")

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(runtime.EvidenceError, match="duplicate"):
        runtime.load_bounded_json(duplicate, "status")

    unknown = tmp_path / "unknown.json"
    write_json(unknown, {**valid_status(), "unexpected": True})
    with pytest.raises(runtime.EvidenceError, match="unexpected"):
        runtime.load_bounded_json(unknown, "status")

    collection = valid_collection()
    collection["nodeids"] = [f"test::{index}" for index in range(10001)]
    collection["count"] = len(collection["nodeids"])
    too_many = tmp_path / "collection.json"
    write_json(too_many, collection)
    with pytest.raises(runtime.EvidenceError, match="10000|node"):
        runtime.load_bounded_json(too_many, "collection")


def test_load_bounded_json_rejects_invalid_enums_and_collection_digest(tmp_path: Path) -> None:
    status = valid_status()
    status["state"] = "maybe"
    path = tmp_path / "status.json"
    write_json(path, status)
    with pytest.raises(runtime.EvidenceError, match="state"):
        runtime.load_bounded_json(path, "status")

    collection = valid_collection()
    collection["nodeids"] = [collection["nodeids"][0], collection["nodeids"][0]]
    collection["count"] = 2
    path = tmp_path / "collection.json"
    write_json(path, collection)
    with pytest.raises(runtime.EvidenceError, match="duplicate|node"):
        runtime.load_bounded_json(path, "collection")

    collection = valid_collection()
    collection["nodeids_sha256"] = "0" * 64
    write_json(path, collection)
    with pytest.raises(runtime.EvidenceError, match="digest|nodeids_sha256"):
        runtime.load_bounded_json(path, "collection")


def test_materialize_verify_and_emit_wheelhouse(tmp_path: Path) -> None:
    alpha_path = make_wheel(tmp_path / "alpha-1.0-py3-none-any.whl", name="alpha")
    beta_path = make_wheel(tmp_path / "beta-2.0-py3-none-any.whl", name="beta", version="2.0")
    blobs = {"alpha": alpha_path.read_bytes(), "beta": beta_path.read_bytes()}
    alpha = package_record(
        "alpha",
        "1.0",
        alpha_path.name,
        url=f"https://files.pythonhosted.org/a/{alpha_path.name}",
        data=blobs["alpha"],
    )
    beta = package_record(
        "beta",
        "2.0",
        beta_path.name,
        url=f"https://files.pythonhosted.org/b/{beta_path.name}",
        data=blobs["beta"],
    )
    lock_path = tmp_path / "runtime.lock"
    write_json(lock_path, runtime_lock(beta, alpha))

    def downloader(url: str, destination: Path) -> str:
        destination.write_bytes(blobs["alpha"] if "alpha" in url else blobs["beta"])
        return url

    wheelhouse = tmp_path / "wheelhouse"
    runtime.materialize_wheelhouse(lock_path, wheelhouse, downloader=downloader)
    runtime.freeze_wheelhouse(wheelhouse)
    runtime.verify_wheelhouse(lock_path, wheelhouse)
    requirements = tmp_path / "requirements.txt"
    runtime.emit_requirements(lock_path, requirements)
    lines = requirements.read_text(encoding="utf-8").splitlines()
    assert lines == [
        f"alpha==1.0 --hash=sha256:{alpha['sha256']}",
        f"beta==2.0 --hash=sha256:{beta['sha256']}",
    ]
    manifest = json.loads((wheelhouse / "wheelhouse-manifest.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in manifest["packages"]] == ["alpha", "beta"]
    assert all(
        item["url"].startswith("https://files.pythonhosted.org/") for item in manifest["packages"]
    )


@pytest.mark.parametrize("failure", ["wheel", "mode", "extra", "manifest"])
def test_verify_wheelhouse_rejects_mutation(tmp_path: Path, failure: str) -> None:
    source = make_wheel(tmp_path / "alpha-1.0-py3-none-any.whl", name="alpha")
    blob = source.read_bytes()
    package = package_record("alpha", "1.0", source.name, data=blob)
    lock_path = tmp_path / "runtime.lock"
    write_json(lock_path, runtime_lock(package))
    wheelhouse = tmp_path / "wheelhouse"
    runtime.materialize_wheelhouse(
        lock_path,
        wheelhouse,
        downloader=lambda url, destination: (destination.write_bytes(blob), url)[1],
    )
    runtime.freeze_wheelhouse(wheelhouse)
    if failure == "wheel":
        os.chmod(wheelhouse / source.name, 0o644)
        (wheelhouse / source.name).write_bytes(b"changed")
        os.chmod(wheelhouse / source.name, 0o444)
    elif failure == "mode":
        os.chmod(wheelhouse / source.name, 0o644)
    elif failure == "extra":
        os.chmod(wheelhouse, 0o755)
        (wheelhouse / "extra.whl").write_bytes(b"extra")
        os.chmod(wheelhouse / "extra.whl", 0o444)
        os.chmod(wheelhouse, 0o555)
    else:
        manifest = wheelhouse / "wheelhouse-manifest.json"
        os.chmod(manifest, 0o644)
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["packages"][0]["sha256"] = "0" * 64
        write_json(manifest, value)
        os.chmod(manifest, 0o444)
    with pytest.raises(runtime.EvidenceError):
        runtime.verify_wheelhouse(lock_path, wheelhouse)


def test_runtime_cli_help_is_available() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/cross_repo_perf_runtime.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "bootstrap-toolchain" in result.stdout
    assert "normalize-runtime-lock" in result.stdout
    assert "materialize-wheelhouse" in result.stdout


def test_committed_toolchain_and_runtime_locks_validate() -> None:
    root = Path(__file__).resolve().parents[1]
    toolchain = runtime.load_toolchain_lock(root / "scripts/ci/cross_repo_perf_toolchain.lock")
    runtime_lock_value = runtime.load_runtime_lock(root / "scripts/ci/cross_repo_perf_py310.lock")
    assert toolchain["uv"]["sha256"] == (
        "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"
    )
    assert toolchain["python"]["sha256"] == (
        "7b1d02e28b0d36c4b0de044aaf8099cb0395ac3d6826c96ddd158241fcdc6f06"
    )
    packages = {
        runtime.normalize_name(item["name"]): item for item in runtime_lock_value["packages"]
    }
    assert packages["torch"]["url"] == TORCH_URL
    assert packages["triton"]["url"] == TRITON_URL
    assert {"pytest", "pytest-timeout", "scikit-build-core", "cmake", "ninja"} <= set(packages)
