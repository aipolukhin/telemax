#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
UV_VERSION="0.12.7"
TOOLS_DIRECTORY="$SCRIPT_DIRECTORY/.tools"
CONFIG_PATH="$SCRIPT_DIRECTORY/config.yaml"
INSTANCE=""
MANUAL_GUARDIAN=0
SYNC_DEPENDENCIES=1
TEMPORARY_PATH=""
UI_INNER_WIDTH=42

if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
  CYAN=$'\033[36m'
  GREEN=$'\033[32m'
  YELLOW=$'\033[33m'
  RED=$'\033[31m'
  BOLD=$'\033[1m'
  DIM=$'\033[2m'
  RESET=$'\033[0m'
else
  CYAN="" GREEN="" YELLOW="" RED="" BOLD="" DIM="" RESET=""
fi

cleanup() {
  if [[ -n "$TEMPORARY_PATH" && -f "$TEMPORARY_PATH" ]]; then
    rm -f -- "$TEMPORARY_PATH"
  fi
}
trap cleanup EXIT

fail() { printf '%s✗%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }
ok() { printf '%s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
say() { printf '%s→%s %s\n' "$CYAN" "$RESET" "$*"; }
note() { printf '%s%s%s\n' "$DIM" "$*" "$RESET"; }

box_rule() {
  local left="$1" right="$2" rule
  printf -v rule '%*s' "$UI_INNER_WIDTH" ''
  rule="${rule// /─}"
  printf '%s%s%s%s%s\n' "$CYAN" "$left" "$rule" "$right" "$RESET"
}

box_line() {
  local text="$1" style="${2:-}" width left right
  width="${#text}"
  (( width <= UI_INNER_WIDTH )) || fail "installer frame line is too wide"
  left=$(( (UI_INNER_WIDTH - width) / 2 ))
  right=$(( UI_INNER_WIDTH - width - left ))
  printf '%s│%s%*s%s%s%s%*s%s│%s\n' \
    "$CYAN" "$RESET" "$left" '' "$style" "$text" "$RESET" "$right" '' "$CYAN" "$RESET"
}

banner() {
  box_rule '╭' '╮'
  box_line 'TELEMAX' "$BOLD"
  box_line 'Telegram ↔ MAX · bot-first setup'
  box_rule '╰' '╯'
}

step() { printf '\n%sШаг %s из 3%s · %s\n\n' "$BOLD" "$1" "$RESET" "$2"; }

on_interrupt() {
  printf '\n'
  note 'Настройка остановлена. Повторный ./install.sh продолжит безопасно.'
  exit 130
}
trap on_interrupt INT

usage() {
  cat <<'EOF'
Install Telemax for the current Linux user (default deployment).

Usage:
  ./install.sh [options]

Options:
  --config PATH          Config path (default: config.yaml in this checkout)
  --instance NAME        Separate systemd user unit for a second installation
  --manual-guardian      Recovery: paste an existing Guardian token
  --skip-sync            Reuse the existing .venv without dependency sync
  -h, --help             Show this help

The normal path stores no secret in command history. It asks for Telegram
api_id/api_hash, shows a QR, creates Guardian through @BotFather, starts a user
systemd service, and moves MAX login into the Guardian chat.
EOF
}

need_value() { [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) need_value "$@"; CONFIG_PATH="$2"; shift 2 ;;
    --instance) need_value "$@"; INSTANCE="$2"; shift 2 ;;
    --manual-guardian) MANUAL_GUARDIAN=1; shift ;;
    --skip-sync) SYNC_DEPENDENCIES=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || fail "the host installer currently supports Linux only"
[[ -f "$SCRIPT_DIRECTORY/pyproject.toml" && -f "$SCRIPT_DIRECTORY/uv.lock" ]] || \
  fail "run install.sh from a complete Telemax checkout"

banner
step 1 "Runtime"

UV_BINARY="$(command -v uv 2>/dev/null || true)"
UV_FOUND=""
if [[ -n "$UV_BINARY" ]]; then
  UV_FOUND="$($UV_BINARY --version 2>/dev/null | awk '{print $2}' || true)"
fi
if [[ "$UV_FOUND" != "$UV_VERSION" ]]; then
  command -v curl >/dev/null 2>&1 || fail "curl is required to install the pinned runtime"
  mkdir -p -- "$TOOLS_DIRECTORY"
  chmod 0700 "$TOOLS_DIRECTORY"
  say "Installing private uv $UV_VERSION (without sudo)"
  TEMPORARY_PATH="$(mktemp "${TMPDIR:-/tmp}/telemax-uv.XXXXXX")"
  curl --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    "https://astral.sh/uv/$UV_VERSION/install.sh" \
    --output "$TEMPORARY_PATH"
  UV_UNMANAGED_INSTALL="$TOOLS_DIRECTORY" sh "$TEMPORARY_PATH" >/dev/null
  rm -f -- "$TEMPORARY_PATH"
  TEMPORARY_PATH=""
  UV_BINARY="$TOOLS_DIRECTORY/uv"
fi
[[ -x "$UV_BINARY" ]] || fail "uv $UV_VERSION was not installed"
[[ "$($UV_BINARY --version | awk '{print $2}')" == "$UV_VERSION" ]] || \
  fail "Telemax requires uv $UV_VERSION"
ok "uv $UV_VERSION"
note "Python 3.12 will be reused or downloaded into your user directory by uv."

step 2 "Dependencies"
if [[ $SYNC_DEPENDENCIES -eq 1 ]]; then
  say "Installing the frozen production environment"
  (
    cd -- "$SCRIPT_DIRECTORY"
    "$UV_BINARY" sync --locked --no-dev --python 3.12
  )
else
  [[ -x "$SCRIPT_DIRECTORY/.venv/bin/telemax" ]] || \
    fail "--skip-sync needs an existing .venv/bin/telemax"
  note "Keeping the existing virtual environment."
fi
ok "$($SCRIPT_DIRECTORY/.venv/bin/python --version)"

step 3 "Telegram QR → Guardian"
arguments=(--config "$CONFIG_PATH" setup)
[[ -z "$INSTANCE" ]] || arguments+=(--instance "$INSTANCE")
[[ $MANUAL_GUARDIAN -eq 0 ]] || arguments+=(--manual-guardian)

(
  cd -- "$SCRIPT_DIRECTORY"
  "$SCRIPT_DIRECTORY/.venv/bin/telemax" "${arguments[@]}"
)

printf '\n'
ok "Telemax установлен как user service"
note "Дальше всё делается в Guardian: вход в MAX, выбор диалогов, статус и restart."
