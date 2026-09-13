#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================================"
echo "  CIP Global MCP Setup"
echo "============================================================"
echo

# ----------------------------------------------------------
# 1. Install CIP in editable mode
# ----------------------------------------------------------
echo "[1/4] Installing CIP package (editable) ..."
pip install -e "$SCRIPT_DIR" >/dev/null 2>&1 || {
    echo "FAILED — pip install -e . returned an error."
    echo "Make sure Python 3.11+ and pip are on your PATH."
    exit 1
}
echo "      OK — cip-mcp is now on PATH."

command -v cip-mcp >/dev/null 2>&1 || {
    echo "WARNING: cip-mcp not found on PATH."
    echo "You may need to add ~/.local/bin to your PATH."
}

# ----------------------------------------------------------
# 2. Ensure .env exists
# ----------------------------------------------------------
echo
echo "[2/4] Checking .env ..."
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "      Created .env from .env.example — edit DATABASE_URL before first use."
else
    echo "      .env already exists."
fi

# ----------------------------------------------------------
# 3. Check DATABASE_URL
# ----------------------------------------------------------
echo
echo "[3/4] Checking DATABASE_URL ..."
if [ -z "${DATABASE_URL:-}" ]; then
    echo "      DATABASE_URL is not set in your environment."
    echo
    read -rp "      Enter your DATABASE_URL (or press Enter for default): " DB_URL
    DB_URL="${DB_URL:-postgresql://cip:cip@localhost:5432/cip_local}"

    SHELL_RC=""
    if [ -f "$HOME/.zshrc" ]; then
        SHELL_RC="$HOME/.zshrc"
    elif [ -f "$HOME/.bashrc" ]; then
        SHELL_RC="$HOME/.bashrc"
    elif [ -f "$HOME/.bash_profile" ]; then
        SHELL_RC="$HOME/.bash_profile"
    fi

    if [ -n "$SHELL_RC" ]; then
        echo "" >> "$SHELL_RC"
        echo "export DATABASE_URL=\"$DB_URL\"" >> "$SHELL_RC"
        echo "      Appended DATABASE_URL to $SHELL_RC"
        echo "      Run: source $SHELL_RC  (or open a new terminal)"
    else
        echo "      Could not find a shell rc file."
        echo "      Add this to your shell profile manually:"
        echo "        export DATABASE_URL=\"$DB_URL\""
    fi
    export DATABASE_URL="$DB_URL"
else
    echo "      Already set: $DATABASE_URL"
fi

# ----------------------------------------------------------
# 4. Register with Claude Code globally (user scope)
# ----------------------------------------------------------
echo
echo "[4/4] Registering CIP as a global MCP server in Claude Code ..."

# Remove previous registration (ignore errors)
claude mcp remove cip --scope user 2>/dev/null || true

claude mcp add --scope user cip -- "$SCRIPT_DIR/run-cip-mcp.sh"
if [ $? -ne 0 ]; then
    echo
    echo "FAILED — could not register with Claude Code."
    echo "Make sure the 'claude' CLI is installed:"
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi

echo
echo "============================================================"
echo "  Done! CIP is now a global MCP server."
echo
echo "  It will start automatically in every Claude Code session."
echo "  Verify with:  claude mcp list"
echo "============================================================"
