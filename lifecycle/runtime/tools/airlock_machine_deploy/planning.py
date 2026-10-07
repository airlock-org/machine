"""Pure topology validation and lifecycle planning."""

from .common import DeploymentError
from .models import DeploymentPlan, Topology


def automatic_network_pci_owner(topology):
    """The USB lab's sole root provider that discovers physical network PCI."""
    owners = [
        name
        for name in ("sys-agent", "sys-net")
        if (domain := topology.get("domains", {}).get(name))
        and domain.get("prefs", {}).get("provides_network") is True
        and not domain.get("netvm")
        and (
            domain.get("features", {}).get("airlock-auto-network-pci") == "1"
            or name == "sys-net"
        )
        and not domain.get("devices", {}).get("pci")
    ]
    if len(owners) > 1:
        raise DeploymentError("multiple automatic network PCI owners: " + ", ".join(owners))
    return owners[0] if owners else None


def domain_references(topology):
    domains = topology["domains"]
    result = {}
    defaults = topology.get("prefs", {})
    for name, domain in domains.items():
        prefs = domain.get("prefs", {})
        refs = [domain.get("netvm"), domain.get("template")]
        refs += [
            prefs.get(key, defaults.get("default_" + key))
            for key in ("guivm", "audiovm", "dispvm")
        ]
        refs.append(prefs.get("default_dispvm", defaults.get("default_dispvm")))
        devices = domain.get("devices", {})
        if isinstance(devices, dict):
            for assignments in devices.values():
                refs += [device.get("backend") for device in assignments]
        result[name] = {ref for ref in refs if ref and ref != name}
    return result


def topology_references(topology):
    """Explicit machine-wide references that must be removed before retirement."""
    defaults = topology.get("prefs", {})
    vm_prefs = (
        "clockvm",
        "default_dispvm",
        "default_netvm",
        "default_guivm",
        "default_audiovm",
        "default_template",
        "management_dispvm",
        "updatevm",
    )
    refs = {
        f"prefs.{key}": defaults[key]
        for key in vm_prefs
        if isinstance(defaults.get(key), str)
    }
    for index, rule in enumerate(topology.get("qrexec_policy", {}).get("rules", [])):
        for endpoint in ("source", "target"):
            refs[f"qrexec_policy.rules[{index}].{endpoint}"] = rule.get(endpoint)
    return refs


def dependencies(topology):
    domains = topology["domains"]
    return {
        name: {ref for ref in refs if ref in domains}
        for name, refs in domain_references(topology).items()
    }


def gui_reconnect_candidate(old, new, name):
    """Only an unchanged, capable client of the same sys-gui can stay up."""
    before = old["domains"][name]
    after = new["domains"].get(name)
    return (
        before == after
        and before.get("features", {}).get("airlock-gui-reconnect") == "1"
        and before.get("prefs", {}).get("guivm", old.get("prefs", {}).get("default_guivm"))
        == "sys-gui"
        and "sys-gui" in new["domains"]
        and name != "sys-gui"
    )


def restart_dependencies(old, new):
    refs = dependencies(old)
    # Other uses of sys-gui (networking, audio, storage or devices) remain hard
    # dependencies even when the same VM also uses it for display.
    without_gui = {
        **old,
        "prefs": {**old.get("prefs", {}), "default_guivm": None},
        "domains": {
            name: {**domain, "prefs": {**domain.get("prefs", {}), "guivm": None}}
            for name, domain in old["domains"].items()
        },
    }
    hard_refs = dependencies(without_gui)
    for name in refs:
        if gui_reconnect_candidate(old, new, name) and "sys-gui" not in hard_refs[name]:
            refs[name].discard("sys-gui")
    return refs


def ordered(topology):
    pending = dependencies(topology)
    result = []
    while pending:
        ready = sorted(name for name, deps in pending.items() if not deps - set(result))
        if not ready:
            raise DeploymentError("VM dependency cycle: " + ", ".join(sorted(pending)))
        result.extend(ready)
        for name in ready:
            del pending[name]
    return result


def validate_retirement(new, removed, retire_removed, trashed, retain_removed):
    # A trashed VM is still registered with Qubes, so setup-config's ordinary
    # "domain exists" validation cannot distinguish it from an active target.
    unavailable = removed | (set(trashed) - new["domains"].keys())
    authorized = set(retire_removed)
    if authorized - removed:
        raise DeploymentError(
            "retirement does not match removed VMs: "
            + ", ".join(sorted(authorized - removed))
        )
    unauthorized = removed - authorized
    if unauthorized and not retain_removed:
        raise DeploymentError(
            "VM removal requires explicit retirement: "
            + ", ".join(sorted(unauthorized))
        )
    for name, refs in domain_references(new).items():
        blocked = refs & unavailable
        if blocked:
            raise DeploymentError(
                f"{name}: still depends on retired VM(s): " + ", ".join(sorted(blocked))
            )
    for field, target in topology_references(new).items():
        if target in unavailable:
            raise DeploymentError(f"{field}: still references retired VM {target}")


def validate_domain_update(name, domain, before):
    backend = domain.get("backend", {})
    if backend.get("type") != "nix-closure":
        raise DeploymentError(
            f"{name}: only Nix-closure VM deployment is supported"
        )
    if backend.get("store", {}).get("mode") != "shared-closure":
        raise DeploymentError(f"{name}: shared-closure storage is required")
    if name not in before:
        return
    previous = before[name]
    for key in ("class", "template", "devices"):
        if previous.get(key) != domain.get(key):
            raise DeploymentError(f"{name}: changing {key} requires a migration")
    for key in ("type", "state"):
        if previous.get("backend", {}).get(key) != backend.get(key):
            raise DeploymentError(
                f"{name}: changing backend {key} requires a migration"
            )
    for key in ("prefs", "features", "services"):
        missing = previous.get(key, {}).keys() - domain.get(key, {}).keys()
        if missing:
            raise DeploymentError(
                f"{name}: removing {key} requires explicit reset values"
            )


def validate_transition(old, new, retire_removed, trashed, retain_removed):
    removed = old["domains"].keys() - new["domains"].keys()
    validate_retirement(new, removed, retire_removed, trashed, retain_removed)
    for name, domain in new["domains"].items():
        validate_domain_update(name, domain, old["domains"])
    # setup-config is a converger, not a garbage collector.
    for key in ("prefs",) if retain_removed else ("kernels", "templates", "prefs"):
        if old.get(key, {}).keys() - new.get(key, {}).keys():
            raise DeploymentError(f"removing topology {key} requires a migration")


def plan(
    old: Topology,
    new: Topology,
    running: set[str],
    retire_removed=(),
    trashed=(),
    retain_removed: bool = False,
) -> DeploymentPlan:
    """Conservative lifecycle plan; never silently ignore destructive changes."""
    before, after = old["domains"], new["domains"]
    removed = before.keys() - after.keys()
    validate_transition(old, new, retire_removed, trashed, retain_removed)
    old_pci_owner = automatic_network_pci_owner(old)
    new_pci_owner = automatic_network_pci_owner(new)
    changed = {name for name in after if before.get(name) != after[name]}
    if old.get("kernels") != new.get("kernels") or old.get("prefs") != new.get("prefs"):
        changed.update(after)
    affected = set(changed) | removed
    deps = restart_dependencies(old, new)
    while True:
        expanded = affected | {name for name, refs in deps.items() if refs & affected}
        if expanded == affected:
            break
        affected = expanded
    start = {
        name
        for name in after
        if name in running or after[name].get("prefs", {}).get("autostart") is True
    }
    new_deps = dependencies(new)
    while True:
        expanded = start | set().union(*(new_deps[name] for name in start))
        if expanded == start:
            break
        start = expanded
    result = {
        "changed": sorted(changed),
        "stage": sorted(
            name
            for name in changed
            if before.get(name, {}).get("backend") != after[name].get("backend")
        ),
        "stop": [name for name in reversed(ordered(old)) if name in affected & running],
        "start": [name for name in ordered(new) if name in start],
        "affected": sorted(affected),
        "retire": sorted(removed),
        "restore": sorted(set(after) & set(trashed)),
        "retained_undeclared": sorted(removed),
    }
    reconnect = sorted(
        name for name in running - affected
        if name in before and gui_reconnect_candidate(old, new, name)
        and "sys-gui" in affected
    )
    if reconnect:
        result["gui_reconnect"] = {"sys-gui": reconnect}
    if old_pci_owner and new_pci_owner and old_pci_owner != new_pci_owner:
        result["pci_handoff"] = {"from": old_pci_owner, "to": new_pci_owner}
    return result


def references_domain(topology: Topology, name: str) -> bool:
    """Include Qubes defaults and policy endpoints when protecting recovery state."""
    return (
        name in topology["domains"]
        or any(name in refs for refs in domain_references(topology).values())
        or name in topology_references(topology).values()
    )
