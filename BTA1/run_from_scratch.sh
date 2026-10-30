#!/usr/bin/env bash
#
# End-to-end demo: from a bare checkout to a verified bug reproduction.
#
# Does everything in one go:
#   1. activate .venv, or create it and install from requirements.txt
#   2. download and unpack llama.cpp (prebuilt, no compiler needed)
#   3. start llama-server with Qwen3-4B-Instruct-2507
#   4. wait for it to answer
#   5. run the agent on the target folder, writing the script to output/
#   6. run gdb with that script and show the bug reproducing
#   7. stop the server and deactivate
#
# Usage:
#   ./run_from_scratch.sh                         # examples/overflow
#   ./run_from_scratch.sh <project-dir> [binary]  # your own target
#
# First run downloads ~2.5 GB (llama.cpp + the model) and takes a while. Later
# runs reuse both caches. Expect 2-5 minutes per agent run on a laptop CPU.

set -euo pipefail

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

PROJECT_DIR="${1:-examples/overflow}"
PROGRAM="${2:-buggy}"

VENV="$REPO/.venv"
OUTPUT_DIR="$REPO/output"
# One .gdb script per bug found lands in $OUTPUT_DIR as bug-NN-<slug>.gdb

# llama.cpp prebuilt release (Ubuntu x64 -- no local build required)
LLAMA_VERSION="b11800"
LLAMA_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/llama-${LLAMA_VERSION}-bin-ubuntu-x64.tar.gz"
LLAMA_DIR="$REPO/.llama.cpp/$LLAMA_VERSION"
LLAMA_TARBALL="$REPO/.llama.cpp/llama-${LLAMA_VERSION}.tar.gz"

# The non-thinking Instruct variant: nothing for tool-call parsing to collide
# with. See docs/llm-server-setup.md.
MODEL_HF="${MODEL_HF:-unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M}"

# Overridable so you can point at a server you already run elsewhere:
#   LLAMA_HOST=172.17.0.1 ./run_from_scratch.sh     # server on the Docker host
# When a server is already reachable, the script reuses it and skips both the
# download and the startup entirely.
HOST="${LLAMA_HOST:-127.0.0.1}"
PORT="${LLAMA_PORT:-8000}"
CTX="${LLAMA_CTX:-16384}"
THREADS="${LLAMA_THREADS:-$(nproc 2>/dev/null || echo 4)}"

SERVER_LOG="$REPO/.llama.cpp/server.log"
SERVER_PID=""
STARTUP_TIMEOUT=600   # first run has to download the model

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

say()  { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Always stop the server we started, however we exit.
cleanup() {
    local rc=$?
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        say "Stopping llama-server (pid $SERVER_PID)"
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    # Only meaningful if we activated it; harmless otherwise.
    if declare -F deactivate >/dev/null 2>&1; then
        say "Deactivating the virtual environment"
        deactivate || true
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

need() { command -v "$1" >/dev/null 2>&1 || die "required tool not found: $1"; }

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------

say "Checking prerequisites"
for tool in python3 curl tar gcc make gdb prlimit; do
    need "$tool"
    info "$tool: $(command -v "$tool")"
done
[[ -d "$PROJECT_DIR" ]] || die "target folder does not exist: $PROJECT_DIR"
info "target folder: $PROJECT_DIR"
info "program name : $PROGRAM"

# ---------------------------------------------------------------------------
# 1. Virtual environment
# ---------------------------------------------------------------------------

if [[ -f "$VENV/bin/activate" ]]; then
    say "Activating the existing environment ($VENV)"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
else
    say "No environment found -- creating $VENV"
    python3 -m venv "$VENV"
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"

    say "Installing pinned dependencies from requirements.txt"
    pip install --upgrade pip
    pip install -r requirements.txt

    # requirements.txt holds the dependencies only; this makes the project's own
    # package importable without letting pip re-resolve the pinned set.
    say "Installing the project itself (editable, no dependency resolution)"
    pip install -e . --no-deps
fi
info "python: $(command -v python)"

# A fresh checkout may have requirements.txt but no installed package.
python -c "import bta1" 2>/dev/null || {
    say "Project package missing from the environment -- installing it"
    pip install -e . --no-deps
}

# ---------------------------------------------------------------------------
# 2. llama.cpp -- only if we actually need to start a server
# ---------------------------------------------------------------------------

if curl -fsS --max-time 3 "http://$HOST:$PORT/v1/models" >/dev/null 2>&1; then
    say "A server is already listening on $HOST:$PORT -- reusing it"
    info "skipping the llama.cpp download and startup"
    served="$(curl -fsS --max-time 5 "http://$HOST:$PORT/v1/models" \
        | python -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' \
        2>/dev/null || echo unknown)"
    info "serving: $served"
    REUSED_SERVER=1
else
    REUSED_SERVER=0
fi

mkdir -p "$REPO/.llama.cpp"

if [[ "$REUSED_SERVER" == "1" ]]; then
    :   # nothing to download
elif [[ -x "$LLAMA_DIR/llama-server" ]]; then
    say "llama.cpp $LLAMA_VERSION already present"
else
    say "Downloading llama.cpp $LLAMA_VERSION"
    info "$LLAMA_URL"
    curl -fL --progress-bar -o "$LLAMA_TARBALL" "$LLAMA_URL" \
        || die "download failed -- check connectivity, or that release $LLAMA_VERSION still exists"

    say "Unpacking"
    rm -rf "$LLAMA_DIR" && mkdir -p "$LLAMA_DIR"
    tar -xzf "$LLAMA_TARBALL" -C "$LLAMA_DIR" --strip-components=1 \
        || tar -xzf "$LLAMA_TARBALL" -C "$LLAMA_DIR"

    # Release layouts have moved around between versions (build/bin/, bin/, or
    # the archive root), so locate the binary rather than assuming a path.
    found="$(find "$LLAMA_DIR" -type f -name 'llama-server' -print -quit 2>/dev/null || true)"
    [[ -n "$found" ]] || die "llama-server not found inside the archive"
    if [[ "$found" != "$LLAMA_DIR/llama-server" ]]; then
        ln -sf "$found" "$LLAMA_DIR/llama-server"
    fi
    chmod +x "$found"

    # Bundled shared libraries live next to the binary.
    rm -f "$LLAMA_TARBALL"
fi

# ---------------------------------------------------------------------------
# 3. Start the server
# ---------------------------------------------------------------------------

if [[ "$REUSED_SERVER" == "1" ]]; then
    say "Using the server already running on $HOST:$PORT"
else
    LLAMA_SERVER="$LLAMA_DIR/llama-server"
    LLAMA_LIBDIR="$(dirname "$(readlink -f "$LLAMA_SERVER")")"
    info "server binary: $LLAMA_SERVER"

    say "Starting llama-server on $HOST:$PORT"
    info "model: $MODEL_HF"
    info "--jinja is REQUIRED: it applies the chat template that emits and"
    info "parses tool calls. Without it the model cannot call tools at all."
    info "log: $SERVER_LOG"

    LD_LIBRARY_PATH="$LLAMA_LIBDIR:${LD_LIBRARY_PATH:-}" \
    "$LLAMA_SERVER" \
        -hf "$MODEL_HF" \
        --host "$HOST" \
        --port "$PORT" \
        --jinja \
        -c "$CTX" \
        -t "$THREADS" \
        >"$SERVER_LOG" 2>&1 &
    SERVER_PID=$!
    info "pid: $SERVER_PID"

    # ---------------------------------------------------------------------
    # 4. Wait for it
    # ---------------------------------------------------------------------
    say "Waiting for the server (first run downloads ~2.5 GB of weights)"
    deadline=$(( SECONDS + STARTUP_TIMEOUT ))
    until curl -fsS --max-time 3 "http://$HOST:$PORT/v1/models" >/dev/null 2>&1; do
        kill -0 "$SERVER_PID" 2>/dev/null || {
            tail -30 "$SERVER_LOG" >&2
            die "llama-server exited during startup (see $SERVER_LOG)"
        }
        (( SECONDS < deadline )) || {
            tail -30 "$SERVER_LOG" >&2
            die "server did not become ready within ${STARTUP_TIMEOUT}s"
        }
        printf '.'
        sleep 3
    done
    printf '\n'
    info "ready"
fi

# ---------------------------------------------------------------------------
# 5. Run the agent
# ---------------------------------------------------------------------------

# The server is local to this script, so point the agent at localhost. The
# committed default in config.yaml is the Docker bridge address (for an agent in
# a container reaching a server on the host); this env var overrides it without
# editing the file. See the ${LLM_BASE_URL:-...} entry in config.yaml.
export LLM_BASE_URL="http://$HOST:$PORT/v1"

mkdir -p "$OUTPUT_DIR"

say "Running the agent on $PROJECT_DIR"
info "base_url  : $LLM_BASE_URL"
info "output dir: $OUTPUT_DIR"
info "The agent compiles the project, reads every source file, then drives gdb"
info "to test its hypotheses -- saving one script per bug it reproduces."
info "A few minutes on CPU for one bug; several times that for a multi-bug project."

# Start from a clean slate so we only count scripts from THIS run.
rm -f "$OUTPUT_DIR"/bug-*.gdb

python -m bta1.agent \
    --project-root "$PROJECT_DIR" \
    --output-dir "$OUTPUT_DIR" \
    || die "the agent did not produce any reproduction script (see the log above)"

mapfile -t SCRIPTS < <(find "$OUTPUT_DIR" -maxdepth 1 -name 'bug-*.gdb' | sort)
(( ${#SCRIPTS[@]} > 0 )) || die "no scripts were written to $OUTPUT_DIR"

say "Generated ${#SCRIPTS[@]} reproduction script(s)"
for s in "${SCRIPTS[@]}"; do
    echo "---------------------------------------------------------------"
    echo "### $(basename "$s")"
    cat "$s"
done
echo "---------------------------------------------------------------"

# ---------------------------------------------------------------------------
# 6. Verify every script actually reproduces its bug
# ---------------------------------------------------------------------------

say "Running each generated script under gdb"
info "This is the real test: each artifact has to reproduce its failure on its"
info "own, with no agent involved."

passed=0
failed=0
errored=0

for s in "${SCRIPTS[@]}"; do
    name="$(basename "$s")"
    log="$OUTPUT_DIR/${name%.gdb}.out"

    echo "---------------------------------------------------------------"
    echo "### $name"
    set +e
    ( cd "$PROJECT_DIR" && gdb --batch -nx -x "$s" "./$PROGRAM" ) >"$log" 2>&1
    set -e

    if grep -qE 'SIG(SEGV|ABRT|BUS|FPE|ILL)' "$log"; then
        sig="$(grep -oE 'SIG[A-Z]+' "$log" | head -1)"
        printf '  \033[1;32mPASS\033[0m  reproduces %s\n' "$sig"
        passed=$(( passed + 1 ))
        # Not fatal: a script can reproduce the crash and still have a bad line
        # afterwards, which is worth knowing about.
        if grep -q 'Error in sourced command file' "$log"; then
            printf '  \033[1;33mNOTE\033[0m  but it errors partway through\n'
            errored=$(( errored + 1 ))
        fi
    else
        printf '  \033[1;31mFAIL\033[0m  no crash reproduced\n'
        failed=$(( failed + 1 ))
    fi
    info "gdb output: $log"
done
echo "---------------------------------------------------------------"

say "Result"
printf '  scripts generated : %d\n' "${#SCRIPTS[@]}"
printf '  reproduce a crash : %d\n' "$passed"
printf '  reproduce nothing : %d\n' "$failed"
(( errored > 0 )) && printf '  error partway     : %d\n' "$errored"
echo

if (( failed == 0 && passed > 0 )); then
    printf '\033[1;32mSUCCESS\033[0m  every generated script reproduces its bug\n'
    info "scripts: $OUTPUT_DIR"
    exit 0
elif (( passed > 0 )); then
    printf '\033[1;33mPARTIAL\033[0m  %d of %d scripts reproduce a bug\n' \
        "$passed" "${#SCRIPTS[@]}"
    info "A small model does not always get every script right. The ones that"
    info "passed are still usable artifacts."
    exit 1
else
    printf '\033[1;31mFAILED\033[0m   no script reproduced a crash\n'
    info ""
    info "This is usually the model, not the plumbing. Things to try:"
    info "  - re-run: a 4B model is nondeterministic and succeeds most but not"
    info "    all of the time"
    info "  - check the scripts above actually set the program's input"
    info "  - edit MODEL_HF to use a larger model"
    info "  - 'make probe-tools' to confirm tool calling still works"
    exit 1
fi
