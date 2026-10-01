"""JSON contracts used by deployment plans and durable journals.

TypedDict preserves the existing dictionaries and their on-disk representation.
Nix/Qubes-owned domain configuration remains an open mapping.
"""

from typing import Any, Literal, NotRequired, TypedDict


class Topology(TypedDict):
    schema: str
    domains: dict[str, dict[str, Any]]
    prefs: NotRequired[dict[str, Any]]
    kernels: NotRequired[dict[str, Any]]
    templates: NotRequired[dict[str, Any]]
    qrexec_policy: NotRequired[dict[str, Any]]


class RetirementDetail(TypedDict):
    domain: str
    uuid: str
    private: str | None
    pool_generation: int
    pool_manifest: str | None
    retention_days: int


class DeploymentPlan(TypedDict):
    changed: list[str]
    stage: list[str]
    stop: list[str]
    start: list[str]
    affected: list[str]
    retire: list[str]
    restore: list[str]
    retained_undeclared: list[str]
    pci_handoff: NotRequired[dict[str, str]]
    retirement: NotRequired[list[RetirementDetail]]
    host_reboot_required: NotRequired[bool]
    revision: NotRequired[str]
    private_copies: NotRequired[list[dict[str, Any]]]


DeploymentPhase = Literal[
    "preflight", "staging", "host-boot-preparation", "stopping", "retiring", "configuring",
    "host-boot-selection", "copying-private", "starting", "verifying", "reboot-required",
    "complete", "failed",
]


class DeploymentRecord(TypedDict):
    id: str
    boot_id: str
    release: str
    previous: str
    revision: str
    plan: DeploymentPlan
    phase: DeploymentPhase
    observed: NotRequired[dict[str, dict[str, str | None]]]
    trash: NotRequired[dict[str, list[str]]]
    host_running: NotRequired[str]
    host_selected: NotRequired[str]
    recovered_by: NotRequired[str]
    error: NotRequired[str]
    failed_phase: NotRequired[DeploymentPhase]
    trash_active: NotRequired[list[str]]
    verified_at: NotRequired[float]


class Selection(TypedDict):
    release: str
    previous: str
    transaction: str
    profile: str


class TrashStorage(TypedDict):
    disposition: Literal["registered-stopped", "active", "purged"]
    private: str | None
    pool_manifest: str | None
    volumes: dict[str, dict[str, Any]]


class TrashRecord(TypedDict):
    schema: str
    id: str
    status: Literal["planned", "retiring", "trashed", "purging", "purged", "restored"]
    domain: str
    uuid: str
    domain_config: dict[str, Any]
    source_release: str
    source_transaction: str
    pool_generation: int
    retired_at: str | None
    purge_after: float
    retention_days: int
    storage: TrashStorage
    restored_at: NotRequired[str]
    restored_by_release: NotRequired[str]
    restored_by_transaction: NotRequired[str]
    purged_at: NotRequired[str]
    purged_before_expiry: NotRequired[bool]


class PrunePlan(TypedDict):
    schema: str
    release: str
    host: str
    manifest: dict[str, Any]
    roots: dict[str, str]
    profile_generations: list[int]
    blockers: list[str]
    waived_blockers: NotRequired[list[str]]


class PruneRecord(PrunePlan):
    status: Literal["pruning", "pruned"]
    started_at: float
    finished_at: NotRequired[float]
