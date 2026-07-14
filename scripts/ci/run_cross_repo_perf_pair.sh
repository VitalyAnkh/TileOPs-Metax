#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --pair baseline|candidate --pair-root PATH --trusted-source PATH --tileops-source PATH --tileops-sha SHA --tilelang-source PATH --tilelang-sha SHA --trusted-tileops-sha SHA --resolved PATH --runtime-tool PATH --harness-tool PATH --runtime-lock PATH --wheelhouse PATH --uv PATH --python PATH --python-include PATH --git PATH --repository OWNER/REPO --run-id N --run-attempt N --system-path PATH --maca-path PATH --maca-library-path PATHS --tilelang-mxcc-flags FLAGS --payload-root PATH --payload-manifest PATH" >&2
  exit 2
}

PAIR=""
PAIR_ROOT=""
TRUSTED_SOURCE=""
TILEOPS_SOURCE=""
TILEOPS_SHA=""
TILELANG_SOURCE=""
TILELANG_SHA=""
TRUSTED_TILEOPS_SHA=""
RESOLVED=""
RUNTIME_TOOL=""
HARNESS_TOOL=""
RUNTIME_LOCK=""
WHEELHOUSE=""
UV=""
PYTHON=""
PYTHON_INCLUDE=""
GIT=""
REPOSITORY=""
RUN_ID=""
RUN_ATTEMPT=""
SYSTEM_PATH=""
MACA_PATH=""
MACA_LIBRARY_PATH=""
TILELANG_MXCC_FLAGS=""
PAYLOAD_ROOT=""
PAYLOAD_MANIFEST=""

while (($#)); do
  case "$1" in
    --pair) PAIR=${2-}; shift 2 ;;
    --pair-root) PAIR_ROOT=${2-}; shift 2 ;;
    --trusted-source) TRUSTED_SOURCE=${2-}; shift 2 ;;
    --tileops-source) TILEOPS_SOURCE=${2-}; shift 2 ;;
    --tileops-sha) TILEOPS_SHA=${2-}; shift 2 ;;
    --tilelang-source) TILELANG_SOURCE=${2-}; shift 2 ;;
    --tilelang-sha) TILELANG_SHA=${2-}; shift 2 ;;
    --trusted-tileops-sha) TRUSTED_TILEOPS_SHA=${2-}; shift 2 ;;
    --resolved) RESOLVED=${2-}; shift 2 ;;
    --runtime-tool) RUNTIME_TOOL=${2-}; shift 2 ;;
    --harness-tool) HARNESS_TOOL=${2-}; shift 2 ;;
    --runtime-lock) RUNTIME_LOCK=${2-}; shift 2 ;;
    --wheelhouse) WHEELHOUSE=${2-}; shift 2 ;;
    --uv) UV=${2-}; shift 2 ;;
    --python) PYTHON=${2-}; shift 2 ;;
    --python-include) PYTHON_INCLUDE=${2-}; shift 2 ;;
    --git) GIT=${2-}; shift 2 ;;
    --repository) REPOSITORY=${2-}; shift 2 ;;
    --run-id) RUN_ID=${2-}; shift 2 ;;
    --run-attempt) RUN_ATTEMPT=${2-}; shift 2 ;;
    --system-path) SYSTEM_PATH=${2-}; shift 2 ;;
    --maca-path) MACA_PATH=${2-}; shift 2 ;;
    --maca-library-path) MACA_LIBRARY_PATH=${2-}; shift 2 ;;
    --tilelang-mxcc-flags) TILELANG_MXCC_FLAGS=${2-}; shift 2 ;;
    --payload-root) PAYLOAD_ROOT=${2-}; shift 2 ;;
    --payload-manifest) PAYLOAD_MANIFEST=${2-}; shift 2 ;;
    *) usage ;;
  esac
done

[[ $PAIR == baseline || $PAIR == candidate ]] || usage
[[ $TILEOPS_SHA =~ ^[0-9a-f]{40}$ ]] || usage
[[ $TILELANG_SHA =~ ^[0-9a-f]{40}$ ]] || usage
[[ $TRUSTED_TILEOPS_SHA =~ ^[0-9a-f]{40}$ ]] || usage
[[ $REPOSITORY =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] || usage
[[ $RUN_ID =~ ^[1-9][0-9]*$ ]] || usage
[[ $RUN_ATTEMPT =~ ^[1-9][0-9]*$ ]] || usage

for value in "$PAIR_ROOT" "$TRUSTED_SOURCE" "$TILEOPS_SOURCE" "$TILELANG_SOURCE" \
  "$RESOLVED" "$RUNTIME_TOOL" "$HARNESS_TOOL" "$RUNTIME_LOCK" "$WHEELHOUSE" \
  "$UV" "$PYTHON" "$PYTHON_INCLUDE" "$GIT" "$PAYLOAD_ROOT" "$PAYLOAD_MANIFEST"; do
  [[ $value == /* ]] || usage
done
[[ $MACA_PATH == /* && -d $MACA_PATH ]] || usage
[[ $TILELANG_MXCC_FLAGS =~ ^-gcc-version\ [1-9][0-9]*$ ]] || usage
IFS=: read -r -a MACA_LIBRARY_DIRECTORIES <<<"$MACA_LIBRARY_PATH"
((${#MACA_LIBRARY_DIRECTORIES[@]} > 0)) || usage
for value in "${MACA_LIBRARY_DIRECTORIES[@]}"; do
  [[ $value == /* && -d $value ]] || usage
done

STARTED_AT=$(/usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
ARTIFACT_ROOT=$PAIR_ROOT/artifact
BUILD_ROOT=$PAIR_ROOT/build
WHEEL_ROOT=$PAIR_ROOT/wheels
VENV_ROOT=$PAIR_ROOT/venv
BUILD_LOG=$ARTIFACT_ROOT/build.log
PYTEST_LOG=$ARTIFACT_ROOT/pytest.log
PHASE=setup
STATE=failed
REASON=setup_failed

TRUSTED_ENV=(
  /usr/bin/env -i
  PATH="$SYSTEM_PATH"
  LANG=C.UTF-8
  LC_ALL=C.UTF-8
  TZ=UTC
  PYTHONPATH=
  PYTHONNOUSERSITE=1
  GIT_CONFIG_GLOBAL=/dev/null
  GIT_CONFIG_SYSTEM=/dev/null
  GIT_TERMINAL_PROMPT=0
)

bound_log() {
  local path=$1
  local limit=$2
  "${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" bound-log \
    --path "$path" --limit "$limit"
}

finalize() {
  local command_status=$?
  local final_status=0
  local finished_at
  trap - EXIT
  set +e
  if (( command_status != 0 )); then
    STATE=failed
    REASON=${PHASE}_failed
  fi
  bound_log "$BUILD_LOG" $((8 * 1024 * 1024)) || final_status=$?
  bound_log "$PYTEST_LOG" $((8 * 1024 * 1024)) || final_status=$?
  finished_at=$(/usr/bin/date -u +%Y-%m-%dT%H:%M:%SZ)
  "${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" finalize-pair \
    --artifact-root "$ARTIFACT_ROOT" \
    --resolved "$RESOLVED" \
    --repository "$REPOSITORY" \
    --pair "$PAIR" \
    --state "$STATE" \
    --phase "$PHASE" \
    --exit-code "$command_status" \
    --reason "$REASON" \
    --started-at "$STARTED_AT" \
    --finished-at "$finished_at" \
    --run-id "$RUN_ID" \
    --run-attempt "$RUN_ATTEMPT" \
    --tileops-sha "$TILEOPS_SHA" \
    --tilelang-sha "$TILELANG_SHA" \
    --payload-manifest "$PAYLOAD_MANIFEST" || final_status=$?
  if (( command_status == 0 && final_status != 0 )); then
    command_status=$final_status
  fi
  exit "$command_status"
}
trap finalize EXIT

"${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" prepare-pair-root \
  --root "$PAIR_ROOT" --system-path "$SYSTEM_PATH"
/usr/bin/touch "$BUILD_LOG" "$PYTEST_LOG"

PHASE=toolchain
[[ $("${TRUSTED_ENV[@]}" "$GIT" -C "$TILEOPS_SOURCE" rev-parse HEAD) == "$TILEOPS_SHA" ]]
[[ $("${TRUSTED_ENV[@]}" "$GIT" -C "$TILELANG_SOURCE" rev-parse HEAD) == "$TILELANG_SHA" ]]
[[ $("${TRUSTED_ENV[@]}" "$GIT" -C "$TRUSTED_SOURCE" rev-parse HEAD) == "$TRUSTED_TILEOPS_SHA" ]]
SUBMODULE_STATUS=$("${TRUSTED_ENV[@]}" "$GIT" -C "$TILELANG_SOURCE" submodule status --recursive)
if /usr/bin/grep -Eq '^[+-U]' <<<"$SUBMODULE_STATUS"; then
  echo "TileLang recursive submodules are not at recorded commits" >&2
  exit 1
fi
"${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" verify-wheelhouse \
  --lock "$RUNTIME_LOCK" --wheelhouse "$WHEELHOUSE"

"${TRUSTED_ENV[@]}" "$UV" venv --offline --no-python-downloads --no-config \
  --python "$PYTHON" "$VENV_ROOT" >>"$BUILD_LOG" 2>&1

PAIR_ENV=(
  /usr/bin/env -i
  PATH="$VENV_ROOT/bin:$SYSTEM_PATH"
  LANG=C.UTF-8
  LC_ALL=C.UTF-8
  TZ=UTC
  HOME="$PAIR_ROOT/home"
  TMPDIR="$PAIR_ROOT/tmp"
  TMP="$PAIR_ROOT/tmp"
  TEMP="$PAIR_ROOT/tmp"
  XDG_CACHE_HOME="$PAIR_ROOT/xdg-cache"
  XDG_CONFIG_HOME="$PAIR_ROOT/xdg-config"
  XDG_DATA_HOME="$PAIR_ROOT/xdg-data"
  XDG_STATE_HOME="$PAIR_ROOT/xdg-state"
  UV_CACHE_DIR="$PAIR_ROOT/uv-cache"
  PIP_CACHE_DIR="$PAIR_ROOT/pip-cache"
  CCACHE_DIR="$PAIR_ROOT/ccache"
  TILELANG_CACHE_DIR="$PAIR_ROOT/tilelang-cache"
  TILELANG_TMP_DIR="$PAIR_ROOT/tilelang-tmp"
  TRITON_CACHE_DIR="$PAIR_ROOT/triton-cache"
  TORCH_EXTENSIONS_DIR="$PAIR_ROOT/torch-extensions"
  TORCH_HOME="$PAIR_ROOT/torch-home"
  CUDA_CACHE_PATH="$PAIR_ROOT/cuda-cache"
  PYTHONPYCACHEPREFIX="$PAIR_ROOT/pycache"
  NUMBA_CACHE_DIR="$PAIR_ROOT/numba-cache"
  MACA_CACHE_DIR="$PAIR_ROOT/maca-cache"
  MCC_CACHE_DIR="$PAIR_ROOT/mcc-cache"
  MXCC_CACHE_DIR="$PAIR_ROOT/mxcc-cache"
  PYTHONPATH=
  PYTHONNOUSERSITE=1
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
  TILELANG_DISABLE_CACHE=1
  PIP_NO_INDEX=1
  USE_MACA=ON
  MACA_PATH="$MACA_PATH"
  LD_LIBRARY_PATH="$MACA_LIBRARY_PATH"
  TILELANG_MXCC_FLAGS="$TILELANG_MXCC_FLAGS"
)

"${PAIR_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" emit-requirements \
  --lock "$RUNTIME_LOCK" --output "$BUILD_ROOT/runtime-requirements.txt"
"${PAIR_ENV[@]}" "$UV" pip sync --python "$VENV_ROOT/bin/python" \
  --require-hashes --no-index --find-links "$WHEELHOUSE" \
  "$BUILD_ROOT/runtime-requirements.txt" >>"$BUILD_LOG" 2>&1

PHASE=build
COMPANION_SOURCE=$TILELANG_SOURCE/3rdparty/tvm/3rdparty/tvm-ffi
"${PAIR_ENV[@]}" "$UV" build --offline --no-index --find-links "$WHEELHOUSE" \
  --wheel --clear --no-create-gitignore --no-build-isolation --no-config \
  --python "$VENV_ROOT/bin/python" \
  --out-dir "$WHEEL_ROOT" "$COMPANION_SOURCE" >>"$BUILD_LOG" 2>&1
"${PAIR_ENV[@]}" "$UV" build --offline --no-index --find-links "$WHEELHOUSE" \
  --wheel --no-create-gitignore --no-build-isolation --no-config \
  --python "$VENV_ROOT/bin/python" \
  --out-dir "$WHEEL_ROOT" "$TILELANG_SOURCE" >>"$BUILD_LOG" 2>&1
"${PAIR_ENV[@]}" "$UV" build --offline --no-index --find-links "$WHEELHOUSE" \
  --wheel --no-create-gitignore --no-build-isolation --no-config \
  --python "$VENV_ROOT/bin/python" \
  --out-dir "$WHEEL_ROOT" "$TILEOPS_SOURCE" >>"$BUILD_LOG" 2>&1
"${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" audit-pair-wheels \
  --wheel-dir "$WHEEL_ROOT" --tileops-sha "$TILEOPS_SHA" --tilelang-sha "$TILELANG_SHA" \
  --output "$BUILD_ROOT/pair-wheels.json"
COMPANION_WHEEL=$("${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" pair-wheel-path \
  --manifest "$BUILD_ROOT/pair-wheels.json" --distribution companion)
TILELANG_WHEEL=$("${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" pair-wheel-path \
  --manifest "$BUILD_ROOT/pair-wheels.json" --distribution tilelang)
TILEOPS_WHEEL=$("${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" pair-wheel-path \
  --manifest "$BUILD_ROOT/pair-wheels.json" --distribution tileops)

PHASE=install
"${PAIR_ENV[@]}" "$UV" pip install --python "$VENV_ROOT/bin/python" \
  --no-index --no-deps --reinstall \
  "$COMPANION_WHEEL" "$TILELANG_WHEEL" "$TILEOPS_WHEEL" \
  >>"$BUILD_LOG" 2>&1
"${PAIR_ENV[@]}" "$UV" pip check --python "$VENV_ROOT/bin/python" >>"$BUILD_LOG" 2>&1

PHASE=import
"${PAIR_ENV[@]}" "$VENV_ROOT/bin/python" "$RUNTIME_TOOL" audit-installed-pair \
  --pair-root "$PAIR_ROOT" \
  --wheel-manifest "$BUILD_ROOT/pair-wheels.json" \
  --tileops-source "$TILEOPS_SOURCE" \
  --tilelang-source "$TILELANG_SOURCE" \
  --trusted-source "$TRUSTED_SOURCE" \
  --output "$BUILD_ROOT/installed.json"

PHASE=payload
[[ -d $PAYLOAD_ROOT && ! -L $PAYLOAD_ROOT ]]
[[ -f $PAYLOAD_MANIFEST && ! -L $PAYLOAD_MANIFEST ]]
"${TRUSTED_ENV[@]}" "$PYTHON" "$RUNTIME_TOOL" write-pair-provenance \
  --pair "$PAIR" \
  --run-id "$RUN_ID" \
  --run-attempt "$RUN_ATTEMPT" \
  --python "$VENV_ROOT/bin/python" \
  --python-include "$PYTHON_INCLUDE" \
  --runtime-lock "$RUNTIME_LOCK" \
  --wheelhouse "$WHEELHOUSE" \
  --wheel-manifest "$BUILD_ROOT/pair-wheels.json" \
  --installed "$BUILD_ROOT/installed.json" \
  --payload-manifest "$PAYLOAD_MANIFEST" \
  --output "$ARTIFACT_ROOT/provenance.json"

PHASE=benchmark
"${TRUSTED_ENV[@]}" "$PYTHON" "$HARNESS_TOOL" run-pytest \
  --payload-root "$PAYLOAD_ROOT" \
  --resolved "$RESOLVED" \
  --python "$VENV_ROOT/bin/python" \
  --output-root "$ARTIFACT_ROOT" \
  --pair "$PAIR" \
  --run-id "$RUN_ID" \
  --run-attempt "$RUN_ATTEMPT" \
  --system-path "$SYSTEM_PATH" \
  --maca-path "$MACA_PATH" \
  --maca-library-path "$MACA_LIBRARY_PATH" \
  --tilelang-mxcc-flags="$TILELANG_MXCC_FLAGS" >"$PYTEST_LOG" 2>&1

STATE=success
PHASE=complete
REASON=""
