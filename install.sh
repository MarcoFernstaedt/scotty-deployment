#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SOURCE_DIR
readonly TARGET_ROOT=/srv/Scotty
readonly OPERATOR_DIR=${TARGET_ROOT}/operator
readonly DATA_DIR=${TARGET_ROOT}/data
readonly COMPOSE_FILE=${OPERATOR_DIR}/compose.yaml
readonly SETUP_BIN=/srv/Scotty/operator/setup-scotty
readonly START_BIN=/usr/local/sbin/scotty-start
readonly PLUGINS_DIR=/srv/Scotty/data/plugins
readonly PLUGIN_DIR=/srv/Scotty/data/plugins/scotty_business
readonly PLUGIN_ADAPTERS_DIR=/srv/Scotty/data/plugins/scotty_business/adapters
readonly PROFILES_DIR=/srv/Scotty/data/profiles
# The full profile home never receives the bounded plugin. Only the two client
# profile homes are staged with it.
readonly -a SERVED_PROFILES=(scotty-maintainer scotty-main-operator scotty-employee)
readonly -a CLIENT_PROFILES=(scotty-main-operator scotty-employee)
readonly MAINTAINER_PROFILE=scotty-maintainer
# The guard registers only a pre-dispatch authorization hook: no model tools,
# no prompt section, no bounded client identity.
readonly -a GUARD_FILES=(
  "__init__.py"
  "plugin.yaml"
  "guard.py"
)
readonly GUARD_BIN=/usr/local/libexec/scotty-egress-guard
readonly GUARD_UNIT=/etc/systemd/system/scotty-egress-guard.service
readonly CONTAINER=scotty
readonly NETWORK=scotty-egress
readonly SUBNET=172.30.50.0/24
readonly CHAIN=SCOTTY-EGRESS
readonly IMAGE='nousresearch/hermes-agent@sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517'
readonly IMAGE_ID='sha256:f002ea7b37bec9aef0213f155dc27efe3fbc47eeb5b46d109115dae9a45dcf63'

COMMITTED=0
CLEANING=0
CREATED_ROOT=0
CREATED_OPERATOR=0
CREATED_DATA=0
CREATED_PLUGINS=0
CREATED_PLUGIN=0
CREATED_PLUGIN_ADAPTERS=0
INSTALLED_COMPOSE=0
INSTALLED_SETUP=0
INSTALLED_START=0
INSTALLED_GUARD=0
INSTALLED_UNIT=0
RELOADED_SYSTEMD=0
ENABLED_GUARD=0
CREATED_NETWORK=0
CREATED_CONTAINER=0
INSTALLED_PLUGIN_FILES=()
CREATED_PROFILE_DIRS=()

readonly -a PLUGIN_FILES=(
  "__init__.py"
  "plugin.yaml"
  "approvals.py"
  "calculations.py"
  "config.py"
  "credential_intake.py"
  "discord_policy.py"
  "guidance.py"
  "google_oauth.py"
  "google_policy.py"
  "google_readback.py"
  "identity.py"
  "ingress.py"
  "policy.py"
  "progress.py"
  "provisioning.py"
  "reminders.py"
  "routing.py"
  "runtime.py"
  "self_repair.py"
  "service.py"
  "setup.py"
  "setup_flow.py"
  "wizard.py"
  "adapters/__init__.py"
  "adapters/discord.py"
  "adapters/ghl.py"
  "adapters/google_workspace.py"
  "adapters/http.py"
  "adapters/records.py"
  "adapters/rentcast.py"
  "adapters/trello.py"
)

die() {
  printf 'install: %s\n' "$*" >&2
  exit 1
}

explicit_status_begin() {
  trap - ERR
  set +e
}

explicit_status_end() {
  set -e
  trap 'exit $?' ERR
}

command_output() {
  local __name=$1
  shift
  local __captured rc
  explicit_status_begin
  __captured="$("$@" 2>&1)"
  rc=$?
  explicit_status_end
  (( rc == 0 )) || die "command failed (status ${rc}): $* :: ${__captured}"
  printf -v "$__name" '%s' "$__captured"
}

contains_exact_line() {
  local haystack=$1 needle=$2 line
  while IFS= read -r line; do
    [[ $line == "$needle" ]] && return 0
  done <<<"$haystack"
  return 1
}

require_absent_destination() {
  local path=$1
  [[ ! -e $path && ! -L $path ]] || die "protected destination already exists: ${path}"
}

require_safe_ancestors() {
  local path=$1 ancestor next
  ancestor=${path%/*}
  while [[ -n $ancestor && $ancestor != / ]]; do
    [[ ! -L $ancestor ]] || die "protected destination has a symlinked ancestor: ${ancestor}"
    [[ -e $ancestor ]] || die "protected destination ancestor is absent: ${ancestor}"
    [[ -d $ancestor ]] || die "protected destination ancestor is not a directory: ${ancestor}"
    next=${ancestor%/*}
    [[ -n $next ]] || next=/
    ancestor=$next
  done
}

install_owned() {
  local flag_name=$1 target=$2 install_rc
  shift 2
  require_safe_ancestors "$target"
  require_absent_destination "$target"
  explicit_status_begin
  install "$@"
  install_rc=$?
  explicit_status_end
  if [[ -e $target && ! -L $target ]]; then
    printf -v "$flag_name" '%s' 1
  elif [[ -L $target ]]; then
    die "install produced or encountered an unowned symlink: ${target}"
  fi
  (( install_rc == 0 )) || die "install failed for ${target} (status ${install_rc})"
  [[ -e $target && ! -L $target ]] || die "install reported success but safe target is absent: ${target}"
}

install_profile_dir() {
  local target=$1 install_rc
  [[ $target == "${DATA_DIR}/"* ]] || die "profile directory is outside the data mount: ${target}"
  require_safe_ancestors "$target"
  require_absent_destination "$target"
  explicit_status_begin
  install -d -o 10000 -g 10000 -m 0700 "$target"
  install_rc=$?
  explicit_status_end
  if [[ -d $target && ! -L $target ]]; then
    CREATED_PROFILE_DIRS+=("$target")
  elif [[ -L $target ]]; then
    die "profile install produced or encountered an unowned symlink: ${target}"
  fi
  (( install_rc == 0 )) || die "profile directory install failed for ${target} (status ${install_rc})"
  [[ -d $target && ! -L $target ]] || die "profile directory is absent after install: ${target}"
}

install_guard_file() {
  local relative=$1 root=$2 source target install_rc
  source=${SOURCE_DIR}/assistant/scotty_guard/${relative}
  target=${root}/${relative}
  [[ -f $source && ! -L $source ]] || die "guard source is absent or unsafe: ${relative}"
  require_safe_ancestors "$target"
  require_absent_destination "$target"
  explicit_status_begin
  install -o 10000 -g 10000 -m 0600 "$source" "$target"
  install_rc=$?
  explicit_status_end
  if [[ -f $target && ! -L $target ]]; then
    INSTALLED_PLUGIN_FILES+=("$target")
  elif [[ -L $target ]]; then
    die "guard install produced or encountered an unowned symlink: ${target}"
  fi
  (( install_rc == 0 )) || die "guard install failed for ${relative} (status ${install_rc})"
  [[ -f $target && ! -L $target ]] || die "guard install reported success but target is absent: ${relative}"
}

install_plugin_file() {
  local relative=$1 root=${2:-$PLUGIN_DIR} source target install_rc
  source=${SOURCE_DIR}/assistant/scotty_business/${relative}
  target=${root}/${relative}
  [[ -f $source && ! -L $source ]] || die "plugin source is absent or unsafe: ${relative}"
  require_safe_ancestors "$target"
  require_absent_destination "$target"
  explicit_status_begin
  install -o 10000 -g 10000 -m 0600 "$source" "$target"
  install_rc=$?
  explicit_status_end
  if [[ -f $target && ! -L $target ]]; then
    INSTALLED_PLUGIN_FILES+=("$target")
  elif [[ -L $target ]]; then
    die "plugin install produced or encountered an unowned symlink: ${target}"
  fi
  (( install_rc == 0 )) || die "plugin install failed for ${relative} (status ${install_rc})"
  [[ -f $target && ! -L $target ]] || die "plugin install reported success but safe target is absent: ${relative}"
}

cleanup() {
  local original_rc=$1 label inventory probe_rc remove_rc disable_rc guard_rc rc=0
  local installed_file index
  local management_safe=1
  (( CLEANING == 0 )) || return
  CLEANING=1
  trap - ERR
  set +e
  printf 'install: transaction not committed; cleaning invocation-owned objects\n' >&2

  if (( CREATED_CONTAINER )); then
    inventory="$(docker container ls -a --format '{{.Names}}' 2>&1)"
    probe_rc=$?
    if (( probe_rc != 0 )); then
      printf 'install: container inventory probe failed during cleanup (status %s): %s\n' "$probe_rc" "$inventory" >&2
      rc=1
    elif contains_exact_line "$inventory" "$CONTAINER"; then
      label="$(docker inspect --format '{{ index .Config.Labels "com.scotty.deployment" }}' "$CONTAINER" 2>&1)"
      probe_rc=$?
      if (( probe_rc != 0 )); then
        printf 'install: container ownership inspect failed during cleanup (status %s): %s\n' "$probe_rc" "$label" >&2
        rc=1
      elif [[ $label != managed ]]; then
        printf 'install: refusing to remove container with unexpected ownership label\n' >&2
        rc=1
      else
        docker container rm "$CONTAINER" >/dev/null 2>&1
        remove_rc=$?
        (( remove_rc == 0 )) || rc=1
      fi
    fi
  fi
  if (( CREATED_NETWORK )); then
    inventory="$(docker network ls --format '{{.Name}}' 2>&1)"
    probe_rc=$?
    if (( probe_rc != 0 )); then
      printf 'install: network inventory probe failed during cleanup (status %s): %s\n' "$probe_rc" "$inventory" >&2
      rc=1
    elif contains_exact_line "$inventory" "$NETWORK"; then
      label="$(docker network inspect --format '{{ index .Labels "com.scotty.deployment" }}' "$NETWORK" 2>&1)"
      probe_rc=$?
      if (( probe_rc != 0 )); then
        printf 'install: network ownership inspect failed during cleanup (status %s): %s\n' "$probe_rc" "$label" >&2
        rc=1
      elif [[ $label != managed ]]; then
        printf 'install: refusing to remove network with unexpected ownership label\n' >&2
        rc=1
      else
        docker network rm "$NETWORK" >/dev/null 2>&1
        remove_rc=$?
        (( remove_rc == 0 )) || rc=1
      fi
    fi
  fi
  if (( ENABLED_GUARD )); then
    management_safe=0
    systemctl disable --now scotty-egress-guard.service >/dev/null 2>&1
    remove_rc=$?

    "$GUARD_BIN" remove >/dev/null 2>&1
    guard_rc=$?

    systemctl disable scotty-egress-guard.service >/dev/null 2>&1
    disable_rc=$?

    if (( guard_rc == 0 && disable_rc == 0 )); then
      management_safe=1
    else
      printf 'install: retaining firewall management artifacts; absence or disablement is unproven (stop=%s guard=%s disable=%s)\n' "$remove_rc" "$guard_rc" "$disable_rc" >&2
      rc=1
    fi
  fi
  if (( INSTALLED_UNIT )); then
    if (( management_safe )); then
      if [[ -L $GUARD_UNIT ]]; then
        printf 'install: refusing to remove replacement symlink: %s\n' "$GUARD_UNIT" >&2
        rc=1
      else
        rm -f -- "$GUARD_UNIT"
        remove_rc=$?
        (( remove_rc == 0 )) || rc=1
      fi
    fi
  fi
  if (( INSTALLED_GUARD )); then
    if (( management_safe )); then
      if [[ -L $GUARD_BIN ]]; then
        printf 'install: refusing to remove replacement symlink: %s\n' "$GUARD_BIN" >&2
        rc=1
      else
        rm -f -- "$GUARD_BIN"
        remove_rc=$?
        (( remove_rc == 0 )) || rc=1
      fi
    fi
  fi
  if (( RELOADED_SYSTEMD )); then
    systemctl daemon-reload >/dev/null 2>&1
    remove_rc=$?
    (( remove_rc == 0 )) || rc=1
  fi
  if (( INSTALLED_COMPOSE )); then
    if [[ -L $COMPOSE_FILE ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$COMPOSE_FILE" >&2
      rc=1
    else
      rm -f -- "$COMPOSE_FILE"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( INSTALLED_SETUP )); then
    if [[ -L $SETUP_BIN ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$SETUP_BIN" >&2
      rc=1
    else
      rm -f -- "$SETUP_BIN"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( INSTALLED_START )); then
    if [[ -L $START_BIN ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$START_BIN" >&2
      rc=1
    else
      rm -f -- "$START_BIN"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  for (( index=${#INSTALLED_PLUGIN_FILES[@]}-1; index>=0; index-- )); do
    installed_file=${INSTALLED_PLUGIN_FILES[index]}
    if [[ $installed_file != "$DATA_DIR/"* || -L $installed_file ]]; then
      printf 'install: refusing to remove unsafe plugin path: %s\n' "$installed_file" >&2
      rc=1
    else
      rm -f -- "$installed_file"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  done
  for (( index=${#CREATED_PROFILE_DIRS[@]}-1; index>=0; index-- )); do
    installed_file=${CREATED_PROFILE_DIRS[index]}
    if [[ $installed_file != "$DATA_DIR/"* || -L $installed_file ]]; then
      printf 'install: refusing to remove unsafe profile path: %s\n' "$installed_file" >&2
      rc=1
    else
      rmdir -- "$installed_file"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  done
  if (( CREATED_PLUGIN_ADAPTERS )); then
    if [[ -L $PLUGIN_ADAPTERS_DIR ]]; then
      rc=1
    else
      rmdir -- "$PLUGIN_ADAPTERS_DIR"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( CREATED_PLUGIN )); then
    if [[ -L $PLUGIN_DIR ]]; then
      rc=1
    else
      rmdir -- "$PLUGIN_DIR"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( CREATED_PLUGINS )); then
    if [[ -L $PLUGINS_DIR ]]; then
      rc=1
    else
      rmdir -- "$PLUGINS_DIR"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( CREATED_OPERATOR )); then
    if [[ -L $OPERATOR_DIR ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$OPERATOR_DIR" >&2
      rc=1
    else
      rmdir -- "$OPERATOR_DIR"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( CREATED_DATA )); then
    if [[ -L $DATA_DIR ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$DATA_DIR" >&2
      rc=1
    else
      rmdir -- "$DATA_DIR"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi
  if (( CREATED_ROOT )); then
    if [[ -L $TARGET_ROOT ]]; then
      printf 'install: refusing to remove replacement symlink: %s\n' "$TARGET_ROOT" >&2
      rc=1
    else
      rmdir -- "$TARGET_ROOT"
      remove_rc=$?
      (( remove_rc == 0 )) || rc=1
    fi
  fi

  if (( rc != 0 )); then
    printf 'install: cleanup incomplete; inspect only the named invocation-owned objects\n' >&2
    return 1
  fi
  return "$original_rc"
}

on_exit() {
  local exit_rc=$? cleanup_rc
  if (( COMMITTED == 0 )); then
    trap - ERR
    set +e
    cleanup "$exit_rc"
    cleanup_rc=$?
    set -e
    if (( exit_rc == 0 && cleanup_rc != 0 )); then
      exit_rc=$cleanup_rc
    fi
  fi
  exit "$exit_rc"
}

trap 'exit $?' ERR
trap 'exit 130' INT
trap 'exit 143' TERM
trap on_exit EXIT

preflight() {
  local output containers networks tables unit_state image_id image_ref security

  (( EUID == 0 )) || die 'must run as root (use the documented sudo command)'
  [[ $PWD != "$SOURCE_DIR" || -f "$SOURCE_DIR/compose.yaml" ]] || die 'source directory resolution failed'

  local required
  for required in docker systemctl install stat sha256sum grep /usr/sbin/iptables; do
    command -v "$required" >/dev/null 2>&1 || die "missing required command: ${required}"
  done

  command_output output docker --version
  [[ $output == 'Docker version 26.1.5+dfsg1, build a72d7cd' ]] || die "unsupported Docker version: ${output}"
  command_output output docker compose version
  [[ $output == 'Docker Compose version 2.26.1-4' ]] || die "unsupported Compose version: ${output}"
  command_output output /usr/sbin/iptables --version
  [[ $output == 'iptables v1.8.11 (nf_tables)' ]] || die "unsupported iptables version: ${output}"
  command_output output docker compose create --help
  [[ $output == *'--pull string'* ]] || die 'Compose create does not support --pull'

  command_output security docker info --format '{{json .SecurityOptions}}'
  [[ $security == *'"name=apparmor"'* ]] || die 'Docker AppArmor support is unavailable'
  [[ $security == *'"name=seccomp,profile=builtin"'* ]] || die 'Docker builtin seccomp is unavailable'
  [[ $security == *'"name=cgroupns"'* ]] || die 'Docker private cgroup namespaces are unavailable'

  command_output image_id docker image inspect "$IMAGE" --format '{{.Id}}'
  [[ $image_id == "$IMAGE_ID" ]] || die "local image ID mismatch: ${image_id}"
  command_output image_ref docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}'
  [[ $image_ref == "$IMAGE" ]] || die "local image digest mismatch: ${image_ref}"

  (cd -- "$SOURCE_DIR" && sha256sum -c SHA256SUMS >/dev/null) || die 'source integrity manifest failed'
  docker compose -f "$SOURCE_DIR/compose.yaml" config --quiet || die 'Compose render failed'

  local -a protected_destinations=(
    "$TARGET_ROOT"
    "$OPERATOR_DIR"
    "$DATA_DIR"
    "$COMPOSE_FILE"
    "$SETUP_BIN"
    "$START_BIN"
    "$GUARD_BIN"
    "$GUARD_UNIT"
  )
  local destination
  for destination in "${protected_destinations[@]}"; do
    require_absent_destination "$destination"
  done
  require_safe_ancestors "$TARGET_ROOT"
  require_safe_ancestors "$GUARD_BIN"
  require_safe_ancestors "$START_BIN"
  require_safe_ancestors "$GUARD_UNIT"

  command_output containers docker container ls -a --format '{{.Names}}'
  contains_exact_line "$containers" "$CONTAINER" && die "container already exists: ${CONTAINER}"
  command_output networks docker network ls --format '{{.Name}}'
  contains_exact_line "$networks" "$NETWORK" && die "network already exists: ${NETWORK}"

  command_output tables /usr/sbin/iptables -w 5 -S
  contains_exact_line "$tables" "-N ${CHAIN}" && die "firewall chain already exists: ${CHAIN}"
  command_output output /usr/sbin/iptables -w 5 -S DOCKER-USER
  [[ $output != *"-s ${SUBNET} -j ${CHAIN}"* ]] || die 'firewall jump already exists'

  command_output unit_state systemctl show scotty-egress-guard.service --property=LoadState --value
  [[ $unit_state == not-found ]] || die "systemd unit is already loaded: ${unit_state}"
}

verify_install() {
  local actual

  command_output actual docker inspect --format '{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}|{{.State.FinishedAt}}' "$CONTAINER"
  [[ $actual == 'created|false|0001-01-01T00:00:00Z|0001-01-01T00:00:00Z' ]] || die "container state mismatch: ${actual}"

  command_output actual docker inspect --format '{{json .Config.Entrypoint}}|{{json .Config.Cmd}}|{{.Config.Image}}|{{.Image}}' "$CONTAINER"
  [[ $actual == "[\"/opt/hermes/docker/entrypoint-dispatch.sh\"]|[\"gateway\",\"run\"]|${IMAGE}|${IMAGE_ID}" ]] || die "entrypoint/command/image mismatch: ${actual}"

  command_output actual docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER"
  contains_exact_line "$actual" 'HERMES_UID=10000' || die 'HERMES_UID mismatch'
  contains_exact_line "$actual" 'HERMES_GID=10000' || die 'HERMES_GID mismatch'

  command_output actual docker inspect --format '{{index .Config.Labels "com.scotty.deployment"}}|{{index .Config.Labels "com.scotty.image.digest"}}' "$CONTAINER"
  [[ $actual == "managed|sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517" ]] || die "label mismatch: ${actual}"

  command_output actual docker inspect --format '{{len .Mounts}}|{{(index .Mounts 0).Type}}|{{(index .Mounts 0).Source}}|{{(index .Mounts 0).Destination}}|{{(index .Mounts 0).RW}}' "$CONTAINER"
  [[ $actual == '1|bind|/srv/Scotty/data|/opt/data|true' ]] || die "mount mismatch: ${actual}"

  command_output actual docker inspect --format '{{json .Config.ExposedPorts}}|{{json .HostConfig.PortBindings}}|{{json .HostConfig.Devices}}|{{.HostConfig.Privileged}}|{{json .HostConfig.SecurityOpt}}|{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER"
  [[ $actual == 'null|{}|[]|false|["no-new-privileges:true"]|no' ]] || die "exposure/security mismatch: ${actual}"

  command_output actual docker inspect --format '{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.CgroupnsMode}}' "$CONTAINER"
  [[ $actual == '2000000000|4294967296|512|private' ]] || die "resource/cgroup limit mismatch: ${actual}"

  # Docker Go template; dollar signs are not shell expansions.
  # shellcheck disable=SC2016
  command_output actual docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' "$CONTAINER"
  [[ $actual == scotty-egress ]] || die "network attachment mismatch: ${actual}"
  command_output actual docker inspect --format '{{(index .NetworkSettings.Networks "scotty-egress").IPAddress}}' "$CONTAINER"
  [[ $actual =~ ^172\.30\.50\.[0-9]+$ ]] || die "container IPv4 is outside expected subnet: ${actual}"

  command_output actual docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}|{{index .Labels "com.scotty.deployment"}}' "$NETWORK"
  [[ $actual == '172.30.50.0/24|managed' ]] || die "network configuration mismatch: ${actual}"

  command_output actual stat -c '%u:%g:%a' "$TARGET_ROOT" "$OPERATOR_DIR" "$DATA_DIR" "$COMPOSE_FILE" "$GUARD_BIN" "$GUARD_UNIT"
  [[ $actual == $'0:0:700\n0:0:700\n10000:10000:700\n0:0:600\n0:0:755\n0:0:644' ]] || die "ownership/mode mismatch: ${actual}"
  command_output actual stat -c '%u:%g:%a' "$SETUP_BIN" "$START_BIN" "$PLUGIN_DIR/plugin.yaml" "$PLUGIN_DIR/runtime.py"
  [[ $actual == $'0:0:700\n0:0:755\n10000:10000:600\n10000:10000:600' ]] || die "assistant package ownership/mode mismatch: ${actual}"

  for served_profile in "${SERVED_PROFILES[@]}"; do
    command_output actual stat -c '%u:%g:%a' "${PROFILES_DIR}/${served_profile}"
    [[ $actual == '10000:10000:700' ]] || die "profile home ownership/mode mismatch: ${served_profile}"
  done
  for client_profile in "${CLIENT_PROFILES[@]}"; do
    command_output actual stat -c '%u:%g:%a' \
      "${PROFILES_DIR}/${client_profile}/plugins/scotty_business/plugin.yaml"
    [[ $actual == '10000:10000:600' ]] || die "client profile plugin staging mismatch: ${client_profile}"
  done
  [[ ! -e ${PROFILES_DIR}/${MAINTAINER_PROFILE}/plugins/scotty_business ]] \
    || die 'the full profile home must not carry the bounded plugin'
  command_output actual stat -c '%u:%g:%a' \
    "${PROFILES_DIR}/${MAINTAINER_PROFILE}/plugins/scotty_guard/plugin.yaml"
  [[ $actual == '10000:10000:600' ]] || die 'maintainer guard staging mismatch'
  for client_profile in "${CLIENT_PROFILES[@]}"; do
    [[ ! -e ${PROFILES_DIR}/${client_profile}/plugins/scotty_guard ]] \
      || die "client profile must not carry the maintainer guard: ${client_profile}"
  done

  systemctl is-active --quiet scotty-egress-guard.service || die 'firewall service is not active'
  "$GUARD_BIN" verify
}

preflight

install_owned CREATED_ROOT "$TARGET_ROOT" -d -o root -g root -m 0700 "$TARGET_ROOT"
install_owned CREATED_OPERATOR "$OPERATOR_DIR" -d -o root -g root -m 0700 "$OPERATOR_DIR"
install_owned CREATED_DATA "$DATA_DIR" -d -o 10000 -g 10000 -m 0700 "$DATA_DIR"
install_owned CREATED_PLUGINS "$PLUGINS_DIR" -d -o 10000 -g 10000 -m 0700 "$PLUGINS_DIR"
install_owned CREATED_PLUGIN "$PLUGIN_DIR" -d -o 10000 -g 10000 -m 0700 "$PLUGIN_DIR"
install_owned CREATED_PLUGIN_ADAPTERS "$PLUGIN_ADAPTERS_DIR" -d -o 10000 -g 10000 -m 0700 "$PLUGIN_ADAPTERS_DIR"
install_owned INSTALLED_COMPOSE "$COMPOSE_FILE" -o root -g root -m 0600 "$SOURCE_DIR/compose.yaml" "$COMPOSE_FILE"
install_owned INSTALLED_SETUP "$SETUP_BIN" -o root -g root -m 0700 "$SOURCE_DIR/setup-scotty" "$SETUP_BIN"
install_owned INSTALLED_START "$START_BIN" -o root -g root -m 0755 "$SOURCE_DIR/scotty-start" "$START_BIN"
for plugin_file in "${PLUGIN_FILES[@]}"; do
  install_plugin_file "$plugin_file"
done
install_profile_dir "$PROFILES_DIR"
for served_profile in "${SERVED_PROFILES[@]}"; do
  install_profile_dir "${PROFILES_DIR}/${served_profile}"
done
for client_profile in "${CLIENT_PROFILES[@]}"; do
  install_profile_dir "${PROFILES_DIR}/${client_profile}/plugins"
  install_profile_dir "${PROFILES_DIR}/${client_profile}/plugins/scotty_business"
  install_profile_dir "${PROFILES_DIR}/${client_profile}/plugins/scotty_business/adapters"
  for plugin_file in "${PLUGIN_FILES[@]}"; do
    install_plugin_file "$plugin_file" "${PROFILES_DIR}/${client_profile}/plugins/scotty_business"
  done
done
install_profile_dir "${PROFILES_DIR}/${MAINTAINER_PROFILE}/plugins"
install_profile_dir "${PROFILES_DIR}/${MAINTAINER_PROFILE}/plugins/scotty_guard"
for guard_file in "${GUARD_FILES[@]}"; do
  install_guard_file "$guard_file" "${PROFILES_DIR}/${MAINTAINER_PROFILE}/plugins/scotty_guard"
done
install_owned INSTALLED_GUARD "$GUARD_BIN" -o root -g root -m 0755 "$SOURCE_DIR/firewall/scotty-egress-guard" "$GUARD_BIN"
install_owned INSTALLED_UNIT "$GUARD_UNIT" -o root -g root -m 0644 "$SOURCE_DIR/firewall/scotty-egress-guard.service" "$GUARD_UNIT"
RELOADED_SYSTEMD=1
systemctl daemon-reload
ENABLED_GUARD=1
systemctl enable --now scotty-egress-guard.service

explicit_status_begin
create_output="$(docker network create --driver bridge --subnet "$SUBNET" --label com.scotty.deployment=managed "$NETWORK" 2>&1)"
network_rc=$?
networks_after="$(docker network ls --format '{{.Name}}' 2>&1)"
probe_rc=$?
explicit_status_end
if (( probe_rc == 0 )) && contains_exact_line "$networks_after" "$NETWORK"; then
  CREATED_NETWORK=1
elif (( probe_rc != 0 && network_rc == 0 )); then
  CREATED_NETWORK=1
  die "network create succeeded but post-create inventory failed (status ${probe_rc}): ${networks_after}"
elif (( probe_rc != 0 )); then
  die "network create and post-create inventory both failed (create ${network_rc}, probe ${probe_rc}): ${create_output}; ${networks_after}"
fi
(( network_rc == 0 )) || die "network create failed (status ${network_rc}): ${create_output}"
(( CREATED_NETWORK == 1 )) || die 'network create reported success but exact-name inventory is absent'

explicit_status_begin
create_output="$(docker compose -p scotty -f "$COMPOSE_FILE" create --pull never scotty 2>&1)"
compose_rc=$?
containers_after="$(docker container ls -a --format '{{.Names}}' 2>&1)"
probe_rc=$?
explicit_status_end
if (( probe_rc == 0 )) && contains_exact_line "$containers_after" "$CONTAINER"; then
  CREATED_CONTAINER=1
elif (( probe_rc != 0 && compose_rc == 0 )); then
  CREATED_CONTAINER=1
  die "Compose create succeeded but post-create inventory failed (status ${probe_rc}): ${containers_after}"
elif (( probe_rc != 0 )); then
  die "Compose create and post-create inventory both failed (create ${compose_rc}, probe ${probe_rc}): ${create_output}; ${containers_after}"
fi
(( compose_rc == 0 )) || die "Compose create failed (status ${compose_rc}): ${create_output}"
(( CREATED_CONTAINER == 1 )) || die 'Compose create reported success but exact-name inventory is absent'
explicit_status_begin
authority="$(docker inspect --format '{{ index .Config.Labels "com.scotty.deployment" }}' "$CONTAINER" 2>&1)"
probe_rc=$?
explicit_status_end
(( probe_rc == 0 )) || die "created container ownership inspect failed (status ${probe_rc}): ${authority}"
[[ $authority == managed ]] || die 'created container ownership label mismatch'

verify_install
COMMITTED=1
printf 'Scotty deployment prepared: container created and never started.\n'
