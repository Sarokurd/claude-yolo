#!/usr/bin/env bash
# common.sh — Shared utilities for claude-yolo

# Colors (disabled if not a terminal)
if [[ -t 2 ]]; then
    _RED='\033[0;31m'
    _YELLOW='\033[0;33m'
    _BLUE='\033[0;34m'
    _RESET='\033[0m'
else
    _RED='' _YELLOW='' _BLUE='' _RESET=''
fi

log_info() {
    printf "${_BLUE}[%s INFO]${_RESET} %s\n" "$(date '+%H:%M:%S')" "$*" >&2
}

log_warn() {
    printf "${_YELLOW}[%s WARN]${_RESET} %s\n" "$(date '+%H:%M:%S')" "$*" >&2
}

log_error() {
    printf "${_RED}[%s ERROR]${_RESET} %s\n" "$(date '+%H:%M:%S')" "$*" >&2
}

check_prereqs() {
    local missing=0
    if ! command -v tmux &>/dev/null; then
        log_error "tmux is not installed"
        missing=1
    fi
    if ! command -v claude &>/dev/null; then
        log_error "claude (Claude Code CLI) is not installed"
        missing=1
    fi
    return $missing
}

# Return a writable directory for audit logs.
# Prefers /tmp; falls back to ~/.claude-yolo/logs (e.g. Termux where /tmp is not writable).
log_dir() {
    if touch /tmp/.claude-yolo-probe 2>/dev/null; then
        rm -f /tmp/.claude-yolo-probe
        echo "/tmp"
    else
        local d="$HOME/.claude-yolo/logs"
        mkdir -p "$d" 2>/dev/null || true
        echo "$d"
    fi
}

resolve_script_dir() {
    local src="${BASH_SOURCE[1]:-$0}"
    local dir
    dir="$(cd "$(dirname "$src")" && pwd)"
    echo "$dir"
}

# Ensure a directory is trusted by Claude Code so the "trust this folder" prompt is skipped.
# Adds the directory to ~/.claude.json under projects with hasTrustDialogAccepted: true.
ensure_trusted() {
    local dir="$1"
    local settings="$HOME/.claude.json"

    # Create settings file if it doesn't exist
    if [[ ! -f "$settings" ]]; then
        cat > "$settings" <<EOJSON
{
  "projects": {}
}
EOJSON
    fi

    # Check if already trusted (project entry with hasTrustDialogAccepted)
    if command -v python3 &>/dev/null; then
        local already
        already="$(python3 -c "
import json, sys
with open('$settings') as f:
    s = json.load(f)
p = s.get('projects', {}).get('$dir', {})
print('yes' if p.get('hasTrustDialogAccepted') else 'no')
" 2>/dev/null)" || already="no"

        if [[ "$already" == "yes" ]]; then
            return 0
        fi

        # Add project entry with hasTrustDialogAccepted
        python3 -c "
import json
with open('$settings') as f:
    s = json.load(f)

projects = s.setdefault('projects', {})
proj = projects.setdefault('$dir', {})
proj['hasTrustDialogAccepted'] = True
proj.setdefault('allowedTools', [])

with open('$settings', 'w') as f:
    json.dump(s, f, indent=2)
    f.write('\n')
" 2>/dev/null && log_info "Auto-trusted directory: $dir" || \
            log_warn "Could not auto-trust directory: $dir (update ~/.claude.json manually)"
    else
        log_warn "python3 not found — cannot auto-trust directory. You may see a 'trust this folder' prompt."
    fi
}
