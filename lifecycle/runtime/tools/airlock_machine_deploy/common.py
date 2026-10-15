"""Shared deployment schemas and errors."""

SCHEMA = "airlock.machine-release.v1"
ADMISSION_SCHEMA = "airlock.closure-store.admission.v1"
GENERATION_INFO_SCHEMA = "airlock.closure-store.generation-info.v1"
TRASH_SCHEMA = "airlock.machine-trash.v1"
DEFAULT_TRASH_RETENTION_DAYS = 30
RETIRED_POLICY = "/var/lib/airlock/qubes-policy.d/05-airlock-retired.policy"


class DeploymentError(RuntimeError):
    pass


class DeploymentBusy(DeploymentError):
    def __init__(self, error):
        self.holder = error.holder
        super().__init__("another machine operation is running: " + str(error))
