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
        f"import {{MAX_SLOTS,readLayout,reconcileGroups,serializeLayout,workspaceContents,initialDestination}} from {modules['model']};\n"
        f"import {{WindowIdentityCache,LOCAL,UNKNOWN,validateIdentity}} from {modules['identity']};\n"
        f"import {{parseInventory}} from {modules['inventory']};\n"
    )
    controller = tmp_path / "extension.mjs"
    controller.write_text(
        imports + "const {Clutter,Gio,GLib,Meta,St,Main,PanelMenu,Extension}=globalThis.shellMocks;\n"
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


def test_groups_preserve_uuid_slots_on_rename_and_inventory_failure(javascript):
    javascript("""
const groups=reconcileGroups([],inventory);
assert.deepEqual(groups.map(g=>g.key),[HOME,AGENT,'local']);
const workspace={}; groups[0].slots=[workspace,{}];
inventory.set(HOME,{name:'usr-renamed',color:'#0000ff',running:false});
reconcileGroups(groups,inventory);
assert.equal(groups[0].name,'usr-renamed');
assert.equal(groups[0].slots[0],workspace);
reconcileGroups(groups,new Map());
assert.equal(groups[0].name,'Unavailable VM');
assert.equal(groups[0].slots[0],workspace);
assert.deepEqual(readLayout(serializeLayout(groups)),[
 {key:HOME,count:2},{key:AGENT,count:1},{key:'local',count:1}]);
""")


def test_layout_validation_and_limit(javascript):
    javascript("""
for(const text of ['{','null','[]','{"version":2,"groups":[]}']) assert.deepEqual(readLayout(text),[]);
const groups=[{key:HOME,count:2},{key:HOME,count:3},{key:'fake',count:1},
 {key:AGENT,count:1.5},{key:'local',count:0},{key:'local',count:34}];
assert.deepEqual(readLayout(JSON.stringify({version:1,groups})),[{key:HOME,count:2},{key:'local',count:34}]);
assert.deepEqual(readLayout(JSON.stringify({version:1,groups:[{key:HOME,count:36}]})),[]);
const many=new Map(Array.from({length:100},(_,i)=>[
 `00000000-0000-0000-0000-${String(i).padStart(12,'0')}`,{name:`usr-${i}`,color:null}]));
assert.equal(reconcileGroups([],many).length,MAX_SLOTS);
""")


def test_mixed_marker_is_based_on_verified_window_identity(javascript):
    javascript("""
assert.deepEqual(workspaceContents('usr-home',[]),{mixed:false,names:[]});
assert.equal(workspaceContents('usr-home',[{kind:'vm',name:'usr-home'}]).mixed,false);
assert.deepEqual(workspaceContents('usr-home',[
 {kind:'vm',name:'sys-agent'},{kind:'vm',name:'sys-agent'},UNKNOWN]),
 {mixed:true,names:['sys-agent','Unidentified']});
const groups=reconcileGroups([],inventory);
const first={},second={},other={}; groups[0].slots=[first,second]; groups[0].lastUsed=second;
assert.equal(initialDestination(groups,{kind:'vm',name:'usr-home'},other,null),second);
assert.equal(initialDestination(groups,{kind:'vm',name:'usr-home'},first,null),first);
assert.equal(initialDestination(groups,UNKNOWN,other,null),null);
assert.equal(initialDestination(groups,{kind:'vm',name:'usr-home'},other,other),other);
""")


MOCKS = """
class Signals {
 constructor(){this.handlers=new Map();this.next=1;}
 connect(signal,callback){const id=this.next++;this.handlers.set(id,{signal,callback});return id;}
 disconnect(id){this.handlers.delete(id);}
 emit(signal,...args){for(const {signal:name,callback} of [...this.handlers.values()])if(name===signal)callback(this,...args);}
}
class Actor extends Signals {
 constructor(properties={}){super();Object.assign(this,properties);this.children=[];this.classes=new Set();}
 add_child(child){this.children.push(child);}
 set_child(child){this.add_child(child);}
 destroy_all_children(){this.children=[];}
 destroy(){this.destroyed=true;}
 set_label(label){this.label=label;}
 add_style_class_name(name){this.classes.add(name);}
 remove_style_class_name(name){this.classes.delete(name);}
}
class Workspace {
 constructor(manager){this.manager=manager;}
 index(){return this.manager.workspaces.indexOf(this);}
 activate(){this.manager.current=this;this.manager.emit('workspace-switched');}
}
class Manager extends Signals {
 constructor(){super();this.workspaces=[new Workspace(this),new Workspace(this)];this.current=this.workspaces[0];}
 get n_workspaces(){return this.workspaces.length;}
 get_workspace_by_index(index){return this.workspaces[index];}
 get_active_workspace(){return this.current;}
 append_new_workspace(){const w=new Workspace(this);this.workspaces.push(w);this.emit('workspace-added');return w;}
 reorder_workspace(workspace,index){this.workspaces.splice(workspace.index(),1);this.workspaces.splice(index,0,workspace);}
}
class Window extends Signals {
 constructor(workspace,kind=0){super();this.workspace=workspace;this.kind=kind;this.moves=0;}
 get_window_type(){return this.kind;}
 get_client_type(){return 1;}
 get_transient_for(){return null;}
 get_workspace(){return this.workspace;}
 located_on_workspace(workspace){return this.workspace===workspace;}
 change_workspace(workspace){this.moves++;this.workspace=workspace;this.emit('workspace-changed');}
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
globalThis.global={workspace_manager:wm,display,get_current_time:()=>42,get_window_actors:()=>actors};
globalThis.shellMocks={
 Clutter:{ActorAlign:{CENTER:0}},Gio:{Settings,Cancellable,
 File:{new_for_path:path=>path.endsWith('inventory.json')?inventoryFile:{monitor_directory:()=>monitor}},
 FileMonitorFlags:{WATCH_MOVES:1}},
 GLib:{get_user_state_dir:()=>'/state',build_filenamev:parts=>parts.join('/'),mkdir_with_parents(){},
 file_get_contents:path=>{if(!files.has(path))throw Error('missing');return [true,new TextEncoder().encode(files.get(path))];},
 file_set_contents:(path,text)=>files.set(path,text),PRIORITY_DEFAULT:0,SOURCE_REMOVE:false,
 timeout_add:(_priority,_delay,callback)=>{const id=sources.size+1;sources.set(id,callback);return id;},
 Source:{remove:id=>sources.delete(id)}},
 Meta:{WindowType:{NORMAL:0,DIALOG:1,MODAL_DIALOG:2},WindowClientType:{WAYLAND:1}},
 St:{BoxLayout:Actor,Widget:Actor,Label:Actor,Button:Actor,ScrollView:Actor,PolicyType:{AUTOMATIC:1,NEVER:2}},
 Main:{panel:{addToStatusArea(){}},sessionMode:new Signals()},PanelMenu:{Button:Actor},Extension:class {}
};
const inventoryText=JSON.stringify(Object.fromEntries([...inventory].map(([uuid,vm])=>[uuid,{uuid,...vm,label:vm.color}])));
const finishInventory=()=>reads.at(-1).callback(inventoryFile,{text:inventoryText});
"""


def test_inserting_slot_keeps_workspace_objects_and_unrestricted_window_moves(javascript):
    javascript("""
const existing=new Window(wm.current);actors.push({meta_window:existing});
const ext=new Controller();ext.enable();finishInventory();
assert.equal(existing.moves,0);
const home=ext._groups.find(g=>g.key===HOME),agent=ext._groups.find(g=>g.key===AGENT);
const agentWorkspace=agent.slots[0],localWorkspace=existing.workspace;
home.slots.push(null);ext._sync();
assert.equal(agent.slots[0],agentWorkspace);
assert.equal(existing.workspace,localWorkspace);
assert.equal(agentWorkspace.index(),2);
const foreign=new Window(home.slots[0]);ext._watch(foreign);
ext._identities.set(foreign,{kind:'vm',name:'sys-agent',color:'#cc0000'});ext._refresh();
assert.equal(ext._buttons.find(b=>b.workspace===home.slots[0]).button.label,'1!');
foreign.change_workspace(agentWorkspace);
assert.equal(foreign.workspace,agentWorkspace);assert.equal(foreign.moves,1);
assert.equal(ext._buttons.find(b=>b.workspace===agentWorkspace).button.label,'1');
const add=ext._box.children[0].children.at(-1);add.emit('clicked');
assert.equal(home.slots.length,3);assert.equal(wm.current,home.slots.at(-1));
ext.disable();assert.equal(existing.handlers.size,0);assert.equal(foreign.handlers.size,0);
assert.equal(settings.get('org.gnome.mutter')['dynamic-workspaces'],true);
assert.equal(settings.get('org.gnome.desktop.wm.preferences')['num-workspaces'],4);
assert.equal(monitor.cancelled,true);
""", shell=True)


def test_inventory_out_of_order_and_failed_refresh_preserve_layout(javascript):
    javascript("""
const ext=new Controller();ext.enable();const stale=reads[0];ext._loadInventory();finishInventory();
const groups=ext._groups.slice();
stale.callback(inventoryFile,{text:'{}'});assert.deepEqual(ext._groups,groups);
ext._loadInventory();reads.at(-1).callback(inventoryFile,{error:true});
assert.deepEqual(ext._groups,groups);
const late=reads.at(-1);ext.disable();late.callback(inventoryFile,{text:inventoryText});
assert.equal(ext._indicator.destroyed,true);
""", shell=True)


def test_new_window_suggestion_runs_once_and_user_move_wins_during_lookup(javascript):
    javascript("""
const ext=new Controller();ext.enable();finishInventory();
const home=ext._groups.find(g=>g.key===HOME),agent=ext._groups.find(g=>g.key===AGENT);
const window=new Window(agent.slots[0]);display.emit('window-created',window);
ext._resolve=async()=>({kind:'vm',name:'usr-home',color:'#3465a4'});
await ext._scan();assert.equal(window.workspace,home.slots[0]);assert.equal(window.moves,1);
window.change_workspace(agent.slots[0]);await ext._scan();
assert.equal(window.workspace,agent.slots[0]);assert.equal(window.moves,2);
const slow=new Window(agent.slots[0]);display.emit('window-created',slow);
let complete;ext._resolve=()=>new Promise(resolve=>{complete=resolve;});
// Ignore the already resolved window to test the pending lookup on the new one.
ext._windows.delete(window);
const scanning=ext._scan();slow.change_workspace(home.slots[0]);
complete({kind:'vm',name:'sys-agent',color:'#cc0000'});await scanning;
assert.equal(slow.workspace,home.slots[0]);assert.equal(slow.moves,1);
ext._windows.set(window,[]);ext.disable();
""", shell=True)


def test_disable_during_identity_lookup_prevents_late_window_move(javascript):
    javascript("""
const ext=new Controller();ext.enable();finishInventory();
const workspace=wm.current,window=new Window(workspace);display.emit('window-created',window);
let complete;ext._resolve=()=>new Promise(resolve=>{complete=resolve;});
const scanning=ext._scan();ext.disable();
complete({kind:'vm',name:'usr-home',color:'#3465a4'});await scanning;
assert.equal(window.moves,0);assert.equal(window.workspace,workspace);
assert.equal(window.handlers.size,0);
""", shell=True)


def test_lock_hides_vm_labels_and_disable_preserves_external_preferences(javascript):
    javascript("""
const ext=new Controller();ext.enable();finishInventory();
const mode=globalThis.shellMocks.Main.sessionMode;
mode.isLocked=true;mode.emit('updated');assert.equal(ext._indicator.visible,false);
mode.isLocked=false;mode.emit('updated');assert.equal(ext._indicator.visible,true);
settings.get('org.gnome.desktop.wm.preferences')['num-workspaces']=7;
ext.disable();assert.equal(settings.get('org.gnome.desktop.wm.preferences')['num-workspaces'],7);
""", shell=True)
