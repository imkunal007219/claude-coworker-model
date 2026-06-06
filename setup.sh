#!/usr/bin/env bash
set -euo pipefail

# Claude Coworker Model — Setup Script
# Creates venv, installs deps, copies tools to ~/.local/bin/ with correct shebang

INSTALL_DIR="${HOME}/.local/share/claude-coworker"
BIN_DIR="${HOME}/.local/bin"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="${INSTALL_DIR}/venv/bin/python3"

echo "=== Claude Coworker Model Setup ==="
echo ""

# 1. Create venv
echo "[1/4] Creating Python venv at ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
python3 -m venv "${INSTALL_DIR}/venv"
source "${INSTALL_DIR}/venv/bin/activate"

# 2. Install deps
echo "[2/4] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "${SCRIPT_DIR}/requirements.txt"

# 3. Install tools with correct shebang pointing to venv Python
echo "[3/4] Installing tools to ${BIN_DIR}..."
mkdir -p "${BIN_DIR}"

# Shared library: the Coworker Memory cache. Copied next to the tools so that
# `import coworker_memory` resolves from the script's own directory.
cp "${SCRIPT_DIR}/tools/coworker_memory.py" "${BIN_DIR}/coworker_memory.py"
echo "  ✓ coworker_memory.py (shared cache library)"

# ask-kimi and kimi-write need the venv (openai package)
for tool in ask-kimi kimi-write; do
    sed "1s|#!/usr/bin/env python3|#!${VENV_PYTHON}|" \
        "${SCRIPT_DIR}/tools/${tool}" > "${BIN_DIR}/${tool}"
    chmod +x "${BIN_DIR}/${tool}"
    echo "  ✓ ${tool} (using venv python)"
done

# extract-chat and coworker-memory use only stdlib — symlinks are fine
for tool in extract-chat coworker-memory; do
    chmod +x "${SCRIPT_DIR}/tools/${tool}"
    ln -sf "${SCRIPT_DIR}/tools/${tool}" "${BIN_DIR}/${tool}"
    echo "  ✓ ${tool} (stdlib only)"
done

# 4. Check API key
echo "[4/4] Checking environment..."
if [ -z "${WORKER_API_KEY:-}" ] && [ -z "${MOONSHOT_API_KEY:-}" ]; then
    echo ""
    echo "⚠  No API key found. Set one of these in your shell profile:"
    echo ""
    echo "  # Kimi (Moonshot AI)"
    echo "  export WORKER_API_KEY=\"your-key-here\""
    echo "  export WORKER_BASE_URL=\"https://api.moonshot.ai/v1\""
    echo "  export WORKER_MODEL=\"kimi-k2.5\""
    echo ""
    echo "  # OR DeepSeek"
    echo "  export WORKER_API_KEY=\"your-key-here\""
    echo "  export WORKER_BASE_URL=\"https://api.deepseek.com/v1\""
    echo "  export WORKER_MODEL=\"deepseek-chat\""
    echo ""
    echo "  # OR Ollama (local, free)"
    echo "  export WORKER_BASE_URL=\"http://localhost:11434/v1\""
    echo "  export WORKER_MODEL=\"qwen2.5:32b\""
    echo ""
else
    echo "  ✓ API key found"
fi

echo ""
echo "=== Done! ==="
echo ""

# Guard against PATH shadowing: if another ask-kimi earlier on PATH wins, the
# user will silently run a different (possibly memory-less) build. Catch it here.
RESOLVED="$(command -v ask-kimi || true)"
if [ -n "${RESOLVED}" ] && [ "${RESOLVED}" != "${BIN_DIR}/ask-kimi" ]; then
    echo "⚠  PATH SHADOW: 'ask-kimi' resolves to ${RESOLVED}, not ${BIN_DIR}/ask-kimi."
    echo "   Another copy earlier on PATH will run instead of the one just installed."
    echo "   Fix: put ${BIN_DIR} first on PATH, or replace ${RESOLVED}."
    echo ""
fi

echo "Make sure ${BIN_DIR} is on your PATH, then try:"
echo "  ask-kimi --paths some_file.py --question 'what does this do?'"
echo ""
echo "Copy CLAUDE.md.template into your project's CLAUDE.md for auto-routing."
