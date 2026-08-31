#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT
readonly IMAGE='nousresearch/hermes-agent@sha256:d64f4e9aba92884fff3d5020c02a75676066f237622d0776759ca1437b9b0517'
readonly IMAGE_ID='sha256:f002ea7b37bec9aef0213f155dc27efe3fbc47eeb5b46d109115dae9a45dcf63'

fail() {
  printf 'test: FAIL: %s\n' "$*" >&2
  exit 1
}

cd -- "$ROOT"

set +e
nonroot_output="$(cd / && "$ROOT/install.sh" 2>&1)"
nonroot_rc=$?
set -e
[[ $nonroot_rc == 1 && $nonroot_output == *'must run as root'* ]] || fail 'non-root preflight did not fail closed'
[[ ! -e /srv/Scotty ]] || fail 'non-root preflight mutated /srv/Scotty'

bash -n install.sh scotty-start firewall/scotty-egress-guard tests/test.sh tests/test_firewall_cleanup.sh tests/test_err_traps.sh tests/test_symlink_preflight.sh
shellcheck -x install.sh scotty-start firewall/scotty-egress-guard tests/test.sh tests/test_firewall_cleanup.sh tests/test_err_traps.sh tests/test_symlink_preflight.sh
python3 -m compileall -q assistant tests tools
python3 -m py_compile setup-scotty
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tools/generate_checksums.py --check
sha256sum -c SHA256SUMS

docker compose -f compose.yaml config --quiet
rendered="$(docker compose -f compose.yaml config)"
[[ $rendered == *"image: ${IMAGE}"* ]] || fail 'rendered image digest mismatch'
[[ $rendered == *'source: /srv/Scotty/data'* ]] || fail 'rendered mount source mismatch'
[[ $rendered == *'target: /opt/data'* ]] || fail 'rendered mount target mismatch'
[[ $(grep -c 'source: /srv/Scotty/data' <<<"$rendered") == 1 ]] || fail 'rendered mount cardinality mismatch'
[[ $rendered != *'ports:'* ]] || fail 'ports are forbidden'
[[ $rendered != *'devices:'* ]] || fail 'devices are forbidden'
[[ $rendered != *'user:'* ]] || fail 'Compose user override is forbidden'
[[ $rendered != *'/var/run/docker.sock'* && $rendered != *'/run/podman/podman.sock'* ]] || fail 'container-engine sockets are forbidden'
[[ $rendered == *'HERMES_UID: "10000"'* && $rendered == *'HERMES_GID: "10000"'* ]] || fail 'runtime UID/GID controls are missing'

if grep -Eq 'docker compose .* (up|start|run)( |$)|docker (container )?start( |$)' install.sh; then
  fail 'installer contains a container start path'
fi

# Match fixed installer source literals; dollar signs are intentionally literal.
# shellcheck disable=SC2016
compose_create_line="$(grep -nF 'docker compose -p scotty -f "$COMPOSE_FILE" create --pull never scotty' install.sh | cut -d: -f1)"
mapfile -t container_owned_lines < <(grep -nFx '  CREATED_CONTAINER=1' install.sh | cut -d: -f1)
# shellcheck disable=SC2016
container_inspect_line="$(grep -nF 'authority="$(docker inspect' install.sh | cut -d: -f1)"
[[ -n $compose_create_line && ${#container_owned_lines[@]} -gt 0 && -n $container_inspect_line ]] || fail 'container transaction markers are missing'
for container_owned_line in "${container_owned_lines[@]}"; do
  (( compose_create_line < container_owned_line && container_owned_line < container_inspect_line )) || fail 'container ownership is not marked before the fallible ownership inspect'
done

actual_id="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
[[ $actual_id == "$IMAGE_ID" ]] || fail "local image ID mismatch: ${actual_id}"
actual_digest="$(docker image inspect "$IMAGE" --format '{{index .RepoDigests 0}}')"
[[ $actual_digest == "$IMAGE" ]] || fail "local image digest mismatch: ${actual_digest}"

[[ ! -e /srv/Scotty ]] || fail 'tests refuse to run with live /srv/Scotty state present'

for forbidden in .env .env.local .env.production; do
  [[ ! -e $forbidden ]] || fail "forbidden environment file: ${forbidden}"
done

while IFS= read -r path; do
  case $path in
    *.db|*.sqlite|*.sqlite3|*.log|*.cache|*.bak|*.backup|*.pem|*.key|*.p12|*.pfx|*~)
      fail "forbidden artifact: ${path}"
      ;;
  esac
done < <(git ls-files --cached --others --exclude-standard 2>/dev/null || printf '%s\n' README.md compose.yaml install.sh firewall/scotty-egress-guard firewall/scotty-egress-guard.service tests/test.sh)

if grep -RInE --exclude-dir=.git --exclude=SHA256SUMS --exclude='test.sh' -- '(BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|[0-9]{8,12}:[A-Za-z0-9_-]{20,})' .; then
  fail 'credential-like content detected'
fi

[[ ! -e rollback.sh && ! -e uninstall.sh ]] || fail 'persistent rollback or uninstall script is forbidden'
# Match fixed source literals; dollar signs are intentionally literal.
# shellcheck disable=SC2016
[[ $(grep -Fxc -- '  "-A ${CHAIN} -j ACCEPT"' firewall/scotty-egress-guard) == 1 ]] || fail 'firewall terminal ACCEPT rule mismatch'
# shellcheck disable=SC2016
[[ $(grep -Fxc -- '  "-A ${CHAIN} -d 100.64.0.0/10 -j REJECT"' firewall/scotty-egress-guard) == 1 ]] || fail 'Tailnet block missing'
# shellcheck disable=SC2016
[[ $(grep -Fxc -- '  "-A ${CHAIN} -d 169.254.0.0/16 -j REJECT"' firewall/scotty-egress-guard) == 1 ]] || fail 'metadata/link-local block missing'
# shellcheck disable=SC2016
[[ $(grep -Fxc -- '  "-A ${CHAIN} -d 224.0.0.0/4 -j REJECT"' firewall/scotty-egress-guard) == 1 ]] || fail 'multicast block missing'

./tests/test_firewall_cleanup.sh
./tests/test_err_traps.sh
./tests/test_symlink_preflight.sh

printf 'test: PASS\n'
