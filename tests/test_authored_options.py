"""Canonical authored spellings keep the normalized deployment contracts."""

import json
import subprocess

import pytest

pytestmark = pytest.mark.nix

BACKEND = """{
  type = "nix-closure";
  system.source = "/nix/store/test-system";
  kernel.name = "test-kernel";
  root.source = "/nix/store/test-root";
  store.source = "/nix/store/test-store";
}"""

CASES = [
    ("system", "dom0.profile", "activationCommand", "activation_command",
     '"bin/custom-activate"', "bin/custom-activate"),
    ("system", "dom0.profile", "activationAction", "activation_action", '"none"', "none"),
    ("system", "dom0.nixos.system", "switchAction", "switch_action", '"boot"', "boot"),
    ("topology", "templates.test", "skipStart", "skip_start", "false", False),
    ("topology", "root", "readOnly", "read_only", "false", False),
    ("topology", "store", "readOnly", "read_only", "true", True),
    ("topology", "store", "blobDevice", "blob_device", '"/dev/xvdf"', "/dev/xvdf"),
    ("topology", "store", "viewBackend", "view_backend", '"erofs-objectmap"', "erofs-objectmap"),
    ("topology", "state", "persistentSize", "persistent_size", "8192", 8192),
    ("topology", "", "qrexecPolicy", "qrexec_policy", "{ rules = [ ]; }", {"rules": []}),
]


@pytest.fixture(params=CASES, ids=[f"{c[1] or 'topology'}.{c[2]}" for c in CASES])
def authored_option(request, repo_root):
    library, context, canonical, obsolete, value, expected = request.param

    def evaluate(assignment):
        if context in ("root", "store", "state"):
            config = f"""{{ domains.test = {{
              label = "blue";
              backend = {BACKEND} // {{
                {context} = (({BACKEND}).{context} or {{ }}) // {{ {assignment} }};
              }};
            }}; }}"""
            selector = f".domains.test.backend.{context}.{obsolete}"
        else:
            path = f"{context}." if context else ""
            config = f"{{ {context} = {{ {assignment} }}; }}" if context else f"{{ {assignment} }}"
            selector = f".{path}{obsolete}"
        function = "mkSystem" if library == "system" else "mkTopology"
        schema = repo_root / f"lifecycle/misc/lib/{library}.nix"
        return subprocess.run(
            ["nix", "eval", "--impure", "--json", "--expr",
             f"((import {schema}).{function} {config}){selector}"],
            text=True, capture_output=True, check=False,
        )

    return evaluate, canonical, obsolete, value, expected


def test_canonical_option_preserves_normalized_field(authored_option):
    evaluate, canonical, _, value, expected = authored_option
    result = evaluate(f"{canonical} = {value};")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("duplicate", [False, True])
def test_obsolete_option_is_rejected(authored_option, duplicate):
    evaluate, canonical, obsolete, value, _ = authored_option
    fields = f"{obsolete} = {value};"
    if duplicate:
        fields += f" {canonical} = {value};"
    result = evaluate(fields)
    assert result.returncode != 0
    assert f"unknown option(s): {obsolete}" in result.stderr


@pytest.mark.parametrize("name", ["chunkSize", "chunk_size"])
def test_unused_store_chunk_size_is_rejected(repo_root, name):
    schema = repo_root / "lifecycle/misc/lib/topology.nix"
    config = f"""{{ domains.test = {{ label = "blue"; backend = {BACKEND} // {{
      store = ({BACKEND}).store // {{ {name} = 4096; }};
    }}; }}; }}"""
    result = subprocess.run(
        ["nix", "eval", "--impure", "--json", "--expr",
         f"(import {schema}).mkTopology {config}"],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert f"unknown option(s): {name}" in result.stderr
