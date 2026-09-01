.PHONY: format-check lint shellcheck typecheck test acceptance package smoke oauth-probe scan checksums verify

format-check:
	uvx ruff@0.12.9 format --check assistant tests tools setup-scotty scotty-credential-broker scotty-supervisor

lint:
	uvx ruff@0.12.9 check assistant tests tools setup-scotty scotty-credential-broker scotty-supervisor
	$(MAKE) shellcheck

shellcheck:
	shellcheck -x install.sh scotty-start firewall/scotty-egress-guard tests/*.sh

typecheck:
	uvx mypy@1.17.1 assistant/scotty_business assistant/scotty_guard assistant/scotty_broker assistant/scotty_supervisor tools

test:
	./tests/test.sh

acceptance:
	python3 tools/synthetic_acceptance.py

package:
	python3 tools/build_package.py
	cd dist && sha256sum -c scotty-business-1.0.0.tar.gz.sha256
	cd dist && sha256sum -c scotty-guard-1.0.0.tar.gz.sha256

smoke:
	python3 tools/pinned_smoke.py

oauth-probe:
	python3 tools/pinned_oauth_probe.py

scan:
	python3 tools/scan_repository.py

checksums:
	python3 tools/generate_checksums.py --check
	sha256sum -c SHA256SUMS

verify: format-check lint typecheck test acceptance package smoke scan checksums
