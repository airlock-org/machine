import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {WindowIdentityCache, LOCAL, UNKNOWN, validateIdentity} from './identity.js';
import {parseInventory} from './inventory.js';
import {readLayout, reconcileGroups, reuseWorkspaces, serializeLayout, workspaceContents, initialDestination} from './model.js';

const READER = '@identityReader@';
const TYPES = [Meta.WindowType.NORMAL, Meta.WindowType.DIALOG, Meta.WindowType.MODAL_DIALOG];

export default class VMWorkspacesExtension extends Extension {
    enable() {
        this._active = true;
        this._scanning = false;
        this._dirty = false;
        this._signals = [];
        this._windows = new Map();
        this._identities = new Map();
        this._pendingPlacement = new Set();
        this._cache = new WindowIdentityCache();
        this._inventory = new Map();
        this._inventoryFingerprint = null;
        this._inventoryRevision = 0;
        this._cancellable = new Gio.Cancellable();
        this._wm = global.workspace_manager;
        this._mutter = new Gio.Settings({schema_id: 'org.gnome.mutter'});
        this._preferences = new Gio.Settings({schema_id: 'org.gnome.desktop.wm.preferences'});
        this._originalDynamic = this._mutter.get_boolean('dynamic-workspaces');
        this._originalCount = this._preferences.get_int('num-workspaces');
        this._preferences.set_int('num-workspaces', this._wm.n_workspaces);
        this._mutter.set_boolean('dynamic-workspaces', false);
        const directory = GLib.build_filenamev([GLib.get_user_state_dir(), 'airlock-vm-workspaces']);
        GLib.mkdir_with_parents(directory, 0o700);
        this._layoutFile = GLib.build_filenamev([directory, 'layout.json']);
        let saved = [];
        try {
            const [, bytes] = GLib.file_get_contents(this._layoutFile);
            if (bytes.length < 65536)
                saved = readLayout(new TextDecoder().decode(bytes));
        } catch (_error) { /* A new session starts with one slot per VM. */ }
        this._groups = saved.map(g => ({key: g.key, slots: Array(g.count).fill(null)}));
        reconcileGroups(this._groups, this._inventory);
        // Bind the existing desktop in place. Enabling never relocates open windows.
        let index = 0;
        for (const group of this._groups) {
            for (let i = 0; i < group.slots.length && index < this._wm.n_workspaces; i++)
                group.slots[i] = this._wm.get_workspace_by_index(index++);
        }
        // Keep GNOME's actual Activities actor, its gestures, accessibility role,
        // overview animation and panel ownership. Only replace its passive child.
        this._activities = Main.panel.statusArea.activities;
        this._nativeIndicators = this._activities.get_first_child();
        this._originalAccessibleName = this._activities.accessible_name;
        this._box = new St.BoxLayout({style_class: 'airlock-workspaces',
            reactive: false, can_focus: false, clip_to_allocation: true,
            style: 'max-width: 620px;'});
        this._connect(this._wm, 'workspace-switched', () => this._refresh());
        this._connect(this._wm, 'workspace-added', () => this._queue());
        this._connect(this._wm, 'workspace-removed', () => this._queue());
        this._connect(Main.sessionMode, 'updated', () => this._sessionChanged());
        this._sessionChanged();
        this._connect(global.display, 'window-created', (_display, window) => {
            this._watch(window);
            if (window.get_window_type() === Meta.WindowType.NORMAL)
                this._pendingPlacement.add(window);
            this._queue();
        });
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window);
        const menus = GLib.build_filenamev([GLib.get_user_state_dir(), 'airlock-appmenus']);
        GLib.mkdir_with_parents(menus, 0o700);
        this._inventoryFile = Gio.File.new_for_path(GLib.build_filenamev([menus, 'inventory.json']));
        this._monitor = Gio.File.new_for_path(menus).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, null);
        this._connect(this._monitor, 'changed', (_monitor, file, other) => {
            if ([file, other].some(f => f?.get_basename() === 'inventory.json'))
                this._loadInventory();
        });
        this._sync();
        this._loadInventory();
        this._queue();
    }

    _connect(object, signal, callback) {
        this._signals.push([object, object.connect(signal, callback)]);
    }

    _watch(window) {
        if (!window || this._windows.has(window) || !TYPES.includes(window.get_window_type()))
            return;
        this._cache.watch(window);
        const changed = window.connect('workspace-changed', () => {
            // An explicit move during a slow identity lookup takes precedence.
            this._pendingPlacement.delete(window);
            this._refresh();
        });
        const unmanaged = window.connect('unmanaged', () => {
            window.disconnect(changed);
            window.disconnect(unmanaged);
            this._windows.delete(window);
            this._identities.delete(window);
            this._pendingPlacement.delete(window);
            this._refresh();
        });
        this._windows.set(window, [changed, unmanaged]);
    }

    _sync() {
        // Never reuse indexes as slot identity: insertion must keep neighbouring
        // workspaces and all their windows attached to the same VM label.
        if (!this._groups.length) {
            this._ownedCount = this._wm.n_workspaces;
            this._render();
            return;
        }
        const workspaces = Array.from({length: this._wm.n_workspaces},
            (_, index) => this._wm.get_workspace_by_index(index));
        const occupied = new Set(workspaces.filter(workspace => workspace.list_windows()
            .some(window => TYPES.includes(window.get_window_type()))));
        const unused = reuseWorkspaces(this._groups, workspaces, occupied);
        for (const group of this._groups) {
            for (let i = 0; i < group.slots.length; i++) {
                if (!group.slots[i])
                    group.slots[i] = this._wm.append_new_workspace(false, global.get_current_time());
            }
            if (!group.slots.includes(group.lastUsed))
                group.lastUsed = null;
        }
        if (unused.includes(this._wm.get_active_workspace()))
            this._groups[0].slots[0].activate(global.get_current_time());
        for (const workspace of unused) {
            // Only redundant empty slots are removed. Occupied objects retain
            // their windows and are assigned a normal application-group label.
            this._wm.remove_workspace(workspace, global.get_current_time());
        }
        this._groups.flatMap(g => g.slots).forEach((workspace, index) => {
            if (workspace.index() !== index)
                this._wm.reorder_workspace(workspace, index);
        });
        this._ownedCount = this._wm.n_workspaces;
        this._preferences.set_int('num-workspaces', this._ownedCount);
        GLib.file_set_contents(this._layoutFile, serializeLayout(this._groups));
        this._render();
    }

    _sessionChanged() {
        // GNOME still decides whether Activities belongs in the current panel.
        // Keep our replacement child hidden during locking without changing the
        // actor's visibility, handlers or session-mode management.
        this._box.visible = !Main.sessionMode.isLocked && !Main.sessionMode.isGreeter;
    }

    _useVMDisplay(enabled) {
        const child = enabled ? this._box : this._nativeIndicators;
        const previous = this._activities.get_first_child();
        if (previous === child)
            return;
        if (previous)
            this._activities.remove_child(previous);
        this._activities.add_child(child);
    }

    _loadInventory() {
        const revision = ++this._inventoryRevision;
        const cancellable = this._cancellable;
        this._inventoryFile.load_contents_async(cancellable, (file, result) => {
            let inventory;
            try {
                const [, bytes] = file.load_contents_finish(result);
                if (!this._active || cancellable.is_cancelled() || revision !== this._inventoryRevision || bytes.length > 1048576)
                    return;
                inventory = parseInventory(new TextDecoder().decode(bytes));
            } catch (_error) {
                return; // Retain the previous valid snapshot.
            }
            const fingerprint = JSON.stringify([...inventory].sort(([a], [b]) => a.localeCompare(b)));
            if (fingerprint === this._inventoryFingerprint) {
                this._queue();
                return;
            }
            this._inventory = inventory;
            reconcileGroups(this._groups, this._inventory);
            try {
                this._sync();
                this._inventoryFingerprint = fingerprint;
                this._queue();
            } catch (error) {
                this._inventoryFingerprint = null;
                console.error(`Airlock workspace layout: ${error.message}`);
            }
        });
    }

    _render() {
        this._box.destroy_all_children();
        this._dots = [];
        for (const group of this._groups) {
            const box = new St.BoxLayout({style_class: 'airlock-workspace-group',
                reactive: false, can_focus: false});
            box.add_child(new St.Label({text: group.name, style_class: 'airlock-workspace-name',
                reactive: false, can_focus: false, y_align: Clutter.ActorAlign.CENTER}));
            group.slots.forEach((workspace, i) => {
                const dot = new St.Widget({style_class: 'airlock-workspace-dot',
                    reactive: false, can_focus: false, y_align: Clutter.ActorAlign.CENTER,
                    style: `background-color: ${group.color ?? '#bbb'};`});
                this._dots.push({group, workspace, dot, number: i + 1});
                box.add_child(dot);
            });
            this._box.add_child(box);
        }
        this._useVMDisplay(this._groups.length > 0);
        this._sessionChanged();
        this._refresh();
    }

    _refresh() {
        if (!this._active || !this._dots)
            return;
        const active = this._wm.get_active_workspace();
        const descriptions = [];
        for (const {group, workspace, dot, number} of this._dots) {
            const identities = [...this._windows.keys()].filter(w => w.located_on_workspace(workspace))
                .map(w => this._identities.get(w) ?? UNKNOWN);
            const contents = workspaceContents(group.name, identities);
            descriptions.push(`${group.name}, workspace ${number}${workspace === active ? ', current' : ''}${contents.names.length ? `; windows from ${contents.names.join(', ')}` : '; empty'}`);
            if (workspace === active) {
                dot.add_style_class_name('active');
                group.lastUsed = workspace;
            } else {
                dot.remove_style_class_name('active');
            }
        }
        this._activities.accessible_name = descriptions.length
            ? `${this._originalAccessibleName}: ${descriptions.join('. ')}`
            : this._originalAccessibleName;
    }

    _queue() {
        if (!this._active)
            return;
        this._dirty = true;
        if (this._source || this._scanning)
            return;
        this._source = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
            this._source = 0;
            this._scan();
            return GLib.SOURCE_REMOVE;
        });
    }

    async _scan() {
        this._scanning = true;
        this._dirty = false;
        const cache = this._cache;
        const cancellable = this._cancellable;
        try {
            // A single bounded identity reader at a time, with the same trusted
            // provenance checks as the existing focused-VM panel.
            for (const window of [...this._windows.keys()]) {
                if (!this._active || cancellable.is_cancelled())
                    break;
                const identity = await this._resolve(window, cache, cancellable, new Set());
                if (!this._active || cancellable.is_cancelled() || !this._windows.has(window))
                    continue;
                this._identities.set(window, validateIdentity(identity));
                if (identity.kind === 'local')
                    this._pendingPlacement.delete(window);
                if (this._pendingPlacement.has(window) && identity.kind !== 'unknown') {
                    const destination = initialDestination(this._groups, identity, window.get_workspace(),
                        window.get_transient_for()?.get_workspace());
                    if (destination) {
                        this._pendingPlacement.delete(window);
                        const focused = global.display.focus_window === window;
                        window.change_workspace(destination);
                        if (focused && !Main.sessionMode.isLocked && !Main.sessionMode.isGreeter)
                            destination.activate(global.get_current_time());
                    }
                }
            }
            if (this._active) {
                // Also absorb an extra workspace created by GNOME itself.
                if (this._wm.n_workspaces !== this._ownedCount)
                    this._sync();
                this._refresh();
            }
        } catch (error) {
            if (this._active)
                console.error(`Airlock workspace identity: ${error.message}`);
        } finally {
            if (this._cancellable === cancellable) {
                this._scanning = false;
                if (this._active && this._dirty)
                    this._queue();
            }
        }
    }

    async _resolve(window, cache, cancellable, seen) {
        if (seen.has(window) || cancellable.is_cancelled())
            return UNKNOWN;
        seen.add(window);
        const cached = cache.get(window);
        if (cached)
            return cached;
        if (window.get_client_type() === Meta.WindowClientType.WAYLAND)
            return LOCAL;
        const xid = /^(0x[0-9a-fA-F]+)\s/.exec(window.get_description())?.[1];
        if (!xid)
            return UNKNOWN;
        cache.watch(window);
        const identity = await this._read(xid, cancellable);
        if (identity.kind === 'absent') {
            const parent = window.get_transient_for();
            return parent ? this._resolve(parent, cache, cancellable, seen) : UNKNOWN;
        }
        cache.remember(window, identity);
        return identity;
    }

    _read(xid, cancellable) {
        return new Promise(resolve => {
            if (cancellable.is_cancelled()) {
                resolve(UNKNOWN);
                return;
            }
            const process = Gio.Subprocess.new([READER, xid],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            this._process = process;
            process.communicate_utf8_async(null, cancellable, (source, result) => {
                if (this._process === process)
                    this._process = null;
                try {
                    const [, stdout] = source.communicate_utf8_finish(result);
                    resolve(source.get_successful() ? JSON.parse(stdout) : UNKNOWN);
                } catch (_error) {
                    resolve(UNKNOWN);
                }
            });
        });
    }

    disable() {
        this._active = false;
        ++this._inventoryRevision;
        if (this._source)
            GLib.Source.remove(this._source);
        this._source = 0;
        this._cancellable?.cancel();
        this._process?.force_exit();
        this._monitor?.cancel();
        for (const [object, id] of this._signals)
            object.disconnect(id);
        for (const [window, signals] of this._windows) {
            for (const id of signals)
                window.disconnect(id);
        }
        this._windows.clear();
        this._cache.clear();
        this._useVMDisplay(false);
        this._activities.accessible_name = this._originalAccessibleName;
        this._box.destroy();
        // Restore preferences only when they still match our last settings.
        // Keep occupied workspace objects until GNOME handles its normal cleanup.
        if (this._preferences.get_int('num-workspaces') === this._ownedCount)
            this._preferences.set_int('num-workspaces', this._originalCount);
        if (!this._mutter.get_boolean('dynamic-workspaces'))
            this._mutter.set_boolean('dynamic-workspaces', this._originalDynamic);
    }
}
