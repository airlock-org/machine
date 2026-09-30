# Machine deployment implementation

`../machine-deploy.py` remains the command-line entry point. Its Nix package
includes this directory alongside the entry point; importing it must not depend
on the checkout or a development `PYTHONPATH`.

- `cli.py` parses commands and formats responses and errors.
- `coordinator.py` sequences staging, deployment, recovery, and boot activation.
- `planning.py` computes lifecycle plans from topology data without performing I/O.
- `validation.py` checks release, admission, and storage ownership data without I/O.
- `admission.py` authorizes exact guest artifacts from an imported candidate,
  including new domain names, without creating VMs or selecting a host generation.
- `retirement.py` handles VM retirement, restoration, and resumable trash purging.
- `releases.py` handles release inventory, retention, and resumable pruning.
- `state.py` loads releases and performs durable JSON writes.
- `models.py` describes plan and journal dictionaries without changing their JSON.

The coordinator supplies command execution, runtime inventory, and the shared
machine lock. Public purge and pruning operations acquire that same lock.
Retirement helpers called during deployment or boot use the caller's existing
lock, so they must not acquire a second lock. Keep journal writes and mutations
in order: interrupted operations are resumed from those records.

The installed host exposes `admission-prepare` through controller-only
`airlock.Ctrl.MachinePrepare`. Preparation pins the transport and writes a
root-owned token bound to its guest identities, caller, boot and deployment
state. `admission-receive` holds the machine lock before taking the pool lock
and uses the existing incremental artifact receiver. Both the reuse probe and
payload inventory must match the candidate; a reused generation retains its
original verified provenance. The receiver emits the same single-line pool
protocol as ordinary admission. Host activation remains deferred to reboot.

The existing CLI, journal fields, schema identifiers, and recovery behavior are
preserved. Tests in `tests/test_machine_deploy.py` exercise those contracts,
including interruption, rollback, shared locking, and installed-package imports.

Host and controller preparation can extend an authenticated EROFS index when
its registered objects are an additive subset of the next store. Existing
logical offsets stay fixed; new fragments append before the terminal tar EOF.
The predecessor image is copied and never edited. The erofs-utils overlay adds
`--tar-offset` so upstream incremental tar-index construction can address the
appended fragments in the combined external data device. Fresh images, removed
objects, legacy controller indexes without integrity metadata, and depth 16
use a full index build. Rebuilds bound appended metadata growth.

Generation metadata binds the index checksum and logical blob layout. Existing
pool admission, physical-range checks, retention pins, boot checksums and
journalled publication still apply. Compaction may move physical ranges while
preserving logical addresses. Host publication timing output now separates
object verification/admission, view preparation and index construction, and
reports the number of inherited and newly indexed objects for host/controller
views. This allows changes in preparation cost to be measured separately from
package transfer volume.
