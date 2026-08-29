.PHONY: format-check lint typecheck test package smoke scan checksums verify

format-check:
	uvx ruff@0.12.9 format --check assistant tests tools setup-scotty

lint:
	uvx ruff@0.12.9 check assistant tests tools setup-scotty
	shellcheck -x install.sh firewall/scotty-egress-guard tests/*.sh

typecheck:
	uvx mypy@1.17.1 assistant/scotty_business tools

test:
	./tests/test.sh

package:
	python3 tools/build_package.py
	cd dist && sha256sum -c scotty-business-1.0.0.tar.gz.sha256

smoke:
	python3 tools/pinned_smoke.py

scan:
	python3 tools/scan_repository.py

checksums:
	python3 tools/generate_checksums.py --check
	sha256sum -c SHA256SUMS

verify: format-check lint typecheck test package smoke scan checksums
