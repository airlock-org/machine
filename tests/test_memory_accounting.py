"""Exercise the C helpers used by the packaged Qubes guest reporter.

The quick suite uses an existing compiler, and never provisions one. The
qubes-linux-utils package check always runs these cases with its compiler.
"""

import ctypes
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADER = Path(os.environ.get(
    "AIRLOCK_MEMINFO_ACCOUNTING_HEADER",
    ROOT / "lifecycle/misc/patches/qubes-linux-utils/memory-accounting.h",
))


@pytest.fixture(scope="module")
def accounting(tmp_path_factory):
    compiler = shlex.split(os.environ.get("CC", "cc"))
    if not shutil.which(compiler[0]):
        pytest.skip("C compiler absent; these regressions also run in the Nix package check")
    directory = tmp_path_factory.mktemp("memory-accounting")
    source = directory / "wrapper.c"
    source.write_text('''
#define _GNU_SOURCE
#include "memory-accounting.h"
int read_pcp(const char *text, long page_size, long long *result) {
    FILE *stream = fmemopen((void *)text, strlen(text), "r");
    if (!stream) return 0;
    int ok = pcp_free_kib(stream, page_size, result);
    fclose(stream);
    return ok;
}
int consistent(long long before, long long after, int threshold) {
    return pcp_sample_consistent(before, after, threshold);
}
int used(long long total, long long free_kib, long long pcp,
         long long buffers, long long cached, long long swap_total,
         long long swap_free, long long *result) {
    return memory_used_kib(total, free_kib, pcp, buffers, cached,
                           swap_total, swap_free, result);
}
''')
    library = directory / "accounting.so"
    subprocess.run(compiler + [
        "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
        "-I", str(HEADER.parent), str(source), "-o", str(library),
    ], check=True, capture_output=True, text=True)
    lib = ctypes.CDLL(str(library))
    integer = ctypes.c_longlong
    lib.read_pcp.argtypes = [ctypes.c_char_p, ctypes.c_long, ctypes.POINTER(integer)]
    lib.consistent.argtypes = [integer, integer, ctypes.c_int]
    lib.used.argtypes = [integer] * 7 + [ctypes.POINTER(integer)]
    return lib


def zone(counts, name="Normal"):
    return f"Node 0, zone {name}\n  pages free 123\n  pagesets\n" + "".join(
        f"    cpu: {cpu}\n              count: {count}\n"
        "              high: 999999\n              batch: 63\n"
        "  vm stats threshold: 32\n"
        for cpu, count in enumerate(counts)
    ) + "  node_unreclaimable: 0\n"


def used(accounting, free_kib, pcp, total=1048576, cached=65536):
    result = ctypes.c_longlong(-1)
    assert accounting.used(total, free_kib, pcp, 0, cached, 0, 0,
                           ctypes.byref(result))
    return result.value


@pytest.mark.parametrize("page_size", [4096, 16384, 65536])
def test_sums_all_zones_and_cpus_without_counting_high_limits(accounting, page_size):
    result = ctypes.c_longlong(-1)
    data = zone([0, 17], "DMA32") + zone([2000, 3000])
    assert accounting.read_pcp(data.encode(), page_size, ctypes.byref(result))
    assert result.value == 5017 * (page_size // 1024)


def test_moving_free_pages_between_buddy_and_pcp_preserves_demand(accounting):
    results = [used(accounting, 600000 - pcp, pcp)
               for pcp in (0, 4096, 128000, 320000, 500000, 128000, 0)]
    assert results == [383040] * len(results)


def test_real_allocations_and_balloon_reservation_changes(accounting):
    baseline = used(accounting, 600000, 0)
    assert used(accounting, 500000, 0) == baseline + 100000
    # A larger Xen allocation is all free, not additional workload demand.
    assert used(accounting, 700000, 100000, total=1248576) == baseline
    assert used(accounting, 600000, 0, cached=75536) == baseline - 10000


def test_swap_demand_is_retained(accounting):
    result = ctypes.c_longlong(-1)
    assert accounting.used(1000000, 400000, 100000, 10000, 50000,
                           200000, 170000, ctypes.byref(result))
    assert result.value == 470000


@pytest.mark.parametrize("text", [
    "", "Node 0, zone Normal\n", "pagesets\n    cpu: 0\n",
    zone(["bad"]), zone([-1]), zone(["12garbage"]),
    zone(["18446744073709551616"]),
    zone(["18446744073709551615", 1]),
    zone(["9223372036854775807"]),
    zone([0]).replace("count: 0", "high: 0"),
    zone([0]).replace("cpu: 0\n", "cpu: 0\n    cpu: 1\n"),
    zone([0]).replace("count: 0\n", "count: 0\n              count: 1\n"),
    zone([0]).rstrip("\n"),
    zone([17]) + "Node 1, zone Normal\n pagesets\n cpu: 0\n count: bad\n",
])
def test_malformed_or_partial_pcp_input_never_returns_partial_correction(accounting, text):
    result = ctypes.c_longlong(123456)
    assert not accounting.read_pcp(text.encode(), 4096, ctypes.byref(result))
    assert result.value == 123456


@pytest.mark.parametrize("page_size", [-1, 0, 512, 4097])
def test_invalid_page_size_rejected(accounting, page_size):
    result = ctypes.c_longlong(-1)
    assert not accounting.read_pcp(zone([1]).encode(), page_size, ctypes.byref(result))


@pytest.mark.parametrize("before,after,expected", [
    (100000, 100000, True), (100000, 115000, True),
    (115000, 100000, True), (100000, 115001, False),
    (500000, 100000, False), (-1, 0, False),
])
def test_pcp_transfer_during_snapshot_requires_retry(accounting, before, after, expected):
    assert bool(accounting.consistent(before, after, 30000)) is expected


@pytest.mark.parametrize("values", [
    (100, 50, 60, 0, 0, 0, 0),
    (100, 0, 0, 0, 0, 0, 1),
    (100, -1, 0, 0, 0, 0, 0),
    (9223372036854775807, 0, 0, 0, 0, 1, 0),
])
def test_invalid_combined_snapshots_and_overflow_are_rejected(accounting, values):
    result = ctypes.c_longlong(-1)
    assert not accounting.used(*values, ctypes.byref(result))
    assert result.value == -1


def test_zero_pcp_fallback_preserves_upstream_formula(accounting):
    assert used(accounting, 600000, 0) == 1048576 - 600000 - 65536


def test_unused_large_swap_does_not_overflow(accounting):
    result = ctypes.c_longlong(-1)
    maximum = 9223372036854775807
    assert accounting.used(maximum, 0, 0, 0, 0, maximum, maximum,
                           ctypes.byref(result))
    assert result.value == maximum
