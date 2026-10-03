#!/usr/bin/env sh
# Install mycodex from this directory. Everything mycodex owns stays in this directory;
# the only things created outside it are the PATH link below and (later, by
# `mycodex remote start`) the systemd unit link ~/.config/systemd/user/mycodex-remote.service.
# Accounts live where Codex keeps its state: ~/.codex/profiles/<name>.
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
chmod -R go-w "$ROOT"
chmod +x "$ROOT/bin/mycodex" "$ROOT/install.sh"
mkdir -p "$ROOT/state" && chmod 700 "$ROOT/state"
mkdir -p "$HOME/.local/bin"
ln -sfn "$ROOT/bin/mycodex" "$HOME/.local/bin/mycodex"
echo "linked $HOME/.local/bin/mycodex -> $ROOT/bin/mycodex"
command -v codex >/dev/null 2>&1 || [ -x "$HOME/.local/bin/codex" ] || echo "warning: the official codex CLI was not found"
command -v python3 >/dev/null 2>&1 || echo "warning: python3 was not found"
