#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIRECTORY="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_STATE_DIRECTORY="${XDG_DATA_HOME:-${HOME:?HOME is not set}/.local/share}/telemax/docker"
STATE_DIRECTORY="${TELEMAX_DOCKER_STATE:-$DEFAULT_STATE_DIRECTORY}"
ACTION="${1:-help}"
[[ $# -eq 0 ]] || shift
UI_INNER_WIDTH=42
TEMPORARY_PATH=""

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
  box_line 'TELEMAX · DOCKER' "$BOLD"
  box_line 'optional isolated deployment'
  box_rule '╰' '╯'
}

step() { printf '\n%sШаг %s из 3%s · %s\n\n' "$BOLD" "$1" "$RESET" "$2"; }

on_interrupt() {
  printf '\n'
  note 'Настройка остановлена. Повторный docker.sh setup продолжит безопасно.'
  exit 130
}
trap on_interrupt INT

usage() {
  cat <<'EOF'
Optional Docker deployment for Telemax.

Usage:
  ./docker.sh setup [--state-dir PATH]
  ./docker.sh up|down|restart|status|logs|doctor|build

Docker is optional. The default installation is ./install.sh with a user
systemd service. Persistent config, sessions, SQLite and media stay in one host
directory; `down` and image rebuilds never delete it.
EOF
}

need_value() { [[ $# -ge 2 && -n "$2" ]] || fail "$1 requires a value"; }

parse_setup_options() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --state-dir) need_value "$@"; STATE_DIRECTORY="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) fail "unknown setup option: $1" ;;
    esac
  done
}

require_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker Engine is required for this optional path"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose is unavailable"
  docker info >/dev/null 2>&1 || fail "Docker daemon is unavailable for the current user"
}

environment_path() { printf '%s/compose.env\n' "$STATE_DIRECTORY"; }

compose() {
  docker compose \
    --env-file "$(environment_path)" \
    --file "$SCRIPT_DIRECTORY/compose.yaml" \
    "$@"
}

compose_quote() {
  local value="$1"
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "state path contains a newline"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "$value"
}

prepare_state() {
  [[ "$STATE_DIRECTORY" == /* ]] || fail "--state-dir must be an absolute path"
  if [[ -e "$STATE_DIRECTORY" && ( ! -d "$STATE_DIRECTORY" || -L "$STATE_DIRECTORY" ) ]]; then
    fail "state target must be a real directory: $STATE_DIRECTORY"
  fi
  mkdir -p -- "$STATE_DIRECTORY" "$STATE_DIRECTORY/home"
  chmod 0700 "$STATE_DIRECTORY" "$STATE_DIRECTORY/home"
  STATE_DIRECTORY="$(CDPATH= cd -- "$STATE_DIRECTORY" && pwd -P)"

  TEMPORARY_PATH="$(mktemp "$STATE_DIRECTORY/.compose.env.XXXXXX")"
  chmod 0600 "$TEMPORARY_PATH"
  {
    printf 'COMPOSE_PROJECT_NAME=telemax\n'
    printf 'TELEMAX_STATE_DIR=%s\n' "$(compose_quote "$STATE_DIRECTORY")"
    printf 'TELEMAX_UID=%s\n' "$(id -u)"
    printf 'TELEMAX_GID=%s\n' "$(id -g)"
    printf 'TELEMAX_IMAGE=telemax:local\n'
  } > "$TEMPORARY_PATH"
  mv -f -- "$TEMPORARY_PATH" "$(environment_path)"
  TEMPORARY_PATH=""
}

require_setup() {
  [[ -f "$(environment_path)" && ! -L "$(environment_path)" ]] || \
    fail "Docker state is not configured; run ./docker.sh setup"
}

setup() {
  banner
  step 1 "Docker runtime"
  require_docker
  ok "$(docker --version)"
  ok "$(docker compose version)"
  note "No ports and no Docker socket are exposed to Telemax."

  step 2 "Persistent state"
  prepare_state
  ok "$STATE_DIRECTORY"
  note "Config, credentials, MAX/Telegram sessions and SQLite stay on the host."

  say "Building the frozen Telemax image"
  compose build telemax

  if [[ ! -s "$STATE_DIRECTORY/config.yaml" ]]; then
    step 3 "Telegram QR → Guardian"
    compose run --rm --no-deps setup
    [[ -s "$STATE_DIRECTORY/config.yaml" ]] || fail "setup did not create config.yaml"
  else
    step 3 "Existing installation"
    ok "Keeping the existing config and sessions"
  fi

  say "Starting Guardian and the bridge supervisor"
  compose up --detach --wait --wait-timeout 120 telemax
  printf '\n'
  ok "Telemax container is healthy"
  note "Дальше терминал не нужен: вход в MAX и управление находятся в Guardian."
}

if [[ "$ACTION" != "help" && "$ACTION" != "-h" && "$ACTION" != "--help" ]]; then
  parse_setup_options "$@"
  set --
fi

case "$ACTION" in
  setup) setup "$@" ;;
  up)
    require_docker; require_setup
    compose up --detach --wait --wait-timeout 120 telemax
    ;;
  down)
    require_docker; require_setup
    compose down
    note "Persistent state kept at $STATE_DIRECTORY"
    ;;
  restart)
    require_docker; require_setup
    compose restart telemax
    ;;
  status)
    require_docker; require_setup
    compose ps
    ;;
  logs)
    require_docker; require_setup
    compose logs --follow --tail 200 telemax
    ;;
  doctor)
    require_docker; require_setup
    compose exec telemax /app/.venv/bin/telemax \
      --config /state/config.yaml healthcheck
    ;;
  build)
    require_docker
    prepare_state
    compose build telemax
    ;;
  help|-h|--help) usage ;;
  *) fail "unknown action: $ACTION" ;;
esac
