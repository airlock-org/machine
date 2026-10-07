"""Exercise visual workspace layout and the actual Shell controller without a desktop."""

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

GNOME = Path(__file__).resolve().parents[1] / "lifecycle/misc/modules/sys-gui/desktop/gnome"
EXTENSION = GNOME / "vm-workspaces-extension"
HOME = "738a6af9-8e51-590d-afe4-790b26fbca79"
AGENT = "a8c14e6d-05c6-412a-952d-28f41c910aca"


@pytest.fixture
def javascript(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime not installed; quick tests never provision tools")
    modules = {}
    for name, source in {
        "model": EXTENSION / "model.js",
        "identity": GNOME / "focused-vm-extension/state.js",
        "inventory": GNOME / "vm-search-extension/model.js",
    }.items():
        target = tmp_path / f"{name}.mjs"
        target.write_text(source.read_text())
        modules[name] = json.dumps(target.as_uri())
    imports = (
        f"import {{MAX_SLOTS,describeWorkspace,groupOccupiedWorkspaces,groupOwnedWorkspaces}} from {modules['model']};\n"
        f"import {{WindowIdentityCache,LOCAL,UNKNOWN,validateIdentity}} from {modules['identity']};\n"
        f"import {{parseInventory}} from {modules['inventory']};\n"
    )
    controller = tmp_path / "extension.mjs"
    controller.write_text(
        imports + "const {Clutter,Gio,GLib,Meta,St,Main,PanelMenu,DND,Workspace,WorkspaceThumbnail,Extension,InjectionManager}=globalThis.shellMocks;\n"
        + re.sub(r"^import .*;\n", "", (EXTENSION / "extension.js").read_text(), flags=re.MULTILINE)
    )

    def run(script, *, shell=False):
        header = (
            "import assert from 'node:assert/strict';\n" + imports
            + f"const HOME={json.dumps(HOME)},AGENT={json.dumps(AGENT)};\n"
            + "const inventory=new Map([[HOME,{name:'usr-home',color:'#3465a4',running:true}],"
            "[AGENT,{name:'sys-agent',color:'#cc0000',running:false}]]);\n"
        )
        if shell:
            header += MOCKS + f"\nconst {{default:Controller}}=await import({json.dumps(controller.as_uri())});\n"
        result = subprocess.run([node, "--input-type=module", "-e", header + script], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr

    return run


def test_only_occupied_application_workspaces_form_groups(javascript):
    javascript("""
const a={},b={},identities=new Map([[a,[]],[b,[LOCAL,{kind:'vm',name:'sys-gui'}]]]);
assert.deepEqual(groupOccupiedWorkspaces([], [a,b], identities, inventory), []);
identities.set(a,[{kind:'vm',name:'usr-home',color:'#ffffff'}]);
const groups=groupOccupiedWorkspaces([], [a,b], identities, inventory);
assert.equal(groups.length,1);assert.equal(groups[0].key,HOME);
assert.equal(groups[0].color,'#3465a4');assert.deepEqual(groups[0].slots,[a]);
assert.equal(groups.some(g=>g.name==='sys-agent'),false);
""")


def test_groups_keep_order_and_drop_closed_workspaces(javascript):
    javascript("""
const a={},b={},c={},d={};
const home={kind:'vm',name:'usr-home'},agent={kind:'vm',name:'sys-agent'};
const identities=new Map([[a,[home]],[b,[agent]],[c,[home]],[d,[agent]]]);
let groups=groupOccupiedWorkspaces([], [a,b,c,d], identities, inventory);
assert.deepEqual(groups.map(g=>g.slots),[[a,c],[b,d]]);
groups[0].lastUsed=c;
groups=groupOccupiedWorkspaces(groups,[b,a,d,c],identities,inventory);
assert.deepEqual(groups.map(g=>g.key),[HOME,AGENT]);assert.equal(groups[0].lastUsed,c);
identities.set(a,[]);identities.set(c,[]);
groups=groupOccupiedWorkspaces(groups,[b,a,d,c],identities,inventory);
assert.deepEqual(groups.map(g=>g.key),[AGENT]);
""")


def test_mixed_and_unknown_windows_use_neutral_identity(javascript):
    javascript("""
const description=describeWorkspace([
 {kind:'vm',name:'usr-home'}, {kind:'vm',name:'sys-agent'},LOCAL],inventory);
assert.equal(description.kind,'mixed');assert.equal(description.color,'#888888');
assert.deepEqual(description.names,['sys-agent','usr-home']);
assert.equal(describeWorkspace([UNKNOWN],inventory).kind,'unknown');
assert.equal(describeWorkspace([LOCAL],inventory),null);
""")


def test_controller_keeps_native_activities_and_removes_empty_slots(javascript):
    javascript("""
const homeWindow=new Window(wm.workspaces[0]),agentWindow=new Window(wm.workspaces[1]);
actors.push({meta_window:homeWindow},{meta_window:agentWindow});
const ext=new Controller();ext.enable();finishInventory();
ext._identities.set(homeWindow,{kind:'vm',name:'usr-home'});
ext._identities.set(agentWindow,{kind:'vm',name:'sys-agent'});ext._sync();
assert.equal(activities.get_first_child(),nativeIndicators);
assert.equal(activities.reactive,true);assert.equal(activities.accessible_name,'Activities');
assert.equal(shellMocks.Main.panel.placement.side,'right');
assert.equal(shellMocks.Main.panel.placement.index,0);
assert.equal(homeWindow.moves,0);assert.equal(agentWindow.moves,0);
assert.equal(ext._groups.length,2);assert.equal(ext._tabs.length,2);
ext._tabs[1].button.emit('clicked');assert.equal(wm.current,agentWindow.workspace);
activities._clickGesture.emit('recognize');assert.equal(overview.toggles,1);
const homeTab=ext._tabs[0];homeTab.button.hover=true;homeTab.button.emit('notify::hover');
sources.get(ext._tooltipSource)();assert.match(ext._tooltip.text,/usr-home/);
assert.equal(ext._tooltip.visible,true);
agentWindow.emit('unmanaged');allWindows.delete(agentWindow);ext._sync();
assert.equal(ext._groups.length,1);assert.equal(wm.n_workspaces,1);
homeWindow.emit('unmanaged');allWindows.delete(homeWindow);ext._sync();
assert.equal(ext._groups.length,0);assert.equal(wm.n_workspaces,1);
assert.equal(ext._indicator.visible,false);
ext.disable();assert.equal(settings.get('org.gnome.mutter')['dynamic-workspaces'],true);
""", shell=True)


def test_multiple_slots_stay_contiguous_and_foreign_moves_are_blocked(javascript):
    javascript("""
const homeWindow=new Window(wm.workspaces[0]),agentWindow=new Window(wm.workspaces[1]);
actors.push({meta_window:homeWindow},{meta_window:agentWindow});
const ext=new Controller();ext.enable();finishInventory();
ext._identities.set(homeWindow,{kind:'vm',name:'usr-home'});
ext._identities.set(agentWindow,{kind:'vm',name:'sys-agent'});ext._sync();
const second=new Window(homeWindow.workspace);ext._watch(second);
ext._identities.set(second,{kind:'vm',name:'usr-home'});ext._splitWindow(second);ext._sync();
assert.deepEqual(ext._groups.map(g=>g.slots.length),[2,1]);
assert.equal(agentWindow.workspace.index(),2);
agentWindow.change_workspace(second.workspace);ext._sync();
assert.equal(agentWindow.workspace.index(),2);assert.equal(agentWindow.moves,0);
assert.equal(ext._groups.length,2);assert.equal(ext._groups[0].key,HOME);
assert.equal(ext._groups[0].slots.length,2);
assert.equal(ext._tabs[1].button._delegate.acceptDrop({metaWindow:agentWindow}),false);
second.change_workspace(homeWindow.workspace);assert.equal(second.workspace,homeWindow.workspace);
ext.disable();
""", shell=True)


def test_overview_decoration_tints_hover_labels_and_cleanup(javascript):
    javascript(r"""
const homeWindow=new Window(wm.workspaces[0]),agentWindow=new Window(wm.workspaces[1]);
actors.push({meta_window:homeWindow},{meta_window:agentWindow});
const existing=new WorkspacePreview(homeWindow.workspace);
overview._overview.controls._workspacesDisplay._workspacesViews=[{_workspaces:[existing]}];
const originalInit=WorkspacePreview.prototype._init;
const ext=new Controller();ext.enable();finishInventory();
ext._identities.set(homeWindow,{kind:'vm',name:'usr-home'});
ext._identities.set(agentWindow,{kind:'vm',name:'sys-agent'});ext._sync();
const thumbnail=new ThumbnailPreview(agentWindow.workspace);
assert.equal(ext._overviewActors.size,2);
assert.match(ext._overviewActors.get(existing).border.style,/#3465a4/);
assert.match(ext._overviewActors.get(thumbnail).border.style,/#cc0000/);
assert.equal(ext._overviewActors.get(thumbnail).border.reactive,false);
const agent=ext._tabs[1];assert.match(agent.button.style,/rgba\(204,0,0,0.35\)/);
agent.button.hover=true;agent.button.emit('notify::hover');assert.match(agent.button.style,/,0.45\)/);
agent.button.emit('clicked');assert.match(agent.button.style,/,0.55\)/);
overview.visible=true;thumbnail.hover=true;thumbnail.emit('notify::hover');
sources.get(ext._tooltipSource)();assert.match(ext._tooltip.text,/sys-agent/);
overview.emit('window-drag-begin');assert.equal(ext._tooltip.visible,false);
thumbnail.emit('notify::hover');assert.equal(ext._tooltipSource,0);
overview.emit('window-drag-end');
const record=ext._overviewActors.get(existing);ext.disable();
assert.equal(record.border.destroyed,true);assert.equal(existing.reactive,false);
assert.equal(WorkspacePreview.prototype._init,originalInit);
""", shell=True)


MOCKS = """
class Signals {
 constructor(){this.handlers=new Map();this.next=1;}
 connect(signal,callback){const id=this.next++;this.handlers.set(id,{signal,callback});return id;}
 disconnect(id){this.handlers.delete(id);}
 emit(signal,...args){for(const {signal:name,callback} of [...this.handlers.values()])if(name===signal)callback(this,...args);}
}
class Actor extends Signals {
 constructor(properties={}){super();Object.assign(this,{reactive:false,can_focus:false},properties);this.children=[];this.classes=new Set();}
 add_child(child){assert.equal(child.parent??null,null);this.children.push(child);child.parent=this;}
 insert_child_above(child,sibling){this.children.splice(this.children.indexOf(sibling)+1,0,child);child.parent=this;}
 remove_child(child){this.children=this.children.filter(c=>c!==child);child.parent=null;}
 get_first_child(){return this.children[0]??null;}
 set_child(child){this.add_child(child);}
 destroy_all_children(){for(const child of [...this.children]){this.remove_child(child);child.destroy();}}
 destroy(){this.emit("destroy");this.destroyed=true;}
 add_constraint(constraint){this.constraint=constraint;}
 hide(){this.visible=false;}
 show(){this.visible=true;}
 get_transformed_position(){return [400,0];}
 get_transformed_size(){return [30,24];}
 get_preferred_width(){return [80,180];}
 get_preferred_height(){return [20,30];}
 set_position(x,y){this.position=[x,y];}
 get_style(){return this.style??null;}
 set_label(label){this.label=label;}
 add_style_class_name(name){this.classes.add(name);}
 remove_style_class_name(name){this.classes.delete(name);}
}
const allWindows=new Set();
class Workspace {
 constructor(manager){this.manager=manager;}
 index(){return this.manager.workspaces.indexOf(this);}
 list_windows(){return [...allWindows].filter(window=>window.workspace===this);}
 activate(){this.manager.current=this;this.manager.emit('workspace-switched');}
}
class Manager extends Signals {
 constructor(){super();this.workspaces=[new Workspace(this),new Workspace(this)];this.current=this.workspaces[0];}
 get n_workspaces(){return this.workspaces.length;}
 get_workspace_by_index(index){return this.workspaces[index];}
 get_active_workspace(){return this.current;}
 append_new_workspace(){const w=new Workspace(this);this.workspaces.push(w);this.emit('workspace-added');return w;}
 remove_workspace(workspace){this.workspaces.splice(workspace.index(),1);this.emit('workspace-removed');}
 reorder_workspace(workspace,index){this.workspaces.splice(workspace.index(),1);this.workspaces.splice(index,0,workspace);}
}
class Window extends Signals {
 constructor(workspace,kind=0){super();this.workspace=workspace;this.kind=kind;this.moves=0;allWindows.add(this);}
 get_window_type(){return this.kind;}
 get_client_type(){return 1;}
 is_on_all_workspaces(){return false;}
 get_transient_for(){return null;}
 get_workspace(){return this.workspace;}
 located_on_workspace(workspace){return this.workspace===workspace;}
 change_workspace(workspace){this.moves++;this.workspace=workspace;this.emit('workspace-changed');}
 change_workspace_by_index(index){this.change_workspace(wm.get_workspace_by_index(index));}
 stick(){this.sticky=true;this.emit('notify::on-all-workspaces');}
 unstick(){this.sticky=false;this.emit('notify::on-all-workspaces');}
}
const files=new Map(), sources=new Map(), settings=new Map([
 ['org.gnome.mutter',{'dynamic-workspaces':true}],
 ['org.gnome.desktop.wm.preferences',{'num-workspaces':4}]]);
const reads=[];
class Cancellable {cancel(){this.cancelled=true;}is_cancelled(){return Boolean(this.cancelled);}}
class Settings {
 constructor({schema_id}){this.values=settings.get(schema_id);}
 get_boolean(key){return this.values[key];}set_boolean(key,value){this.values[key]=value;}
 get_int(key){return this.values[key];}set_int(key,value){this.values[key]=value;}
}
const inventoryFile={
 load_contents_async(cancellable,callback){reads.push({cancellable,callback});},
 load_contents_finish(result){if(result.error)throw Error('missing');return [true,new TextEncoder().encode(result.text)];}
};
const monitor=new Signals();monitor.cancel=()=>{monitor.cancelled=true;};
const wm=new Manager(), display=new Signals(), actors=[];
globalThis.global={workspace_manager:wm,display,get_current_time:()=>42,get_pointer:()=>[0,0,0],get_window_actors:()=>actors};
class WorkspacePreview extends Actor {
 constructor(workspace){super();this._init(workspace);}
 _init(workspace){this.metaWorkspace=workspace;this._background=new Actor();this.add_child(this._background);}
}
class ThumbnailPreview extends Actor {
 constructor(workspace){super();this._init(workspace);}
 _init(workspace){this.metaWorkspace=workspace;}
}
class InjectionManager {
 constructor(){this.restore=[];}
 overrideMethod(prototype,name,factory){const original=prototype[name];this.restore.push(()=>prototype[name]=original);prototype[name]=factory(original);}
 clear(){for(const restore of this.restore.reverse())restore();}
}
const overview=Object.assign(new Signals(),{visible:false,toggles:0,allowed:true,shouldToggleByCornerOrButton(){return this.allowed;},
 toggle(){this.visible=!this.visible;this.toggles++;},
 _overview:{controls:{_thumbnailsBox:{_thumbnails:[]},_workspacesDisplay:{_workspacesViews:[]}}}});
const nativeIndicators=new Actor(),activities=new Actor({reactive:true,can_focus:true,accessible_name:'Activities'});
activities.add_child(nativeIndicators);
activities._clickGesture=new Signals();activities._clickGesture.connect('recognize',()=>{
 if(overview.shouldToggleByCornerOrButton())overview.toggle();});
activities.vfunc_scroll_event=event=>event;
activities.handleDragOver=source=>source;
globalThis.shellMocks={
 Clutter:{ActorAlign:{CENTER:0},ModifierType:{SHIFT_MASK:1},BindConstraint:class {constructor(properties){Object.assign(this,properties);}},BindCoordinate:{SIZE:1,ALL:2}},Gio:{Settings,Cancellable,
 File:{new_for_path:path=>path.endsWith('inventory.json')?inventoryFile:{monitor_directory:()=>monitor}},
 FileMonitorFlags:{WATCH_MOVES:1},DBus:{session:{}},BusNameOwnerFlags:{NONE:0},
 DBusExportedObject:{wrapJSObject:()=>({export(){},unexport(){}})},
 bus_own_name_on_connection:()=>1,bus_unown_name(){}},
 GLib:{get_user_state_dir:()=>'/state',build_filenamev:parts=>parts.join('/'),mkdir_with_parents(){},
 file_get_contents:path=>{if(!files.has(path))throw Error('missing');return [true,new TextEncoder().encode(files.get(path))];},
 file_set_contents:(path,text)=>files.set(path,text),PRIORITY_DEFAULT:0,SOURCE_REMOVE:false,
 timeout_add:(_priority,_delay,callback)=>{const id=sources.size+1;sources.set(id,callback);return id;},
 Source:{remove:id=>sources.delete(id)},get_monotonic_time:()=>0,uuid_string_random:()=>String(sources.size)},
 Meta:{Window,WindowType:{NORMAL:0,DIALOG:1,MODAL_DIALOG:2},WindowClientType:{WAYLAND:1}},
 St:{BoxLayout:Actor,Widget:Actor,Label:Actor,Button:Actor,ThemeContext:{get_for_stage:()=>({scaleFactor:1})}},
 PanelMenu:{Button:Actor},DND:{DragMotionResult:{MOVE_DROP:2,CONTINUE:3,NO_DROP:0}},
 Main:{panel:{statusArea:{activities},addToStatusArea(name,indicator,index,side){this.placement={name,indicator,index,side};}},
 layoutManager:{addChrome(){},removeChrome(){},primaryMonitor:{x:0,y:0,width:1920,height:1080},findMonitorForActor(){return this.primaryMonitor;}},
 overview,sessionMode:new Signals(),wm:{_shouldAnimateActor:()=>true,actionMoveWindow(window,workspace){window.change_workspace(workspace);workspace.activate();}}},
 Workspace:{Workspace:WorkspacePreview,WorkspaceBackground:class {_updateBorderRadius(){}}},WorkspaceThumbnail:{WorkspaceThumbnail:ThumbnailPreview,ThumbnailsBox:class {_init(){} _updateIndicator(){} addThumbnails(){}}},InjectionManager,
 Extension:class {}
};
const inventoryText=JSON.stringify(Object.fromEntries([...inventory].map(([uuid,vm])=>[uuid,{uuid,...vm,label:vm.color}])));
const finishInventory=()=>reads.at(-1).callback(inventoryFile,{text:inventoryText});
"""
