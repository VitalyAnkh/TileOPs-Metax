# Cross-Repository Performance Regression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure comment-triggered three-job GitHub Actions workflow that builds paired TileOps/TileLang revisions, runs the complete trusted `benchmarks/ops` suite for baseline and candidate on MetaX, compares JUnit performance data, and posts an idempotent PR report.

**Architecture:** A GitHub-hosted resolver validates the command, maintainer permissions, and immutable PR/default-branch SHAs. A read-only GPU job bootstraps a pinned Python 3.10 runtime, runs baseline then candidate from fresh wheels with a fixed trusted benchmark payload, and uploads integrity-checked artifacts. A GitHub-hosted reporter validates artifacts, compares JUnit data, and is the only job allowed to write a PR comment.

**Tech Stack:** GitHub Actions YAML, Node.js CommonJS plus `node:test`, Python 3.10 standard library and pytest, Bash, uv, JUnit XML, PyYAML/actionlint for workflow validation.

---

## File Map

- Create `.github/workflows/cross-repo-perf.yml`: event filtering, three jobs, permissions, ordering, artifact transfer, and report comment publication.
- Create `scripts/ci/cross_repo_perf_resolve.cjs`: strict comment parser, permission/ref resolver, output serialization, and bot-comment upsert helper.
- Create `scripts/ci/cross_repo_perf_resolve.test.cjs`: Node built-in tests for resolver and comment behavior.
- Create `scripts/ci/cross_repo_perf_toolchain.lock`: pinned uv and managed CPython 3.10 artifact metadata and hashes.
- Create `scripts/ci/cross_repo_perf_py310.in`: human-maintained direct Python 3.10 runtime requirements.
- Create `scripts/ci/cross_repo_perf_py310.lock`: generated, fully hashed runtime closure.
- Create `scripts/cross_repo_perf_runtime.py`: lock validation, toolchain/runtime bootstrap helpers, cache-root construction, wheel audits, provenance, and artifact-manifest validation.
- Create `scripts/ci/cross_repo_perf_harness.py`: early pytest plugin for trusted manifest loading, import audits, collection fingerprinting, and result status generation.
- Create `scripts/ci/run_cross_repo_perf_pair.sh`: minimal trusted shell orchestration for one baseline or candidate pair.
- Create `scripts/cross_repo_perf_report.py`: bounded JUnit/JSON parsing, comparison, Markdown/JSON rendering, and final success decision.
- Create `tests/test_cross_repo_perf_runtime.py`: runtime/cache/wheel/artifact tests.
- Create `tests/test_cross_repo_perf_harness.py`: trusted manifest and collection fingerprint tests.
- Create `tests/test_cross_repo_perf_report.py`: JUnit comparison/reporting tests.
- Create `tests/test_cross_repo_perf_workflow.py`: static workflow contract tests.

Do not modify `benchmarks/`, `workloads/`, `tileops/manifest/`, `tileops/ops/`, or `tileops/kernels/` for this feature.

### Task 0: Worktree And Evidence Safety

**Files:**
- No repository files are changed in this task.

- [ ] **Step 1: Assert the isolated feature worktree**

Run:

```bash
test "$(git branch --show-current)" = cross-repo-perf
test "$(git rev-parse --show-toplevel)" = \
  /local/qzheng/projects/dev/ai/tileops-metax-cross-repo-perf-20260714
git status --short
```

Expected: only the approved spec/plan files are present before implementation.
Stop if an unexpected tracked or untracked file appears; do not clean or reset it.

- [ ] **Step 2: Keep validation evidence outside the worktree**

Use a local-only directory such as
`/local/qzheng/projects/dev/ai/.cross-repo-perf-validation/<run-id>/` for wheel,
remote, JUnit, log, and report artifacts. Do not add a broad `.gitignore` entry
that could hide repository output. Every later commit uses explicit path
allowlists; never use `git add .` or `git add -A`.

### Task 1: Resolver And Comment Contract

**Files:**
- Create: `scripts/ci/cross_repo_perf_resolve.cjs`
- Create: `scripts/ci/cross_repo_perf_resolve.test.cjs`

- [ ] **Step 1: Write strict parser tests**

Cover the exact accepted body, optional trailing slash, surrounding prose, duplicate commands, query/fragment, wrong owner/repo, zero/negative/non-numeric PR numbers, CRLF, and whitespace-only prefixes.

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseCommand } = require("./cross_repo_perf_resolve.cjs");

test("accepts only the exact TileLang PR command", () => {
  assert.deepEqual(
    parseCommand("@cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/90"),
    { tilelangPrNumber: 90 },
  );
});

test("rejects surrounding prose", () => {
  assert.equal(
    parseCommand("please run @cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/90"),
    null,
  );
});
```

- [ ] **Step 2: Run parser tests and verify they fail**

Run: `node --test scripts/ci/cross_repo_perf_resolve.test.cjs`

Expected: FAIL because the resolver module does not exist.

- [ ] **Step 3: Implement the pure parser and permission helpers**

Export:

```javascript
module.exports = {
  parseCommand,
  permissionAllowed,
  resolveRequest,
  upsertBotComment,
};
```

Use one anchored regular expression over `body.trim()`. `permissionAllowed`
accepts only `write`, `maintain`, or `admin`.

- [ ] **Step 4: Add mocked resolver tests**

Mock the Octokit methods used to verify:

- current issue is an open TileOps PR;
- trigger actor, TileOps PR author, and TileLang PR author permissions;
- both heads are same-repository branches;
- both PRs target each repository's API-reported default branch;
- bounded retry while `mergeable === null`;
- rejection on conflicts, stale base SHA, closed PR, or missing merge SHA;
- output contains immutable baseline/head/base/merge SHAs and URLs;
- dispositions are exactly `ignore`, `reject`, and `run`.

- [ ] **Step 5: Add bot-comment upsert tests**

Verify pagination, hidden marker matching, update only for
`github-actions[bot]`, ignore a user-authored forged marker, create when no bot
comment exists, and preserve a maximum 60,000-character body.

- [ ] **Step 6: Run Node tests**

Run: `node --test scripts/ci/cross_repo_perf_resolve.test.cjs`

Expected: all tests PASS.

- [ ] **Step 7: Commit resolver**

```bash
git add scripts/ci/cross_repo_perf_resolve.cjs scripts/ci/cross_repo_perf_resolve.test.cjs
git commit -m "ci: add cross-repo perf resolver"
```

### Task 2: Python 3.10 Toolchain, Runtime, And Artifact Utilities

**Files:**
- Create: `scripts/ci/cross_repo_perf_toolchain.lock`
- Create: `scripts/ci/cross_repo_perf_py310.in`
- Create: `scripts/ci/cross_repo_perf_py310.lock`
- Create: `scripts/cross_repo_perf_runtime.py`
- Create: `tests/test_cross_repo_perf_runtime.py`

- [ ] **Step 1: Create the worktree uv test environment**

Run:

```bash
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python pytest pytest-timeout pyyaml packaging
```

Expected: `.venv/bin/python -V` reports Python 3.10.x.

- [ ] **Step 2: Write all runtime behavior tests before implementation**

Test that the runtime module:

- rejects a missing field, non-HTTPS URL, malformed SHA-256, unexpected Python
  version, incompatible wheel tag, sdist, or duplicate normalized distribution;
- verifies a file hash without loading the full file into memory;
- validates all transitive lock entries contain hashes;
- bootstraps only the locked uv/CPython archives into an empty root, rejecting
  size/hash mismatch, redirects outside the approved hosts, archive traversal,
  symlink/hardlink/device entries, wrong executable versions, missing
  `Python.h`, failed C-extension compile/import, or any fallback to an
  unqualified/default Python;
- emits deterministic JSON with sorted keys;
- replaces every inherited cache/home/temp path, rejects symlink components, and
  keeps `HOME`, `TMPDIR`, XDG, uv, pip, ccache, TileLang, Triton, Torch, CUDA,
  Python bytecode, and MACA-specific cache roots under the empty pair root;
- audits fixture wheels and rejects wrong distributions, duplicate selections,
  `.pth`, `sitecustomize.py`, `usercustomize.py`, and `pytest11` entry points;
- writes and validates deterministic internal artifact manifests, rejecting path
  traversal, symlinks, extra/missing files, size/hash mismatch, and wrong
  run-attempt identity;
- validates an Actions service record containing exact artifact name, numeric
  artifact ID, `sha256:<64 hex>` service digest, workflow run ID, run attempt,
  non-expired state, and repository identity;
- rejects service digest/ID/name/run-attempt mismatches before any artifact is
  accepted;
- bounded-loads `resolved.json`, `status.json`, `provenance.json`, and
  `collection.json` at a maximum of 2 MiB each, with fixed required keys, type
  checks, length/count limits, enum checks, no duplicate JSON object keys, and
  no unexpected keys.

```python
def test_verify_sha256_rejects_modified_file(tmp_path):
    artifact = tmp_path / "uv"
    artifact.write_bytes(b"first")
    expected = sha256(b"first").hexdigest()
    runtime.verify_sha256(artifact, expected)
    artifact.write_bytes(b"second")
    with pytest.raises(runtime.EvidenceError, match="sha256"):
        runtime.verify_sha256(artifact, expected)
```

- [ ] **Step 3: Run focused tests and verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_runtime.py`

Expected: FAIL because `scripts/cross_repo_perf_runtime.py` does not exist.

- [ ] **Step 4: Write the immutable lock contracts**

`cross_repo_perf_toolchain.lock` is deterministic JSON with schema version 1
and exactly two records:

```json
{
  "schema_version": 1,
  "platform": "x86_64-unknown-linux-gnu",
  "uv": {
    "version": "0.11.16",
    "url": "https://github.com/astral-sh/uv/releases/download/0.11.16/uv-x86_64-unknown-linux-gnu.tar.gz",
    "sha256": "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131",
    "size": 24014155,
    "executable": "uv-x86_64-unknown-linux-gnu/uv"
  },
  "python": {
    "version": "3.10.18",
    "url": "https://releases.astral.sh/github/python-build-standalone/releases/download/20251007/cpython-3.10.18%2B20251007-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
    "sha256": "7b1d02e28b0d36c4b0de044aaf8099cb0395ac3d6826c96ddd158241fcdc6f06",
    "size": 28836654,
    "executable": "python/bin/python3.10",
    "include": "python/include/python3.10"
  }
}
```

`cross_repo_perf_py310.in` pins direct runtime requirements, including
`torch==2.8.0+metax3.7.1.3`, `triton==3.5.1`,
`apache-tvm-ffi==0.1.11`, exact build tooling, `pytest`, `pytest-timeout`,
PyYAML, einops, and benchmark collection dependencies. Registry resolution is
used only while regenerating the lock; runtime never performs multi-index
package resolution. The MetaX torch and validated Triton wheels are direct URL
requirements with exact artifacts:

```text
torch @ https://repos.metax-tech.com/r/maca-pypi/packages/torch/2.8.0+metax3.7.1.3/torch-2.8.0+metax3.7.1.3-cp310-cp310-linux_x86_64.whl#sha256=81775cdc54b870f9c18c01266cdf1bf2f462a22e9609b6f7b35fd33aef1292da
triton @ https://files.pythonhosted.org/packages/fd/6e/676ab5019b4dde8b9b7bab71245102fc02778ef3df48218b298686b9ffd6/triton-3.5.1-cp310-cp310-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl#sha256=5fc53d849f879911ea13f4a877243afc513187bc7ee92d1f2c0f1ba3169e3c94
```

The exact sizes are `368585264` and `170320692` bytes respectively. The files
contain no credentials. `cross_repo_perf_py310.lock` is deterministic JSON with
schema version, Python/platform tags, and one entry per normalized
distribution. Every entry records exact version, filename, HTTPS source URL,
approved source host, size, and SHA-256. Torch must use the exact MetaX host/path
above; Triton must use the exact validated PyPI artifact above; every other
artifact must be an exact `files.pythonhosted.org` wheel selected from the
regenerated resolution. Sdists, index URLs at runtime, local/VCS paths,
duplicate names, and host/source-policy mismatches are invalid.

- [ ] **Step 5: Implement the runtime utility and bounded schemas**

Provide pure/testable functions and a small argparse CLI:

```python
class EvidenceError(RuntimeError): ...

def verify_sha256(path: Path, expected: str) -> None: ...
def load_toolchain_lock(path: Path) -> ToolchainLock: ...
def validate_requirements_lock(path: Path) -> None: ...
def bootstrap_toolchain(lock: Path, root: Path, provenance: Path) -> Toolchain: ...
def make_pair_environment(pair_root: Path, inherited: Mapping[str, str]) -> dict[str, str]: ...
def audit_wheel(path: Path, expected_name: str) -> WheelAudit: ...
def write_artifact_manifest(root: Path, output: Path, identity: Mapping[str, str]) -> None: ...
def validate_artifact_manifest(root: Path, manifest: Path, expected: Mapping[str, str]) -> None: ...
def validate_service_artifact(record: Mapping[str, object], expected: Mapping[str, object]) -> None: ...
def load_bounded_json(path: Path, schema: str, max_bytes: int = 2 * 1024 * 1024) -> dict[str, object]: ...
```

Schema version 1 uses these exact top-level contracts (nested objects also
reject unknown keys):

- `resolved`: `schema_version`, `disposition`, `reason`, `run_id`,
  `run_attempt`, `trigger_comment_id`, `trigger_actor`, `harness_sha256`,
  `tileops`, `tilelang`; each repository object contains only `repository`,
  `pr_number`, `pr_url`, `author`, `default_branch`, `default_sha`, `base_ref`,
  `base_sha`, `head_ref`, `head_sha`, and `merge_sha`;
- `status`: `schema_version`, `pair`, `state`, `phase`, `exit_code`, `reason`,
  `started_at`, `finished_at`, `run_id`, `run_attempt`, `tileops_sha`,
  `tilelang_sha`, and `payload_sha256`;
- `provenance`: `schema_version`, `pair`, `run_id`, `run_attempt`, `python`,
  `runtime_lock_sha256`, `wheelhouse_manifest_sha256`, `payload_sha256`,
  `manifest_sha256`, `harness_sha256`, `companion`, `tilelang`, and `tileops`;
  each wheel record contains only distribution, version, filename, SHA-256,
  source SHA, and resolved import path;
- `collection`: `schema_version`, `pair`, `run_id`, `run_attempt`,
  `payload_sha256`, `manifest_sha256`, `harness_sha256`, `nodeids_sha256`,
  `count`, and `nodeids`.

Strings are at most 4,096 UTF-8 bytes (node IDs 16,384), nesting depth is at
most 8, collection contains at most 10,000 unique node IDs, and no other array
exceeds 256 entries. Enums are fixed to the pair/state/phase/disposition values
used by the runner. The Actions service record schema contains only
`schema_version`, `repository`, `run_id`, `run_attempt`, `name`, `artifact_id`,
`artifact_digest`, and `expired`.

Use `zipfile` plus `email.parser` for wheel metadata. Reject `.pth`,
`sitecustomize.py`, `usercustomize.py`, and `pytest11` entry points.

The fixed bootstrap CLI is:

```bash
python3 scripts/cross_repo_perf_runtime.py bootstrap-toolchain \
  --lock scripts/ci/cross_repo_perf_toolchain.lock \
  --root "$RUNNER_TEMP/cross-repo-perf-toolchain" \
  --provenance "$RUNNER_TEMP/cross-repo-perf-toolchain.json"
```

The invoking `python3` is only a trusted stdlib controller. The command itself
downloads from the two lock URLs without executing them, enforces final URL
host/path, size, and SHA-256, safely extracts regular files/directories only,
checks exact uv/Python versions, verifies `Python.h`, compiles and imports a
minimal extension with the pinned interpreter/include directory, and writes
deterministic provenance containing archive and executable hashes. It prints
the absolute pinned uv, Python, and include paths as JSON. All later commands
must consume those returned absolute paths; falling back to `python`,
`python3`, runner `/opt/conda`, or `uv python install/find` is an error.

- [ ] **Step 6: Generate the source-locked closure and freeze the wheelhouse**

Regenerate in two explicit stages. First use the pinned uv binary to resolve a
temporary hashed requirements file for CPython 3.10/manylinux 2.28 with PyPI as
the only registry; torch and Triton remain direct URLs from the input file:

```bash
uv pip compile scripts/ci/cross_repo_perf_py310.in \
  --python-version 3.10 \
  --python-platform x86_64-manylinux_2_28 \
  --default-index https://pypi.org/simple \
  --generate-hashes --only-binary=:all: --no-emit-index-url \
  --output-file "$TMPDIR/cross_repo_perf_py310.resolved.txt"
python3 scripts/cross_repo_perf_runtime.py normalize-runtime-lock \
  --requirements "$TMPDIR/cross_repo_perf_py310.resolved.txt" \
  --output scripts/ci/cross_repo_perf_py310.lock
```

`normalize-runtime-lock` resolves the exact PyPI wheel URL/size for each hash,
applies the per-package source policy, and emits deterministic JSON. Tests feed
a forged public-index `torch` candidate and a private-index replacement for
another package and require rejection. At runtime, populate one shared
wheelhouse by downloading each JSON lock entry directly; no pip/uv index lookup
is allowed:

```bash
"$PINNED_PYTHON" scripts/cross_repo_perf_runtime.py materialize-wheelhouse \
  --lock scripts/ci/cross_repo_perf_py310.lock --wheelhouse "$WHEELHOUSE"
chmod -R a-w "$WHEELHOUSE"
"$PINNED_PYTHON" scripts/cross_repo_perf_runtime.py verify-wheelhouse \
  --lock scripts/ci/cross_repo_perf_py310.lock --wheelhouse "$WHEELHOUSE"
```

`materialize-wheelhouse` writes a source-URL/hash/size/tag manifest and rejects
symlinks, extra files, sdists, or missing lock artifacts. After `chmod`,
`verify-wheelhouse` also verifies every directory/file is non-writable.
Immediately before *each* baseline and
candidate environment install, run `verify-wheelhouse` against that manifest
and lock, emit a temporary hash-locked requirements file from the JSON lock,
then install offline only:

```bash
"$PINNED_PYTHON" scripts/cross_repo_perf_runtime.py emit-requirements \
  --lock scripts/ci/cross_repo_perf_py310.lock --output "$PAIR_REQUIREMENTS"
"$PINNED_UV" pip sync --python "$PAIR_VENV/bin/python" --require-hashes \
  --no-index --find-links "$WHEELHOUSE" "$PAIR_REQUIREMENTS"
```

Tests mutate a wheel, mode bit, manifest, and lock between pair installs and
require the second verification to fail before installation.

- [ ] **Step 7: Test the local toolchain/bootstrap path**

Use a small fixture archive in unit tests, then run the real trusted bootstrap
locally through the CLI. Compile/import a minimal C extension against the pinned
Python headers and remove it afterward. Assert `Python.h` is under the verified
include directory and the imported extension's path is under the temporary
bootstrap root.

- [ ] **Step 8: Run focused tests**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_runtime.py`

Expected: all tests PASS.

- [ ] **Step 9: Commit runtime utilities and locks**

```bash
git add scripts/ci/cross_repo_perf_toolchain.lock \
  scripts/ci/cross_repo_perf_py310.in \
  scripts/ci/cross_repo_perf_py310.lock \
  scripts/cross_repo_perf_runtime.py \
  tests/test_cross_repo_perf_runtime.py
git commit -m "ci: add isolated Python runtime utilities"
```

### Task 3: Trusted Pytest Harness

**Files:**
- Create: `scripts/ci/cross_repo_perf_harness.py`
- Create: `tests/test_cross_repo_perf_harness.py`

- [ ] **Step 1: Write failing trusted-payload boundary tests**

Construct a fake baseline checkout and assert the payload builder copies only:

```text
benchmarks/**
workloads/**
tests/__init__.py
tests/test_base.py
tests/ops/__init__.py
tests/ops/test_mamba.py
tileops/manifest/*.yaml          -> trusted_manifest/*.yaml
pyproject.toml                   -> pyproject.toml
scripts/ci/cross_repo_perf_harness.py -> cross_repo_perf_harness.py
```

Reject symlinks, device files, path traversal, missing required paths, and any
other copied file. Walk the completed neutral root and fail if it contains an
importable top-level `tileops` or `tilelang` package/module, `.pth`,
`sitecustomize.py`, `usercustomize.py`, or repository `.git` metadata. Assert
the harness SHA-256 equals the trusted value emitted by the resolver and that
the payload tree hash changes for any allowed-file mutation.

- [ ] **Step 2: Write failing manifest override tests**

Create a fake installed `tileops.manifest` module and two manifest directories.
Assert the harness installs the baseline directory before collection, clears
the existing `load_manifest` cache, and causes `load_workloads()` to return the
baseline values rather than candidate values.

- [ ] **Step 3: Write failing digest/fingerprint tests**

Test canonical tree hashing, payload hash stability, sorted node-ID output,
duplicate node IDs, import-path audit, and different node lists producing
different fingerprints.

- [ ] **Step 4: Run tests and verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_harness.py`

Expected: FAIL because the harness does not exist.

- [ ] **Step 5: Implement payload construction and the early pytest plugin**

The module must expose pure helpers plus hooks:

```python
def pytest_configure(config): ...
def pytest_collection_finish(session): ...
def pytest_sessionfinish(session, exitstatus): ...
```

Read trusted paths and output paths from required environment variables. Patch
`tileops.manifest.manifest_files` and clear `load_manifest.cache_clear()` in
`pytest_configure`, before repository conftest files import benchmark modules.
Write `collection.json` atomically with payload, manifest, harness, and node-ID
digests using the Task 2 bounded schema. Payload construction always reads from
the immutable TileOps baseline checkout, never from candidate source.

- [ ] **Step 6: Add subprocess integration tests**

Run a tiny neutral pytest payload with:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  -p cross_repo_perf_harness -p pytest_timeout -q payload/tests
```

Install a fixture distribution declaring `pytest11`; prove it is not loaded.
Prove the harness is loaded before `conftest.py` and candidate manifest data is
not observed. Immediately before spawning pytest, recompute the copied
`cross_repo_perf_harness.py` SHA-256 and compare it with both `resolved.json`
and the payload manifest; mutate it after payload construction and prove pytest
is never invoked.

- [ ] **Step 7: Run tests**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_harness.py`

Expected: all tests PASS.

- [ ] **Step 8: Commit harness**

```bash
git add scripts/ci/cross_repo_perf_harness.py tests/test_cross_repo_perf_harness.py
git commit -m "ci: add trusted benchmark harness"
```

### Task 4: JUnit Comparison And Report Renderer

**Files:**
- Create: `scripts/cross_repo_perf_report.py`
- Create: `tests/test_cross_repo_perf_report.py`

- [ ] **Step 1: Write failing bounded-parser tests**

Use inline XML fixtures to cover passed, skipped, failure, error, missing
latency, invalid/NaN/infinite/non-positive latency, duplicate testcase identity,
DOCTYPE/entity rejection, too many testcases/properties, and oversized values.
Add pair-artifact fixtures that exercise the Task 2 strict 2 MiB schemas for
`status.json`, `provenance.json`, and `collection.json`; reject unknown keys,
wrong types/enums, duplicate JSON keys, oversized arrays/strings, identity
mismatch, and a service artifact record whose ID or digest differs from the
trusted workflow output/API response.

- [ ] **Step 2: Write failing comparison tests**

Verify exact thresholds at 1.05, 0.95, and 0.90; severe as a regression subset;
added/removed and outcome transitions; and unweighted geometric mean.

```python
def test_speedup_thresholds():
    rows = compare(
        baseline=[case("a", 10.0), case("b", 10.0)],
        candidate=[case("a", 10.0 / 1.05), case("b", 10.0 / 0.90)],
    )
    assert rows[0].classification == "improved"
    assert rows[1].classification == "regressed"
    assert rows[1].severe is False
```

- [ ] **Step 3: Write failing Markdown/JSON tests**

Test Markdown escaping, bounded failure messages, top regression/improvement
ordering, 60,000-character comment truncation, hidden marker, exact SHAs/wheel
hashes, artifact/run links, and deterministic `comparison.json`.

- [ ] **Step 4: Run tests and verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_report.py`

Expected: FAIL because the reporter does not exist.

- [ ] **Step 5: Implement parser, comparator, and renderer**

Use `xml.etree.ElementTree` only after byte-size and `DOCTYPE`/`ENTITY` checks.
Treat `<failure>` and `<error>` separately. Join on `classname::name`. Parse
only expected JUnit properties and reject duplicates/limit violations. Before
opening JUnit XML, validate `resolved.json`, the exact Actions service records,
each internal artifact manifest, and the bounded status/provenance/collection
documents. A mismatch is infrastructure failure, never an informational row.

CLI contract:

```text
python scripts/cross_repo_perf_report.py \
  --resolved resolved.json \
  --baseline-dir artifacts/baseline \
  --candidate-dir artifacts/candidate \
  --output-markdown report.md \
  --output-comment comment.md \
  --output-json comparison.json
```

- [ ] **Step 6: Run reporter tests**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_report.py`

Expected: all tests PASS.

- [ ] **Step 7: Commit reporter**

```bash
git add scripts/cross_repo_perf_report.py tests/test_cross_repo_perf_report.py
git commit -m "ci: add cross-repo performance reporter"
```

### Task 5: Pair Build And Benchmark Runner

**Files:**
- Create: `scripts/ci/run_cross_repo_perf_pair.sh`
- Modify: `tests/test_cross_repo_perf_runtime.py`

- [ ] **Step 1: Write failing orchestration tests**

Use a temporary fake `PATH` with scripts for `git`, `uv`, `python`, and build
commands. Assert command order is:

1. pair-root/cache validation;
2. exact source SHA verification;
3. recursive TileLang submodule verification;
4. build companion/TileLang/TileOps wheels;
5. wheel audit and SHA recording;
6. install shared closure, companion, TileLang, TileOps;
7. import/provenance audit;
8. neutral payload creation and hash;
9. harness SHA revalidation immediately before pytest;
10. explicit trusted pytest command;
11. status and artifact manifest written on success or failure.

In the same red-test phase, inject failures at toolchain verification,
wheelhouse revalidation, build, install, import audit, payload construction,
harness rehash, collection, pytest, status writing, and manifest writing.
Verify nonzero exit status, no stale success state, retained bounded logs, and
that no later command executes. Mutate the shared wheelhouse between baseline
and candidate fixture runs and require candidate installation to stop.

- [ ] **Step 2: Run tests and verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_runtime.py -k pair_runner`

Expected: FAIL because the pair runner does not exist.

- [ ] **Step 3: Implement the minimal shell runner**

Requirements:

- `set -euo pipefail`;
- positional/flag inputs are fixed paths and immutable SHAs supplied by the
  workflow, never raw comment strings;
- all structured reads/writes go through `cross_repo_perf_runtime.py`;
- build outputs are discovered/audited, never hard-coded wheel names;
- immediately revalidate the read-only runtime wheelhouse and its manifest
  before installing either pair;
- `PYTHONPATH` cleared, `PYTHONNOUSERSITE=1`,
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and `TILELANG_DISABLE_CACHE=1`;
- trap always writes bounded `status.json` and `artifact-manifest.json`;
- build the exact Task 3 baseline-only payload, reject source-package leakage,
  rehash `cross_repo_perf_harness.py` on the line immediately before execution,
  and run from the neutral payload root with explicit harness and timeout
  plugins.

- [ ] **Step 4: Prove the failure-path tests now pass**

Run every injected failure from Step 1 and inspect command logs to confirm the
runner stops at the expected boundary and still emits bounded failure evidence.

- [ ] **Step 5: Run shell and Python tests**

Run:

```bash
bash -n scripts/ci/run_cross_repo_perf_pair.sh
.venv/bin/python -m pytest -q tests/test_cross_repo_perf_runtime.py
```

Expected: PASS.

- [ ] **Step 6: Commit pair runner**

```bash
git add scripts/ci/run_cross_repo_perf_pair.sh tests/test_cross_repo_perf_runtime.py
git commit -m "ci: add isolated pair benchmark runner"
```

### Task 6: Three-Job GitHub Actions Workflow

**Files:**
- Create: `.github/workflows/cross-repo-perf.yml`
- Create: `tests/test_cross_repo_perf_workflow.py`

- [ ] **Step 1: Write failing static workflow tests**

Parse YAML with a loader that preserves `on` as a string key. Assert:

- only `issue_comment.created` triggers the workflow;
- exactly `resolve`, `benchmark`, and `report` jobs exist;
- concurrency is per TileOps PR and cancels older runs;
- resolver/report run on `ubuntu-latest`, benchmark on
  `tileops-metax-runner`;
- benchmark has no write permissions or secrets and every checkout has
  `persist-credentials: false`;
- benchmark calls exactly one `bootstrap-toolchain --lock ... --root ...
  --provenance ...` command before runtime materialization, exports the returned
  absolute pinned uv/Python/include paths, and contains no `uv python
  install/find`, `/opt/conda`, or unqualified Python fallback in later steps;
- report alone has `issues: write`;
- benchmark condition is disposition `run`;
- report uses `always()` and skips only disposition `ignore`/resolver failure;
- baseline upload precedes and gates candidate checkout/run;
- all artifact names contain run ID and run attempt;
- every upload exports `artifact-id`, `artifact-url`, and `artifact-digest` as
  trusted job outputs;
- report grants only `contents: read`, `actions: read`, and `issues: write`,
  queries each artifact ID through the Actions REST API, and verifies exact
  name/run/repository/non-expired/service-digest identity;
- report downloads exact artifact IDs, not wildcard/latest names, and consumes
  only the current `run_id` plus `run_attempt` outputs.

- [ ] **Step 2: Run tests and verify they fail**

Run: `.venv/bin/python -m pytest -q tests/test_cross_repo_perf_workflow.py`

Expected: FAIL because the workflow does not exist.

- [ ] **Step 3: Implement the resolve job**

Checkout only trusted default-branch resolver code with
`persist-credentials: false`. Use `actions/github-script` to require the module,
write outputs plus `resolved.json`, and upload the run-attempt-specific resolver
artifact. Export its exact upload `artifact-id` and `artifact-digest` as job
outputs. Expected validation errors produce `reject`; unexpected exceptions
fail the job.

- [ ] **Step 4: Implement the benchmark job**

Use a 300-minute timeout and read-only permissions. Bootstrap toolchain/runtime
before candidate checkout by invoking the exact Task 2 `bootstrap-toolchain`
CLI, parse its JSON output, and use only the returned pinned absolute paths for
wheelhouse creation and both pair environments. Materialize all runtime wheels
from the source-locked JSON URLs without any package index access. Run baseline
pair, upload baseline artifact, and only
then conditionally checkout/run candidate. On baseline upload failure, create
the trusted skipped candidate status artifact. Upload candidate artifact and
export each upload action's exact ID/digest/name as benchmark job outputs before
finishing with an adjudication step. Candidate checkout/build/run steps must be
guarded by successful baseline artifact upload, not merely baseline pytest
completion.

- [ ] **Step 5: Implement the report job**

Use `needs: [resolve, benchmark]` plus `if: always()` and disposition gating.
Checkout reporting code from the immutable TileOps baseline SHA. Download exact
artifact IDs. For each ID, call the Actions REST API and compare its service
digest, name, workflow run ID, run attempt, repository, and expiry state with
trusted job outputs before validating the internal manifest. Then render the
report/comment, upsert the bot comment via the resolver helper, upload the full
report artifact, export/record its exact ID and digest, and return the final
success/failure decision.

- [ ] **Step 6: Run workflow tests and actionlint**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_cross_repo_perf_workflow.py
actionlint -color .github/workflows/cross-repo-perf.yml
```

Expected: PASS. If `actionlint` is absent, install the repository-pinned 1.7.11
binary into a temporary directory and rerun.

- [ ] **Step 7: Commit workflow**

```bash
git add .github/workflows/cross-repo-perf.yml tests/test_cross_repo_perf_workflow.py
git commit -m "ci: add cross-repo performance workflow"
```

### Task 7: Local Integration And Review

**Files:**
- Modify only files from Tasks 1-6 when fixes are required.

- [ ] **Step 1: Run all new focused tests**

```bash
node --test scripts/ci/cross_repo_perf_resolve.test.cjs
.venv/bin/python -m pytest -q \
  tests/test_cross_repo_perf_runtime.py \
  tests/test_cross_repo_perf_harness.py \
  tests/test_cross_repo_perf_report.py \
  tests/test_cross_repo_perf_workflow.py
bash -n scripts/ci/run_cross_repo_perf_pair.sh
```

- [ ] **Step 2: Run repository quality gates**

```bash
.venv/bin/python scripts/test_node_delta.py --base origin/dev
pre-commit run --all-files --show-diff-on-failure
actionlint -color
git diff --check origin/dev...HEAD
```

New test files require no node-delta growth justification, but record the
script result.

- [ ] **Step 3: Run a local fake end-to-end comparison**

Use tiny fixture TileLang/TileOps wheels, two fixture JUnit files, and fake
GitHub metadata. Exercise resolve serialization, runtime/payload preparation,
service/internal artifact validation, bounded JSON schemas, report rendering,
and comment upsert mocks in one command.

- [ ] **Step 4: Review security and simplify**

Read the final diff specifically for comment-to-shell interpolation, token
scope, candidate-controlled helper code, path traversal, symlink escapes,
artifact fallback, and accidental `/ci-cache` use. Remove duplicate helpers and
nonessential abstraction without changing behavior.

- [ ] **Step 5: Commit integration fixes**

```bash
git add .github/workflows/cross-repo-perf.yml \
  scripts/ci/cross_repo_perf_resolve.cjs \
  scripts/ci/cross_repo_perf_resolve.test.cjs \
  scripts/ci/cross_repo_perf_toolchain.lock \
  scripts/ci/cross_repo_perf_py310.in \
  scripts/ci/cross_repo_perf_py310.lock \
  scripts/ci/cross_repo_perf_harness.py \
  scripts/ci/run_cross_repo_perf_pair.sh \
  scripts/cross_repo_perf_runtime.py \
  scripts/cross_repo_perf_report.py \
  tests/test_cross_repo_perf_runtime.py \
  tests/test_cross_repo_perf_harness.py \
  tests/test_cross_repo_perf_report.py \
  tests/test_cross_repo_perf_workflow.py
git commit -m "ci: harden cross-repo performance validation"
```

Skip the commit if no integration changes were needed.

### Task 8: Full MetaX C500 Validation

**Files:**
- Create local-only artifacts under the Task 0 directory outside the worktree.
- Do not modify public workflow/report text with host-specific details.

- [ ] **Step 1: Create a parameterized local-only validation record**

Set the remote target, remote scratch root, remote uv executable, and remote
CPython 3.10 executable only in the invoking shell. Do not write those values or
credentials into the repository or PR. Record sanitized tool versions,
`mx-smi` device identity, source SHAs, toolchain lock SHA, runtime lock SHA, and
payload SHA in the external validation directory.

- [ ] **Step 2: Resolve exact validation SHAs locally**

Use `gh` to resolve the current TileOps branch/default SHA and an approved
same-repository TileLang PR/default SHA. Record merge/head/base SHAs and URLs in
the local validation directory.

- [ ] **Step 3: Build fresh source wheels from exact source trees**

Build baseline and candidate TileLang companion/TileLang/TileOps wheels from
the exact SHAs. Remove old `build/`, `dist/`, and egg-info directories first.
Record SHA-256, size, distribution metadata, and source SHA for every wheel.
Run the Task 2 wheel audit before transfer. No preinstalled wheel is accepted as
an input.

- [ ] **Step 4: Transfer immutable inputs and verify hashes remotely**

Use the `maca-remote-performance-test` workflow. Prefer SCP; on the known OpenSSL
mismatch, switch immediately to SSH stream copy. Transfer the verified
toolchain archive, read-only wheelhouse plus manifest, runtime lock, all freshly
built wheels, exact trusted payload, and `SHA256SUMS`. Before creating either
environment, run `sha256sum -c SHA256SUMS` remotely and record the result. Do not
write credentials to files.

- [ ] **Step 5: Create and audit separate uv Python 3.10 environments**

For each pair, use the declared remote uv executable to create a new environment
from the declared CPython 3.10 executable under an empty pair root. Revalidate
the wheelhouse lock/manifest and read-only modes, then install the shared closure
offline. Install every freshly built companion, TileLang, and TileOps wheel with
`--no-deps --no-index` and an exact absolute wheel path:

```bash
"$REMOTE_UV" venv --python "$REMOTE_PYTHON310" "$PAIR_ROOT/venv"
"$REMOTE_PYTHON310" "$REMOTE_RUNTIME_TOOL" emit-requirements \
  --lock "$REMOTE_RUNTIME_LOCK" --output "$PAIR_ROOT/runtime-requirements.txt"
"$REMOTE_UV" pip sync --python "$PAIR_ROOT/venv/bin/python" \
  --require-hashes --no-index --find-links "$REMOTE_WHEELHOUSE" \
  "$PAIR_ROOT/runtime-requirements.txt"
"$REMOTE_UV" pip install --python "$PAIR_ROOT/venv/bin/python" \
  --no-deps --no-index "$COMPANION_WHEEL" "$TILELANG_WHEEL" "$TILEOPS_WHEEL"
```

Verify `python -V`, wheel hashes, installed distribution versions, `pip check`
equivalent dependency metadata, and `importlib.util.find_spec()` paths. All
`tilelang`, companion, and `tileops` imports must resolve under the new pair
environment, never a source checkout, user site, `/ci-cache`, or another pair.

- [ ] **Step 6: Run the complete baseline suite with the exact payload command**

Run the trusted neutral payload with `TILELANG_DISABLE_CACHE=1`, empty pair-local
caches, explicit harness/timeout plugins, and no inherited Python paths. From
the neutral payload root execute exactly:

```bash
env -i \
  PATH="$PAIR_ROOT/venv/bin:$SYSTEM_PATH" \
  HOME="$PAIR_ROOT/home" TMPDIR="$PAIR_ROOT/tmp" \
  XDG_CACHE_HOME="$PAIR_ROOT/xdg-cache" XDG_CONFIG_HOME="$PAIR_ROOT/xdg-config" \
  XDG_DATA_HOME="$PAIR_ROOT/xdg-data" UV_CACHE_DIR="$PAIR_ROOT/uv-cache" \
  PIP_CACHE_DIR="$PAIR_ROOT/pip-cache" CCACHE_DIR="$PAIR_ROOT/ccache" \
  TILELANG_CACHE_DIR="$PAIR_ROOT/tilelang-cache" \
  TILELANG_TMP_DIR="$PAIR_ROOT/tilelang-tmp" \
  TRITON_CACHE_DIR="$PAIR_ROOT/triton-cache" \
  TORCH_HOME="$PAIR_ROOT/torch-home" TORCH_EXTENSIONS_DIR="$PAIR_ROOT/torch-ext" \
  CUDA_CACHE_PATH="$PAIR_ROOT/cuda-cache" \
  PYTHONPYCACHEPREFIX="$PAIR_ROOT/pycache" PYTHONNOUSERSITE=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 TILELANG_DISABLE_CACHE=1 USE_MACA=ON \
  "$PAIR_ROOT/venv/bin/python" -m pytest \
  -p cross_repo_perf_harness -p pytest_timeout -q benchmarks/ops \
  --timeout=900 --timeout-method=thread --junit-xml=bench_results.xml
```

`SYSTEM_PATH` is a sanitized, recorded toolchain path without any Python
environment `bin` directories. Recompute the harness SHA on the command's
immediately preceding step. Retain bounded XML/log/provenance/collection/status
and internal manifest files even if pytest exits nonzero.

- [ ] **Step 7: Freeze baseline evidence, then run candidate**

Hash and copy baseline results into the immutable local validation artifact
directory before candidate execution. Run candidate with an independent empty
environment/cache root and retain the same outputs.

- [ ] **Step 8: Generate and inspect the comparison report**

Run `scripts/cross_repo_perf_report.py` locally over the transferred artifacts.
Confirm nonzero comparable cases, identical collection fingerprints, correct
status counts, and no provenance mismatch. Record total runtime and any full
suite failures separately from performance deltas.

- [ ] **Step 9: Commit only code fixes discovered by validation**

Never commit credentials, remote paths, raw host identifiers, or local artifact
directories. Re-run Task 7 after any fix.

### Task 9: Final Verification And Pull Request

- [ ] **Step 1: Fetch and rebase on the latest `origin/dev` if needed**

Use non-interactive rebase. Re-run all Task 7 checks and the relevant remote
smoke/full validation if the base or runtime contract changed.

- [ ] **Step 2: Run completion verification**

Use `superpowers:verification-before-completion`. Confirm clean status, expected
commit list, exact workflow job count, test outputs, actionlint, wheel hashes,
and full C500 report artifacts.

- [ ] **Step 3: Push the branch and create the PR**

Push `cross-repo-perf` and open a PR against `MetaX-MACA/TileOPs-Metax:dev`.
Public title/body describe only code-level design and validation outcome. Do not
name the internal validation host, credentials, cache flags, or local paths.

- [ ] **Step 4: Note the deployment limitation**

State in the PR that `issue_comment` workflows become triggerable only after the
workflow lands on the default branch. Do not claim a pre-merge comment-trigger
end-to-end run.

- [ ] **Step 5: Monitor and address review/CI**

Use `gh` to inspect checks and review threads, apply technically valid feedback,
rerun focused/local/remote validation as required, and update the same PR branch.

### Task 10: Post-Merge GitHub End-To-End Validation

This task is intentionally operational and cannot be claimed before the
workflow file is present on the repository default branch. PR creation is a
delivery checkpoint, not evidence that `issue_comment` dispatch works.

- [ ] **Step 1: Create controlled same-repository test PRs**

After merge, create or select one open TileOps PR whose head branch belongs to
the TileOps repository and one open TileLang PR whose head belongs to the
TileLang repository. Both must target their API-reported default branches and
have authorized maintainers as authors. Use no fork-controlled code as trusted
workflow/helper input.

- [ ] **Step 2: Trigger with the exact authorized comment**

Post only the approved command from an authorized account. Use `gh run view`
and the Actions API to verify resolver disposition `run`, immutable base/head/
merge SHAs, three-job topology, benchmark read-only permissions, and report as
the sole writer. Public comments contain only code-level and validation outcome
information.

- [ ] **Step 3: Verify concurrency cancellation**

While a controlled run is active, post a second valid trigger on the same
TileOps PR. Verify the older run is cancelled by the per-PR concurrency group
and the newer run owns the report marker/artifacts.

- [ ] **Step 4: Verify run-attempt artifact selection**

Rerun a controlled workflow attempt. Confirm resolver, baseline, candidate, and
report downloads use the exact current run ID plus run-attempt artifact IDs and
service digests, never artifacts from the previous attempt or name matching.

- [ ] **Step 5: Verify idempotent report upsert**

Trigger/report twice and confirm exactly one marker keyed by the trigger comment
ID exists. The second report updates only the `github-actions[bot]` comment; a
user-authored forged marker remains untouched.

- [ ] **Step 6: Record and close operational evidence**

Record sanitized run URLs, job conclusions, artifact IDs/digests, cancellation,
rerun selection, and comment-upsert evidence outside the worktree. Remove or
close temporary test branches/PRs after evidence is captured. Only then mark the
end-to-end workflow deployment complete.
