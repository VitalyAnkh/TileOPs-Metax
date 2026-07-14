from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci import cross_repo_perf_harness as harness


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: str | bytes = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _fake_baseline(root: Path, *, harness_bytes: bytes | None = None) -> bytes:
    harness_bytes = harness_bytes or Path(harness.__file__).read_bytes()
    files = {
        "benchmarks/__init__.py": "",
        "benchmarks/conftest.py": "",
        "benchmarks/ops/__init__.py": "",
        "benchmarks/ops/bench_probe.py": "def test_probe():\n    pass\n",
        "workloads/__init__.py": "",
        "workloads/workload_base.py": "VALUE = 'baseline'\n",
        "tests/__init__.py": "",
        "tests/test_base.py": "VALUE = 'baseline'\n",
        "tests/ops/__init__.py": "",
        "tests/ops/test_mamba.py": "VALUE = 'baseline'\n",
        "tileops/manifest/gemm.yaml": '{"Probe": {"workloads": ["baseline"]}}\n',
        "tileops/manifest/reduction.yaml": '{"Reduce": {"workloads": ["baseline"]}}\n',
        "pyproject.toml": (
            '[tool.pytest.ini_options]\npython_files = ["test_*.py", "bench_*.py"]\n'
        ),
    }
    for relative, contents in files.items():
        _write(root / relative, contents)
    _write(root / "scripts/ci/cross_repo_perf_harness.py", harness_bytes)
    _write(root / "tileops/__init__.py", "CANDIDATE_SOURCE_MUST_NOT_BE_COPIED = True\n")
    _write(root / ".git/config", "not copied\n")
    return harness_bytes


def _payload_files(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _resolved(harness_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "disposition": "run",
        "reason": "",
        "run_id": 42,
        "run_attempt": 1,
        "trigger_comment_id": 99,
        "trigger_actor": "maintainer",
        "harness_sha256": harness_sha256,
        "tileops": {},
        "tilelang": {},
    }


def _maca_environment(tmp_path: Path) -> tuple[Path, str, str]:
    maca = tmp_path / "maca"
    maca.mkdir()
    maca_library = tmp_path / "maca-lib"
    maca_library.mkdir()
    return maca, str(maca_library), "-gcc-version 11"


def test_build_trusted_payload_copies_only_the_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "source"
    payload = tmp_path / "payload"
    harness_bytes = _fake_baseline(source)

    manifest = harness.build_trusted_payload(
        source,
        payload,
        expected_harness_sha256=_sha256(harness_bytes),
    )

    assert _payload_files(payload) == [
        "benchmarks/__init__.py",
        "benchmarks/conftest.py",
        "benchmarks/ops/__init__.py",
        "benchmarks/ops/bench_probe.py",
        "cross_repo_perf_harness.py",
        "payload-manifest.json",
        "pyproject.toml",
        "tests/__init__.py",
        "tests/ops/__init__.py",
        "tests/ops/test_mamba.py",
        "tests/test_base.py",
        "trusted_manifest/gemm.yaml",
        "trusted_manifest/reduction.yaml",
        "workloads/__init__.py",
        "workloads/workload_base.py",
    ]
    assert manifest["harness_sha256"] == _sha256(harness_bytes)
    assert not (payload / "tileops").exists()
    assert not (payload / ".git").exists()
    assert harness.verify_trusted_payload(payload) == manifest


@pytest.mark.parametrize(
    "relative",
    [
        "benchmarks/escape.py",
        "workloads/escape.py",
        "tileops/manifest/escape.yaml",
        "scripts/ci/cross_repo_perf_harness.py",
    ],
)
def test_payload_builder_rejects_symlinks(tmp_path: Path, relative: str) -> None:
    source = tmp_path / "source"
    _fake_baseline(source)
    target = source / relative
    target.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source / "pyproject.toml")

    with pytest.raises(harness.HarnessError, match="symlink"):
        harness.build_trusted_payload(source, tmp_path / "payload")


def test_payload_builder_rejects_special_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fake_baseline(source)
    os.mkfifo(source / "workloads/fifo")

    with pytest.raises(harness.HarnessError, match="regular file or directory"):
        harness.build_trusted_payload(source, tmp_path / "payload")


@pytest.mark.parametrize(
    "relative",
    [
        "benchmarks",
        "workloads",
        "tests/__init__.py",
        "tests/test_base.py",
        "tests/ops/__init__.py",
        "tests/ops/test_mamba.py",
        "tileops/manifest",
        "pyproject.toml",
        "scripts/ci/cross_repo_perf_harness.py",
    ],
)
def test_payload_builder_rejects_missing_required_paths(tmp_path: Path, relative: str) -> None:
    source = tmp_path / "source"
    _fake_baseline(source)
    path = source / relative
    if path.is_dir():
        for child in sorted(path.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        path.rmdir()
    else:
        path.unlink()

    with pytest.raises(harness.HarnessError, match="required"):
        harness.build_trusted_payload(source, tmp_path / "payload")


@pytest.mark.parametrize(
    "relative",
    [
        "tileops/__init__.py",
        "tilelang.py",
        "escape.pth",
        "sitecustomize.py",
        "usercustomize.py",
        ".git/config",
    ],
)
def test_payload_audit_rejects_import_and_startup_poisoning(tmp_path: Path, relative: str) -> None:
    root = tmp_path / "payload"
    root.mkdir()
    _write(root / relative, "poison\n")

    with pytest.raises(harness.HarnessError):
        harness.audit_payload_root(root)


def test_payload_manifest_rejects_traversal_and_extra_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fake_baseline(source)
    payload = tmp_path / "payload"
    harness.build_trusted_payload(source, payload)
    manifest_path = payload / "payload-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(harness.HarnessError, match="relative path"):
        harness.verify_trusted_payload(payload)

    harness.build_trusted_payload(source, tmp_path / "clean-payload")
    _write(tmp_path / "clean-payload/extra.txt", "extra\n")
    with pytest.raises(harness.HarnessError, match="unexpected payload files"):
        harness.verify_trusted_payload(tmp_path / "clean-payload")


def test_tree_hash_is_canonical_and_detects_mutation(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first / "b/z.txt", "z")
    _write(first / "a.txt", "a")
    _write(second / "a.txt", "a")
    _write(second / "b/z.txt", "z")

    assert harness.canonical_tree_sha256(first) == harness.canonical_tree_sha256(second)
    _write(second / "b/z.txt", "changed")
    assert harness.canonical_tree_sha256(first) != harness.canonical_tree_sha256(second)


def test_session_finish_rejects_any_trusted_payload_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    harness_bytes = _fake_baseline(source)
    payload = tmp_path / "payload"
    manifest = harness.build_trusted_payload(
        source, payload, expected_harness_sha256=_sha256(harness_bytes)
    )
    state = harness._PluginState(
        pair="baseline",
        run_id=42,
        run_attempt=1,
        payload_root=payload,
        environment_root=tmp_path / "pair/venv",
        collection_path=tmp_path / "pair/artifact/collection.json",
        payload_sha256=manifest["payload_sha256"],
        manifest_sha256=manifest["manifest_sha256"],
        harness_sha256=manifest["harness_sha256"],
    )
    session = SimpleNamespace(
        config=SimpleNamespace(_cross_repo_perf_state=state),
        exitstatus=0,
    )
    _write(payload / "benchmarks/ops/bench_probe.py", "def test_changed():\n    pass\n")

    with pytest.raises(harness.HarnessError, match="payload|trusted benchmark inputs"):
        harness.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 3


def test_payload_digest_changes_for_each_allowed_file_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _fake_baseline(source)
    baseline = harness.build_trusted_payload(source, tmp_path / "payload-a")

    _write(source / "workloads/workload_base.py", "VALUE = 'changed'\n")
    changed = harness.build_trusted_payload(source, tmp_path / "payload-b")

    assert baseline["payload_sha256"] != changed["payload_sha256"]


def test_manifest_override_uses_baseline_and_clears_cache(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    baseline = tmp_path / "baseline"
    _write(candidate / "gemm.yaml", '{"Gemm": {"workloads": ["candidate"]}}')
    _write(baseline / "gemm.yaml", '{"Gemm": {"workloads": ["baseline"]}}')

    module = SimpleNamespace()
    module.manifest_files = lambda: [candidate / "gemm.yaml"]

    @functools.lru_cache(maxsize=1)
    def load_manifest() -> dict[str, object]:
        merged: dict[str, object] = {}
        for path in module.manifest_files():
            merged.update(json.loads(path.read_text(encoding="utf-8")))
        return merged

    module.load_manifest = load_manifest
    module.load_workloads = lambda name: module.load_manifest()[name]["workloads"]
    assert module.load_workloads("Gemm") == ["candidate"]

    digest = harness.install_manifest_override(baseline, manifest_module=module)

    assert digest == harness.canonical_tree_sha256(baseline)
    assert module.load_workloads("Gemm") == ["baseline"]


def test_manifest_override_rejects_empty_or_non_yaml_snapshot(tmp_path: Path) -> None:
    with pytest.raises(harness.HarnessError, match="YAML"):
        harness.install_manifest_override(tmp_path)

    _write(tmp_path / "readme.txt", "not yaml")
    with pytest.raises(harness.HarnessError, match="YAML"):
        harness.install_manifest_override(tmp_path)


def test_collection_document_sorts_nodeids_and_hashes_them() -> None:
    document = harness.collection_document(
        pair="candidate",
        run_id=7,
        run_attempt=2,
        payload_sha256="1" * 64,
        manifest_sha256="2" * 64,
        harness_sha256="3" * 64,
        nodeids=["z::test_b", "a::test_a"],
    )

    assert document["nodeids"] == ["a::test_a", "z::test_b"]
    assert document["count"] == 2
    assert document["nodeids_sha256"] == _sha256(b"a::test_a\nz::test_b\n")


def test_collection_document_rejects_duplicate_nodeids() -> None:
    with pytest.raises(harness.HarnessError, match="duplicate"):
        harness.collection_document(
            pair="baseline",
            run_id=1,
            run_attempt=1,
            payload_sha256="1" * 64,
            manifest_sha256="2" * 64,
            harness_sha256="3" * 64,
            nodeids=["same", "same"],
        )


def test_collection_document_accepts_16k_nodeids_and_rejects_larger_values() -> None:
    common = {
        "pair": "baseline",
        "run_id": 1,
        "run_attempt": 1,
        "payload_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "harness_sha256": "3" * 64,
    }
    document = harness.collection_document(**common, nodeids=["n" * 16_384])
    assert document["count"] == 1
    with pytest.raises(harness.HarnessError, match="16384|node ID"):
        harness.collection_document(**common, nodeids=["n" * 16_385])


def test_harness_json_reader_rejects_symlink_fifo_and_deep_nesting(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(target)
    with pytest.raises(harness.HarnessError, match="symlink|regular|JSON"):
        harness._load_json(symlink)

    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(harness.HarnessError, match="regular|JSON"):
        harness._load_json(fifo)

    deep = tmp_path / "deep.json"
    deep.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
    with pytest.raises(harness.HarnessError, match="JSON|recursive|nesting"):
        harness._load_json(deep)


def test_collection_fingerprint_changes_with_nodeids() -> None:
    first = harness.collection_fingerprint("1" * 64, "2" * 64, ["a"])
    second = harness.collection_fingerprint("1" * 64, "2" * 64, ["b"])
    assert first != second


def test_import_path_audit_enforces_payload_and_environment_roots(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    environment = tmp_path / "venv"
    origins = {
        "benchmarks": payload / "benchmarks/__init__.py",
        "workloads": payload / "workloads/__init__.py",
        "tests": payload / "tests/__init__.py",
        "tileops": environment / "lib/python3.10/site-packages/tileops/__init__.py",
        "tilelang": environment / "lib/python3.10/site-packages/tilelang/__init__.py",
    }
    for origin in origins.values():
        _write(origin, "")

    harness.audit_import_origins(origins, payload, environment)
    origins["tileops"] = payload / "tileops/__init__.py"
    _write(origins["tileops"], "")
    with pytest.raises(harness.HarnessError, match="tileops"):
        harness.audit_import_origins(origins, payload, environment)


def test_verified_pytest_refuses_mutated_harness_before_spawn(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = _fake_baseline(source)
    payload = tmp_path / "payload"
    harness.build_trusted_payload(
        source,
        payload,
        expected_harness_sha256=_sha256(original),
    )
    resolved_path = tmp_path / "resolved.json"
    resolved_path.write_text(json.dumps(_resolved(_sha256(original))), encoding="utf-8")
    environment = tmp_path / "pair/venv"
    output = tmp_path / "pair/artifact"
    _write(environment / "bin/python", "#!/bin/sh\n")
    os.chmod(environment / "bin/python", 0o755)
    _write(environment / "pyvenv.cfg", "home = /trusted\n")
    output.mkdir()
    maca, maca_library_path, mxcc_flags = _maca_environment(tmp_path)
    _write(payload / "cross_repo_perf_harness.py", b"mutated")
    invoked = False

    def runner(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("pytest must not be invoked")

    with pytest.raises(harness.HarnessError, match="harness SHA-256"):
        harness.run_verified_pytest(
            payload,
            resolved_path,
            environment / "bin/python",
            output,
            pair="baseline",
            run_id=42,
            run_attempt=1,
            maca_path=maca,
            maca_library_path=maca_library_path,
            tilelang_mxcc_flags=mxcc_flags,
            runner=runner,
        )
    assert invoked is False


def test_verified_pytest_binds_run_identity_to_resolved_json(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = _fake_baseline(source)
    payload = tmp_path / "payload"
    harness.build_trusted_payload(source, payload, expected_harness_sha256=_sha256(original))
    resolved_value = _resolved(_sha256(original))
    resolved_value["run_attempt"] = 2
    resolved_path = tmp_path / "resolved.json"
    resolved_path.write_text(json.dumps(resolved_value), encoding="utf-8")
    environment = tmp_path / "pair/venv"
    output = tmp_path / "pair/artifact"
    _write(environment / "bin/python", "#!/bin/sh\n")
    os.chmod(environment / "bin/python", 0o755)
    _write(environment / "pyvenv.cfg", "home = /trusted\n")
    output.mkdir()
    maca, maca_library_path, mxcc_flags = _maca_environment(tmp_path)

    with pytest.raises(harness.HarnessError, match="run attempt|identity|resolved"):
        harness.run_verified_pytest(
            payload,
            resolved_path,
            environment / "bin/python",
            output,
            pair="baseline",
            run_id=42,
            run_attempt=1,
            maca_path=maca,
            maca_library_path=maca_library_path,
            tilelang_mxcc_flags=mxcc_flags,
            runner=lambda *args, **kwargs: pytest.fail("pytest must not be invoked"),
        )


def test_verified_pytest_constructs_fixed_command_and_allowlisted_environment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    original = _fake_baseline(source)
    payload = tmp_path / "payload"
    harness.build_trusted_payload(source, payload, expected_harness_sha256=_sha256(original))
    resolved_path = tmp_path / "resolved.json"
    resolved_path.write_text(json.dumps(_resolved(_sha256(original))), encoding="utf-8")
    environment = tmp_path / "pair/venv"
    output = tmp_path / "pair/artifact"
    _write(environment / "bin/python", "#!/bin/sh\n")
    os.chmod(environment / "bin/python", 0o755)
    _write(environment / "pyvenv.cfg", "home = /trusted\n")
    output.mkdir()
    maca, maca_library_path, mxcc_flags = _maca_environment(tmp_path)
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0)

    harness.run_verified_pytest(
        payload,
        resolved_path,
        environment / "bin/python",
        output,
        pair="candidate",
        run_id=42,
        run_attempt=1,
        maca_path=maca,
        maca_library_path=maca_library_path,
        tilelang_mxcc_flags=mxcc_flags,
        runner=runner,
    )

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert command == [
        str((environment / "bin/python").absolute()),
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
        f"--junit-xml={output.absolute() / 'bench_results.xml'}",
    ]
    assert kwargs["cwd"] == payload.resolve()
    assert kwargs["check"] is False
    launch_environment = kwargs["env"]
    assert launch_environment["PYTHONPATH"] == ""
    assert launch_environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert launch_environment["TILELANG_DISABLE_CACHE"] == "1"
    assert launch_environment["MACA_PATH"] == str(maca.resolve())
    assert launch_environment["LD_LIBRARY_PATH"] == str(Path(maca_library_path).resolve())
    assert launch_environment["TILELANG_MXCC_FLAGS"] == "-gcc-version 11"
    assert launch_environment[harness.ENV_OUTPUT_ROOT] == str(output.resolve())
    assert "LD_PRELOAD" not in launch_environment
    assert "PIP_CONFIG_FILE" not in launch_environment


def test_harness_cli_builds_payload_and_writes_digest_summary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = _fake_baseline(source)
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps(_resolved(_sha256(original))), encoding="utf-8")
    payload = tmp_path / "payload"
    result = tmp_path / "payload-result.json"

    assert (
        harness.main(
            [
                "build-payload",
                "--baseline-root",
                str(source),
                "--payload-root",
                str(payload),
                "--resolved",
                str(resolved),
                "--result",
                str(result),
            ]
        )
        == 0
    )
    summary = json.loads(result.read_text(encoding="utf-8"))
    manifest = harness.verify_trusted_payload(payload)
    assert summary == {
        "schema_version": 1,
        "payload_sha256": manifest["payload_sha256"],
        "manifest_sha256": manifest["manifest_sha256"],
        "harness_sha256": manifest["harness_sha256"],
    }


def test_harness_cli_propagates_pytest_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess([], 7)

    monkeypatch.setattr(harness, "run_verified_pytest", fake_run)
    assert (
        harness.main(
            [
                "run-pytest",
                "--payload-root",
                str(tmp_path / "payload"),
                "--resolved",
                str(tmp_path / "resolved.json"),
                "--python",
                str(tmp_path / "pair/venv/bin/python"),
                "--output-root",
                str(tmp_path / "pair/artifact"),
                "--pair",
                "candidate",
                "--run-id",
                "42",
                "--run-attempt",
                "1",
                "--system-path",
                "/usr/bin:/bin",
                "--maca-path",
                str(tmp_path / "maca"),
                "--maca-library-path",
                str(tmp_path / "maca-lib"),
                "--tilelang-mxcc-flags",
                "-gcc-version 11",
            ]
        )
        == 7
    )
    assert captured["kwargs"] == {
        "pair": "candidate",
        "run_id": 42,
        "run_attempt": 1,
        "system_path": "/usr/bin:/bin",
        "maca_path": tmp_path / "maca",
        "maca_library_path": str(tmp_path / "maca-lib"),
        "tilelang_mxcc_flags": "-gcc-version 11",
    }


def test_pair_path_environment_rejects_missing_or_escaping_values(tmp_path: Path) -> None:
    pair_root = tmp_path / "pair"
    pair_root.mkdir()
    environment: dict[str, str] = {}
    for name, relative in harness._PAIR_PATH_NAMES.items():
        path = pair_root / relative
        path.mkdir(exist_ok=True)
        environment[name] = str(path)

    harness._validate_pair_path_environment(pair_root, environment)
    outside = tmp_path / "outside"
    outside.mkdir()
    environment["TMPDIR"] = str(outside)
    with pytest.raises(harness.HarnessError, match="TMPDIR|pair root"):
        harness._validate_pair_path_environment(pair_root, environment)
    environment.pop("TMPDIR")
    with pytest.raises(harness.HarnessError, match="TMPDIR|missing"):
        harness._validate_pair_path_environment(pair_root, environment)


def _fake_installed_packages(site: Path) -> None:
    _write(site / "tileops/__init__.py", "")
    _write(
        site / "tileops/manifest/__init__.py",
        """import functools
import json
from pathlib import Path

def manifest_files():
    return [Path(__file__).with_name("candidate.yaml")]

@functools.lru_cache(maxsize=1)
def load_manifest():
    merged = {}
    for path in manifest_files():
        merged.update(json.loads(path.read_text(encoding="utf-8")))
    return merged

def load_workloads(name):
    return load_manifest()[name]["workloads"]
""",
    )
    _write(
        site / "tileops/manifest/candidate.yaml",
        '{"Probe": {"workloads": ["candidate"]}}',
    )
    _write(site / "tilelang/__init__.py", "")


def test_subprocess_plugin_precedes_conftest_and_disables_entrypoint_autoload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    harness_bytes = _fake_baseline(source)
    _write(
        source / "benchmarks/conftest.py",
        """from pathlib import Path

import tileops.manifest as manifest
from benchmarks.benchmark_base import BenchmarkReport
from tileops.manifest import load_workloads

assert load_workloads("Probe") == ["baseline"]
manifest.manifest_files = lambda: [Path(manifest.__file__).with_name("candidate.yaml")]
manifest.load_manifest.cache_clear()
assert load_workloads("Probe") == ["candidate"]

def pytest_sessionfinish(session, exitstatus):
    BenchmarkReport.dump("profile_run.log")
""",
    )
    _write(
        source / "benchmarks/benchmark_base.py",
        """from pathlib import Path

class BenchmarkReport:
    @staticmethod
    def dump(path):
        Path(path).write_text("trusted report", encoding="utf-8")
""",
    )
    _write(
        source / "benchmarks/ops/bench_probe.py",
        """from tileops.manifest import load_workloads

def test_manifest_is_baseline():
    assert load_workloads("Probe") == ["baseline"]
""",
    )
    payload = tmp_path / "payload"
    manifest = harness.build_trusted_payload(
        source,
        payload,
        expected_harness_sha256=_sha256(harness_bytes),
    )

    environment = tmp_path / "pair/venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(environment)],
        check=True,
    )
    site_result = subprocess.run(
        [
            str(environment / "bin/python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    site = Path(site_result.stdout.strip())
    _write(site / "outer-venv.pth", sysconfig.get_paths()["purelib"] + "\n")
    _fake_installed_packages(site)
    marker = tmp_path / "evil-loaded"
    _write(
        site / "evil_plugin.py",
        "import os\nfrom pathlib import Path\nPath(os.environ['EVIL_PLUGIN_MARKER']).write_text('loaded')\n",
    )
    _write(
        site / "evil_plugin-1.0.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: evil-plugin\nVersion: 1.0\n",
    )
    _write(
        site / "evil_plugin-1.0.dist-info/entry_points.txt",
        "[pytest11]\nevil = evil_plugin\n",
    )
    output = tmp_path / "pair/artifact"
    output.mkdir()
    collection = output / "collection.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pair/pycache"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "EVIL_PLUGIN_MARKER": str(marker),
            harness.ENV_PAIR: "baseline",
            harness.ENV_RUN_ID: "42",
            harness.ENV_RUN_ATTEMPT: "1",
            harness.ENV_OUTPUT_ROOT: str(output),
            harness.ENV_PAYLOAD_SHA256: manifest["payload_sha256"],
            harness.ENV_MANIFEST_SHA256: manifest["manifest_sha256"],
            harness.ENV_HARNESS_SHA256: manifest["harness_sha256"],
        }
    )
    for name, relative in harness._PAIR_PATH_NAMES.items():
        path = tmp_path / "pair" / relative
        path.mkdir(exist_ok=True)
        env[name] = str(path)

    completed = subprocess.run(
        [
            str(environment / "bin/python"),
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
        ],
        cwd=payload,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert not marker.exists()
    assert not (payload / "profile_run.log").exists()
    assert (output / "profile_run.log").read_text(encoding="utf-8") == "trusted report"
    document = json.loads(collection.read_text(encoding="utf-8"))
    assert document["pair"] == "baseline"
    assert document["payload_sha256"] == manifest["payload_sha256"]
    assert document["manifest_sha256"] == manifest["manifest_sha256"]
    assert document["harness_sha256"] == manifest["harness_sha256"]
    assert document["nodeids"] == ["benchmarks/ops/bench_probe.py::test_manifest_is_baseline"]
