#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT
TMP="$(mktemp -d)" || exit 1
readonly TMP
readonly MOCK_BIN="$TMP/bin"
mkdir -p "$MOCK_BIN"

cleanup_temp() {
  rm -rf -- "$TMP"
}
trap cleanup_temp EXIT INT TERM

cat >"$MOCK_BIN/docker" <<'MOCK_DOCKER'
#!/usr/bin/env bash
set -u
case "$*" in
  "--version") printf '%s\n' 'Docker version 26.1.5+dfsg1, build a72d7cd' ;;
  "compose version") printf '%s\n' 'Docker Compose version 2.26.1-4' ;;
  "compose create --help") printf '%s\n' '      --pull string' ;;
  "info --format {{json .SecurityOptions}}") printf '%s\n' '["name=apparmor","name=seccomp,profile=builtin","name=cgroupns"]' ;;
  *"image inspect"*"--format {{.Id}}") printf '%s\n' 'sha256:f002ea7b37bec9aef0213f155dc27efe3fbc47eeb5b46d109115dae9a45dcf63' ;;
  *"image inspect"*"--format {{index .RepoDigests 0}}") printf '%s\n' 'nousresearch/hermes-agent@sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517' ;;
  "compose -f "*"/compose.yaml config --quiet") exit 0 ;;
  "container ls -a --format {{.Names}}") exit 0 ;;
  "network ls --format {{.Name}}") exit 0 ;;
  *) printf 'unexpected docker probe: %s\n' "$*" >&2; exit 64 ;;
esac
MOCK_DOCKER

cat >"$MOCK_BIN/systemctl" <<'MOCK_SYSTEMCTL'
#!/usr/bin/env bash
set -u
if [[ $* == 'show scotty-egress-guard.service --property=LoadState --value' ]]; then
  printf 'not-found\n'
  exit 0
fi
exit 64
MOCK_SYSTEMCTL

cat >"$MOCK_BIN/iptables" <<'MOCK_IPTABLES'
#!/usr/bin/env bash
set -u
case "$*" in
  "--version") printf '%s\n' 'iptables v1.8.11 (nf_tables)' ;;
  "-w 5 -S") printf '%s\n' '-N DOCKER-USER' ;;
  "-w 5 -S DOCKER-USER") printf '%s\n' '-N DOCKER-USER' ;;
  *) exit 64 ;;
esac
MOCK_IPTABLES
cat >"$MOCK_BIN/sha256sum" <<'MOCK_SHA'
#!/usr/bin/env bash
exit 0
MOCK_SHA
chmod 0755 "$MOCK_BIN/docker" "$MOCK_BIN/systemctl" "$MOCK_BIN/iptables" "$MOCK_BIN/sha256sum"

prepare_normal_tree() {
  local case_root=$1
  mkdir -p "$case_root/srv" "$case_root/usr/local/libexec" "$case_root/etc/systemd/system"
}

install_harness() {
  local case_root=$1 harness=$2 marker=$3
  sed -e '/^preflight$/,$d' \
    -e "s|^SOURCE_DIR=.*|SOURCE_DIR='$ROOT'|" \
    -e "s|^readonly TARGET_ROOT=.*|readonly TARGET_ROOT='$case_root/srv/Scotty'|" \
    -e "s|^readonly GUARD_BIN=.*|readonly GUARD_BIN='$case_root/usr/local/libexec/scotty-egress-guard'|" \
    -e "s|^readonly GUARD_UNIT=.*|readonly GUARD_UNIT='$case_root/etc/systemd/system/scotty-egress-guard.service'|" \
    -e "s|/usr/sbin/iptables|$MOCK_BIN/iptables|g" \
    -e 's|^  (( EUID == 0 )).*|  : # disposable test bypasses only the root gate|' \
    "$ROOT/install.sh" >"$harness"
  cat >>"$harness" <<HARNESS_EOF
preflight
: >'$marker'
HARNESS_EOF
  chmod 0755 "$harness"
}

run_case() {
  local name=$1 kind=$2 relative=$3 case_root="$TMP/$1" harness marker link_path target rc
  harness="$case_root/harness"
  marker="$case_root/mutated"
  mkdir -p "$case_root"
  prepare_normal_tree "$case_root"
  link_path="$case_root/$relative"

  case $kind in
    dangling)
      mkdir -p "$(dirname -- "$link_path")"
      ln -s "$case_root/nonexistent-target" "$link_path"
      ;;
    ancestor)
      rm -rf -- "$link_path"
      target="$case_root/foreign-tree"
      mkdir -p "$target"
      ln -s "$target" "$link_path"
      ;;
    *) return 64 ;;
  esac

  install_harness "$case_root" "$harness" "$marker"
  PATH="$MOCK_BIN:$PATH" bash "$harness" >/dev/null 2>&1
  rc=$?
  if (( rc == 0 )); then
    printf 'symlink preflight test: accepted %s\n' "$name" >&2
    return 1
  fi
  if [[ -e $marker || -L $marker ]]; then
    printf 'symlink preflight test: mutation marker created for %s\n' "$name" >&2
    return 1
  fi
  if [[ ! -L $link_path ]]; then
    printf 'symlink preflight test: foreign symlink replaced for %s\n' "$name" >&2
    return 1
  fi
  return 0
}

overall=0
cases=(
  'target-root:dangling:srv/Scotty'
  'operator-dir:dangling:srv/Scotty/operator'
  'data-dir:dangling:srv/Scotty/data'
  'compose-file:dangling:srv/Scotty/operator/compose.yaml'
  'guard-bin:dangling:usr/local/libexec/scotty-egress-guard'
  'guard-unit:dangling:etc/systemd/system/scotty-egress-guard.service'
  'srv-ancestor:ancestor:srv'
  'usr-ancestor:ancestor:usr'
  'usr-local-ancestor:ancestor:usr/local'
  'libexec-ancestor:ancestor:usr/local/libexec'
  'etc-ancestor:ancestor:etc'
  'systemd-ancestor:ancestor:etc/systemd'
  'system-unit-ancestor:ancestor:etc/systemd/system'
)
for entry in "${cases[@]}"; do
  IFS=: read -r name kind relative <<<"$entry"
  run_case "$name" "$kind" "$relative"
  case_rc=$?
  (( case_rc == 0 )) || overall=1
done

if (( overall != 0 )); then
  exit 1
fi
printf 'symlink preflight test: PASS\n'
