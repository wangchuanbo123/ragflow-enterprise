#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-up}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed."
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is not available."
  docker info >/dev/null 2>&1 || die "Docker daemon is not running or the current user cannot access it."
}

env_value() {
  local key="$1"
  awk -v wanted="${key}" '
    index($0, wanted "=") == 1 {
      sub(/^[^=]*=/, "")
      gsub(/^[[:space:]\047\"]+|[[:space:]\047\"]+$/, "")
      value=$0
    }
    END { print value }
  ' "${ENV_FILE}"
}

require_env_file() {
  if [[ -f "${ENV_FILE}" ]]; then
    return
  fi
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  die "Created ${ENV_FILE}. Replace every placeholder, then run this command again."
}

validate_env() {
  local required=(
    POSTGRES_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD JWT_SECRET_KEY
    INITIAL_ADMIN_PASSWORD CORS_ORIGINS ANSWER_LLM_MODEL ANSWER_LLM_BASE_URL
    ANSWER_LLM_API_KEY EMBEDDING_MODEL EMBEDDING_BASE_URL EMBEDDING_API_KEY
  )
  local key value failed=0

  for key in "${required[@]}"; do
    value="$(env_value "${key}")"
    if [[ -z "${value}" || "${value}" == *replace* || "${value}" == *your-* || "${value}" == *example.com* ]]; then
      printf 'Invalid or placeholder value: %s\n' "${key}" >&2
      failed=1
    fi
  done

  value="$(env_value JWT_SECRET_KEY)"
  if (( ${#value} < 48 )); then
    printf 'JWT_SECRET_KEY must contain at least 48 characters.\n' >&2
    failed=1
  fi
  value="$(env_value INITIAL_ADMIN_PASSWORD)"
  if [[ "$(env_value ALLOW_WEAK_INITIAL_ADMIN_PASSWORD)" != "true" ]] && (( ${#value} < 12 )); then
    printf 'INITIAL_ADMIN_PASSWORD must contain at least 12 characters.\n' >&2
    failed=1
  fi
  value="$(env_value APP_PORT)"
  [[ -z "${value}" || "${value}" =~ ^[0-9]+$ ]] || {
    printf 'APP_PORT must be numeric.\n' >&2
    failed=1
  }
  (( failed == 0 )) || die "Enterprise environment validation failed."
}

wait_for_api() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose exec -T api curl -fsS http://localhost:8080/api/v1/health >/dev/null 2>&1; then
      printf 'API health check passed.\n'
      return
    fi
    sleep 3
  done
  compose logs --tail 150 api worker beat >&2 || true
  die "The API did not become healthy within 180 seconds."
}

validate_runtime() {
  wait_for_api
  compose exec -T api python -m scripts.validate_enterprise_profile --write-check
}

case "${ACTION}" in
  up)
    require_docker
    require_env_file
    validate_env
    compose config --quiet
    compose up -d --build
    validate_runtime
    printf 'Enterprise RAG is available on port %s.\n' "$(env_value APP_PORT)"
    ;;
  down)
    require_docker
    require_env_file
    compose down
    printf 'Containers were removed; database and object-store volumes were preserved.\n'
    ;;
  restart)
    require_docker
    require_env_file
    validate_env
    compose config --quiet
    compose up -d --build --force-recreate
    validate_runtime
    ;;
  status)
    require_docker
    require_env_file
    compose ps
    ;;
  logs)
    require_docker
    require_env_file
    compose logs -f --tail 200 api worker beat
    ;;
  validate)
    require_docker
    require_env_file
    validate_env
    compose config --quiet
    validate_runtime
    ;;
  *)
    die "Unknown action '${ACTION}'. Use: up, down, restart, status, logs, or validate."
    ;;
esac
