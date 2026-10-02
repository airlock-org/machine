"""VM search presentation preserves Qubes identity and GNOME app controls."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "lifecycle/misc/modules/sys-gui/desktop/gnome/vm-search-extension"
HOME = "738a6af9-8e51-590d-afe4-790b26fbca79"
AGENT = "a8c14e6d-05c6-412a-952d-28f41c910aca"


@pytest.fixture
def javascript(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("JavaScript runtime is not installed; quick tests never provision tools")
    model = tmp_path / "model.mjs"
    model.write_text((EXTENSION / "model.js").read_text())
    extension = tmp_path / "extension.mjs"
    source = re.sub(r"^import .*;\n", "", (EXTENSION / "extension.js").read_text(), flags=re.MULTILINE)
    extension.write_text(
        f"import {{describeResult, groupResults, parseInventory}} from {json.dumps(model.as_uri())};\n"
        "const {Clutter, Gio, GLib, GObject, Pango, Shell, St, Main, Search, Extension, InjectionManager} = globalThis.shellMocks;\n"
        + source
    )

    def run(script, *, presentation=False):
        header = (
            "import assert from 'node:assert/strict';\n"
            f"import {{describeResult, groupResults, parseInventory}} from {json.dumps(model.as_uri())};\n"
            f"const HOME={json.dumps(HOME)}, AGENT={json.dumps(AGENT)};\n"
            "const app=(vm,name='org.gnome.Console')=>`org.qubes-os.vm.${vm}.${name}.desktop`;\n"
            "const snapshot=()=>({[HOME]:{uuid:HOME,name:'usr-home',label:'0x3465a4',running:true},"
            "[AGENT]:{uuid:AGENT,name:'sys-agent',label:'0xcc0000',running:false}});\n"
        )
        if presentation:
            header += SHELL_MOCKS + f"\nconst {{VMSearchResults, default: VMSearchExtension}} = await import({json.dumps(extension.as_uri())});\n"
        result = subprocess.run(
            [node, "--input-type=module", "-e", header + script], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    return run


def test_groups_follow_best_match_and_preserve_gnome_order(javascript):
    javascript("""
const inventory=parseInventory(JSON.stringify(snapshot()));
const ids=[app(AGENT), app(HOME,'firefox'), app(AGENT,'dev-terminal'), app(HOME)];
const groups=groupResults(ids,inventory);
assert.deepEqual(groups.map(g=>g.heading),['sys-agent','usr-home']);
assert.deepEqual(groups[0].ids,[ids[0],ids[2]]);
assert.deepEqual(groups[1].ids,[ids[1],ids[3]]);
assert.equal(groups[0].stopped,true);
assert.equal(groups[0].color,'#cc0000');
assert.equal(groups.flatMap(g=>g.ids)[0],ids[0]);
assert.deepEqual(groupResults([],inventory),[]);
""")


def test_many_matches_do_not_hide_another_vm(javascript):
    javascript("""
const inventory=parseInventory(JSON.stringify(snapshot()));
const ids=Array.from({length:30},(_,i)=>app(HOME,`application-${i}`));
ids.push(app(AGENT));
const groups=groupResults(ids,inventory);
assert.equal(groups.length,2);
assert.equal(groups[0].ids.length,30);
assert.deepEqual(groups[1].ids,[app(AGENT)]);
""")


def test_names_colours_and_renames_use_inventory_uuid(javascript):
    javascript("""
const data=snapshot();
data[AGENT].label=data[HOME].label;
let inventory=parseInventory(JSON.stringify(data));
assert.equal(groupResults([app(HOME),app(AGENT)],inventory).length,2);
assert.equal(describeResult(app(HOME),'usr-home: Console: Long title',inventory).title,'Console: Long title');
assert.equal(describeResult(app(HOME),'Another full title',inventory).title,'Another full title');
data[HOME].name='usr-renamed';
inventory=parseInventory(JSON.stringify(data));
assert.equal(describeResult(app(HOME),'usr-renamed: Console',inventory).heading,'usr-renamed');
assert.equal(describeResult(app(HOME),'usr-renamed: Console',inventory).title,'Console');
delete data[HOME];
assert.equal(describeResult(app(HOME),'usr-home: Console',parseInventory(JSON.stringify(data))).heading,'Other applications');
""")


def test_guest_names_cannot_supply_vm_identity_or_css(javascript):
    javascript("""
const data=snapshot();
for(const label of ['0x3465a4','#3465A4']) {
 data[HOME].label=label;
 assert.equal(parseInventory(JSON.stringify(data)).get(HOME).color,'#3465a4');
}
data[HOME].label='red; width:10000px';
data[AGENT].uuid=HOME;
const inventory=parseInventory(JSON.stringify(data));
assert.equal(inventory.get(HOME).color,null);
assert.equal(inventory.has(AGENT),false);
const local=describeResult('fake.desktop','sys-agent: Console',inventory);
assert.equal(local.heading,'sys-gui');
assert.equal(local.title,'sys-agent: Console');
assert.equal(local.color,null);
assert.equal(describeResult('shutdown','Power Off',inventory).heading,'System');
for(const name of ['<b>bad</b>','bad\\n','a'.repeat(32)]) {
 data[HOME].name=name;
 assert.equal(parseInventory(JSON.stringify(data)).has(HOME),false);
}
for(const text of ['{','null','[]','1']) assert.throws(()=>parseInventory(text));
""")


SHELL_MOCKS = """
class Actor {
 constructor(...args) {this._init(...args);}
 _init(properties={}) {this.children=[]; this.visible=true; this.clutter_text={set(properties){Object.assign(this,properties);}}; Object.assign(this,properties);}
 add_child(child) {assert.equal(child.parent??null,null);this.children.push(child);child.parent=this;}
 remove_child(child) {this.children=this.children.filter(item=>item!==child);child.parent=null;}
 get_parent() {return this.parent??null;}
 get_child() {return this.children[0]??null;}
 set_child(child) {for(const old of [...this.children])this.remove_child(old);if(child)this.add_child(child);}
 get_children() {return this.children;}
 set_child_at_index(child,index) {this.remove_child(child);this.children.splice(index,0,child);child.parent=this;}
 set(properties) {Object.assign(this,properties);}
 set_size(width,height) {this.width=width;this.height=height;}
 set_style(style) {this.style=style;}
 hide() {this.visible=false;}
 show() {this.visible=true;}
 connect(name,callback) {this.callback=callback;return 1;}
 destroy() {this._onDestroy?.();this.destroyed=true;for(const child of [...this.children])child.destroy();this.parent?.remove_child(this);}
}
class Grid extends Actor {
 _init(provider,view) {
  super._init();this.provider=provider;this._resultsView=view;this._resultDisplays={};
  this._cancellable={cancel(){}};this._resultDisplayBin={child:new Actor()};
 }
 async _ensureResultActors(ids) {
  for(const id of ids) if(!this._resultDisplays[id]) this._resultDisplays[id]=this._createResultDisplay({id,name:this.provider.names[id]});
 }
 clear() {for(const row of Object.values(this._resultDisplays))row.destroy();this._resultDisplays={};this._clearResultDisplay();}
 _onDestroy() {}
}
const Clutter={ActorAlign:{CENTER:1},Orientation:{HORIZONTAL:0,VERTICAL:1}};
const St={Widget:Actor,BoxLayout:Actor,Label:Actor};
const Pango={WrapMode:{WORD_CHAR:1},EllipsizeMode:{NONE:0}};
const callbacks=[];
const Gio={IOErrorEnum:{NOT_FOUND:1,CANCELLED:2},io_error_quark:()=>42};
const Search={GridSearchResults:Grid,SearchResultsView:class {}};
globalThis.shellMocks={Clutter,St,Pango,Gio,Search,GObject:{registerClass:cls=>cls},GLib:{},Shell:{},Main:{},Extension:class {},InjectionManager:class {}};
function provider(names) {
 return {names,createResultObject(meta) {
  const row=new Actor();row.app=meta.id.endsWith('.desktop')?{get_name:()=>names[meta.id]}:null;row.metaInfo=meta;
  row.icon=new Actor();row.icon.label=new Actor();row.icon.setIconSize=function(size){assert.equal(this._setSizeManually,true);this.iconSize=size;};
  row.set_child(new Actor());row.activate=()=>meta.id;row.popup_menu=()=>meta.id;row.getDragActorSource=()=>row.icon;
  return row;
 }};
}
"""


def test_rows_reuse_original_controls_and_wrap_full_names(javascript):
    javascript("""
const inventory=parseInventory(JSON.stringify(snapshot()));
const id=app(AGENT), action='shutdown';
const names={[id]:'sys-agent: A very long application name', [action]:'Power Off'};
const display=new VMSearchResults(provider(names),{},()=>inventory);
await display.updateSearch([id,action],['app']);
const row=display.getFirstResult();
assert.equal(row._airlockTitle.text,'A very long application name');
assert.equal(row.accessible_name,names[id]);
assert.equal(row._airlockTitle.clutter_text.line_wrap,true);
assert.equal(row._airlockTitle.clutter_text.ellipsize,Pango.EllipsizeMode.NONE);
assert.equal(row.activate(),id);assert.equal(row.popup_menu(),id);
assert.equal(row.getDragActorSource(),row.icon);
assert.equal(row.icon.iconSize,32);
assert.equal(display._groups[0].get_child().get_children().at(-1).text,'Stopped');
assert.equal(display._resultDisplays[action].activate(),action);
assert.equal(display._groups[0].get_child().can_focus,false);
names[id]='sys-agent: Renamed application';
await display.updateSearch([id],['app']);
assert.equal(display.getFirstResult(),row);
assert.equal(row.destroyed,undefined);
assert.equal(row._airlockTitle.text,'Renamed application');
await display.updateSearch([],[]);
assert.equal(display.visible,false);assert.equal(display.getFirstResult(),null);
display.clear();assert.equal(row.destroyed,true);
""", presentation=True)


def test_late_queries_and_destroyed_displays_cannot_publish(javascript):
    javascript("""
const inventory=parseInventory(JSON.stringify(snapshot()));
const home=app(HOME), agent=app(AGENT);
const display=new VMSearchResults(provider({[home]:'usr-home: Console',[agent]:'sys-agent: Console'}),{},()=>inventory);
const requests=[];
const original=display._ensureResultActors.bind(display);
display._ensureResultActors=ids=>new Promise(resolve=>requests.push(async()=>{await original(ids);resolve();}));
const old=display.updateSearch([home],['old']);
const latest=display.updateSearch([agent],['latest']);
await requests[1]();await latest;
const row=display.getFirstResult();
await requests[0]();await old;
assert.equal(display.getFirstResult(),row);assert.equal(row.activate(),agent);
const dying=display.updateSearch([home],['next']);
display._onDestroy();await requests[2]();await dying;
assert.equal(display.getFirstResult(),row);
""", presentation=True)


def test_inventory_refresh_preserves_snapshot_and_ignores_stale_reads(javascript):
    javascript("""
const extension=new VMSearchExtension();
extension._active=true;extension._inventoryRead=0;extension._inventory=new Map();
const cancellable={is_cancelled:()=>false};extension._cancellable=cancellable;
const requests=[];let refreshed=0;
extension._refreshViews=()=>refreshed++;
extension._file={load_contents_async(cancellable,callback){requests.push(callback);}};
const result=text=>({load_contents_finish:()=>[true,new TextEncoder().encode(text)]});
const data=snapshot();
extension._loadInventory();extension._loadInventory();
requests[1](result(JSON.stringify(data)),{});
data[HOME].name='old-name';requests[0](result(JSON.stringify(data)),{});
assert.equal(extension._inventory.get(HOME).name,'usr-home');assert.equal(refreshed,1);
globalThis.logError=()=>{};
extension._loadInventory();requests[2](result('{'),{});
assert.equal(extension._inventory.get(HOME).name,'usr-home');
extension._loadInventory();
requests[3]({load_contents_finish(){throw {matches(domain,code){assert.equal(domain,42);return code===Gio.IOErrorEnum.NOT_FOUND;}};}},{});
assert.equal(extension._inventory.get(HOME).name,'usr-home');
extension._loadInventory();extension._active=false;
requests[4](result(JSON.stringify(data)),{});
assert.equal(extension._inventory.get(HOME).name,'usr-home');
""", presentation=True)


def test_enable_disable_restores_search_and_leaves_other_providers(javascript):
    javascript("""
const names={[app(HOME)]:'usr-home: Console'};
const applications={...provider(names),id:'applications',display:new Actor()};
applications.display.clear=()=>{};
const files={id:'files',display:new Actor()};
const content=new Actor();content.add_child(applications.display);content.add_child(files.display);
const filesDisplay=files.display;
const view=new Search.SearchResultsView();
Object.assign(view,{_content:content,_providers:[applications,files],terms:[],_defaultResult:null,
 _setSelected(){},_focusChildChanged(){}});
const original=function(item) {if(!item.display){item.display=new Actor();item.display.clear=()=>{};this._content.add_child(item.display);}};
Search.SearchResultsView.prototype._ensureProviderDisplay=original;
// Replace the test's captured constructor implementations before enable.
Object.assign(shellMocks.Main,{overview:{_overview:{controls:{_searchController:{_searchResults:view}}}}});
shellMocks.Shell.AppSystem={get_default:()=>({connect:()=>1,disconnect(){}})};
Object.assign(shellMocks.GLib,{build_filenamev:parts=>parts.join('/'),get_user_state_dir:()=>'/state',mkdir_with_parents(){}});
Gio.Cancellable=class {cancel(){};is_cancelled(){return false;}};
Gio.FileMonitorFlags={WATCH_MOVES:1};
Gio.File={new_for_path:()=>({monitor_directory:()=>({connect(){},cancel(){}})})};
shellMocks.InjectionManager.prototype.overrideMethod=function(prototype,name,override) {
 this.restore=()=>prototype[name]=original;prototype[name]=override(original);
};
shellMocks.InjectionManager.prototype.clear=function(){this.restore();};
const extension=new VMSearchExtension();extension._loadInventory=()=>{};
extension.enable();
assert.ok(applications.display instanceof VMSearchResults);
assert.equal(files.display,filesDisplay);
assert.equal(content.children[0],applications.display);
const grouped=applications.display;
extension.disable();
assert.equal(Search.SearchResultsView.prototype._ensureProviderDisplay,original);
assert.equal(grouped.destroyed,true);assert.equal(grouped._alive,false);
assert.equal(applications.display instanceof VMSearchResults,false);
assert.equal(files.display,filesDisplay);
assert.equal(extension._views.size,0);
""", presentation=True)


@pytest.mark.nix
def test_packaged_vm_search_matches_gnome_and_is_enabled():
    expression = f"""
      let
        flake = builtins.getFlake "{ROOT}";
        guest = flake.nixosConfigurations.airlock-domain-sys-gui-usb-lab;
        extension = builtins.head (builtins.filter (p:
          (p.pname or "") == "airlock-gnome-vm-search"
        ) guest.config.environment.systemPackages);
        directory = "${{extension}}/share/gnome-shell/extensions/airlock-vm-search@airlock.local";
      in {{
        overrides = guest.config.services.desktopManager.gnome.extraGSettingsOverrides;
        shellVersion = guest.pkgs.gnome-shell.version;
        metadata = builtins.fromJSON (builtins.readFile (directory + "/metadata.json"));
        files = builtins.attrNames (builtins.readDir directory);
        source = builtins.readFile (directory + "/extension.js");
      }}
    """
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr", expression],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    logs = Path(os.environ["AIRLOCK_LOG_DIR"]) / "vm-search"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "configuration.json").write_text(result.stdout)
    assert report["metadata"]["uuid"] in report["overrides"]
    assert report["shellVersion"].split(".")[0] in report["metadata"]["shell-version"]
    assert set(report["files"]) == {"extension.js", "model.js", "metadata.json", "stylesheet.css"}
    assert "InjectionManager" in report["source"]
