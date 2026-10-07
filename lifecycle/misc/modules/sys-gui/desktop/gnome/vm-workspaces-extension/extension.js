import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as DND from 'resource:///org/gnome/shell/ui/dnd.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {WindowIdentityCache, LOCAL, UNKNOWN, validateIdentity} from './identity.js';
import {parseInventory} from './inventory.js';
import {MAX_SLOTS, describeWorkspace, groupOccupiedWorkspaces} from './model.js';

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
        this._groups = [];
        this._indicator = new PanelMenu.Button(0.0, 'VM workspaces', true);
        this._indicator.reactive = false;
        this._indicator.can_focus = false;
        this._indicator.track_hover = false;
        this._indicator.add_style_class_name('airlock-workspace-bar');
        this._box = new St.BoxLayout({style_class: 'airlock-workspaces',
            reactive: false, clip_to_allocation: true, style: 'max-width: 480px;'});
        this._indicator.add_child(this._box);
        // GNOME owns the original left-hand Activities/workspace dots unchanged.
        Main.panel.addToStatusArea('airlock-vm-workspaces', this._indicator, 0, 'right');
        this._tooltip = new St.Label({style_class: 'dash-label', visible: false, reactive: false});
        Main.layoutManager.addChrome(this._tooltip);
        this._connect(this._wm, 'workspace-switched', () => this._refresh());
        this._connect(this._wm, 'workspace-added', () => this._queue());
        this._connect(this._wm, 'workspace-removed', () => this._queue());
        this._connect(this._wm, 'workspaces-reordered', () => this._queue());
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
            this._queue();
        });
        const unmanaged = window.connect('unmanaged', () => {
            window.disconnect(changed);
            window.disconnect(unmanaged);
            this._windows.delete(window);
            this._identities.delete(window);
            this._pendingPlacement.delete(window);
            this._queue();
        });
        this._windows.set(window, [changed, unmanaged]);
    }

    _workspaceWindows(workspace) {
        // Count sticky windows once, rather than creating an entry on every slot.
        return [...this._windows.keys()].filter(window => window.get_workspace() === workspace);
    }

    _sync() {
        const workspaces = Array.from({length: this._wm.n_workspaces},
            (_, index) => this._wm.get_workspace_by_index(index));
        const identities = new Map(workspaces.map(workspace => [workspace,
            this._workspaceWindows(workspace).map(window => this._identities.get(window) ?? UNKNOWN)]));
        this._groups = groupOccupiedWorkspaces(this._groups, workspaces, identities, this._inventory);
        // A local system prompt may occupy a workspace, but gets no sys-gui tab.
        const occupied = workspaces.filter(workspace => this._workspaceWindows(workspace).length);
        const active = this._wm.get_active_workspace();
        if (occupied.length && !occupied.includes(active)) {
            const nearest = occupied.reduce((best, workspace) =>
                Math.abs(workspace.index() - active.index()) < Math.abs(best.index() - active.index())
                    ? workspace : best);
            nearest.activate(global.get_current_time());
        }
        for (const workspace of workspaces) {
            if (!occupied.includes(workspace) && (occupied.length || workspace !== active) && this._wm.n_workspaces > 1)
                this._wm.remove_workspace(workspace, global.get_current_time());
        }
        // Reorder workspace objects; existing windows stay attached to them.
        this._groups.flatMap(group => group.slots).forEach((workspace, index) => {
            if (workspace.index() !== index)
                this._wm.reorder_workspace(workspace, index);
        });
        this._ownedCount = this._wm.n_workspaces;
        this._preferences.set_int('num-workspaces', this._ownedCount);
        this._render();
    }

    _sessionChanged() {
        this._indicator.visible = this._groups.length > 0 &&
            !Main.sessionMode.isLocked && !Main.sessionMode.isGreeter;
        this._hideTooltip();
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
            this._inventoryFingerprint = fingerprint;
            this._queue();
        });
    }

    _render() {
        const entries = this._groups.flatMap(group => group.slots.map(workspace => ({group, workspace})));
        // Preserve hovered/focused actors when only the active workspace changes.
        const unchanged = this._tabs?.length === entries.length && entries.every(({group, workspace}, index) => {
            const old = this._tabs[index];
            return old.workspace === workspace && old.group.key === group.key &&
                old.group.color === group.color && old.group.name === group.name;
        });
        if (unchanged) {
            this._tabs.forEach((tab, index) => { tab.group = entries[index].group; });
        } else {
            this._hideTooltip();
            this._box.destroy_all_children();
            this._tabs = [];
            for (const group of this._groups) {
                const box = new St.BoxLayout({style_class: 'airlock-workspace-group',
                    reactive: false, style: `border-bottom-color: ${group.color};`});
                for (const workspace of group.slots) {
                    const button = new St.Button({style_class: 'airlock-workspace-tab',
                        reactive: true, can_focus: true, track_hover: true});
                    const label = new St.Label({reactive: false, can_focus: false,
                        y_align: Clutter.ActorAlign.CENTER});
                    button.set_child(label);
                    const tab = {group, workspace, button, label};
                    this._tabs.push(tab);
                    button.connect('clicked', () => {
                        this._hideTooltip();
                        const focused = global.display.focus_window;
                        if ((global.get_pointer()[2] & Clutter.ModifierType.SHIFT_MASK) &&
                            focused && this._identities.get(focused)?.name === tab.group.name)
                            this._splitWindow(focused);
                        else
                            workspace.activate(global.get_current_time());
                    });
                    button.connect('notify::hover', () => button.hover
                        ? this._showTooltip(tab) : this._hideTooltip());
                    button.connect('key-focus-in', () => this._showTooltip(tab));
                    button.connect('key-focus-out', () => this._hideTooltip());
                    button._delegate = {
                        handleDragOver: source => source.metaWindow ? DND.DragMotionResult.MOVE_DROP : DND.DragMotionResult.CONTINUE,
                        acceptDrop: source => {
                            if (!source.metaWindow)
                                return false;
                            source.metaWindow.change_workspace(workspace);
                            this._queue();
                            return true;
                        },
                    };
                    box.add_child(button);
                }
                this._box.add_child(box);
            }
        }
        this._refresh();
        this._sessionChanged();
    }

    _description(tab) {
        const description = describeWorkspace(this._workspaceWindows(tab.workspace)
            .map(window => this._identities.get(window) ?? UNKNOWN), this._inventory);
        const owner = description?.kind === 'mixed' ? `Mixed: ${description.names.join(', ')}`
            : description?.name ?? tab.group.name;
        return `${owner} · workspace ${tab.workspace.index() + 1}`;
    }

    _refresh() {
        const active = this._wm.get_active_workspace();
        for (const tab of this._tabs ?? []) {
            tab.label.text = String(tab.workspace.index() + 1);
            tab.button.accessible_name = this._description(tab);
            if (tab.workspace === active) {
                tab.button.add_style_class_name('active');
                tab.group.lastUsed = tab.workspace;
            } else {
                tab.button.remove_style_class_name('active');
            }
        }
    }

    _showTooltip(tab) {
        this._hideTooltip();
        this._tooltipSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 200, () => {
            this._tooltipSource = 0;
            if (!this._active || !this._indicator.visible)
                return GLib.SOURCE_REMOVE;
            this._tooltip.text = this._description(tab);
            const [x, y] = tab.button.get_transformed_position();
            const [width, height] = tab.button.get_transformed_size();
            const [, naturalWidth] = this._tooltip.get_preferred_width(-1);
            const monitor = Main.layoutManager.findMonitorForActor(tab.button) ?? Main.layoutManager.primaryMonitor;
            this._tooltip.set_position(Math.max(monitor.x, Math.min(x + width / 2 - naturalWidth / 2,
                monitor.x + monitor.width - naturalWidth)), y + height + 8);
            this._tooltip.show();
            return GLib.SOURCE_REMOVE;
        });
    }

    _hideTooltip() {
        if (this._tooltipSource)
            GLib.Source.remove(this._tooltipSource);
        this._tooltipSource = 0;
        this._tooltip?.hide();
    }

    _splitWindow(window) {
        if (this._wm.n_workspaces >= MAX_SLOTS || window.is_on_all_workspaces())
            return;
        const workspace = this._wm.append_new_workspace(false, global.get_current_time());
        window.change_workspace(workspace);
        workspace.activate(global.get_current_time());
        this._queue();
    }

    _placeNewWindow(window, identity) {
        const parent = window.get_transient_for();
        let destination = parent?.get_workspace();
        if (!destination && identity.kind === 'vm') {
            const current = window.get_workspace();
            const suitable = workspace => this._workspaceWindows(workspace).filter(other => other !== window)
                .every(other => this._identities.get(other)?.kind === 'local' ||
                    this._identities.get(other)?.name === identity.name);
            const group = this._groups.find(candidate => candidate.kind === 'vm' && candidate.name === identity.name);
            const candidates = group?.slots.filter(suitable) ?? [];
            destination = candidates.includes(current) ? current
                : candidates.includes(group?.lastUsed) ? group.lastUsed : candidates[0];
            if (!destination)
                destination = suitable(current) ? current
                    : this._wm.append_new_workspace(false, global.get_current_time());
        }
        this._pendingPlacement.delete(window);
        if (destination && destination !== window.get_workspace()) {
            const focused = global.display.focus_window === window;
            window.change_workspace(destination);
            if (focused && !Main.sessionMode.isLocked && !Main.sessionMode.isGreeter)
                destination.activate(global.get_current_time());
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
                if (this._pendingPlacement.has(window) && identity.kind !== 'unknown')
                    this._placeNewWindow(window, identity);
            }
            if (this._active)
                this._sync();
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
        this._hideTooltip();
        Main.layoutManager.removeChrome(this._tooltip);
        this._tooltip.destroy();
        this._indicator.destroy();
        // Restore preferences only when they still match our last settings.
        // Keep occupied workspace objects until GNOME handles its normal cleanup.
        if (this._preferences.get_int('num-workspaces') === this._ownedCount)
            this._preferences.set_int('num-workspaces', this._originalCount);
        if (!this._mutter.get_boolean('dynamic-workspaces'))
            this._mutter.set_boolean('dynamic-workspaces', this._originalDynamic);
    }
}
