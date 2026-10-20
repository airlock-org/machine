SHELL := bash

# Prefer explicitly supplied or installed native tooling; the parent bootstrap supports source checkouts.
AIRLOCK_DEV_BIN ?= $(or $(shell command -v airlock-dev 2>/dev/null),$(abspath ../lifecycle/misc/tools/scripts/airlock-dev.sh))
AIRLOCK_DEV = $(AIRLOCK_DEV_BIN) --repository-root "$(CURDIR)"
AIRLOCK_NIX = $(AIRLOCK_DEV) with-environment -- environment run-store -- nix
AIRLOCK_LOG_DIR ?= $(if $(AIRLOCK_DATA_ROOT),$(AIRLOCK_DATA_ROOT)/logs/airlock-machine,$(if $(wildcard /run/media/user/data/.),/run/media/user/data/airlock/logs/airlock-machine,$(CURDIR)/.airlock-logs))
# The application checkout owns shared Rust integration tests for both repositories.
AIRLOCK_TEST_SOURCE ?= $(abspath ..)

.PHONY: clean-generated test-quick test-nix check-eval machine-build machine-build-gui gc-auto gc-roots gc-dry-run gc

check-eval:
	$(AIRLOCK_NIX) flake check --no-build "$$( $(MAKE) --silent prepare )"

test-quick test-nix:
	@test -f "$(AIRLOCK_TEST_SOURCE)/crates/airlock-tests/Cargo.toml" || { echo 'Set AIRLOCK_TEST_SOURCE to the application checkout with prepared native tests' >&2; exit 1; }
	$(MAKE) -C "$(AIRLOCK_TEST_SOURCE)" $@ AIRLOCK_LOG_DIR="$(AIRLOCK_LOG_DIR)"

machine-build:
	$(AIRLOCK_NIX) build "$$( $(MAKE) --silent prepare )"#airlock-machine-release-usb-lab --print-build-logs

machine-build-gui:
	$(AIRLOCK_NIX) build "$$( $(MAKE) --silent prepare )"#airlock-machine-release-usb-gui-lab --print-build-logs

gc-auto gc-roots gc-dry-run gc:
	$(AIRLOCK_DEV) gc $(patsubst gc-%,%,$@)

# Authoring dependencies must be explicitly prepared or supplied by the installed VM.
AIRLOCK_APPS ?= /etc/airlock/config-inputs/airlock-apps
AIRLOCK_CARGO_VENDOR ?= /etc/airlock/authoring/vendor

AIRLOCK_PREPARED_CACHE ?= $(CURDIR)/target/nix
OUTPUT ?=

.PHONY: prepare generate check-generated
prepare generate:
	cargo --config 'patch.crates-io.airlock-config.path="$(AIRLOCK_APPS)/airlock-config"' \
	  --config 'source.airlock-admitted.directory="$(AIRLOCK_CARGO_VENDOR)"' \
	  --config 'build.rustflags=["--remap-path-prefix=$(AIRLOCK_APPS)/="]' run --locked --offline -- \
	  $(if $(OUTPUT),--emit "$(OUTPUT)",prepare "$(AIRLOCK_PREPARED_CACHE)")

# Repeated preparation verifies an existing content-addressed artifact before reusing it.
check-generated: prepare

rust-check:
	cargo fmt --all -- --check
	cargo clippy --config 'patch.crates-io.airlock-config.path="$(AIRLOCK_APPS)/airlock-config"' \
	  --config 'source.airlock-admitted.directory="$(AIRLOCK_CARGO_VENDOR)"' \
	  --config 'build.rustflags=["--remap-path-prefix=$(AIRLOCK_APPS)/="]' --locked --offline --all-targets -- -D warnings

# Explicit cleanup remains guarded by the native development tool.
clean-generated:
	$(AIRLOCK_DEV) clean $(MODE)
