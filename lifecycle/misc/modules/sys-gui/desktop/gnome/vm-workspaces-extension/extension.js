import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {WindowIdentityCache, LOCAL, UNKNOWN, validateIdentity} from './identity.js';
import {parseInventory} from './inventory.js';
import {MAX_SLOTS, readLayout, reconcileGroups, serializeLayout, workspaceContents, initialDestination} from './model.js';

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
        const local = this._groups.find(g => g.key === 'local');
        while (index < this._wm.n_workspaces)
            local.slots.push(this._wm.get_workspace_by_index(index++));
        this._indicator = new PanelMenu.Button(0, 'VM workspaces', true);
        this._box = new St.BoxLayout({style_class: 'airlock-workspaces'});
        // Limit panel width while retaining access to groups through horizontal scrolling.
        this._scroll = new St.ScrollView({style: 'max-width: 620px;',
            hscrollbar_policy: St.PolicyType.AUTOMATIC, vscrollbar_policy: St.PolicyType.NEVER});
        this._scroll.set_child(this._box);
        this._indicator.add_child(this._scroll);
        Main.panel.addToStatusArea('airlock-vm-workspaces', this._indicator, 2, 'left');
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
        const existing = new Set(Array.from({length: this._wm.n_workspaces},
            (_, index) => this._wm.get_workspace_by_index(index)));
        for (const group of this._groups) {
            for (let i = 0; i < group.slots.length; i++) {
                if (!existing.has(group.slots[i]))
                    group.slots[i] = this._wm.append_new_workspace(false, global.get_current_time());
            }
            if (!group.slots.includes(group.lastUsed))
                group.lastUsed = null;
        }
        const assigned = new Set(this._groups.flatMap(g => g.slots));
        const local = this._groups.find(g => g.key === 'local');
        for (let i = 0; i < this._wm.n_workspaces; i++) {
            const workspace = this._wm.get_workspace_by_index(i);
            if (!assigned.has(workspace))
                local.slots.push(workspace);
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
        this._indicator.visible = !Main.sessionMode.isLocked && !Main.sessionMode.isGreeter;
    }

    _loadInventory() {
        const revision = ++this._inventoryRevision;
        const cancellable = this._cancellable;
        this._inventoryFile.load_contents_async(cancellable, (file, result) => {
            try {
                const [, bytes] = file.load_contents_finish(result);
                if (!this._active || cancellable.is_cancelled() || revision !== this._inventoryRevision || bytes.length > 1048576)
                    return;
                this._inventory = parseInventory(new TextDecoder().decode(bytes));
                reconcileGroups(this._groups, this._inventory);
                this._sync();
                this._queue();
            } catch (_error) { /* Retain the previous valid snapshot. */ }
        });
    }

    _render() {
        this._box.destroy_all_children();
        this._buttons = [];
        for (const group of this._groups) {
            const box = new St.BoxLayout({style_class: 'airlock-workspace-group'});
            if (group.color)
                box.add_child(new St.Widget({style_class: 'airlock-workspace-dot',
                    y_align: Clutter.ActorAlign.CENTER, style: `background-color: ${group.color};`}));
            box.add_child(new St.Label({text: group.name, style_class: 'airlock-workspace-name',
                y_align: Clutter.ActorAlign.CENTER}));
            group.slots.forEach((workspace, i) => {
                const button = new St.Button({label: `${i + 1}`, style_class: 'airlock-workspace-slot', can_focus: true});
                button.connect('clicked', () => workspace.activate(global.get_current_time()));
                this._buttons.push({group, workspace, button, number: i + 1});
                box.add_child(button);
            });
            const add = new St.Button({label: '+', style_class: 'airlock-workspace-add', can_focus: true,
                accessible_name: `Add workspace for ${group.name}`});
            add.reactive = this._wm.n_workspaces < MAX_SLOTS;
            add.connect('clicked', () => {
                if (this._wm.n_workspaces >= MAX_SLOTS)
                    return;
                group.slots.push(null);
                this._sync();
                group.slots.at(-1).activate(global.get_current_time());
            });
            box.add_child(add);
            this._box.add_child(box);
        }
        this._refresh();
    }

    _refresh() {
        if (!this._active || !this._buttons)
            return;
        const active = this._wm.get_active_workspace();
        for (const {group, workspace, button, number} of this._buttons) {
            const identities = [...this._windows.keys()].filter(w => w.located_on_workspace(workspace))
                .map(w => this._identities.get(w) ?? UNKNOWN);
            const contents = workspaceContents(group.name, identities);
            button.set_label(`${number}${contents.mixed ? '!' : ''}`);
            button.accessible_name = `${group.name}, workspace ${number}${contents.names.length ? `; windows from ${contents.names.join(', ')}` : '; empty'}`;
            for (const [name, enabled] of [['active', workspace === active], ['mixed', contents.mixed]]) {
                if (enabled)
                    button.add_style_class_name(name);
                else
                    button.remove_style_class_name(name);
            }
            if (workspace === active)
                group.lastUsed = workspace;
        }
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
        this._indicator.destroy();
        // Restore preferences only when they still match our last settings.
        // Keep occupied workspace objects until GNOME handles its normal cleanup.
        if (this._preferences.get_int('num-workspaces') === this._ownedCount)
            this._preferences.set_int('num-workspaces', this._originalCount);
        if (!this._mutter.get_boolean('dynamic-workspaces'))
            this._mutter.set_boolean('dynamic-workspaces', this._originalDynamic);
    }
}
