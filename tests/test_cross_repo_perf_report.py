from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts import cross_repo_perf_report as report
from scripts import cross_repo_perf_runtime as runtime


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _junit(*testcases: str) -> str:
    return "<testsuites><testsuite>" + "".join(testcases) + "</testsuite></testsuites>"


def _testcase(
    name: str,
    *,
    classname: str = "benchmarks.ops.bench_demo",
    properties: dict[str, str] | None = None,
    child: str = "",
) -> str:
    property_xml = "".join(
        f'<property name="{key}" value="{value}"/>' for key, value in (properties or {}).items()
    )
    return (
        f'<testcase classname="{classname}" name="{name}">'
        f"<properties>{property_xml}</properties>{child}</testcase>"
    )


def _case(
    identity: str,
    latency: float | None,
    *,
    outcome: str = "passed",
    failure_message: str | None = None,
) -> report.BenchmarkCase:
    classname, name = identity.split("::", 1)
    return report.BenchmarkCase(
        identity=identity,
        classname=classname,
        name=name,
        outcome=outcome,
        latency_ms=latency,
        op="Demo|Op",
        op_module="tileops.ops.demo",
        tflops=100.0 if latency else None,
        bandwidth_tbs=None,
        variant=None,
        failure_message=failure_message,
    )


def _repository(repository: str, number: int, default: str, merge: str) -> dict[str, object]:
    return {
        "repository": repository,
        "pr_number": number,
        "pr_url": f"https://github.com/{repository}/pull/{number}",
        "author": "maintainer",
        "default_branch": "dev",
        "default_sha": default,
        "base_ref": "dev",
        "base_sha": default,
        "head_ref": "feature",
        "head_sha": "c" * 40,
        "merge_sha": merge,
    }


def _resolved() -> dict[str, object]:
    return {
        "schema_version": 1,
        "disposition": "run",
        "reason": "",
        "run_id": 7,
        "run_attempt": 2,
        "trigger_comment_id": 99,
        "trigger_actor": "maintainer",
        "harness_sha256": "a" * 64,
        "tileops": _repository("tile-ai/tileops-metax", 42, "1" * 40, "2" * 40),
        "tilelang": _repository("tile-ai/tilelang-metax", 90, "3" * 40, "4" * 40),
    }


def _wheel(distribution: str, source_sha: str) -> dict[str, object]:
    return {
        "distribution": distribution,
        "version": "1.0",
        "filename": f"{distribution}-1.0-py3-none-any.whl",
        "sha256": (distribution[0] if distribution[0] in "abcdef" else "b") * 64,
        "source_sha": source_sha,
        "import_path": f"/pair/venv/site-packages/{distribution}/__init__.py",
    }


def _service_record(pair: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "tile-ai/tileops-metax",
        "run_id": 7,
        "run_attempt": 2,
        "name": f"cross-repo-perf-{pair}-7-2",
        "artifact_id": 100 if pair == "baseline" else 101,
        "artifact_digest": f"sha256:{'d' if pair == 'baseline' else 'e'}",
        "expired": False,
    }


def _valid_service_record(pair: str) -> dict[str, object]:
    value = _service_record(pair)
    value["artifact_digest"] = f"sha256:{'d' * 64 if pair == 'baseline' else 'e' * 64}"
    return value


def _pair_artifact(
    root: Path, pair: str, *, latency: float = 1.0
) -> tuple[dict[str, object], dict[str, object]]:
    resolved = _resolved()
    tileops_sha = (
        resolved["tileops"]["default_sha"]
        if pair == "baseline"
        else resolved["tileops"]["merge_sha"]
    )
    tilelang_sha = (
        resolved["tilelang"]["default_sha"]
        if pair == "baseline"
        else resolved["tilelang"]["merge_sha"]
    )
    payload_sha = "5" * 64
    manifest_sha = "6" * 64
    nodeids = ["benchmarks/ops/bench_demo.py::test_demo"]
    collection = {
        "schema_version": 1,
        "pair": pair,
        "run_id": 7,
        "run_attempt": 2,
        "payload_sha256": payload_sha,
        "manifest_sha256": manifest_sha,
        "harness_sha256": "a" * 64,
        "nodeids_sha256": report.nodeids_sha256(nodeids),
        "count": 1,
        "nodeids": nodeids,
    }
    status = {
        "schema_version": 1,
        "pair": pair,
        "state": "success",
        "phase": "complete",
        "exit_code": 0,
        "reason": "",
        "started_at": "2026-07-14T00:00:00Z",
        "finished_at": "2026-07-14T01:00:00Z",
        "run_id": 7,
        "run_attempt": 2,
        "tileops_sha": tileops_sha,
        "tilelang_sha": tilelang_sha,
        "payload_sha256": payload_sha,
    }
    provenance = {
        "schema_version": 1,
        "pair": pair,
        "run_id": 7,
        "run_attempt": 2,
        "python": {
            "version": "3.10.18",
            "executable": "/pair/venv/bin/python",
            "executable_sha256": "7" * 64,
            "include": "/pair/python/include/python3.10",
        },
        "runtime_lock_sha256": "8" * 64,
        "wheelhouse_manifest_sha256": "9" * 64,
        "payload_sha256": payload_sha,
        "manifest_sha256": manifest_sha,
        "harness_sha256": "a" * 64,
        "companion": _wheel("apache-tvm-ffi", tilelang_sha),
        "tilelang": _wheel("tilelang", tilelang_sha),
        "tileops": _wheel("tileops", tileops_sha),
    }
    _write(root / "status.json", json.dumps(status))
    _write(root / "provenance.json", json.dumps(provenance))
    _write(root / "collection.json", json.dumps(collection))
    _write(
        root / "bench_results.xml",
        _junit(
            _testcase(
                "test_demo",
                properties={
                    "op": "DemoOp",
                    "op_module": "tileops.ops.demo",
                    "tileops_latency_ms": str(latency),
                    "tileops_tflops": "100.0",
                },
            )
        ),
    )
    for name in ("pytest.log", "profile_run.log", "build.log"):
        _write(root / name, "ok\n")
    runtime.write_artifact_manifest(
        root,
        root / "artifact-manifest.json",
        {
            "repository": "tile-ai/tileops-metax",
            "run_id": 7,
            "run_attempt": 2,
            "pair": pair,
        },
    )
    service = _valid_service_record(pair)
    return resolved, service


def test_parse_junit_distinguishes_outcomes_and_metrics(tmp_path: Path) -> None:
    path = tmp_path / "results.xml"
    _write(
        path,
        _junit(
            _testcase(
                "passed",
                properties={
                    "tileops_latency_ms": "1.25",
                    "tileops_tflops": "165.0",
                    "tileops_bandwidth_tbs": "2.5",
                    "tileops_variant": "split-k|packed",
                },
            ),
            _testcase("skipped", child='<skipped message="not supported"/>'),
            _testcase("failed", child='<failure message="wrong | value">trace</failure>'),
            _testcase("errored", child='<error message="compiler failed">trace</error>'),
            _testcase("missing-latency"),
        ),
    )

    cases = report.parse_junit(path)

    assert [case.outcome for case in cases] == [
        "passed",
        "skipped",
        "failed",
        "errored",
        "passed",
    ]
    assert cases[0].latency_ms == 1.25
    assert cases[0].tflops == 165.0
    assert cases[0].bandwidth_tbs == 2.5
    assert cases[0].variant == "split-k|packed"
    assert cases[2].failure_message == "wrong | value"
    assert cases[-1].latency_ms is None


@pytest.mark.parametrize(
    ("nodeid", "identity"),
    [
        (
            "benchmarks/ops/bench_demo.py::test_demo[param]",
            "benchmarks.ops.bench_demo::test_demo[param]",
        ),
        (
            "benchmarks/ops/bench_demo.py::TestGroup::test_demo",
            "benchmarks.ops.bench_demo.TestGroup::test_demo",
        ),
    ],
)
def test_junit_identity_from_nodeid_matches_pytest_xunit2(nodeid: str, identity: str) -> None:
    assert report.junit_identity_from_nodeid(nodeid) == identity


@pytest.mark.parametrize("value", ["bad", "nan", "inf", "-1", "0"])
def test_parse_junit_rejects_invalid_latency(tmp_path: Path, value: str) -> None:
    path = tmp_path / "results.xml"
    _write(path, _junit(_testcase("bad", properties={"tileops_latency_ms": value})))
    with pytest.raises(report.ReportError, match="latency|finite|positive"):
        report.parse_junit(path)


def test_parse_junit_rejects_duplicate_identity_property_and_xml_features(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.xml"
    case = _testcase("same", properties={"tileops_latency_ms": "1"})
    _write(duplicate, _junit(case, case))
    with pytest.raises(report.ReportError, match="duplicate testcase"):
        report.parse_junit(duplicate)

    duplicate_property = tmp_path / "duplicate-property.xml"
    _write(
        duplicate_property,
        _junit(
            '<testcase classname="c" name="n"><properties>'
            '<property name="tileops_latency_ms" value="1"/>'
            '<property name="tileops_latency_ms" value="2"/>'
            "</properties></testcase>"
        ),
    )
    with pytest.raises(report.ReportError, match="duplicate.*property"):
        report.parse_junit(duplicate_property)

    doctype = tmp_path / "doctype.xml"
    _write(doctype, '<!DOCTYPE x [<!ENTITY y "z">]><testsuites/>')
    with pytest.raises(report.ReportError, match="DOCTYPE|ENTITY"):
        report.parse_junit(doctype)


def test_parse_junit_enforces_case_property_and_value_limits(tmp_path: Path) -> None:
    too_many_cases = tmp_path / "many.xml"
    _write(too_many_cases, _junit(*(_testcase(f"n{i}") for i in range(10_001))))
    with pytest.raises(report.ReportError, match="10000|testcase"):
        report.parse_junit(too_many_cases)

    too_many_properties = tmp_path / "properties.xml"
    properties = "".join(f'<property name="p{i}_ratio" value="1"/>' for i in range(129))
    _write(
        too_many_properties,
        _junit(
            f'<testcase classname="c" name="n"><properties>{properties}</properties></testcase>'
        ),
    )
    with pytest.raises(report.ReportError, match="128|properties"):
        report.parse_junit(too_many_properties)

    oversized = tmp_path / "oversized.xml"
    _write(oversized, _junit(_testcase("n", properties={"op": "x" * 5000})))
    with pytest.raises(report.ReportError, match="size|4096|value"):
        report.parse_junit(oversized)


def test_compare_uses_exact_thresholds_and_severe_subset() -> None:
    baseline = [
        _case("c::improved", 10.0),
        _case("c::neutral-fast", 10.0),
        _case("c::neutral-slow", 10.0),
        _case("c::regressed", 10.0),
        _case("c::severe", 10.0),
    ]
    candidate = [
        _case("c::improved", 10.0 / 1.05),
        _case("c::neutral-fast", 10.0 / 1.049999),
        _case("c::neutral-slow", 10.0 / 0.95),
        _case("c::regressed", 10.0 / 0.90),
        _case("c::severe", 10.0 / 0.899999),
    ]

    rows = {row.identity: row for row in report.compare(baseline, candidate)}

    assert rows["c::improved"].classification == "improved"
    assert rows["c::neutral-fast"].classification == "neutral"
    assert rows["c::neutral-slow"].classification == "neutral"
    assert rows["c::regressed"].classification == "regressed"
    assert rows["c::regressed"].severe is False
    assert rows["c::severe"].classification == "regressed"
    assert rows["c::severe"].severe is True


def test_compare_tracks_added_removed_outcome_changes_and_geomean() -> None:
    baseline = [
        _case("c::common-a", 4.0),
        _case("c::common-b", 9.0),
        _case("c::removed", 1.0),
        _case("c::recovers", None, outcome="failed", failure_message="old failure"),
    ]
    candidate = [
        _case("c::common-a", 2.0),
        _case("c::common-b", 4.0),
        _case("c::added", 1.0),
        _case("c::recovers", 1.0),
    ]

    rows = report.compare(baseline, candidate)
    by_id = {row.identity: row for row in rows}

    assert by_id["c::added"].classification == "added"
    assert by_id["c::removed"].classification == "removed"
    assert by_id["c::recovers"].classification == "outcome_changed"
    assert report.geometric_mean_speedup(rows) == pytest.approx(math.sqrt(2.0 * 2.25))


def test_validate_pair_artifact_checks_service_manifest_and_cross_file_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "baseline"
    root.mkdir()
    resolved, service = _pair_artifact(root, "baseline")

    artifact = report.validate_pair_artifact(root, resolved, "baseline", service, service)

    assert artifact.pair == "baseline"
    assert artifact.status["state"] == "success"
    assert artifact.cases[0].latency_ms == 1.0

    wrong_service = {**service, "artifact_id": 999}
    with pytest.raises(runtime.EvidenceError, match="artifact id|mismatch"):
        report.validate_pair_artifact(root, resolved, "baseline", wrong_service, service)

    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    status["tileops_sha"] = "f" * 40
    _write(root / "status.json", json.dumps(status))
    runtime.write_artifact_manifest(
        root,
        root / "artifact-manifest.json",
        {
            "repository": "tile-ai/tileops-metax",
            "run_id": 7,
            "run_attempt": 2,
            "pair": "baseline",
        },
    )
    with pytest.raises(report.ReportError, match="TileOps SHA|identity"):
        report.validate_pair_artifact(root, resolved, "baseline", service, service)


def test_comparison_document_and_markdown_are_deterministic_and_escaped(
    tmp_path: Path,
) -> None:
    baseline_root = tmp_path / "baseline"
    candidate_root = tmp_path / "candidate"
    baseline_root.mkdir()
    candidate_root.mkdir()
    resolved, baseline_service = _pair_artifact(baseline_root, "baseline", latency=2.0)
    _, candidate_service = _pair_artifact(candidate_root, "candidate", latency=1.0)
    baseline = report.validate_pair_artifact(
        baseline_root, resolved, "baseline", baseline_service, baseline_service
    )
    candidate = report.validate_pair_artifact(
        candidate_root, resolved, "candidate", candidate_service, candidate_service
    )
    rows = report.compare(baseline.cases, candidate.cases)
    document = report.build_comparison_document(
        resolved,
        baseline,
        candidate,
        rows,
        run_url="https://github.com/tile-ai/tileops-metax/actions/runs/7",
        artifact_url="https://github.com/tile-ai/tileops-metax/actions/runs/7/artifacts/102",
    )

    first = report.comparison_json_bytes(document)
    second = report.comparison_json_bytes(document)
    markdown = report.render_markdown(document)
    comment = report.render_comment(document, trigger_comment_id=99)

    assert first == second
    assert b'"baseline"' in first
    assert "tile-ai/tileops-metax" in markdown
    assert "`1111111`" in markdown
    assert baseline.provenance["tileops"]["sha256"][:12] in markdown
    assert "actions/runs/7" in markdown
    assert "artifacts/102" in markdown
    assert "<!-- cross-repo-perf:trigger-comment-99 -->" in comment
    assert len(comment) <= 60_000


def test_report_orders_top_changes_bounds_failures_and_truncates_comment() -> None:
    rows = report.compare(
        [
            _case("c::large-regression", 10.0),
            _case("c::small-regression", 10.0),
            _case("c::large-improvement", 10.0),
            _case("c::failure", None, outcome="failed", failure_message="x" * 10_000),
        ],
        [
            _case("c::large-regression", 20.0),
            _case("c::small-regression", 11.0),
            _case("c::large-improvement", 1.0),
            _case("c::failure", None, outcome="failed", failure_message="y" * 10_000),
        ],
    )
    document = {
        "schema_version": 1,
        "trigger_comment_id": 99,
        "metadata": {
            "run_url": "https://example.test/" + "z" * 70_000,
            "artifact_url": "https://example.test/artifact",
            "tileops": {},
            "tilelang": {},
        },
        "baseline": {"outcomes": {}, "wheels": {}},
        "candidate": {"outcomes": {}, "wheels": {}},
        "summary": report.summarize(rows),
        "rows": [report.row_document(row) for row in rows],
    }

    markdown = report.render_markdown(document)
    comment = report.render_comment(document, trigger_comment_id=99)

    assert markdown.index("large-regression") < markdown.index("small-regression")
    assert "Demo\\|Op" in markdown
    assert "x" * 513 not in markdown
    assert len(comment) <= 60_000
    assert comment.startswith("<!-- cross-repo-perf:trigger-comment-99 -->")
