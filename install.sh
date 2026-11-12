#!/usr/bin/env bash
# SupportAgent local install: one command from a fresh clone to a running agent.
#
#   ./install.sh                 set up: check this machine, build, ask the setup
#                                questions, calibrate the model, start everything
#   ./install.sh doctor          diagnose a broken install, with a fix for each problem
#   ./install.sh reconfigure     change answers (LLM, ServiceNow, Confluence, ...)
#   ./install.sh start | stop | restart | status | logs [service]
#   ./install.sh calibrate       re-run the model calibration (after a model change)
#   ./install.sh onboard         onboard the system in local/system.yaml (read-only;
#                                re-run after you act on what it lists)
#   ./install.sh uninstall       stop and delete the containers and data (keeps local/)
#
# Options for set-up: --answers FILE (JSON, no questions), --skip-calibration.
# Needs only Docker (Desktop on macOS/Windows+WSL2, Engine + Compose v2 on Linux).
# Everything else runs in containers; secrets stay in local/.env (chmod 600).
set -euo pipefail

cd "$(dirname "$0")"
ROOT=$(pwd)
LOCAL="$ROOT/local"
ENV_FILE="$LOCAL/.env"
COMPOSE_FILE="$ROOT/docker/local/compose.yaml"
# One install per machine by default. COMPOSE_PROJECT_NAME and SA_IMAGE give a
# second checkout (or a test) its own containers, volumes and image.
PROJECT=${COMPOSE_PROJECT_NAME:-supportagent-local}
IMAGE=${SA_IMAGE:-supportagent:local}
export COMPOSE_PROJECT_NAME=$PROJECT SA_IMAGE=$IMAGE
MIN_COMPOSE=2.20
export SA_UID SA_GID
SA_UID=$(id -u)
SA_GID=$(id -g)

# --- output -----------------------------------------------------------------------

if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; D=""; N=""
fi
step() { printf '\n%s==> %s%s\n' "$B" "$1" "$N"; }
ok() { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
bad() { printf '  %s✗%s %s\n' "$R" "$N" "$1"; }
fix() { printf '    %s→ %s%s\n' "$D" "$1" "$N"; }
die() { bad "$1"; [ $# -gt 1 ] && fix "$2"; exit 1; }

# --- helpers ----------------------------------------------------------------------

compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# Compose's own progress goes to a log; it is shown only when something fails.
up() {
  local log="$LOCAL/compose-up.log"
  mkdir -p "$LOCAL"
  if ! compose up -d --quiet-pull --remove-orphans "$@" >"$log" 2>&1; then
    grep -iE "error|fail|denied|refused" "$log" | tail -5 | sed 's/^/    /'
    return 1
  fi
}

# One value from local/.env, without shell-parsing the file (values are single-quoted).
envval() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed "s/^'\(.*\)'$/\1/"
}

version_ge() { # version_ge 2.24.1 2.20 -> true
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -t. -k1,1n -k2,2n -k3,3n | head -1)" = "$2" ]
}

port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
  fi
}

free_port() { # the first free port from $1 upwards
  p=$1
  while port_in_use "$p"; do p=$((p + 1)); done
  echo "$p"
}

file_mode() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1"; }

# Containers of this project that another checkout created: never touch them
# (an uninstall here would delete that install's database).
guard_owner() {
  local other
  other=$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null |
    sort -u | grep -vx "$ROOT/docker/local" | head -1 || true)
  [ -z "$other" ] || die "the SupportAgent install on this machine belongs to another checkout: ${other%/docker/local}" \
    "run ./install.sh from there, or give this one its own: COMPOSE_PROJECT_NAME=<name> SA_IMAGE=supportagent:<name> ./install.sh"
}

stack_running() { [ -f "$ENV_FILE" ] && [ -n "$(compose ps -q app 2>/dev/null)" ]; }
# Any container of this install, running or not (its ports are ours to reuse).
stack_exists() { [ -f "$ENV_FILE" ] && [ -n "$(compose --profile demo ps -aq 2>/dev/null)" ]; }

# --- machine checks (shared by set-up and doctor) ---------------------------------

check_machine() { # prints findings; returns 1 if set-up can't go on
  local failed=0
  case "$(uname -s)" in
    Darwin | Linux) ok "$(uname -s) $(uname -m)" ;;
    *) bad "unsupported shell environment: $(uname -s)"
       fix "on Windows, install WSL2 and Docker Desktop, then run ./install.sh inside WSL"
       return 1 ;;
  esac
  if ! command -v docker >/dev/null 2>&1; then
    bad "Docker is not installed"
    if [ "$(uname -s)" = Darwin ]; then fix "install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/"
    else fix "install Docker Engine and the Compose plugin: https://docs.docker.com/engine/install/"; fi
    return 1
  fi
  local info
  if ! info=$(docker info --format '{{.ServerVersion}} {{.MemTotal}}' 2>&1); then
    if printf '%s' "$info" | grep -qi "permission denied"; then
      bad "Docker is installed, but this user can't use it"
      fix "sudo usermod -aG docker \$USER, then log out and back in"
    else
      bad "Docker is installed but not running"
      fix "start Docker Desktop (or: sudo systemctl start docker), wait for it, run this again"
    fi
    return 1
  fi
  ok "Docker engine ${info%% *}"
  local mem=${info##* }
  if [ "${mem:-0}" -lt 4000000000 ] 2>/dev/null; then
    warn "Docker has $((mem / 1000000)) MB of memory; Keycloak and Postgres want at least 4 GB"
    fix "Docker Desktop > Settings > Resources > Memory"
  fi
  local cv
  if ! cv=$(docker compose version --short 2>/dev/null); then
    bad "Docker Compose v2 is missing"
    fix "Docker Desktop ships it; on Linux install docker-compose-plugin"
    return 1
  fi
  cv=${cv#v}
  if version_ge "$cv" "$MIN_COMPOSE"; then ok "Docker Compose $cv"
  else bad "Docker Compose $cv is too old (need $MIN_COMPOSE or newer)"; fix "update Docker Desktop or docker-compose-plugin"; failed=1; fi
  local cfg="$HOME/.docker/config.json"
  if [ -f "$cfg" ] && grep -q '"credsStore": *"desktop"' "$cfg" && ! command -v docker-credential-desktop >/dev/null 2>&1; then
    bad "$cfg uses the Docker Desktop credential helper, which isn't installed"
    fix "remove the \"credsStore\": \"desktop\" line from $cfg (image pulls fail until then)"
    failed=1
  fi
  local free_kb
  free_kb=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
  if [ "${free_kb:-0}" -lt 5000000 ]; then warn "less than 5 GB free disk here; the images need about 3 GB"
  else ok "$((free_kb / 1000000)) GB free disk"; fi
  return $failed
}

# --- set-up -----------------------------------------------------------------------

build_image() {
  step "Building the SupportAgent image (the first build takes a few minutes)"
  docker build -q -f docker/Dockerfile -t "$IMAGE" . >/dev/null || die "the image build failed" "run: docker build -f docker/Dockerfile -t $IMAGE ."
  ok "$IMAGE built"
}

run_wizard() { # $1: optional answers file
  local app_port kc_port
  app_port=$(envval SA_APP_PORT); kc_port=$(envval SA_KEYCLOAK_PORT)
  if stack_exists; then
    step "Stopping the current install (its data is kept)"
    compose --profile demo down --remove-orphans >/dev/null 2>&1 || true
  fi
  app_port=$(free_port "${app_port:-8800}")
  kc_port=$(free_port "${kc_port:-8880}")
  [ "$app_port" = 8800 ] || warn "port 8800 is busy: SupportAgent will use $app_port"
  [ "$kc_port" = 8880 ] || warn "port 8880 is busy: Keycloak will use $kc_port"
  mkdir -p "$LOCAL"
  local args=(init --in-container --dir local --app-port "$app_port" --keycloak-port "$kc_port")
  local mounts=(-v "$ROOT:/work" -w /work)
  if [ -n "${1:-}" ]; then
    cp "$1" "$LOCAL/.answers-input.json"
    args+=(--answers local/.answers-input.json)
    docker run --rm "${mounts[@]}" --user "$SA_UID:$SA_GID" --add-host host.docker.internal:host-gateway "$IMAGE" "${args[@]}"
    rm -f "$LOCAL/.answers-input.json"
  else
    [ -t 0 ] || die "no terminal to ask the setup questions in" "run it in a terminal, or pass --answers FILE"
    docker run --rm -it "${mounts[@]}" --user "$SA_UID:$SA_GID" --add-host host.docker.internal:host-gateway "$IMAGE" "${args[@]}"
  fi
  [ -f "$ENV_FILE" ] || die "setup did not finish (no local/.env)" "run ./install.sh again"
  sync_realm
}

# Keycloak imports the realm only when it doesn't exist yet. After answers change
# (a port, the password), import it again over the stored one. Keycloak is stopped
# here; users added in its admin console are replaced, which reconfigure says.
sync_realm() {
  local realm="$LOCAL/keycloak/supportagent-realm.json" stamp="$LOCAL/keycloak/.imported" sum
  sum=$(cksum <"$realm" | awk '{print $1}')
  [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$sum" ] && return 0
  if [ -f "$stamp" ] || [ -n "$(docker volume ls -q --filter name=supportagent-local_kcdata)" ]; then
    step "Updating the sign-in realm from your answers"
    if compose run --rm --no-deps -T keycloak import --dir /opt/keycloak/data/import --override true \
        >"$LOCAL/keycloak/import.log" 2>&1; then
      ok "realm supportagent updated"
    else
      warn "the realm update failed: sign-in may use the old settings"
      fix "details: local/keycloak/import.log"
      return 0
    fi
  fi
  echo "$sum" >"$stamp"
}

calibrate() {
  step "Calibrating your model on the 27 built-in smoke cases"
  printf '  %sThis calls your model about 30 times: a minute with a hosted API, up to ~15\n' "$D"
  printf '  minutes with a large local model. Without it every incident safe-defaults.%s\n' "$N"
  local log="$LOCAL/evals/calibration.log"
  mkdir -p "$LOCAL/evals"
  if compose run --rm --no-deps -T app eval run --suite smoke --live --write-calibration \
      --config local/supportagent.yaml --artifacts local/evals >"$log" 2>&1; then
    ok "calibrated: $(grep -E 'accuracy|passed|pass' "$log" | tail -1 | sed 's/^ *//')"
  elif [ -f "$LOCAL/evals/calibration.json" ]; then
    warn "the smoke gate failed on this model, but the calibration was written"
    fix "details: $log; a stronger model routes more incidents"
  else
    bad "calibration failed; incidents will safe-default until it runs"
    tail -5 "$log" | sed 's/^/    /'
    fix "fix what doctor reports, then: ./install.sh calibrate"
  fi
}

wait_healthy() {
  local port
  port=$(envval SA_APP_PORT)
  printf '  waiting for SupportAgent on http://localhost:%s ' "$port"
  for _ in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then printf '\n'; ok "SupportAgent is up"; return 0; fi
    printf '.'; sleep 2
  done
  printf '\n'; bad "SupportAgent did not come up in 3 minutes"
  fix "./install.sh doctor (and ./install.sh logs app)"
  return 1
}

start_stack() {
  local demo=""
  [ "$(envval COMPOSE_PROFILES)" = demo ] && demo=", the demo ServiceNow"
  step "Starting Postgres, Keycloak$demo and SupportAgent"
  printf '  %sthe first start downloads Postgres and Keycloak (about 600 MB)%s\n' "$D" "$N"
  up || warn "a container didn't start; doctor below says which"
  wait_healthy
}

summary() {
  local port kc user pass demo=""
  port=$(envval SA_APP_PORT); kc=$(envval SA_KEYCLOAK_PORT)
  user=$(envval SA_ADMIN_USER); pass=$(envval SA_ADMIN_PASSWORD)
  [ "$(envval COMPOSE_PROFILES)" = demo ] && demo=$'    - The demo ServiceNow sends a new sample incident every 5 minutes.\n'

  cat <<EOF

${B}SupportAgent is running (shadow mode: it never writes to ServiceNow).${N}

  Open      ${B}http://localhost:${port}/ui${N}
  Sign in   ${B}${user}${N} / ${B}${pass}${N}   (the first sign-in becomes the admin)

  What next
    - Watch cases arrive under Approvals and Cases; /ui/stats shows the shadow run.
${demo}    - ./install.sh doctor      check everything, with a fix for each problem
    - ./install.sh logs app    follow the agent's log
    - ./install.sh stop        stop it (start brings it back with its data)
  Keycloak admin console: http://localhost:${kc}/admin (kcadmin; password in local/.env)
EOF
}

onboard() { # ADR-0017: onboard the system the wizard described; 1 if it couldn't run
  [ -f "$LOCAL/system.yaml" ] || return 0
  local name status=0
  name=$(sed -n 's/^  name: *//p' "$LOCAL/system.yaml" | head -1 | tr -d '"')
  step "Onboarding ${name:-your system} (read-only: it never writes to a source)"
  printf '  %sreads its incidents, docs and repositories; a first run can take a while%s\n' "$D" "$N"
  compose exec -T app supportagent onboard run --system local/system.yaml \
    --workspace local/onboarding --config local/supportagent.yaml || status=$?
  case $status in
    0) ok "ready to support" ;;
    1) ok "onboarded so far: the list above is what it needs from you; ./install.sh onboard re-runs it" ;;
    *) warn "onboarding couldn't run"; fix "./install.sh doctor, then ./install.sh onboard"; return 1 ;;
  esac
}

cmd_install() {
  local answers="" skip_cal=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --answers) answers=$2; shift 2 ;;
      --skip-calibration) skip_cal=1; shift ;;
      *) die "unknown option: $1" "./install.sh help" ;;
    esac
  done
  [ -z "$answers" ] || [ -f "$answers" ] || die "no answers file at $answers"
  [ -z "$answers" ] || answers=$(cd "$(dirname "$answers")" && pwd)/$(basename "$answers")
  step "Checking this machine"
  check_machine || die "fix the problems above, then run ./install.sh again"
  build_image
  run_wizard "$answers"
  step "Starting the database and sign-in (the first start downloads about 600 MB)"
  local early="postgres keycloak"
  [ "$(envval COMPOSE_PROFILES)" = demo ] && early="$early demo-servicenow"
  # shellcheck disable=SC2086 # a word list on purpose
  if up $early; then ok "started: $early"; else warn "not everything started; continuing"; fi
  if [ $skip_cal = 0 ]; then calibrate; else warn "calibration skipped: ./install.sh calibrate when ready"; fi
  start_stack || { cmd_doctor || true; exit 1; }
  step "Checking the install"
  in_app_doctor || true
  onboard || true
  summary
}

# --- doctor -----------------------------------------------------------------------

hint() { # a fix for an in-app doctor check: $1 its name, $2 its detail
  case "${2:-}" in
    *"Permission denied"*)
      fix "the containers can't read this checkout: make it readable (chmod -R a+rX .),"
      fix "then rebuild and restart: ./install.sh"
      return ;;
  esac
  case "$1" in
    config) fix "missing calibration? ./install.sh calibrate. A bad answer? ./install.sh reconfigure" ;;
    db) fix "./install.sh restart; if it persists: ./install.sh logs postgres" ;;
    llm) fix "is the model server running and reachable from Docker? A local server must listen on"
         fix "all interfaces (LM Studio: Developer > Settings > Serve on local network; Ollama:"
         fix "OLLAMA_HOST=0.0.0.0). Wrong key or model: ./install.sh reconfigure" ;;
    oidc) fix "Keycloak isn't answering: ./install.sh logs keycloak, then ./install.sh restart" ;;
    servicenow.auth) fix "wrong URL, user or password, or the instance is asleep (wake a developer"
                     fix "instance at developer.servicenow.com). Change it: ./install.sh reconfigure" ;;
    servicenow.history_access) fix "give the integration user read on sys_audit and sys_journal_field"
                               fix "(a role with read ACLs on both): see docs/quickstart.md" ;;
    servicenow.integration_role) fix "the integration user should hold itil and nothing broader (no admin)" ;;
    servicenow.journal_visibility) fix "work notes must be readable by itil only; see docs/quickstart.md" ;;
    servicenow.*) fix "see docs/quickstart.md, 'The ServiceNow integration user'" ;;
    paging) fix "optional: without PagerDuty, findings stay in SupportAgent (./install.sh reconfigure to add it)" ;;
    knowledge) fix "Confluence: check the space key, email and API token (./install.sh reconfigure)" ;;
    automation_actors) fix "sign in and decide each candidate at /ui/setup" ;;
    packs) fix "a pack under packs/ doesn't validate: supportagent pack validate packs/<id>" ;;
    *) fix "details: docs/quickstart.md" ;;
  esac
}

in_app_doctor() { # runs SupportAgent's own checks inside the container; 1 if any FAIL
  local out status=0 line mark name detail
  if stack_running; then out=$(compose exec -T app supportagent doctor --config local/supportagent.yaml 2>&1) || status=$?
  else out=$(compose run --rm --no-deps -T app doctor --config local/supportagent.yaml 2>&1) || status=$?; fi
  while IFS= read -r line; do
    mark=${line%% *}; name=$(printf '%s' "$line" | awk '{print $2}')
    detail=$(printf '%s' "$line" | sed -E 's/^[A-Z]+ +[^ ]+ +//')
    case "$mark" in
      PASS) ok "$name: $detail" ;;
      WARN) warn "$name: $detail"; hint "$name" "$detail" ;;
      FAIL) bad "$name: $detail"; hint "$name" "$detail" ;;
      *) [ -n "$line" ] && printf '    %s\n' "$line" ;;
    esac
  done <<<"$out"
  [ "$status" = 1 ] && return 1
  return 0
}

cmd_doctor() {
  local failed=0 svc state
  step "This machine"
  check_machine || failed=1
  [ $failed = 0 ] || { printf '\n%sFix the machine first; nothing else can run.%s\n' "$R" "$N"; return 1; }

  step "Install files"
  if [ ! -f "$ENV_FILE" ]; then bad "not set up yet (no local/.env)"; fix "./install.sh"; return 1; fi
  local mode; mode=$(file_mode "$ENV_FILE")
  if [ "$mode" = 600 ]; then ok "local/.env (private)"; else warn "local/.env is mode $mode"; fix "chmod 600 local/.env"; fi
  for f in supportagent.yaml catalog.yaml keycloak/supportagent-realm.json; do
    if [ -f "$LOCAL/$f" ]; then ok "local/$f"; else bad "local/$f is missing"; fix "./install.sh reconfigure"; failed=1; fi
  done
  for k in POSTGRES_PASSWORD SA_MASTER_KEY_ENV SA_SESSION_SECRET OIDC_CLIENT_SECRET SUPPORTAGENT_DB_URL; do
    [ -n "$(envval "$k")" ] || { bad "local/.env has no $k"; fix "./install.sh reconfigure (it keeps everything else)"; failed=1; }
  done
  if [ -f "$LOCAL/evals/calibration.json" ]; then ok "model calibration"
  else bad "no model calibration: every incident safe-defaults"; fix "./install.sh calibrate"; failed=1; fi
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then ok "image $IMAGE"
  else bad "image $IMAGE is missing"; fix "./install.sh (rebuilds it)"; failed=1; fi

  step "Containers"
  local services="postgres keycloak app"
  [ "$(envval COMPOSE_PROFILES)" = demo ] && services="postgres keycloak demo-servicenow app"
  for svc in $services; do
    state=$(compose ps -a "$svc" --format '{{.State}} {{.Health}}' 2>/dev/null | head -1)
    case "$state" in
      "running healthy" | "running ") ok "$svc running" ;;
      "running starting") warn "$svc is still starting"; fix "wait a minute, then ./install.sh doctor" ;;
      "") bad "$svc is not created"; fix "./install.sh start"; failed=1 ;;
      *) bad "$svc: $state"; fix "./install.sh logs $svc   then ./install.sh restart"; failed=1 ;;
    esac
  done
  local mig; mig=$(compose ps -a migrate --format '{{.ExitCode}}' 2>/dev/null | head -1)
  if [ "$mig" = 0 ]; then ok "database migrations applied"
  elif [ -n "$mig" ]; then bad "database migration exited $mig"; fix "./install.sh logs migrate"; failed=1; fi

  step "Endpoints"
  local port kc; port=$(envval SA_APP_PORT); kc=$(envval SA_KEYCLOAK_PORT)
  if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then ok "SupportAgent answers on http://localhost:$port"
  else bad "nothing answers on http://localhost:$port"; fix "./install.sh logs app"; failed=1; fi
  if curl -fsS "http://127.0.0.1:$kc/realms/supportagent/.well-known/openid-configuration" 2>/dev/null | grep -q "localhost:$kc/realms/supportagent"; then
    ok "Keycloak realm supportagent on http://localhost:$kc"
  else bad "Keycloak's realm isn't answering on http://localhost:$kc"; fix "./install.sh logs keycloak"; failed=1; fi

  step "SupportAgent's own checks (config, database, ServiceNow, LLM, sign-in, docs)"
  in_app_doctor || failed=1

  printf '\n'
  if [ $failed = 0 ]; then printf '%sAll required checks pass.%s Warnings (!) are optional extras.\n' "$G" "$N"
  else printf '%sSome checks failed (✗).%s Each one has a fix (→) under it.\n' "$R" "$N"; fi
  return $failed
}

# --- the rest ---------------------------------------------------------------------

need_install() { [ -f "$ENV_FILE" ] || die "not set up yet" "./install.sh"; }

cmd_uninstall() {
  need_install
  local purge=0
  [ "${1:-}" = --purge ] && purge=1
  printf 'This deletes the containers, the database and the Keycloak data%s. Type "yes" to go on: ' \
    "$( [ $purge = 1 ] && printf ', and local/ (config and secrets)')"
  read -r reply
  [ "$reply" = yes ] || die "cancelled"
  compose --profile demo down -v --remove-orphans
  docker image rm "$IMAGE" >/dev/null 2>&1 || true
  [ $purge = 1 ] && rm -rf "$LOCAL"
  ok "uninstalled$( [ $purge = 0 ] && printf '; local/ kept (./install.sh reuses it)')"
}

usage() { sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; }

cmd=${1:-install}
[ $# -gt 0 ] && shift
case "$cmd" in help | -h | --help) ;; *) command -v docker >/dev/null 2>&1 && guard_owner ;; esac
case "$cmd" in
  install | setup) cmd_install "$@" ;;
  --answers | --skip-calibration) cmd_install "$cmd" "$@" ;;
  reconfigure)
    need_install
    printf '  %sYour data is kept. Users added in the Keycloak admin console are reset to\n' "$D"
    printf '  the one you give here; other changes apply when it restarts.%s\n' "$N"
    build_image; run_wizard ""
    printf '  Recalibrate? Needed after a model change. [y/N] '; read -r r
    case "$r" in y | Y) up postgres keycloak || true; calibrate ;; esac
    start_stack; summary ;;
  doctor) cmd_doctor ;;
  start) need_install; start_stack; summary ;;
  stop) need_install; compose --profile demo stop; ok "stopped; ./install.sh start brings it back" ;;
  restart) need_install; compose --profile demo restart; wait_healthy ;;
  status) need_install; compose --profile demo ps ;;
  logs) need_install; compose --profile demo logs -f --tail 200 "$@" ;;
  calibrate) need_install; calibrate; compose restart app >/dev/null 2>&1 || true ;;
  onboard)
    need_install
    [ -f "$LOCAL/system.yaml" ] || die "no local/system.yaml" "./install.sh reconfigure, and answer 'Onboard this system'"
    stack_running || start_stack
    onboard ;;
  uninstall) cmd_uninstall "$@" ;;
  help | -h | --help) usage ;;
  *) bad "unknown command: $cmd"; usage; exit 2 ;;
esac
