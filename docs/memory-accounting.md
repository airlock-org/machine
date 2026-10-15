# Qubes guest memory accounting

The pinned Linux 6.12 allocator excludes free pages held in per-CPU page
caches (PCP lists) from `NR_FREE_PAGES`, exposed as `MemFree`. Qubes
`qubes-linux-utils` 4.3.17 subtracts `MemFree`, buffers and file cache from
Xen's current reservation to calculate guest demand. Consequently, moving free
pages between the buddy allocator and PCP lists changes its reported demand.
Proportional allocation of spare memory can amplify that change into repeated
balloon target reversals.

The guest-only `qubes-memory-accounting.nix` overlay patches the upstream C
reporter. It sums each zone/CPU's actual `count:` in `/proc/zoneinfo`, converts
pages using `_SC_PAGESIZE`, and subtracts this reusable memory as well:

```
used_kib = xen_current_kib - MemFree - PCPFree - Buffers - Cached
           + SwapTotal - SwapFree
```

This retains Qubes' Xenstore interface, polling interval, update threshold,
swap demand and pressure reporting. PCP reads bracket the meminfo read inside
the Xen-current consistency check. A PCP change greater than half the update
threshold causes a retry; eight unsuccessful attempts defer to the next poll.
Invalid or unavailable PCP data logs a warning and falls back to the upstream
formula, without applying a partial correction. Arithmetic overflow and
negative combined samples are rejected.

The patch targets the allocator accounting in the pinned Qubes 6.12 kernel.
Recheck that behavior when upgrading kernels: a kernel that includes PCP free
pages in `MemFree` must not subtract them a second time.

## Validation

`tests/test_memory_accounting.py` compiles and exercises the same C header used
by the reporter: buddy/PCP transfers, real allocations, Xen reservation changes,
swap, multiple zones and CPUs, page sizes, malformed input, overflow and
inconsistent snapshots. The package's mandatory check phase runs all 36 cases.
The quick suite runs them when a compiler is already available and otherwise
skips them, without provisioning tools. Focused integration checks:

```
make test-nix PYTEST_TARGETS=tests/test_memory_accounting_nix.py
```

Physical validation on 2026-10-15 temporarily switched only usr-assistant's
reporter using an automatically cleaned `/run` override. A touched 512 MiB
allocation increased median reported demand from 973,816 to 1,513,536 KiB;
after release it returned to 995,832 KiB. Restoring the original reporter
produced 82 target changes and 21.8% of one core in the balloon thread over
45 seconds. A second corrected run's final 45 seconds produced 21 changes and
5.6% CPU. Other guests still ran the original reporter, and the workloads were
not replayed identically; this comparison establishes a local improvement,
not a guarantee of zero balancing or a complete machine-wide outcome.

Both diagnostic runs restored the original service and removed their temporary
override and binary. Permanent activation requires a committed sys-ctrl build
and deployment. The full pre-deployment quick suite passed (854 passed,
86 skipped); the package C checks and two focused Nix checks passed.

Detailed captures and build/test logs for this run are in
`~/.local/state/airlock/logs/memory-balance-fix/`. Verify the deployed package
path, guest generations, balloon CPU, target changes and response to real
allocation pressure after activation.
