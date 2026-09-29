"""Bind machine setup to the chosen host generation, not the latest update."""
from pathlib import Path

from ._engine import core, host, state

MACHINE = '/var/lib/airlock/machine'


def binding_path(storage, record):
    generation = record['generation']
    if type(generation) is not int or generation < 1:
        core.fail('invalid host generation for machine binding')
    return Path(storage) / 'generations' / str(generation) / 'machine.json'


def save(root, storage, machine, record):
    machine = Path(machine)
    pointer = machine / 'selection.json'
    selection = core.read_json(pointer) if pointer.exists() else None
    if selection:
        release = Path(selection['release'])
        # Normal direct-pool candidates are durable files outside /nix/store.
        # Do not let boot acquire a dependency on a newer store view to find
        # the chosen generation's machine description.
        if not release.is_relative_to(machine) or '..' in release.parts:
            core.fail('host boot binding requires a local machine candidate')
        if core.read_json(release / 'release.json')['host'] != record['system']:
            core.fail('machine selection differs from host boot binding')
        staged = core.read_json(release / 'staged.json') if (release / 'staged.json').exists() else {}
    else:
        # Capture the initial image's pool identities before the first update.
        staged = {}
        for domain in (Path(root) / 'domains').iterdir():
            if domain.name == 'dom0' or domain.name.endswith('-builder'):
                continue
            generation = state.current_generation(root, domain.name)
            if generation is not None:
                staged[domain.name] = generation
    core.atomic_json(binding_path(storage, record), {
        'schema': 'airlock.host-machine.v1', 'system': record['system'],
        'selection': selection, 'staged': staged,
    }, mode=0o600)


def restore(sysroot, record):
    sysroot = Path(sysroot)
    storage = sysroot / host.HOST.lstrip('/')
    machine = sysroot / MACHINE.lstrip('/')
    path = binding_path(storage, record)
    if not path.exists():
        # Only the image's first, unpublished runtime state has no binding.
        # Later generations must be bound by apply before they become bootable.
        if record['generation'] != 1 or (machine / 'selection.json').exists():
            core.fail('host generation has no machine boot binding')
        return
    binding = core.read_json(path)
    if binding.get('schema') != 'airlock.host-machine.v1' or binding.get('system') != record['system']:
        core.fail('host machine boot binding mismatch')
    selection = binding['selection']
    machine.mkdir(parents=True, exist_ok=True)
    if selection:
        release = Path(selection['release'])
        if not release.is_relative_to(MACHINE) or '..' in release.parts:
            core.fail('invalid bound machine candidate')
    restore_selection(sysroot, machine, storage, record, binding)


def restore_selection(sysroot, machine, storage, record, binding):
    selection = binding['selection']
    if selection:
        release = sysroot / Path(selection['release']).relative_to('/')
        manifest = core.read_json(release / 'release.json')
        if manifest['host'] != record['system']:
            core.fail('bound release differs from booted host')
        transaction = selection['transaction']
        if not isinstance(transaction, str) or not transaction or Path(transaction).name != transaction or transaction in ('.', '..'):
            core.fail('invalid bound transaction')
        status = core.read_json(machine / 'transactions' / transaction / 'status.json')
        if status.get('release') != selection['release']:
            core.fail('bound transaction differs from machine selection')
        if status['phase'] not in ('complete', 'reboot-required'):
            core.fail('bound machine deployment is incomplete; use independent recovery')
        expected = manifest.get('stagedGenerations', {})
        if expected and (expected != binding['staged'] or core.read_json(release / 'staged.json') != expected):
            core.fail('bound guest generations differ from machine release')
        core.atomic_json(machine / 'selection.json', selection)
        core.atomic_json(machine / 'latest.json', status)
    else:
        (machine / 'selection.json').unlink(missing_ok=True)
        (machine / 'latest.json').unlink(missing_ok=True)
        core.fsync_dir(machine)
    core.atomic_json(storage / 'boot-staged.json', binding['staged'])
    # Runtime control must use the chosen host's code, not a symlink to the
    # newest deployment's coordinator in another generation's writable layer.
    temporary = machine / 'controller.new'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(record['system'] + '/sw/bin/airlock-machine-deploy')
    temporary.replace(machine / 'controller')
    core.fsync_dir(machine)
