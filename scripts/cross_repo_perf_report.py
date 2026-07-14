#!/usr/bin/env python3
"""Validate paired benchmark artifacts and render a deterministic comparison."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import stat
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from scripts import cross_repo_perf_runtime as runtime

MAX_JUNIT_BYTES = 64 * 1024 * 1024
MAX_TESTCASES = 10_000
MAX_PROPERTIES = 128
MAX_XML_VALUE_BYTES = 4_096
MAX_FAILURE_BYTES = 16_384
MAX_COMMENT_CHARS = 60_000
MAX_FAILURE_RENDER_CHARS = 512
IMPROVEMENT_THRESHOLD = 1.05
REGRESSION_THRESHOLD = 0.95
SEVERE_THRESHOLD = 0.90

_FIXED_PROPERTIES = {
    "op",
    "op_module",
    "tileops_variant",
    "tileops_latency_ms",
    "tileops_tflops",
    "tileops_bandwidth_tbs",
    "baseline_tag",
    "baseline_latency_ms",
    "baseline_tflops",
    "baseline_ratio",
}
_DYNAMIC_PROPERTY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}_(?:latency_ms|tflops|ratio)$")


class ReportError(RuntimeError):
    """Raised when benchmark evidence cannot be compared safely."""


@dataclass(frozen=True)
class BenchmarkCase:
    identity: str
    classname: str
    name: str
    outcome: str
    latency_ms: float | None
    op: str | None
    op_module: str | None
    tflops: float | None
    bandwidth_tbs: float | None
    variant: str | None
    failure_message: str | None


@dataclass(frozen=True)
class ComparisonRow:
    identity: str
    baseline: BenchmarkCase | None
    candidate: BenchmarkCase | None
    classification: str
    speedup: float | None
    severe: bool


@dataclass(frozen=True)
class PairArtifact:
    pair: str
    root: Path
    status: dict[str, object]
    provenance: dict[str, object] | None
    collection: dict[str, object] | None
    cases: tuple[BenchmarkCase, ...]
    service_record: dict[str, object]


def nodeids_sha256(nodeids: Sequence[str]) -> str:
    return hashlib.sha256(("\n".join(nodeids) + "\n").encode("utf-8")).hexdigest()


def junit_identity_from_nodeid(nodeid: str) -> str:
    """Convert a pytest path nodeid to its default xUnit2 classname/name identity."""

    if not isinstance(nodeid, str) or not nodeid or "\\" in nodeid or "\x00" in nodeid:
        raise ReportError("collection nodeid must be a safe non-empty string")
    parts = nodeid.split("::")
    if len(parts) < 2 or any(not part for part in parts):
        raise ReportError(f"collection nodeid has an invalid pytest address: {nodeid!r}")
    source = PurePosixPath(parts[0])
    if (
        source.is_absolute()
        or source.suffix != ".py"
        or any(part in {"", ".", ".."} for part in source.parts)
    ):
        raise ReportError(f"collection nodeid has an invalid Python path: {nodeid!r}")
    module_parts = [*source.parts[:-1], source.stem]
    classname = ".".join([*module_parts, *parts[1:-1]])
    return f"{classname}::{parts[-1]}"


def _read_regular_bytes(path: Path, max_bytes: int) -> bytes:
    runtime._assert_no_symlink_components(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReportError(f"cannot open JUnit XML {path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReportError(f"JUnit XML is not a regular file: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ReportError(f"JUnit XML exceeds the {max_bytes} byte size limit")
        return payload
    except OSError as error:
        raise ReportError(f"cannot read JUnit XML {path}: {error}") from error
    finally:
        os.close(descriptor)


def _bounded_xml_text(value: str | None, context: str, limit: int) -> str:
    text = value or ""
    if len(text.encode("utf-8")) > limit:
        raise ReportError(f"{context} exceeds the {limit} byte size limit")
    return text


def _metric(properties: Mapping[str, str], key: str) -> float | None:
    value = properties.get(key)
    if value is None:
        return None
    try:
        metric = float(value)
    except ValueError as error:
        raise ReportError(f"{key} must be a finite positive number") from error
    if not math.isfinite(metric) or metric <= 0:
        raise ReportError(f"{key} must be a finite positive number")
    return metric


def _property_allowed(name: str) -> bool:
    return name in _FIXED_PROPERTIES or _DYNAMIC_PROPERTY_RE.fullmatch(name) is not None


def _testcase_properties(testcase: ET.Element, identity: str) -> dict[str, str]:
    property_elements = testcase.findall("./properties/property")
    if len(property_elements) > MAX_PROPERTIES:
        raise ReportError(f"testcase {identity} exceeds the 128 property limit")
    properties: dict[str, str] = {}
    for element in property_elements:
        name = _bounded_xml_text(
            element.attrib.get("name"), f"testcase {identity} property name", 256
        )
        if not name or not _property_allowed(name):
            raise ReportError(f"testcase {identity} has an unexpected property: {name!r}")
        if name in properties:
            raise ReportError(f"testcase {identity} contains duplicate property {name}")
        value = _bounded_xml_text(
            element.attrib.get("value", ""),
            f"testcase {identity} property {name} value",
            MAX_XML_VALUE_BYTES,
        )
        properties[name] = value
    return properties


def _testcase_outcome(testcase: ET.Element, identity: str) -> tuple[str, str | None]:
    children = {
        "skipped": testcase.findall("./skipped"),
        "failed": testcase.findall("./failure"),
        "errored": testcase.findall("./error"),
    }
    present = [(outcome, elements) for outcome, elements in children.items() if elements]
    if sum(len(elements) for _, elements in present) > 1:
        raise ReportError(f"testcase {identity} contains multiple outcome elements")
    if not present:
        return "passed", None
    outcome, elements = present[0]
    element = elements[0]
    message = element.attrib.get("message") or element.text or ""
    bounded = _bounded_xml_text(message, f"testcase {identity} failure message", MAX_FAILURE_BYTES)
    return outcome, bounded or None


def parse_junit(path: Path | str) -> list[BenchmarkCase]:
    """Parse bounded JUnit benchmark evidence without resolving XML entities."""

    path = Path(path)
    payload = _read_regular_bytes(path, MAX_JUNIT_BYTES)
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ReportError("JUnit XML must not contain DOCTYPE or ENTITY declarations")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, RecursionError) as error:
        raise ReportError(f"invalid JUnit XML {path}: {error}") from error
    testcase_elements = list(root.iter("testcase"))
    if len(testcase_elements) > MAX_TESTCASES:
        raise ReportError(f"JUnit XML exceeds the {MAX_TESTCASES} testcase limit")
    cases: list[BenchmarkCase] = []
    identities: set[str] = set()
    for testcase in testcase_elements:
        classname = _bounded_xml_text(
            testcase.attrib.get("classname"), "testcase classname", MAX_XML_VALUE_BYTES
        )
        name = _bounded_xml_text(testcase.attrib.get("name"), "testcase name", MAX_XML_VALUE_BYTES)
        if not classname or not name:
            raise ReportError("JUnit testcase classname and name must be non-empty")
        identity = f"{classname}::{name}"
        if identity in identities:
            raise ReportError(f"duplicate testcase identity: {identity}")
        identities.add(identity)
        properties = _testcase_properties(testcase, identity)
        outcome, failure_message = _testcase_outcome(testcase, identity)
        cases.append(
            BenchmarkCase(
                identity=identity,
                classname=classname,
                name=name,
                outcome=outcome,
                latency_ms=_metric(properties, "tileops_latency_ms"),
                op=properties.get("op"),
                op_module=properties.get("op_module"),
                tflops=_metric(properties, "tileops_tflops"),
                bandwidth_tbs=_metric(properties, "tileops_bandwidth_tbs"),
                variant=properties.get("tileops_variant"),
                failure_message=failure_message,
            )
        )
    return cases


def _case_index(cases: Sequence[BenchmarkCase], context: str) -> dict[str, BenchmarkCase]:
    output: dict[str, BenchmarkCase] = {}
    for case in cases:
        if case.identity in output:
            raise ReportError(f"{context} contains duplicate testcase {case.identity}")
        output[case.identity] = case
    return output


def compare(
    baseline: Sequence[BenchmarkCase], candidate: Sequence[BenchmarkCase]
) -> list[ComparisonRow]:
    """Join cases by JUnit identity and classify exact speedup thresholds."""

    baseline_by_id = _case_index(baseline, "baseline")
    candidate_by_id = _case_index(candidate, "candidate")
    rows: list[ComparisonRow] = []
    for identity in sorted(set(baseline_by_id) | set(candidate_by_id)):
        baseline_case = baseline_by_id.get(identity)
        candidate_case = candidate_by_id.get(identity)
        speedup: float | None = None
        severe = False
        if baseline_case is None:
            classification = "added"
        elif candidate_case is None:
            classification = "removed"
        elif baseline_case.outcome != candidate_case.outcome:
            classification = "outcome_changed"
        elif baseline_case.outcome != "passed":
            classification = "unchanged_outcome"
        elif baseline_case.latency_ms is None or candidate_case.latency_ms is None:
            classification = "not_comparable"
        else:
            speedup = baseline_case.latency_ms / candidate_case.latency_ms
            if speedup >= IMPROVEMENT_THRESHOLD:
                classification = "improved"
            elif speedup < REGRESSION_THRESHOLD:
                classification = "regressed"
                severe = speedup < SEVERE_THRESHOLD
            else:
                classification = "neutral"
        rows.append(
            ComparisonRow(
                identity=identity,
                baseline=baseline_case,
                candidate=candidate_case,
                classification=classification,
                speedup=speedup,
                severe=severe,
            )
        )
    return rows


def geometric_mean_speedup(rows: Sequence[ComparisonRow]) -> float | None:
    values = [row.speedup for row in rows if row.speedup is not None]
    if not values:
        return None
    return math.exp(sum(math.log(value) for value in values) / len(values))


def _expected_pair_shas(resolved: Mapping[str, object], pair: str) -> tuple[str, str]:
    tileops = resolved["tileops"]
    tilelang = resolved["tilelang"]
    if not isinstance(tileops, dict) or not isinstance(tilelang, dict):
        raise ReportError("resolved repository identities are invalid")
    key = "default_sha" if pair == "baseline" else "merge_sha"
    return str(tileops[key]), str(tilelang[key])


def _validate_pair_documents(
    resolved: Mapping[str, object],
    pair: str,
    status: Mapping[str, object],
    provenance: Mapping[str, object] | None,
    collection: Mapping[str, object] | None,
) -> None:
    run_id = resolved["run_id"]
    run_attempt = resolved["run_attempt"]
    expected_tileops_sha, expected_tilelang_sha = _expected_pair_shas(resolved, pair)
    if status["pair"] != pair or status["run_id"] != run_id or status["run_attempt"] != run_attempt:
        raise ReportError(f"{pair} status identity mismatch")
    if status["tileops_sha"] != expected_tileops_sha:
        raise ReportError(f"{pair} status TileOps SHA identity mismatch")
    if status["tilelang_sha"] != expected_tilelang_sha:
        raise ReportError(f"{pair} status TileLang SHA identity mismatch")
    if provenance is not None:
        if (
            provenance["pair"] != pair
            or provenance["run_id"] != run_id
            or provenance["run_attempt"] != run_attempt
        ):
            raise ReportError(f"{pair} provenance identity mismatch")
        if provenance["payload_sha256"] != status["payload_sha256"]:
            raise ReportError(f"{pair} payload identity mismatch")
        if provenance["harness_sha256"] != resolved["harness_sha256"]:
            raise ReportError(f"{pair} harness identity mismatch")
        for distribution, expected_sha in (
            ("tileops", expected_tileops_sha),
            ("tilelang", expected_tilelang_sha),
        ):
            wheel = provenance[distribution]
            if not isinstance(wheel, dict) or wheel["source_sha"] != expected_sha:
                raise ReportError(f"{pair} {distribution} wheel source identity mismatch")
    if collection is not None:
        if (
            collection["pair"] != pair
            or collection["run_id"] != run_id
            or collection["run_attempt"] != run_attempt
        ):
            raise ReportError(f"{pair} collection identity mismatch")
        if collection["payload_sha256"] != status["payload_sha256"]:
            raise ReportError(f"{pair} collection payload identity mismatch")
        if collection["harness_sha256"] != resolved["harness_sha256"]:
            raise ReportError(f"{pair} collection harness identity mismatch")
        if provenance is not None:
            for key in ("payload_sha256", "manifest_sha256", "harness_sha256"):
                if collection[key] != provenance[key]:
                    raise ReportError(f"{pair} collection/provenance {key} mismatch")


def validate_pair_artifact(
    root: Path | str,
    resolved: Mapping[str, object],
    pair: str,
    service_record: Mapping[str, object],
    expected_service_record: Mapping[str, object],
) -> PairArtifact:
    """Validate service identity, internal hashes, schemas, and pair identity."""

    if pair not in {"baseline", "candidate"}:
        raise ReportError("pair must be baseline or candidate")
    runtime._validate_resolved(dict(resolved))
    if resolved.get("disposition") != "run":
        raise ReportError("pair artifacts require a resolved run disposition")
    runtime.validate_service_artifact(service_record, expected_service_record)
    root = Path(root).absolute()
    expected_identity = {
        "repository": resolved["tileops"]["repository"],
        "run_id": resolved["run_id"],
        "run_attempt": resolved["run_attempt"],
        "pair": pair,
    }
    runtime.validate_artifact_manifest(root, root / "artifact-manifest.json", expected_identity)
    status = runtime.load_bounded_json(root / "status.json", "status")
    provenance_path = root / "provenance.json"
    collection_path = root / "collection.json"
    provenance = (
        runtime.load_bounded_json(provenance_path, "provenance")
        if provenance_path.exists()
        else None
    )
    collection = (
        runtime.load_bounded_json(collection_path, "collection")
        if collection_path.exists()
        else None
    )
    _validate_pair_documents(resolved, pair, status, provenance, collection)
    xml_path = root / "bench_results.xml"
    if status["state"] == "success":
        if status["phase"] != "complete" or status["exit_code"] != 0:
            raise ReportError(f"{pair} success status has inconsistent phase or exit code")
        if provenance is None or collection is None or not xml_path.is_file():
            raise ReportError(f"{pair} successful artifact is incomplete")
    cases = tuple(parse_junit(xml_path)) if xml_path.is_file() else ()
    if collection is not None and cases:
        collected = sorted(junit_identity_from_nodeid(nodeid) for nodeid in collection["nodeids"])
        if len(set(collected)) != len(collected):
            raise ReportError(f"{pair} collection nodeids collapse to duplicate JUnit identities")
        parsed = sorted(case.identity for case in cases)
        if parsed != collected:
            raise ReportError(f"{pair} JUnit testcase identities differ from collection.json")
    return PairArtifact(
        pair=pair,
        root=root,
        status=status,
        provenance=provenance,
        collection=collection,
        cases=cases,
        service_record=dict(service_record),
    )


def _case_document(case: BenchmarkCase | None) -> dict[str, object] | None:
    if case is None:
        return None
    message = case.failure_message
    if message is not None and len(message) > MAX_FAILURE_RENDER_CHARS:
        message = message[: MAX_FAILURE_RENDER_CHARS - 3] + "..."
    return {
        "outcome": case.outcome,
        "latency_ms": case.latency_ms,
        "tflops": case.tflops,
        "bandwidth_tbs": case.bandwidth_tbs,
        "variant": case.variant,
        "failure_message": message,
    }


def row_document(row: ComparisonRow) -> dict[str, object]:
    source = row.candidate or row.baseline
    return {
        "identity": row.identity,
        "op": source.op if source else None,
        "op_module": source.op_module if source else None,
        "classification": row.classification,
        "severe": row.severe,
        "speedup": row.speedup,
        "baseline": _case_document(row.baseline),
        "candidate": _case_document(row.candidate),
    }


def summarize(rows: Sequence[ComparisonRow]) -> dict[str, object]:
    classifications = Counter(row.classification for row in rows)
    return {
        "total": len(rows),
        "comparable": sum(row.speedup is not None for row in rows),
        "improved": classifications["improved"],
        "neutral": classifications["neutral"],
        "regressed": classifications["regressed"],
        "severe": sum(row.severe for row in rows),
        "added": classifications["added"],
        "removed": classifications["removed"],
        "outcome_changed": classifications["outcome_changed"],
        "not_comparable": classifications["not_comparable"],
        "unchanged_outcome": classifications["unchanged_outcome"],
        "geometric_mean_speedup": geometric_mean_speedup(rows),
    }


def _outcomes(cases: Sequence[BenchmarkCase]) -> dict[str, int]:
    counts = Counter(case.outcome for case in cases)
    return {name: counts[name] for name in ("passed", "skipped", "failed", "errored")}


def _pair_document(artifact: PairArtifact) -> dict[str, object]:
    wheels: dict[str, object] = {}
    if artifact.provenance is not None:
        for name in ("companion", "tilelang", "tileops"):
            wheels[name] = artifact.provenance[name]
    return {
        "state": artifact.status["state"],
        "phase": artifact.status["phase"],
        "reason": artifact.status["reason"],
        "tileops_sha": artifact.status["tileops_sha"],
        "tilelang_sha": artifact.status["tilelang_sha"],
        "payload_sha256": artifact.status["payload_sha256"],
        "outcomes": _outcomes(artifact.cases),
        "wheels": wheels,
        "service_artifact": artifact.service_record,
    }


def build_comparison_document(
    resolved: Mapping[str, object],
    baseline: PairArtifact,
    candidate: PairArtifact,
    rows: Sequence[ComparisonRow],
    *,
    run_url: str,
    artifact_url: str,
) -> dict[str, object]:
    runtime._validate_resolved(dict(resolved))
    if baseline.pair != "baseline" or candidate.pair != "candidate":
        raise ReportError("comparison pair order is invalid")
    if baseline.collection is not None and candidate.collection is not None:
        for key in (
            "payload_sha256",
            "manifest_sha256",
            "harness_sha256",
            "nodeids_sha256",
            "count",
            "nodeids",
        ):
            if baseline.collection[key] != candidate.collection[key]:
                raise ReportError(f"baseline/candidate collection {key} mismatch")
    return {
        "schema_version": 1,
        "trigger_comment_id": resolved["trigger_comment_id"],
        "metadata": {
            "run_url": run_url,
            "artifact_url": artifact_url,
            "tileops": resolved["tileops"],
            "tilelang": resolved["tilelang"],
        },
        "baseline": _pair_document(baseline),
        "candidate": _pair_document(candidate),
        "summary": summarize(rows),
        "rows": [row_document(row) for row in rows],
    }


def comparison_json_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _markdown(value: object) -> str:
    text = "" if value is None else str(value)
    text = html.escape(text, quote=False).replace("\\", "\\\\")
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def _latency(case: Mapping[str, object] | None) -> str:
    if not case or case.get("latency_ms") is None:
        return "-"
    return f"{float(case['latency_ms']):.6f}"


def _speedup(row: Mapping[str, object]) -> str:
    value = row.get("speedup")
    return "-" if value is None else f"{float(value):.4f}x"


def _wheel_rows(pair_name: str, pair: Mapping[str, object]) -> list[str]:
    output: list[str] = []
    wheels = pair.get("wheels", {})
    if isinstance(wheels, dict):
        for name in ("companion", "tilelang", "tileops"):
            wheel = wheels.get(name)
            if isinstance(wheel, dict):
                output.append(
                    f"| {_markdown(pair_name)} | {_markdown(name)} | "
                    f"{_markdown(wheel.get('filename'))} | "
                    f"`{_markdown(str(wheel.get('sha256', ''))[:12])}` |"
                )
    return output


def render_markdown(document: Mapping[str, object]) -> str:
    metadata = document.get("metadata", {})
    summary = document.get("summary", {})
    rows = document.get("rows", [])
    baseline = document.get("baseline", {})
    candidate = document.get("candidate", {})
    if not all(isinstance(value, dict) for value in (metadata, summary, baseline, candidate)):
        raise ReportError("comparison document metadata is invalid")
    if not isinstance(rows, list):
        raise ReportError("comparison document rows must be a list")
    tileops = metadata.get("tileops", {})
    tilelang = metadata.get("tilelang", {})
    lines = [
        "# Cross-Repository Performance",
        "",
        f"[Workflow run]({_markdown(metadata.get('run_url'))}) | "
        f"[Full artifact]({_markdown(metadata.get('artifact_url'))})",
        "",
        "## Revisions",
        "",
        "| Repository | Baseline | Candidate | Pull request |",
        "|---|---:|---:|---|",
    ]
    for repository in (tileops, tilelang):
        if isinstance(repository, dict):
            lines.append(
                f"| {_markdown(repository.get('repository'))} | "
                f"`{_markdown(str(repository.get('default_sha', ''))[:7])}` | "
                f"`{_markdown(str(repository.get('merge_sha', ''))[:7])}` | "
                f"[#{_markdown(repository.get('pr_number'))}]"
                f"({_markdown(repository.get('pr_url'))}) |"
            )
    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| Comparable | Improved | Neutral | Regressed | Severe | Added | Removed | "
            "Outcome changed | Geomean |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {summary.get('comparable', 0)} | {summary.get('improved', 0)} | "
            f"{summary.get('neutral', 0)} | {summary.get('regressed', 0)} | "
            f"{summary.get('severe', 0)} | {summary.get('added', 0)} | "
            f"{summary.get('removed', 0)} | {summary.get('outcome_changed', 0)} | "
            + (
                "- |"
                if summary.get("geometric_mean_speedup") is None
                else f"{float(summary['geometric_mean_speedup']):.4f}x |"
            ),
            "",
            "## Pair Outcomes",
            "",
            "| Pair | State | Passed | Skipped | Failed | Errored |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for pair_name, pair in (("Baseline", baseline), ("Candidate", candidate)):
        outcomes = pair.get("outcomes", {})
        if not isinstance(outcomes, dict):
            outcomes = {}
        lines.append(
            f"| {pair_name} | {_markdown(pair.get('state'))} | "
            f"{outcomes.get('passed', 0)} | {outcomes.get('skipped', 0)} | "
            f"{outcomes.get('failed', 0)} | {outcomes.get('errored', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Wheels",
            "",
            "| Pair | Distribution | Filename | SHA-256 |",
            "|---|---|---|---|",
            *_wheel_rows("Baseline", baseline),
            *_wheel_rows("Candidate", candidate),
        ]
    )
    comparable_rows = [row for row in rows if isinstance(row, dict) and row.get("speedup")]
    regressions = sorted(
        (row for row in comparable_rows if row.get("classification") == "regressed"),
        key=lambda row: float(row["speedup"]),
    )[:20]
    improvements = sorted(
        (row for row in comparable_rows if row.get("classification") == "improved"),
        key=lambda row: float(row["speedup"]),
        reverse=True,
    )[:20]
    for title, selected in (("Top Regressions", regressions), ("Top Improvements", improvements)):
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Testcase | Op | Baseline ms | Candidate ms | Speedup |",
                "|---|---|---:|---:|---:|",
            ]
        )
        if not selected:
            lines.append("| - | - | - | - | - |")
        for row in selected:
            lines.append(
                f"| {_markdown(row.get('identity'))} | {_markdown(row.get('op'))} | "
                f"{_latency(row.get('baseline'))} | {_latency(row.get('candidate'))} | "
                f"{_speedup(row)} |"
            )
    diagnostic_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("classification")
        in {"outcome_changed", "unchanged_outcome", "not_comparable", "added", "removed"}
    ][:50]
    lines.extend(
        [
            "",
            "## Diagnostics",
            "",
            "| Testcase | Op | Classification | Baseline | Candidate | Message |",
            "|---|---|---|---|---|---|",
        ]
    )
    if not diagnostic_rows:
        lines.append("| - | - | - | - | - | - |")
    for row in diagnostic_rows:
        baseline_case = row.get("baseline")
        candidate_case = row.get("candidate")
        baseline_case = baseline_case if isinstance(baseline_case, dict) else {}
        candidate_case = candidate_case if isinstance(candidate_case, dict) else {}
        message = (
            candidate_case.get("failure_message") or baseline_case.get("failure_message") or ""
        )
        lines.append(
            f"| {_markdown(row.get('identity'))} | {_markdown(row.get('op'))} | "
            f"{_markdown(row.get('classification'))} | {_markdown(baseline_case.get('outcome'))} | "
            f"{_markdown(candidate_case.get('outcome'))} | {_markdown(message)} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def bounded_comment(marker: str, body: str) -> str:
    prefix = marker + "\n"
    if len(prefix) > MAX_COMMENT_CHARS:
        raise ReportError("comment marker exceeds the comment size limit")
    if len(prefix) + len(body) <= MAX_COMMENT_CHARS:
        return prefix + body
    note = "\n\n_Report truncated; use the full report artifact._\n"
    budget = MAX_COMMENT_CHARS - len(prefix) - len(note)
    return prefix + body[:budget] + note


def render_comment(document: Mapping[str, object], *, trigger_comment_id: int) -> str:
    if trigger_comment_id <= 0:
        raise ReportError("trigger comment ID must be positive")
    marker = f"<!-- cross-repo-perf:trigger-comment-{trigger_comment_id} -->"
    return bounded_comment(marker, render_markdown(document))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_service_evidence(path: Path) -> dict[str, dict[str, object]]:
    value = runtime._expect_dict(runtime._read_json(path, 256 * 1024), "service evidence")
    runtime._expect_exact_keys(
        value, {"schema_version", "resolution", "baseline", "candidate"}, "service evidence"
    )
    if value["schema_version"] != 1:
        raise ReportError("service evidence schema_version must be 1")
    output: dict[str, dict[str, object]] = {}
    for name in ("resolution", "baseline", "candidate"):
        entry = runtime._expect_dict(value[name], f"service evidence {name}")
        runtime._expect_exact_keys(entry, {"record", "expected"}, f"service evidence {name}")
        record = runtime._expect_dict(entry["record"], f"service evidence {name} record")
        expected = runtime._expect_dict(entry["expected"], f"service evidence {name} expected")
        runtime.validate_service_artifact(record, expected)
        output[name] = {"record": record, "expected": expected}
    return output


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved", type=Path, required=True)
    parser.add_argument("--service-evidence", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--output-comment", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    resolved = runtime.load_bounded_json(args.resolved, "resolved")
    if resolved["disposition"] != "run":
        raise ReportError("report comparison requires a run disposition")
    service = _load_service_evidence(args.service_evidence)
    baseline = validate_pair_artifact(
        args.baseline_dir,
        resolved,
        "baseline",
        service["baseline"]["record"],
        service["baseline"]["expected"],
    )
    candidate = validate_pair_artifact(
        args.candidate_dir,
        resolved,
        "candidate",
        service["candidate"]["record"],
        service["candidate"]["expected"],
    )
    rows = compare(baseline.cases, candidate.cases)
    document = build_comparison_document(
        resolved,
        baseline,
        candidate,
        rows,
        run_url=args.run_url,
        artifact_url=args.artifact_url,
    )
    markdown = render_markdown(document)
    comment = render_comment(document, trigger_comment_id=int(resolved["trigger_comment_id"]))
    _atomic_write(args.output_json, comparison_json_bytes(document))
    _atomic_write(args.output_markdown, markdown.encode("utf-8"))
    _atomic_write(args.output_comment, comment.encode("utf-8"))
    if geometric_mean_speedup(rows) is None:
        raise ReportError("no common testcase has comparable latency data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
