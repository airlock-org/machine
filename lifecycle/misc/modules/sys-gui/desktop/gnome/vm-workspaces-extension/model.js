// Workspace objects stay attached to their slots when a neighbouring group grows.
// UUIDs, rather than names, preserve a user's layout across VM renames.
export const MAX_SLOTS = 36;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function readLayout(text) {
    try {
        const data = JSON.parse(text);
        if (data.version !== 1 || !Array.isArray(data.groups))
            return [];
        const seen = new Set();
        let total = 0;
        const groups = data.groups.filter(group => {
            if (!group || !UUID.test(group.key) ||
                seen.has(group.key) || !Number.isInteger(group.count) ||
                group.count < 1 || total + group.count > MAX_SLOTS)
                return false;
            seen.add(group.key);
            total += group.count;
            return true;
        }).map(({key, count}) => ({key, count}));
        return groups;
    } catch (_error) {
        return [];
    }
}

export function reconcileGroups(groups, inventory) {
    // The GUI domain is the desktop infrastructure, not an application group.
    for (let i = groups.length - 1; i >= 0; i--) {
        if (groups[i].key === 'local')
            groups.splice(i, 1);
    }
    // Keep absent/stopped VM slots: inventory refreshes must not merge their windows.
    for (const group of groups) {
        const vm = inventory.get(group.key);
        group.name = vm?.name ?? 'Unavailable VM';
        group.color = vm?.color ?? null;
    }
    const newVMs = [...inventory].filter(([key]) => !groups.some(g => g.key === key))
        .sort((a, b) => Number(a[1].name.startsWith('sys-')) - Number(b[1].name.startsWith('sys-')) ||
            a[1].name.localeCompare(b[1].name));
    for (const [key, vm] of newVMs) {
        if (groups.reduce((n, g) => n + g.slots.length, 0) >= MAX_SLOTS)
            break;
        groups.push({key, ...vm, slots: [null]});
    }
    return groups;
}

export function reuseWorkspaces(groups, workspaces, occupied) {
    if (!groups.length)
        return [];
    const existing = new Set(workspaces);
    const assigned = new Set(groups.flatMap(group => group.slots).filter(slot => existing.has(slot)));
    const available = workspaces.filter(slot => !assigned.has(slot));
    for (const group of groups) {
        group.slots = group.slots.map(slot => existing.has(slot) ? slot : available.shift() ?? null);
    }
    // Keep occupied leftovers intact, under the personal VM's group. Native
    // desktop windows can share an ordinary application workspace.
    const fallback = groups.find(group => group.name.startsWith('usr-')) ?? groups[0];
    const unused = [];
    for (const workspace of available) {
        if (occupied.has(workspace))
            fallback.slots.push(workspace);
        else
            unused.push(workspace);
    }
    return unused;
}

export function serializeLayout(groups) {
    return JSON.stringify({version: 1, groups: groups.map(g => ({key: g.key, count: g.slots.length}))});
}

export function workspaceContents(owner, identities) {
    const names = [...new Set(identities.filter(i => i?.kind !== 'local')
        .map(i => i?.kind === 'unknown' || !i ? 'Unidentified' : i.name))];
    return {mixed: names.some(name => name !== owner), names};
}

export function initialDestination(groups, identity, current, parent) {
    // This is a one-time suggestion. No rule ever moves a window back later.
    if (parent)
        return parent;
    const group = groups.find(g => g.name === identity?.name && identity?.kind === 'vm');
    if (!group)
        return null;
    return group.slots.includes(current) ? current : group.lastUsed ?? group.slots[0];
}
