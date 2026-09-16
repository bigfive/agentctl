#!/bin/sh
# Install agentctl and its per-harness skills.
#
#   ./install.sh              install the CLI and every skill whose harness is present
#   ./install.sh --opencode-plugin  also install the optional OpenCode tool
#   ./install.sh --skills     skills only
#   ./install.sh --cli        the CLI only
#   ./install.sh --uninstall  remove what this script installed
#
# The CLI and skills are symlinked, so a `git pull` updates them without
# reinstalling. The OpenCode plugin is copied because OpenCode resolves its
# dependencies relative to the installed plugin path, not a symlink target.
# Its CLI helper is linked beside that copied plugin so it does not depend on
# the PATH inherited by a GUI-launched OpenCode server.

set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
NAME=agentctl
DO_CLI=yes
DO_SKILLS=yes
DO_OPENCODE_PLUGIN=no
UNINSTALL=no

for arg in "$@"; do
    case "$arg" in
        --cli) DO_SKILLS=no ;;
        --skills) DO_CLI=no ;;
        --opencode-plugin) DO_OPENCODE_PLUGIN=yes ;;
        --uninstall) UNINSTALL=yes ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 64 ;;
    esac
done

# Pick the first writable directory on PATH that a shell would actually find.
pick_bindir() {
    for dir in "$HOME/.local/bin" /usr/local/bin /opt/homebrew/bin; do
        case ":$PATH:" in
            *":$dir:"*) [ -d "$dir" ] && [ -w "$dir" ] && { echo "$dir"; return 0; } ;;
        esac
    done
    return 1
}

# Where each harness reads user-level skills from. A harness is treated as
# present when its parent directory already exists.
#
# These directories are not private to one harness. Measured on the installed
# versions:
#
#   codex     reads ~/.codex/skills and ~/.agents/skills
#   claude    reads ~/.claude/skills
#   cursor    reads ~/.cursor/skills
#   opencode  reads ~/.config/opencode/skills, ~/.claude/skills, ~/.agents/skills
#
# So a harness can pick up a skill installed for another one. There is a single
# skill, written to route by harness, and every install below is a symlink to
# that same file. A harness that sees it twice therefore sees one set of
# instructions, never two conflicting ones.
#
# ~/.agents/skills is deliberately not used: it would add a second copy for
# both Codex and OpenCode without reaching any harness the list below misses.
skill_dir() {
    case "$1" in
        claude) echo "$HOME/.claude/skills" ;;
        codex) echo "$HOME/.codex/skills" ;;
        cursor) echo "$HOME/.cursor/skills" ;;
        opencode) echo "$HOME/.config/opencode/skills" ;;
    esac
}

harness_root() {
    case "$1" in
        claude) echo "$HOME/.claude" ;;
        codex) echo "$HOME/.codex" ;;
        cursor) echo "$HOME/.cursor" ;;
        opencode) echo "$HOME/.config/opencode" ;;
    esac
}

if [ "$UNINSTALL" = yes ]; then
    for harness in claude codex cursor opencode; do
        target="$(skill_dir "$harness")/$NAME"
        if [ -L "$target" ]; then
            rm "$target"
            echo "removed  $target"
        fi
    done
    plugin="$HOME/.config/opencode/plugins/agentctl.mjs"
    if { [ -L "$plugin" ] && [ "$(readlink "$plugin")" = "$ROOT/opencode/agentctl.mjs" ]; } || { [ -f "$plugin" ] && cmp -s "$ROOT/opencode/agentctl.mjs" "$plugin"; }; then
        rm "$plugin"
        echo "removed  $plugin"
    fi
    plugin_bin="$HOME/.config/opencode/plugins/agentctl/bin/$NAME"
    if [ -L "$plugin_bin" ] && [ "$(readlink "$plugin_bin")" = "$ROOT/bin/$NAME" ]; then
        rm "$plugin_bin"
        rmdir "$(dirname "$plugin_bin")" 2>/dev/null || true
        rmdir "$(dirname "$(dirname "$plugin_bin")")" 2>/dev/null || true
        echo "removed  $plugin_bin"
    fi
    for dir in "$HOME/.local/bin" /usr/local/bin /opt/homebrew/bin; do
        link="$dir/$NAME"
        if [ -L "$link" ] && [ "$(readlink "$link")" = "$ROOT/bin/$NAME" ]; then
            rm "$link"
            echo "removed  $link"
        fi
    done
    echo "The job store at ${AGENTCTL_HOME:-$HOME/.agentctl} was left alone."
    exit 0
fi

if [ "$DO_OPENCODE_PLUGIN" = yes ]; then
    plugin_dir="$HOME/.config/opencode/plugins"
    mkdir -p "$plugin_dir"
    if [ -L "$plugin_dir/agentctl.mjs" ] && [ "$(readlink "$plugin_dir/agentctl.mjs")" = "$ROOT/opencode/agentctl.mjs" ]; then
        rm "$plugin_dir/agentctl.mjs"
    fi
    cp "$ROOT/opencode/agentctl.mjs" "$plugin_dir/agentctl.mjs"
    echo "installed $plugin_dir/agentctl.mjs"
    plugin_bin_dir="$plugin_dir/agentctl/bin"
    mkdir -p "$plugin_bin_dir"
    ln -sfn "$ROOT/bin/$NAME" "$plugin_bin_dir/$NAME"
    echo "installed $plugin_bin_dir/$NAME"
fi

if [ "$DO_CLI" = yes ]; then
    if ! bindir=$(pick_bindir); then
        echo "No writable directory on PATH." >&2
        echo "Create one and re-run, for example:" >&2
        echo "  mkdir -p ~/.local/bin && export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
        exit 75
    fi
    ln -sf "$ROOT/bin/$NAME" "$bindir/$NAME"
    echo "installed $bindir/$NAME"
fi

if [ "$DO_SKILLS" = yes ]; then
    installed=0
    for harness in claude codex cursor opencode; do
        root=$(harness_root "$harness")
        if [ ! -d "$root" ]; then
            echo "skipped  $harness (not installed)"
            continue
        fi
        dir=$(skill_dir "$harness")
        mkdir -p "$dir"
        ln -sfn "$ROOT/skills/$NAME" "$dir/$NAME"
        echo "installed $dir/$NAME"
        installed=$((installed + 1))
    done
    [ "$installed" -gt 0 ] || echo "No harnesses found, so no skills were installed."
fi

echo
echo "Check it with: $NAME daemon status"
