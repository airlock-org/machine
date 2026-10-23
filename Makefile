SHELL := bash

# The installed authoring environment supplies these admitted, offline inputs.
AIRLOCK_APPS ?= /etc/airlock/config-inputs/airlock-apps
AIRLOCK_CARGO_VENDOR ?= /etc/airlock/authoring/vendor
AIRLOCK_PREPARED_CACHE ?= $(CURDIR)/target/nix
OUTPUT ?=

AUTHORING_CONFIG = --config 'patch.crates-io.airlock-config.path="$(AIRLOCK_APPS)/airlock-config"' \
	--config 'source.airlock-admitted.directory="$(AIRLOCK_CARGO_VENDOR)"' \
	--config 'build.rustflags=["--remap-path-prefix=$(AIRLOCK_APPS)/="]'

.PHONY: prepare rust-check

prepare:
	cargo run $(AUTHORING_CONFIG) --locked --offline -- \
	  $(if $(OUTPUT),--emit "$(OUTPUT)",prepare "$(AIRLOCK_PREPARED_CACHE)")

rust-check:
	cargo fmt --all -- --check
	cargo clippy $(AUTHORING_CONFIG) --locked --offline --all-targets -- -D warnings
