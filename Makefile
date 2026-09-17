SHELL := bash

AIRLOCK_NIX := ./lifecycle/misc/tools/scripts/airlock-nix.sh
AIRLOCK_LOG_DIR ?= /run/media/user/data/airlock/logs/airlock-machine

.PHONY: test-quick check-eval machine-build machine-build-gui gc-auto gc-roots gc-dry-run gc

check-eval:
	$(AIRLOCK_NIX) flake check --no-build

test-quick:
	@mkdir -p "$(AIRLOCK_LOG_DIR)"
	uv run pytest \
		tests/test_machine_deploy.py \
		tests/test_pool_artifact.py \
		tests/test_pool_admission_module.py \
		tests/test_amd_vbios_runtime.py \
		tests/test_input_events.py \
		--junitxml "$(AIRLOCK_LOG_DIR)/pytest-junit.xml" \
		--json-report --json-report-file "$(AIRLOCK_LOG_DIR)/pytest-report.json"

machine-build:
	$(AIRLOCK_NIX) build .#airlock-machine-release-usb-lab --print-build-logs

machine-build-gui:
	$(AIRLOCK_NIX) build .#airlock-machine-release-usb-gui-lab --print-build-logs

gc-auto gc-roots gc-dry-run gc:
	./lifecycle/misc/tools/scripts/gc-airlock-store.sh $(patsubst gc-%,%,$@)
