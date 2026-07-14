from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/cross-repo-perf.yml"
CHECKOUT = "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
UPLOAD = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
DOWNLOAD = "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
GITHUB_SCRIPT = "actions/github-script@f28e40c7f34bde8b3046d885e986cb6290c5673b"


def _workflow() -> dict[str, object]:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    return job["steps"]


def _step(job: dict[str, object], step_id: str) -> dict[str, object]:
    return next(step for step in _steps(job) if step.get("id") == step_id)


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_has_exact_trigger_jobs_runners_and_concurrency() -> None:
    workflow = _workflow()
    assert workflow["on"] == {"issue_comment": {"types": ["created"]}}
    assert set(workflow["jobs"]) == {"resolve", "benchmark", "report"}
    jobs = workflow["jobs"]
    assert jobs["resolve"]["runs-on"] == "ubuntu-latest"
    assert jobs["benchmark"]["runs-on"] == "tileops-metax-runner"
    assert jobs["report"]["runs-on"] == "ubuntu-latest"
    assert "concurrency" not in workflow
    benchmark_concurrency = jobs["benchmark"]["concurrency"]
    assert "github.event.issue.number" in benchmark_concurrency["group"]
    assert benchmark_concurrency["cancel-in-progress"] == "true"


def test_workflow_permissions_conditions_and_checkout_credentials_are_minimal() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    assert jobs["benchmark"]["permissions"] == {"contents": "read"}
    assert jobs["report"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "write",
    }
    assert jobs["resolve"]["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert "disposition == 'run'" in jobs["benchmark"]["if"]
    report_condition = jobs["report"]["if"]
    assert "always()" in report_condition
    assert "needs.resolve.result == 'success'" in report_condition
    assert "disposition != 'ignore'" in report_condition
    assert _workflow_text().count("issues: write") == 1
    assert "secrets." not in str(jobs["benchmark"])

    checkout_steps = [
        step for job in jobs.values() for step in _steps(job) if step.get("uses") == CHECKOUT
    ]
    assert checkout_steps
    assert all(
        step.get("with", {}).get("persist-credentials") == "false" for step in checkout_steps
    )


def test_benchmark_bootstraps_one_pinned_toolchain_before_runtime_and_candidate() -> None:
    benchmark = _workflow()["jobs"]["benchmark"]
    steps = _steps(benchmark)
    combined = "\n".join(str(step) for step in steps)
    assert combined.count("bootstrap-toolchain") == 1
    assert "--lock" in combined and "--root" in combined and "--provenance" in combined
    assert "resolve-maca-environment" in combined
    assert "materialize-wheelhouse" in combined
    bootstrap_index = next(
        index for index, step in enumerate(steps) if "bootstrap-toolchain" in str(step)
    )
    runtime_index = next(
        index for index, step in enumerate(steps) if "materialize-wheelhouse" in str(step)
    )
    payload_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "trusted_payload"
    )
    candidate_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "candidate_tileops"
    )
    baseline_index = next(
        index for index, step in enumerate(steps) if step.get("id") == "baseline_pair"
    )
    assert bootstrap_index < payload_index < runtime_index < baseline_index < candidate_index
    assert "build-payload" in str(steps[payload_index])
    assert "uv python" not in combined
    assert "/opt/conda" not in combined
    post_bootstrap = "\n".join(str(step) for step in steps[bootstrap_index + 1 :])
    assert "python3 scripts/cross_repo_perf_runtime.py" not in post_bootstrap
    assert "PINNED_UV" in combined
    assert "PINNED_PYTHON" in combined
    assert "PINNED_PYTHON_INCLUDE" in combined


def test_baseline_upload_precedes_and_gates_candidate_execution() -> None:
    benchmark = _workflow()["jobs"]["benchmark"]
    steps = _steps(benchmark)
    indexes = {step.get("id"): index for index, step in enumerate(steps) if step.get("id")}
    assert indexes["baseline_pair"] < indexes["baseline_upload"]
    assert indexes["baseline_upload"] < indexes["candidate_tileops"]
    assert indexes["candidate_tileops"] < indexes["candidate_pair"]
    assert indexes["candidate_pair"] < indexes["candidate_upload"]
    for step_id in ("candidate_tileops", "candidate_tilelang", "candidate_pair"):
        condition = _step(benchmark, step_id)["if"]
        assert "always()" in condition
        assert "steps.baseline_upload.outcome == 'success'" in condition
    fallback = _step(benchmark, "candidate_suppressed")
    assert "steps.baseline_upload.outcome != 'success'" in fallback["if"]
    assert "baseline_artifact_unavailable" in fallback["run"]
    assert "PINNED_PYTHON" not in str(fallback)
    assert "/usr/bin/python3" in fallback["run"]


def test_artifact_uploads_are_attempt_specific_and_export_service_identity() -> None:
    jobs = _workflow()["jobs"]
    uploads: list[tuple[str, dict[str, object]]] = []
    for job_name, job in jobs.items():
        for step in _steps(job):
            if step.get("uses") == UPLOAD:
                uploads.append((job_name, step))
                name = step["with"]["name"]
                assert "github.run_id" in name
                assert "github.run_attempt" in name
                assert step["with"]["if-no-files-found"] == "error"
    assert {step.get("id") for _, step in uploads} == {
        "resolution_upload",
        "baseline_upload",
        "candidate_upload",
        "report_upload",
    }

    for job_name, ids in {
        "resolve": ("resolution",),
        "benchmark": ("baseline", "candidate"),
        "report": ("report",),
    }.items():
        outputs = jobs[job_name]["outputs"]
        for prefix in ids:
            assert f"{prefix}_artifact_id" in outputs
            assert f"{prefix}_artifact_url" in outputs
            assert f"{prefix}_artifact_digest" in outputs
            assert f"{prefix}_artifact_name" in outputs


def test_report_queries_and_downloads_exact_current_attempt_artifact_ids() -> None:
    report = _workflow()["jobs"]["report"]
    combined = "\n".join(str(step) for step in _steps(report))
    assert "getArtifact" in combined
    assert "getWorkflowRun" in combined
    assert "repos.get" in combined
    for token in (
        "artifact.id",
        "artifact.name",
        "artifact.digest",
        "artifact.expired",
        "workflow_run.id",
        "run_attempt",
        "github.repository",
    ):
        assert token in combined
    assert "service-evidence.json" in combined
    assert "needs.resolve.outputs.run_id" in combined
    assert "needs.resolve.outputs.run_attempt" in combined

    downloads = [step for step in _steps(report) if step.get("uses") == DOWNLOAD]
    assert downloads
    for step in downloads:
        options = step["with"]
        assert "artifact-ids" in options
        assert "name" not in options
        assert "pattern" not in options
    assert "listWorkflowRunArtifacts" not in combined


def test_all_external_actions_are_pinned_to_reviewed_commits() -> None:
    allowed = {CHECKOUT, UPLOAD, DOWNLOAD, GITHUB_SCRIPT}
    uses = {
        step["uses"]
        for job in _workflow()["jobs"].values()
        for step in _steps(job)
        if "uses" in step
    }
    assert uses == allowed


def test_rendered_diagnostics_survive_a_nonzero_comparison_exit() -> None:
    report = _workflow()["jobs"]["report"]
    comparison = _step(report, "comparison")
    fallback = _step(report, "fallback")
    assert "rendered=true" in comparison["run"]
    assert 'exit "$status"' in comparison["run"]
    assert "steps.comparison.outputs.rendered != 'true'" in fallback["if"]
    assert "steps.comparison.outputs.rendered == 'true'" in _step(report, "publish")["if"]
