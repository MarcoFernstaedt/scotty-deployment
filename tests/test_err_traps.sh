#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT
TMP="$(mktemp -d)" || exit 1
readonly TMP
readonly LOG="$TMP/actions.log"
readonly MOCK_BIN="$TMP/bin"
readonly GUARD_MOCK="$TMP/guard-mock"
readonly HARNESS="$TMP/install-cleanup-harness"

cleanup_temp() {
  rm -rf -- "$TMP"
}
trap cleanup_temp EXIT INT TERM
mkdir -p "$MOCK_BIN"

cat >"$MOCK_BIN/docker" <<'MOCK_DOCKER'
#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >>"${MOCK_LOG:?}"
case "$*" in
  "container ls -a --format {{.Names}}") printf 'scotty\n'; exit 0 ;;
  "inspect --format {{ index .Config.Labels \"com.scotty.deployment\" }} scotty") printf 'managed\n'; exit 0 ;;
  "container rm scotty") exit 4 ;;
  "network ls --format {{.Name}}") printf 'scotty-egress\n'; exit 0 ;;
  "network inspect --format {{ index .Labels \"com.scotty.deployment\" }} scotty-egress") printf 'managed\n'; exit 0 ;;
  "network rm scotty-egress") exit 0 ;;
  *) exit 64 ;;
esac
MOCK_DOCKER

cat >"$MOCK_BIN/systemctl" <<'MOCK_SYSTEMCTL'
#!/usr/bin/env bash
set -u
printf 'systemctl %s\n' "$*" >>"${MOCK_LOG:?}"
exit 0
MOCK_SYSTEMCTL

cat >"$MOCK_BIN/rm" <<'MOCK_RM'
#!/usr/bin/env bash
set -u
printf 'rm %s\n' "$*" >>"${MOCK_LOG:?}"
exit 0
MOCK_RM

cat >"$MOCK_BIN/rmdir" <<'MOCK_RMDIR'
#!/usr/bin/env bash
set -u
printf 'rmdir %s\n' "$*" >>"${MOCK_LOG:?}"
exit 0
MOCK_RMDIR

cat >"$GUARD_MOCK" <<'MOCK_GUARD'
#!/usr/bin/env bash
set -u
printf 'guard %s\n' "$*" >>"${MOCK_LOG:?}"
exit 0
MOCK_GUARD
chmod 0755 "$MOCK_BIN/docker" "$MOCK_BIN/systemctl" "$MOCK_BIN/rm" "$MOCK_BIN/rmdir" "$GUARD_MOCK"

sed -e '/^preflight$/,$d' \
  -e "s|^readonly GUARD_BIN=.*|readonly GUARD_BIN='$GUARD_MOCK'|" \
  "$ROOT/install.sh" >"$HARNESS"
cat >>"$HARNESS" <<'HARNESS_EOF'
COMMITTED=1
CREATED_CONTAINER=1
CREATED_NETWORK=1
ENABLED_GUARD=1
INSTALLED_UNIT=1
INSTALLED_GUARD=1
RELOADED_SYSTEMD=1
INSTALLED_COMPOSE=1
CREATED_OPERATOR=1
CREATED_DATA=1
CREATED_ROOT=1
cleanup 1
HARNESS_EOF
chmod 0755 "$HARNESS"

MOCK_LOG="$LOG" PATH="$MOCK_BIN:$PATH" bash "$HARNESS" >/dev/null 2>&1
harness_rc=$?
if (( harness_rc == 0 )); then
  printf 'ERR trap test: cleanup unexpectedly succeeded\n' >&2
  exit 1
fi

required_actions=(
  'docker container rm scotty'
  'docker network rm scotty-egress'
  'systemctl disable --now scotty-egress-guard.service'
  'guard remove'
  'rm -f -- /etc/systemd/system/scotty-egress-guard.service'
  "rm -f -- $GUARD_MOCK"
  'rm -f -- /srv/Scotty/operator/compose.yaml'
  'rmdir -- /srv/Scotty/operator'
  'rmdir -- /srv/Scotty/data'
  'rmdir -- /srv/Scotty'
)
for action in "${required_actions[@]}"; do
  if ! grep -Fqx -- "$action" "$LOG"; then
    printf 'ERR trap test: cleanup stopped before action: %s\n' "$action" >&2
    printf 'ERR trap test actions:\n' >&2
    while IFS= read -r logged_action; do
      printf '  %s\n' "$logged_action" >&2
    done <"$LOG"
    exit 1
  fi
done

printf 'ERR trap test: PASS\n'
