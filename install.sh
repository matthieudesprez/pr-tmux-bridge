#!/usr/bin/env bash
# Install (or refresh) the pr-tmux-bridge launchd agent.
#
# Usage:
#   ./install.sh              # install/reload
#   ./install.sh --uninstall  # stop and remove the agent
#
# After install, paste userscript/pr-tmux-bridge.user.js into Tampermonkey
# (or your userscript manager) and visit a GitHub PR — the "→ tmux" button
# should appear next to the PR title.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_PATH="${REPO_ROOT}/daemon/pr_tmux_bridge.py"
PLIST_TEMPLATE="${REPO_ROOT}/launchd/be.lizy.pr-tmux-bridge.plist"
LABEL="be.lizy.pr-tmux-bridge"
PLIST_TARGET="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="${HOME}/Library/Logs/pr-tmux-bridge.log"

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

if [[ ! -x "${DAEMON_PATH}" ]]; then
    chmod +x "${DAEMON_PATH}"
fi

mkdir -p "${HOME}/Library/LaunchAgents" "$(dirname "${LOG_PATH}")"

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
