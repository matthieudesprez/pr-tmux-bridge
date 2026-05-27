#!/usr/bin/env bash
# Install (or refresh) the pr-tmux-bridge launchd agent.
#
# Usage:
#   ./install.sh              # install/reload
#   ./install.sh --uninstall  # stop and remove the agent
#
# After install, open http://127.0.0.1:47811/userscript.js to install the
# userscript (it carries the auth token), then visit a GitHub PR — the
# "● tmux" button should appear next to the PR title.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PATH="${REPO_ROOT}/daemon/pr_tmux_bridge.py"
PLIST_TEMPLATE="${REPO_ROOT}/launchd/be.lizy.pr-tmux-bridge.plist"
LABEL="be.lizy.pr-tmux-bridge"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="${HOME}/Library/Logs/pr-tmux-bridge.log"
CONFIG_DIR="${HOME}/.config/pr-tmux-bridge"
CONFIG_FILE="${CONFIG_DIR}/config"

uninstall() {
    if [[ -f "${PLIST_TARGET}" ]]; then
        launchctl unload "${PLIST_TARGET}" 2>/dev/null || true
        rm -f "${PLIST_TARGET}"
        echo "removed ${PLIST_TARGET}"
    else
        echo "nothing to uninstall (${PLIST_TARGET} not present)"
    fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

chmod +x "${DAEMON_PATH}" "${REPO_ROOT}/scripts/create-worktree.sh"

mkdir -p "${HOME}/Library/LaunchAgents" "$(dirname "${LOG_PATH}")" "${CONFIG_DIR}"

# Seed the config file if the user hasn't created one yet. CREATE_COMMAND defaults to
# the bundled git-worktree + tmux script; edit it to point at your own worktree manager.
if [[ ! -f "${CONFIG_FILE}" ]]; then
    cat > "${CONFIG_FILE}" <<EOF
# pr-tmux-bridge configuration. Format: KEY=value, '#' starts a comment.
# An env var PR_TMUX_BRIDGE_<KEY> overrides the matching line. Read live (no restart).

# Command that provisions a worktree + tmux session for a branch.
# Tokens {branch} and {repo_root} are substituted as whole argv tokens (no shell).
# Replace with your own worktree manager if you use one.
CREATE_COMMAND=${REPO_ROOT}/scripts/create-worktree.sh {branch}

# Terminal app to focus / spawn (e.g. Ghostty, iTerm, Terminal, WezTerm).
# TERMINAL_APP=Ghostty

# Search roots for local clones (os.pathsep-separated). Repos found as <root>/<name>
# or by matching the clone's origin URL.
# WORKSPACE=~/workspace

# Worktree location used by scripts/create-worktree.sh.
# WORKTREE_BASE=~/wt

# JSON map for clones not found by the above.
# REPOS={"owner/repo": "/path/to/clone"}
EOF
    echo "seeded ${CONFIG_FILE}"
fi

# Substitute placeholders into the plist template.
sed \
    -e "s|__DAEMON_PATH__|${DAEMON_PATH}|g" \
    -e "s|__LOG_PATH__|${LOG_PATH}|g" \
    -e "s|__PATH__|${PATH}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_TARGET}"

# Reload if already loaded.
launchctl unload "${PLIST_TARGET}" 2>/dev/null || true
launchctl load "${PLIST_TARGET}"

echo "installed ${PLIST_TARGET}"
echo "logs:     ${LOG_PATH}"
echo
echo "Testing /health..."
sleep 1
if curl -sf http://127.0.0.1:47811/health >/dev/null; then
    echo "daemon is up ✓"
else
    echo "daemon did not respond on http://127.0.0.1:47811 — check ${LOG_PATH}"
    exit 1
fi
echo
echo "Now install the userscript (Tampermonkey will prompt to confirm):"
echo "    open 'http://127.0.0.1:47811/userscript.js'"
echo
echo "Installing from that URL bakes in the auth token. A hand-pasted copy of the"
echo "raw file has a placeholder token and will be rejected with 401."
