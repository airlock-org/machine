import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as DND from 'resource:///org/gnome/shell/ui/dnd.js';
import * as Workspace from 'resource:///org/gnome/shell/ui/workspace.js';
import * as WorkspaceThumbnail from 'resource:///org/gnome/shell/ui/workspaceThumbnail.js';
import {Extension, InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';
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
        this._overviewActors = new Map();
        this._dragging = false;
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
        this._installOverview();
        this._connect(Main.overview, 'hidden', () => this._hideTooltip());
        for (const kind of ['window', 'item']) {
            this._connect(Main.overview, `${kind}-drag-begin`, () => {
                this._dragging = true;
                this._hideTooltip();
            });
            for (const end of ['end', 'cancelled'])
                this._connect(Main.overview, `${kind}-drag-${end}`, () => { this._dragging = false; });
        }
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
                    const tab = {group, workspace, button, label, focused: false};
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
                    button.connect('notify::hover', () => {
                        this._styleTab(tab);
                        if (button.hover)
                            this._showTooltip(tab);
                        else
                            this._hideTooltip();
                    });
                    button.connect('key-focus-in', () => {
                        tab.focused = true;
                        this._styleTab(tab);
                        this._showTooltip(tab);
                    });
                    button.connect('key-focus-out', () => {
                        tab.focused = false;
                        this._styleTab(tab);
                        this._hideTooltip();
                    });
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
            : description?.name ?? tab.group?.name;
        return `${owner} · workspace ${tab.workspace.index() + 1}`;
    }

    _refresh() {
        const active = this._wm.get_active_workspace();
        for (const tab of this._tabs ?? []) {
            tab.label.text = String(tab.workspace.index() + 1);
            tab.button.accessible_name = this._description(tab);
            this._styleTab(tab);
            if (tab.workspace === active) {
                tab.button.add_style_class_name('active');
                tab.group.lastUsed = tab.workspace;
            } else {
                tab.button.remove_style_class_name('active');
            }
        }
        this._refreshOverview();
    }

    _styleTab(tab) {
        const alpha = tab.workspace === this._wm.get_active_workspace() ? 0.4
            : tab.button.hover || tab.focused ? 0.3 : 0.2;
        const color = /^#[0-9a-f]{6}$/i.test(tab.group.color) ? tab.group.color : '#888888';
        const rgb = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16));
        tab.button.style = `background-color: rgba(${rgb.join(',')},${alpha});`;
    }

    _installOverview() {
        this._injections = new InjectionManager();
        const extension = this;
        for (const [prototype, small] of [[Workspace.Workspace.prototype, false],
            [WorkspaceThumbnail.WorkspaceThumbnail.prototype, true]]) {
            this._injections.overrideMethod(prototype, '_init', original => function (...args) {
                original.apply(this, args);
                extension._decorateOverview(this, small);
            });
        }
        // Also cover previews created before this extension was enabled.
        const controls = Main.overview._overview.controls;
        for (const actor of controls._thumbnailsBox?._thumbnails ?? [])
            this._decorateOverview(actor, true);
        for (const view of controls._workspacesDisplay?._workspacesViews ?? []) {
            for (const actor of view._workspaces ?? view._workspacesView?._workspaces ?? [])
                this._decorateOverview(actor, false);
            for (const actor of view._thumbnails?._thumbnails ?? [])
                this._decorateOverview(actor, true);
        }
    }

    _decorateOverview(actor, small) {
        if (!this._active || !actor.metaWorkspace || this._overviewActors.has(actor))
            return;
        // A nonreactive overlay leaves GNOME's native click and drag handlers intact.
        const border = new St.Widget({reactive: false, can_focus: false,
            style_class: small ? 'airlock-overview-thumbnail-border' : 'airlock-overview-workspace-border'});
        border.add_constraint(new Clutter.BindConstraint({
            source: small ? actor : actor._background,
            coordinate: small ? Clutter.BindCoordinate.SIZE : Clutter.BindCoordinate.ALL,
        }));
        actor.add_child(border);
        const record = {actor, border, workspace: actor.metaWorkspace, button: actor,
            tooltipAbove: !small, reactive: actor.reactive, trackHover: actor.track_hover};
        actor.reactive = true;
        actor.track_hover = true;
        record.hoverSignal = actor.connect('notify::hover', () => {
            if (actor.hover && record.group && Main.overview.visible)
                this._showTooltip(record);
            else
                this._hideTooltip();
        });
        record.destroySignal = actor.connect('destroy', () => {
            this._hideTooltip();
            this._overviewActors.delete(actor);
        });
        this._overviewActors.set(actor, record);
        this._paintOverview(record);
    }

    _paintOverview(record) {
        const {workspace, border} = record;
        record.group = describeWorkspace(this._workspaceWindows(workspace)
            .map(window => this._identities.get(window) ?? UNKNOWN), this._inventory);
        const active = workspace === this._wm.get_active_workspace();
        border.style = `border-color: ${record.group?.color ?? 'transparent'}; border-width: ${active ? 3 : 2}px;`;
    }

    _refreshOverview() {
        for (const record of this._overviewActors.values())
            this._paintOverview(record);
    }

    _showTooltip(tab) {
        this._hideTooltip();
        if (this._dragging)
            return;
        this._tooltipSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 200, () => {
            this._tooltipSource = 0;
            if (!this._active || !this._indicator.visible || this._dragging ||
                (tab.tooltipAbove !== undefined && !Main.overview.visible))
                return GLib.SOURCE_REMOVE;
            this._tooltip.text = this._description(tab);
            const [x, y] = tab.button.get_transformed_position();
            const [width, height] = tab.button.get_transformed_size();
            const [, naturalWidth] = this._tooltip.get_preferred_width(-1);
            const monitor = Main.layoutManager.findMonitorForActor(tab.button) ?? Main.layoutManager.primaryMonitor;
            const [, naturalHeight] = this._tooltip.get_preferred_height(-1);
            const tooltipY = tab.tooltipAbove ? y - naturalHeight - 8 : y + height + 8;
            this._tooltip.set_position(Math.max(monitor.x, Math.min(x + width / 2 - naturalWidth / 2,
                monitor.x + monitor.width - naturalWidth)), Math.max(monitor.y + 8,
                Math.min(tooltipY, monitor.y + monitor.height - naturalHeight - 8)));
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
        this._injections?.clear();
        for (const record of this._overviewActors.values()) {
            record.actor.disconnect(record.hoverSignal);
            record.actor.disconnect(record.destroySignal);
            record.actor.reactive = record.reactive;
            record.actor.track_hover = record.trackHover;
            record.border.destroy();
        }
        this._overviewActors.clear();
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
