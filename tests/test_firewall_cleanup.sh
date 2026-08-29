#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT
readonly GUARD="$ROOT/firewall/scotty-egress-guard"
TMP="$(mktemp -d)" || exit 1
readonly TMP

cleanup() {
  rm -rf -- "$TMP"
}
trap cleanup EXIT INT TERM

MOCK="$TMP/iptables-mock"
HARNESS="$TMP/scotty-egress-guard"
readonly MOCK HARNESS

cat >"$MOCK" <<'MOCK_EOF'
#!/usr/bin/env bash
set -u
state=${MOCK_STATE:?}
mode=${MOCK_FAIL_MODE:?}

jump=$(<"$state/jump")
chain=$(<"$state/chain")
flushed=$(<"$state/flushed")

if [[ ${1:-} == -w && ${2:-} == 5 ]]; then
  shift 2
fi

emit_chain_rules() {
  printf '%s\n' '-N SCOTTY-EGRESS'
  [[ $flushed == 1 ]] && return
  printf '%s\n' \
    '-A SCOTTY-EGRESS -m addrtype --dst-type LOCAL -j REJECT' \
    '-A SCOTTY-EGRESS -d 0.0.0.0/8 -j REJECT' \
    '-A SCOTTY-EGRESS -d 10.0.0.0/8 -j REJECT' \
    '-A SCOTTY-EGRESS -d 100.64.0.0/10 -j REJECT' \
    '-A SCOTTY-EGRESS -d 127.0.0.0/8 -j REJECT' \
    '-A SCOTTY-EGRESS -d 169.254.0.0/16 -j REJECT' \
    '-A SCOTTY-EGRESS -d 172.16.0.0/12 -j REJECT' \
    '-A SCOTTY-EGRESS -d 192.0.0.0/24 -j REJECT' \
    '-A SCOTTY-EGRESS -d 192.0.2.0/24 -j REJECT' \
    '-A SCOTTY-EGRESS -d 192.88.99.0/24 -j REJECT' \
    '-A SCOTTY-EGRESS -d 192.168.0.0/16 -j REJECT' \
    '-A SCOTTY-EGRESS -d 198.18.0.0/15 -j REJECT' \
    '-A SCOTTY-EGRESS -d 198.51.100.0/24 -j REJECT' \
    '-A SCOTTY-EGRESS -d 203.0.113.0/24 -j REJECT' \
    '-A SCOTTY-EGRESS -d 224.0.0.0/4 -j REJECT' \
    '-A SCOTTY-EGRESS -d 240.0.0.0/4 -j REJECT' \
    '-A SCOTTY-EGRESS -j ACCEPT'
}

case ${1:-} in
  -S)
    target=${2:-}
    if [[ -z $target ]]; then
      if [[ $mode == post_delete_probe_once && $chain == 0 && ! -e $state/post_probe_failed ]]; then
        : >"$state/post_probe_failed"
        exit 4
      fi
      printf '%s\n' '-N DOCKER-USER'
      [[ $chain == 1 ]] && printf '%s\n' '-N SCOTTY-EGRESS'
      exit 0
    fi
    if [[ $target == DOCKER-USER ]]; then
      printf '%s\n' '-N DOCKER-USER'
      [[ $jump == 1 ]] && printf '%s\n' '-A DOCKER-USER -s 172.30.50.0/24 -j SCOTTY-EGRESS'
      exit 0
    fi
    if [[ $target == SCOTTY-EGRESS && $chain == 1 ]]; then
      emit_chain_rules
      exit 0
    fi
    exit 1
    ;;
  -D)
    [[ ${2:-} == DOCKER-USER && $jump == 1 ]] || exit 1
    printf '0\n' >"$state/jump"
    exit 0
    ;;
  -F)
    [[ ${2:-} == SCOTTY-EGRESS && $chain == 1 ]] || exit 1
    if [[ $mode == fail_flush_once && ! -e $state/flush_failed ]]; then
      : >"$state/flush_failed"
      exit 4
    fi
    printf '1\n' >"$state/flushed"
    exit 0
    ;;
  -X)
    [[ ${2:-} == SCOTTY-EGRESS && $chain == 1 && $flushed == 1 ]] || exit 1
    if [[ $mode == fail_delete_once && ! -e $state/delete_failed ]]; then
      : >"$state/delete_failed"
      exit 4
    fi
    printf '0\n' >"$state/chain"
    exit 0
    ;;
  *) exit 64 ;;
esac
MOCK_EOF
chmod 0755 "$MOCK"
sed "s|^readonly IPTABLES=.*|readonly IPTABLES='$MOCK'|" "$GUARD" >"$HARNESS"
chmod 0755 "$HARNESS"

overall=0
run_case() {
  local mode=$1 state="$TMP/$1" first_rc second_rc jump chain first_output expected_error
  mkdir -p "$state" || return 1
  printf '1\n' >"$state/jump"
  printf '1\n' >"$state/chain"
  printf '0\n' >"$state/flushed"

  case $mode in
    fail_flush_once) expected_error='chain flush did not reach empty state (status 4)' ;;
    fail_delete_once) expected_error='chain deletion did not reach absence (status 4)' ;;
    post_delete_probe_once) expected_error='iptables post-delete inventory failed (status 4)' ;;
    *) return 1 ;;
  esac

  first_output="$(MOCK_STATE="$state" MOCK_FAIL_MODE="$mode" bash "$HARNESS" remove 2>&1)"
  first_rc=$?
  MOCK_STATE="$state" MOCK_FAIL_MODE="$mode" bash "$HARNESS" remove >/dev/null 2>&1
  second_rc=$?
  jump=$(<"$state/jump")
  chain=$(<"$state/chain")

  if (( first_rc == 0 )); then
    printf 'firewall cleanup test: %s first removal unexpectedly succeeded\n' "$mode" >&2
    return 1
  fi
  if [[ $first_output != *"$expected_error"* ]]; then
    printf 'firewall cleanup test: %s bypassed explicit status handling: %s\n' "$mode" "$first_output" >&2
    return 1
  fi
  if (( second_rc != 0 )); then
    printf 'firewall cleanup test: %s retry failed with status %s\n' "$mode" "$second_rc" >&2
    return 1
  fi
  if [[ $jump != 0 || $chain != 0 ]]; then
    printf 'firewall cleanup test: %s left jump=%s chain=%s\n' "$mode" "$jump" "$chain" >&2
    return 1
  fi
  return 0
}

for mode in fail_flush_once fail_delete_once post_delete_probe_once; do
  run_case "$mode"
  case_rc=$?
  (( case_rc == 0 )) || overall=1
done

if (( overall != 0 )); then
  exit 1
fi
printf 'firewall cleanup test: PASS\n'
