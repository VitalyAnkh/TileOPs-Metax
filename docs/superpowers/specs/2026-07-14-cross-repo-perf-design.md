# Cross-Repository Performance Regression Design

Status: design and independent spec review approved; implementation is gated on
the user's written-spec review.

## Summary

Add a comment-triggered GitHub Actions workflow to `MetaX-MACA/TileOPs-Metax` that
benchmarks one TileOps pull request together with one explicitly referenced
`tile-ai/tilelang-metax` pull request. The candidate pair is compared with a
baseline pair formed from both repositories' default-branch heads at trigger
time.

The workflow runs the complete existing TileOps `benchmarks/ops` suite twice on
the same MetaX runner: baseline first, candidate second. Both runs use freshly
built and freshly installed wheels, isolated Python 3.10 environments, isolated
compiler caches, and the same trusted benchmark payload. The baseline artifact
is uploaded before any candidate build hook or benchmark code executes. A
separate trusted job parses the two JUnit XML files, generates a bounded
Markdown report, and updates a comment on the TileOps pull request.

## Goals

- Trigger the comparison from a TileOps pull-request comment.
- Pair the current TileOps pull request with one TileLang pull request URL.
- Resolve all four source revisions to immutable commit SHAs before GPU work.
- Build fresh TileLang and TileOps wheels for both baseline and candidate pairs.
- Run the full existing `benchmarks/ops` suite for both pairs on the same runner.
- Prevent source checkouts, preinstalled packages, or compiler caches from
  substituting for the newly built wheels.
- Compare per-test latency using stable JUnit identities and report aggregate
  performance without hiding failures, skips, additions, or removals.
- Keep write credentials and PR-comment operations outside the job that executes
  pull-request code.
- Preserve enough provenance and raw artifacts to reproduce or audit a result.

## Non-Goals

- This workflow does not add a new long-K GEMM workload. It measures whatever is
  present in the trusted default-branch `benchmarks/ops` suite at trigger time.
- It does not replace correctness CI, nightly history, or the existing nightly
  report.
- It does not automatically block a pull request on a performance delta in the
  initial version. Infrastructure, provenance, or benchmark execution failures
  still fail the workflow.
- It does not run arbitrary forks or contributors without verified write access
  on the self-hosted GPU runner.
- It does not publish internal validation-host details or local benchmark setup
  in the pull-request report.

## Trigger Contract

The workflow file is `.github/workflows/cross-repo-perf.yml`, displayed as
`Cross-Repository Performance Regression`.

It listens to `issue_comment.created`. The trimmed comment body must consist of
exactly this command and nothing else:

```text
@cross-repo-perf: https://github.com/tile-ai/tilelang-metax/pull/<number>
```

The parser accepts an optional trailing slash but rejects query strings,
fragments, alternate hosts, alternate repositories, multiple commands, and
surrounding prose. The comment body is passed as data to trusted code; it is
never interpolated into a shell command.

Only comments on open TileOps pull requests are eligible. The commenter must
have `write`, `maintain`, or `admin` permission in the TileOps repository.
Unrelated comments and commands from unauthorized users are ignored without
starting GPU work.

Benchmark-job concurrency is scoped to the TileOps pull-request number:

```text
cross-repo-perf-<tileops-pr-number>
```

Resolution happens before a job enters this group, so unrelated or unauthorized
comments cannot cancel active GPU work. A newer authorized invocation for the
same pull request cancels an older benchmark.

## Source Resolution

The resolver runs on `ubuntu-latest` and uses GitHub APIs only. It records a
machine-readable `resolved.json` document and exposes the required values as job
outputs.

For both repositories it resolves:

- repository full name and URL;
- pull-request number and URL for candidate inputs;
- default branch name;
- default-branch head SHA at trigger time;
- candidate pull-request head SHA, base SHA, and merge SHA;
- pull-request state, base repository, head repository, and mergeability.

The four benchmark revisions are:

| Pair | TileOps revision | TileLang revision |
|---|---|---|
| Baseline | TileOps default-branch head | TileLang default-branch head |
| Candidate | Current TileOps PR merge SHA | Referenced TileLang PR merge SHA |

The resolver checks out and later reports exact SHAs, never moving branch names
or pull-request refs. It performs a bounded retry when GitHub reports mergeability
as unknown. It rejects the request if either merge commit is unavailable, either
PR has conflicts, either PR targets a non-default branch, or the PR base SHA does
not match the default-branch head resolved for that repository.

The GPU runner container is ephemeral for one job, but the runner pool can mount
persistent cache storage. Both pull requests must therefore use branches in
their expected base repositories:

- `MetaX-MACA/TileOPs-Metax` for TileOps;
- `tile-ai/tilelang-metax` for TileLang.

The resolver also verifies the TileOps PR author has `write`, `maintain`, or
`admin` permission in TileOps and the TileLang PR author has the same permission
in TileLang. Permission lookup failure rejects the request. This is the same
maintainer-level trust boundary already used by the repository's GPU workflow;
the triggering TileOps maintainer alone cannot authorize arbitrary TileLang
contributor code.

This is an authorization boundary, not a sandbox against malicious maintainers.
The accepted threat model requires an authorized TileOps maintainer to trigger
the run and maintainer-controlled same-repository branches on both sides. The
ephemeral job container and job-local caches prevent accidental cross-run state
reuse; they are not presented as containment for intentionally hostile code. If
organization policy does not accept that dual-maintainer trust, the resolver
must reject the run until a dedicated no-shared-mount runner label is available.

Fork or non-maintainer pull requests fail closed. Supporting them later requires
the existing isolated fork-pool contract or another disposable runner with no
writable shared host mount rather than weakening this gate.

## Workflow Architecture

The workflow contains three jobs with separate trust and permission boundaries.

### 1. Resolve

Runner: `ubuntu-latest`.

Responsibilities:

- parse and validate the command;
- validate commenter permission and pull-request trust constraints;
- resolve immutable baseline and candidate SHAs;
- produce `resolved.json` and job outputs;
- return one of `ignore`, `reject`, or `run`.

`ignore` produces no comment and starts no later jobs. `reject` skips the GPU job
but allows the report job to post a concise validation error for an authorized
request. `run` starts the benchmark job.

`resolved.json` uses a disposition-specific schema: `ignore` and `reject`
contain exactly `disposition` and non-empty `reason`; `run` contains the full
immutable repository identities, trigger metadata, and trusted harness digest.

The benchmark job declares `needs: resolve` and runs only when
`needs.resolve.outputs.disposition == 'run'`. Expected validation errors are
captured as `reject` outputs instead of failing the resolver process, so the
report job can explain them.

Every external Action is pinned to a reviewed full commit SHA. Floating major
tags are not executed on either the self-hosted benchmark runner or the hosted
report job with comment permission.

The resolver has read-only repository, issue, and pull-request access. It never
checks out or executes candidate code.

### 2. Benchmark

Runner: `tileops-metax-runner`.

The job timeout is 300 minutes: two complete suites plus four source builds must
fit within that ceiling, while each testcase keeps the existing 900-second
timeout.

Responsibilities:

- check out each exact SHA into its separate directory only when that pair's
  phase starts;
- recursively initialize TileLang submodules;
- build fresh local wheels for each pair;
- create and populate isolated Python 3.10 uv environments;
- audit package provenance before executing benchmarks;
- build, run, and upload baseline before building or running candidate;
- run baseline and candidate serially using the same benchmark payload;
- always upload build logs, provenance, JUnit XML, pytest logs, and
  `profile_run.log` files, including partial results after failure.

This job has read-only contents permission, receives no repository or environment
secrets, and uses `persist-credentials: false` for every checkout. The workflow
does not expose a write-capable token to candidate build hooks or benchmark code.
It never assigns compiler, pip, ccache, or temporary directories under the
persistent `/ci-cache` mount; all writable run state lives under the job's
ephemeral `RUNNER_TEMP`. A preflight rejects an unexpected writable cache path
in any configured benchmark environment variable.

### 3. Report

Runner: `ubuntu-latest`.

Responsibilities:

- check out the trusted TileOps baseline SHA containing the reporting code;
- download benchmark artifacts;
- parse both JUnit XML files without importing either candidate package;
- write `comparison.json` and the complete Markdown report;
- publish a bounded pull-request comment;
- upload the complete report as an artifact;
- fail after reporting when resolution, build, provenance, or benchmark
  execution did not satisfy the success contract.

Only this job receives `issues: write`; it keeps all other permissions read-only.
It declares `needs: [resolve, benchmark]` and uses `if: always()` together with
an explicit disposition check. It runs once for `reject` and for every attempted
`run`, including when the benchmark job failed or was skipped after a partial
result. It does not run for `ignore`. Unexpected resolver infrastructure failure
fails the workflow without granting the report job ambiguous metadata.

## Python 3.10 Runtime Closure

The current GPU runner's default Python 3.12 environment is not a valid runtime
for this workflow. The benchmark job starts with a trusted preparation step that
bootstraps a pinned CPython 3.10 toolchain and constructs a run-local wheelhouse
before any candidate source is checked out or built.

The trusted bootstrap uses a repository-pinned `uv` release artifact and
SHA-256, then installs one exact CPython 3.10 patch release from uv's managed
Python distribution into `RUNNER_TEMP`. The bootstrap verifies the uv binary,
Python executable, standard library, include directory, and `Python.h`; it must
not use the runner's default interpreter. Before continuing, it compiles,
imports, and deletes a minimal C extension against that interpreter to prove the
headers, compiler, linker, and extension suffix form a working build toolchain.
The extension probe uses a fixed system-tool path and a minimal allowlisted
environment; inherited compiler and dynamic-loader controls are not honored.
Archive extraction rejects traversal, absolute or escaping links, hard links,
and device entries. It permits only relative symbolic links whose normalized
targets remain inside the extraction root, because the pinned managed Python
archive uses such links internally.
The selected uv/Python versions and artifact hashes are recorded in provenance.

The repository owns a hash-locked Python 3.10 requirements file covering the
shared MACA runtime, build tooling, pytest tooling, and dependencies imported by
the complete benchmark suite. It includes the exact MetaX PyTorch and Triton
builds validated for this runner generation. The trusted preparation step:

1. downloads every locked artifact from approved package indexes into a
   `RUNNER_TEMP` wheelhouse with hash verification;
2. writes a wheelhouse manifest containing filename, size, SHA-256, source URL,
   Python tag, and platform tag;
3. rejects sdists or an artifact that is not compatible with CPython 3.10 on the
   runner platform;
4. rejects ZIP special entries, excessive expansion/compression ratios,
   cross-wheel install-path collisions, and ambiguous ownership of the two
   explicitly approved startup-hook modules;
5. creates each pair environment with uv and installs only from that wheelhouse
   using `--no-index` and hash checking;
6. runs dependency consistency checks and imports the required MACA runtime,
   benchmark libraries, pytest, and build tooling before source builds begin.

Both pairs consume the same immutable run-local wheelhouse manifest. TileLang,
TileOps, and any TileLang companion wheels built from the resolved source SHAs
are the only pair-specific distributions. They are installed with `--no-deps`
after the shared closure so a candidate cannot cause dependency resolution or
replace the selected MetaX runtime.

If the locked Python 3.10 runtime cannot be materialized, the benchmark job fails
before checking out or executing candidate code. Missing uv, CPython headers, or
a failed extension-build probe are hard failures. The workflow must never fall
back to the runner's Python 3.12 packages, `/ci-cache/site-packages`, a user
site, or an already installed TileLang/TileOps distribution.

## Build And Environment Isolation

The baseline and candidate pairs use independent directories for source,
wheels, environments, temporary files, compiler caches, and result files. No
directory is reused across pairs. The benchmark job completes and uploads the
baseline artifact before it invokes the candidate TileOps or TileLang build.
This ordering prevents candidate build hooks from changing the baseline result
files used by the report.

Each pair owns a real, non-symlink root under
`RUNNER_TEMP/cross-repo-perf/<run-id>/<run-attempt>/<pair>`. Before every build
or benchmark subprocess, the trusted runner sets pair-local values for at least:

- `HOME`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, and `XDG_DATA_HOME`;
- `UV_CACHE_DIR`, `PIP_CACHE_DIR`, and `CCACHE_DIR`;
- `TILELANG_CACHE_DIR`, `TILELANG_TMP_DIR`, and `TRITON_CACHE_DIR`;
- `TORCH_EXTENSIONS_DIR`, `TORCH_HOME`, `CUDA_CACHE_PATH`, and
  `PYTHONPYCACHEPREFIX`;
- any MACA/compiler-specific cache variable detected in the runner environment.

The helper constructs the subprocess environment from an allowlist, validates a
system-only `PATH`, and does not inherit Python, pip, uv, compiler,
dynamic-loader, token, or agent controls. It recreates every writable directory
empty with non-shared permissions, resolves it with `realpath`, and rejects a
symlink or a path outside the current pair root. `PYTHONPATH` and user-site
loading remain disabled. The shared runtime wheelhouse is a separate read-only
input after its manifest is finalized; it is never used as a writable cache.

Trusted preparation resolves the canonical MACA installation, compiler binary
directories, runtime/driver library directories, and the GCC major passed to
mxcc. Both builds and both benchmark runs receive the same fixed `MACA_PATH`,
`LD_LIBRARY_PATH`, system `PATH`, and `TILELANG_MXCC_FLAGS`; inherited platform
paths are discarded.

Each pair is prepared as follows:

1. Create a Python 3.10 uv environment and install the trusted shared runtime
   closure from the verified run-local wheelhouse without inheriting
   `PYTHONPATH` overrides.
2. Build TileLang from the exact resolved source SHA, including required local
   companion wheels such as `apache-tvm-ffi` when that revision requires them.
3. Build TileOps from the exact resolved source SHA.
4. Discover built artifacts by package metadata rather than hard-coded version,
   Python ABI, commit, or filename strings.
5. Install local companion wheels, then TileLang, then TileOps, all from the
   pair's wheel directory. TileOps is installed with `--no-deps` so dependency
   resolution cannot replace the selected TileLang wheel or drift the runner's
   GPU stack.
6. Record wheel filenames, sizes, SHA-256 hashes, source SHAs, Python version,
   shared wheelhouse digest, package versions, and import locations in
   `provenance.json`.
7. Fail unless `tilelang.__file__` and `tileops.__file__` resolve inside the
   pair's uv environment and outside every source and benchmark checkout.
8. Run dependency consistency checks after installing the pair-specific wheels
   and fail on an unsatisfied or unexpected distribution.

Exactly one TileLang distribution and one TileOps distribution must be selected
for a pair. Zero or ambiguous matches are provenance failures.

## Trusted Benchmark Payload

Both runs execute one identical payload constructed before candidate checkout
from the resolved TileOps baseline SHA. It contains `benchmarks/`, baseline
`workloads/`, the baseline `tests/`
helpers imported by benchmark modules, the baseline `tileops/manifest/*.yaml`
snapshot, the trusted pytest configuration, and a trusted pytest plugin used by
this workflow. The payload is copied to a neutral directory that does not
contain a top-level importable `tileops/` or `tilelang/` source tree.

This boundary provides two guarantees:

- the candidate cannot alter the workload matrix, thresholds, collection hooks,
  workload fixtures, helper references, or result properties used for its own
  comparison;
- Python cannot import TileOps from a checkout merely because the repository
  root is first on `sys.path`.

The installed TileOps wheel supplies executable `tileops` code. The trusted
pytest plugin loads before benchmark collection, patches the public
`tileops.manifest` loader to read only the baseline manifest snapshot, clears its
manifest cache, and records the snapshot's canonical tree digest. The trusted
payload intentionally supplies `workloads` and test helpers for both runs; an
import audit verifies those modules resolve from the payload while `tileops` and
`tilelang` resolve from the selected uv environment.

During collection the plugin writes a sorted list of pytest node IDs and a
fingerprint covering the trusted payload digest, trusted manifest digest, and
node-ID list. Baseline and candidate fingerprints must match before results are
treated as comparable. A candidate that changes collection, cannot honor the
baseline manifest API, or is incompatible with the current trusted benchmark
contract fails visibly rather than silently changing the test harness.

The workflow executes from the neutral payload root, where the reviewed harness
module is copied as `cross_repo_perf_harness.py` and hash-checked immediately
before Python starts. `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` disables candidate or
environment-provided pytest entry points. The command line explicitly loads the
harness first and then the one required trusted third-party plugin,
`pytest_timeout`. The command-line harness implements
`pytest_load_initial_conftests` to install the manifest override before pytest
imports `benchmarks/conftest.py`; `pytest_configure` then verifies the same
state idempotently. Pair wheel audits also reject a TileOps or TileLang wheel
that contains `.pth`, `sitecustomize.py`, `usercustomize.py`, or a `pytest11`
entry point.

## Benchmark Execution

The benchmark job uses this strict order on one GPU runner:

1. check out, build, and install the baseline pair;
2. audit and run the baseline pair;
3. upload the immutable baseline artifact;
4. check out, build, and install the candidate pair;
5. audit and run the candidate pair;
6. upload the candidate artifact.

No two pair runs overlap. The workflow records the fixed timing order in
provenance so the report does not imply that order effects were randomized.

Each run uses this suite and timeout contract:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  -p cross_repo_perf_harness \
  -p pytest_timeout \
  -q benchmarks/ops \
  --timeout=900 \
  --timeout-method=thread \
  --junit-xml=<pair>/bench_results.xml
```

The run uses `PYTHONNOUSERSITE=1`, clears `PYTHONPATH`, sets
`TILELANG_DISABLE_CACHE=1`, and applies the complete pair-local home/cache/temp
environment defined above. Every writable path starts empty and is revalidated
immediately before pytest. The fresh-install and harness-hash audits are also
repeated immediately before pytest.

Pytest exit status is captured without preventing the other pair from running.
The baseline preparation/run step and candidate preparation/run step each write
a bounded `status.json` in a shell `trap`, even on failure. They use
step-level outcome capture rather than allowing the first non-zero command to
skip later evidence steps. The baseline artifact upload uses `if: always()` and
must complete before the candidate preparation/run step. Candidate execution is
gated on `always() && steps.baseline_upload.outcome == 'success'`; a failed
baseline upload prevents all candidate checkout/build/benchmark execution. A
trusted fallback step writes candidate `status.json` and
`artifact-manifest.json` with reason `baseline_artifact_unavailable`, and the
candidate artifact upload still runs so the report has an explicit skipped
state. A final adjudication step runs after both uploads and returns non-zero
when either pair failed or candidate execution was suppressed.

## Artifact Protocol

Artifact names include both `github.run_id` and `github.run_attempt` so workflow
reruns cannot collide with immutable artifacts from an earlier attempt:

```text
cross-repo-perf-resolution-<run-id>-<run-attempt>
cross-repo-perf-baseline-<run-id>-<run-attempt>
cross-repo-perf-candidate-<run-id>-<run-attempt>
cross-repo-perf-report-<run-id>-<run-attempt>
```

The resolver artifact contains only `resolved.json`. Each pair artifact has this
fixed top-level layout:

```text
artifact-manifest.json
status.json
provenance.json
collection.json
bench_results.xml
pytest.log
profile_run.log
build.log
```

Missing files remain listed in `status.json`; uploads use
`if-no-files-found: error` for the required manifest and status files. The
trusted pair runner generates `artifact-manifest.json` last, listing the
relative path, byte size, and SHA-256 of every included file except itself.
It bounds logs through descriptor-based regular-file reads with symlink
following disabled, so a candidate-created log symlink is an evidence failure.

Each upload step exposes the artifact ID and service-provided digest as a job
output. The report job receives resolver metadata through trusted job outputs,
downloads only those exact artifact IDs into fixed empty directories, checks the
expected run ID/attempt/pair in each manifest, validates every listed size and
hash, and rejects extra paths or path traversal before parsing JUnit data. A
missing candidate artifact after a failed candidate step is reported as a
candidate infrastructure failure; an artifact from another run or attempt is
never selected as a fallback.

## Result Model

The reporter parses JUnit XML using the same property contract already emitted
by `benchmarks/conftest.py`:

- `op` and `op_module`;
- `tileops_latency_ms`;
- optional `tileops_tflops`, bandwidth, variant, and library-baseline fields.

The stable comparison key is the JUnit testcase identity
`<classname>::<name>`. Duplicate identities are rejected because they would make
the join ambiguous.

Each testcase is classified independently as passed, skipped, failed, or
errored. A latency is comparable only when both sides passed and both
`tileops_latency_ms` values are finite and greater than zero.

For a comparable testcase:

```text
speedup = baseline_latency_ms / candidate_latency_ms
```

Classification thresholds are:

| Classification | Rule |
|---|---|
| Improved | `speedup >= 1.05` |
| Neutral | `0.95 <= speedup < 1.05` |
| Regressed | `speedup < 0.95` |
| Severe regression | `speedup < 0.90` (subset of regressed) |

Tests present on only one side are reported as added or removed. Outcome
transitions, missing latency properties, and invalid numeric values are reported
separately and are never folded into the aggregate.

The aggregate speedup is the unweighted geometric mean over all common,
comparable testcases:

```text
geomean = exp(mean(log(speedup_i)))
```

No aggregate is emitted when there are no comparable cases.

## Pull-Request Report

The comment contains:

- exact baseline and candidate repository URLs, PR URLs, and short SHAs;
- wheel filenames and SHA-256 prefixes;
- pytest outcome counts for both runs;
- comparable, improved, neutral, regressed, severe, added, and removed counts;
- geometric-mean speedup;
- the largest regressions and improvements with both latency values;
- benchmark or provenance failure summaries;
- a link to the workflow run and complete report artifact.

All dynamic Markdown cell content is escaped. Failure messages are length
bounded. The comment stays below a conservative 60,000-character limit; the
unabridged Markdown and JSON remain in the uploaded artifact.

The trusted parser enforces resource limits before XML parsing: a JUnit file is
at most 64 MiB, contains no `DOCTYPE` or entity declaration, has at most 10,000
testcases, at most 128 properties per testcase, and bounded attribute/property/
failure-message lengths. Provenance, status, collection, and artifact-manifest
JSON files are each at most 2 MiB and must match strict schemas. Oversized or
unexpected inputs are evidence failures, not partial successes.

The report includes this hidden marker, keyed by the trigger comment ID:

```text
<!-- cross-repo-perf:trigger-comment-<id> -->
```

Rerunning the same invocation updates the existing bot comment instead of
creating duplicates. The updater modifies a marker match only when the existing
comment author is `github-actions[bot]`; a user-authored comment containing the
same marker is ignored. A later trigger comment creates a separate result
comment.

## Failure Semantics

Performance changes alone are informational in the initial version. A severe
regression is highlighted but does not fail the workflow.

The workflow fails for any of these conditions:

- an authorized command cannot resolve trusted, mergeable revisions;
- pinned uv/CPython 3.10 bootstrap, headers, extension probe, or locked runtime
  wheelhouse cannot be verified or materialized;
- source checkout or recursive submodule initialization fails;
- wheel build, artifact discovery, or installation fails;
- import paths or wheel hashes do not match recorded provenance;
- baseline and candidate trusted-payload, manifest, or collection fingerprints
  differ;
- required trusted pytest plugins are not the explicitly loaded modules or a
  pair wheel contains a forbidden startup/plugin hook;
- either pytest invocation returns non-zero;
- JUnit XML is missing or malformed after a nominally successful run;
- artifact identity, service digest, internal file hash, schema, or resource
  limit validation fails;
- duplicate testcase identities make the comparison ambiguous;
- no common testcase has valid comparable latency data;
- report generation or comment publication fails.

The reporter records whether complete detailed outputs were rendered separately
from its exit status. A no-comparable-case result therefore keeps its testcase
and provenance diagnostics while final adjudication still fails. Generic
fallback text is generated only when no complete detailed report exists.

The report job still publishes available diagnostics before returning failure.

## Proposed Files

- `.github/workflows/cross-repo-perf.yml`: three-job orchestration and job-level
  permissions.
- `scripts/ci/cross_repo_perf_resolve.cjs`: tested command parsing, permission
  checks, immutable GitHub revision resolution, and comment upsert helpers used
  by `actions/github-script`.
- `scripts/ci/cross_repo_perf_toolchain.lock`: pinned uv and managed CPython
  3.10 artifacts, hashes, and expected toolchain metadata.
- `scripts/ci/cross_repo_perf_py310.lock`: hash-locked shared MACA, build, and
  benchmark runtime closure for CPython 3.10.
- `scripts/ci/run_cross_repo_perf_pair.sh`: fresh wheel build, uv environment
  creation, provenance audit, and one pair's benchmark execution.
- `scripts/ci/cross_repo_perf_harness.py`: trusted manifest override, import
  audit, collection fingerprint, and bounded status/artifact-manifest writing.
- `scripts/cross_repo_perf_report.py`: pure JUnit comparison and Markdown/JSON
  rendering.
- `tests/test_cross_repo_perf_report.py`: parser, comparison, threshold,
  escaping, truncation, and failure-contract tests.
- `scripts/ci/cross_repo_perf_resolve.test.cjs`: Node built-in test coverage for
  strict command parsing and mocked resolution/upsert behavior.

The implementation may adjust names slightly to match an existing adjacent
convention, but it must preserve these component boundaries: orchestration,
untrusted-input resolution, pair preparation/execution, and pure reporting.

## Validation Plan

### Local, No GPU

- parse the workflow YAML;
- run `bash -n` and available shell lint on the pair runner;
- run `node --test scripts/ci/cross_repo_perf_resolve.test.cjs`;
- run focused Python 3.10 pytest coverage for the reporter;
- run existing relevant report tests;
- verify pinned uv/CPython artifacts and run the minimal Python 3.10 C-extension
  build/import probe;
- verify the Python 3.10 lock has hashes for every transitive artifact and can
  materialize both pair environments from a local `--no-index` wheelhouse;
- use fixture JUnit XML to exercise pass, skip, failure, error, added, removed,
  invalid latency, threshold boundary, duplicate identity, and empty-comparison
  cases;
- exercise manifest override and collection-fingerprint mismatch cases;
- install a fixture wheel with a `pytest11` entry point and prove plugin
  auto-loading is disabled/rejected before collection;
- exercise run-attempt artifact naming, exact artifact-ID selection, internal
  hash mismatch, XML/JSON limits, and bot-author comment upsert checks;
- simulate baseline artifact-upload failure and verify no candidate checkout,
  build, or benchmark command runs;
- test cache-root realpath, symlink, inherited-variable, and path-escape
  rejection for every configured writable directory;
- build small local test wheels or use a dry-run fixture to verify wheel
  discovery and import-path rejection without a GPU.

### MetaX C500 Validation

- build fresh baseline and candidate TileLang and TileOps wheels from the exact
  resolved SHAs in the same baseline-then-candidate order as CI;
- create both environments from the same verified CPython 3.10 runtime
  wheelhouse and retain its manifest;
- transfer and install those wheels into separate uv Python 3.10 environments;
- run the complete existing `benchmarks/ops` suite for both pairs with fresh
  compiler caches;
- retain JUnit XML, logs, profile summaries, provenance, and the generated
  comparison report;
- verify that reported wheel hashes and import paths match the installed
  artifacts, and that trusted manifest/collection fingerprints match.

Internal host, credential, and cache-control details remain in local validation
records and are omitted from public pull-request text.

### GitHub End-To-End

`issue_comment` workflows execute from the repository default branch, so the
new trigger becomes available only after the workflow is merged. After merge,
run one authorized command against same-repository test pull requests and verify
resolution, GPU isolation, artifact upload, report upsert, cancellation, and
read/write permission boundaries. Rerun the same workflow attempt once to verify
run-attempt-specific artifact selection and idempotent comment update.

## Acceptance Criteria

- The exact `@cross-repo-perf` command resolves the current TileOps PR and the
  referenced TileLang PR to immutable merge SHAs.
- Baseline SHAs are the two repositories' default-branch heads captured at the
  same invocation.
- Forks, conflicts, stale base resolution, unauthorized comments, and PR authors
  without verified write access in their respective repositories do not run
  candidate code on the self-hosted runner.
- A pinned, hash-verified uv/CPython 3.10 toolchain passes the header and minimal
  extension-build probe; both isolated uv environments then use the same locked
  runtime closure and do not import Python 3.12 or `/ci-cache/site-packages`
  content.
- Fresh wheels for all four repository revisions are built, hashed, installed,
  and proven to be the imported packages.
- Baseline and candidate run the same complete trusted `benchmarks/ops` payload
  with baseline workloads/helpers, isolated environments, and isolated caches.
- The baseline manifest snapshot is forced for both runs, and payload, manifest,
  and collected-node fingerprints are identical.
- Pytest auto-loading is disabled; the trusted harness is explicitly loaded
  before conftest and only the required trusted timeout plugin is enabled.
- The baseline result artifact is uploaded before candidate build or benchmark
  code executes; upload failure suppresses candidate execution and is reported.
- Every writable home/cache/temp path resolves inside the current pair's empty
  `RUNNER_TEMP` root, with the finalized shared wheelhouse made read-only and
  hash-revalidated before each install.
- Report inputs are selected by exact run-attempt artifact IDs and pass service
  digest, internal hash, schema, and resource-limit validation.
- The report correctly joins JUnit cases, computes per-case and geometric-mean
  speedups, distinguishes all non-comparable outcomes, and posts idempotently.
- Performance deltas are visible but do not independently fail the first
  version; infrastructure and evidence failures do.
- Local tests pass, full C500 benchmark artifacts are retained, and public
  GitHub communication contains no internal validation-host details.
