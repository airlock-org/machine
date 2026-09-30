"""Authored sourceRef/build options normalize to source_ref/attr records."""

import json
import subprocess

import pytest

pytestmark = pytest.mark.nix

OUTPUT = "packages.x86_64-linux.test-source"


def evaluate_nix(repo_root, library, expression, selector, reference):
    schema = repo_root / f"lifecycle/misc/lib/{library}.nix"
    return subprocess.run(
        [
            "nix", "eval", "--impure", "--json", "--expr",
            f"let ref = {reference}; schema = import {schema}; "
            f"in (schema.{expression}){selector}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture(params=["system", "template", "root"])
def evaluate_ref(request, repo_root):
    if request.param == "system":
        library = "system"
        expression = "mkSystem { dom0.profile.source = ref; }"
        selector = ".dom0.profile.source_ref"
    elif request.param == "template":
        library = "topology"
        expression = "mkTopology { templates.test.source = ref; }"
        selector = ".templates.test.source_ref"
    else:
        library = "topology"
        expression = """mkTopology {
          domains.test = {
            label = "blue";
            backend = {
              type = "nix-closure";
              system.source = "/nix/store/test-system";
              kernel.name = "test-kernel";
              root.sourceRef = ref;
              store.source = "/nix/store/test-store";
            };
          };
        }"""
        selector = ".domains.test.backend.root.source_ref"
    def evaluate(reference):
        return evaluate_nix(
            repo_root, library, expression, selector, reference,
        )

    return evaluate


def test_build_reference_preserves_normalized_attr(evaluate_ref):
    result = evaluate_ref(f'{{ build = "{OUTPUT}"; subpath = "root.img"; }}')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "flake-output", "attr": OUTPUT, "subpath": "root.img",
    }


@pytest.mark.parametrize(
    ("reference", "error"),
    [
        (f'{{ attr = "{OUTPUT}"; }}', "unknown option(s): attr"),
        (
            f'{{ build = "{OUTPUT}"; attr = "{OUTPUT}"; }}',
            "unknown option(s): attr",
        ),
        ("{ }", "must set build"),
        ('{ build = "invalid/output"; }', "invalid flake output reference"),
        (
            f'{{ build = "{OUTPUT}"; subpath = "/root.img"; }}',
            "invalid source subpath",
        ),
    ],
)
def test_invalid_authored_reference_is_rejected(evaluate_ref, reference, error):
    result = evaluate_ref(reference)
    assert result.returncode != 0
    assert error in result.stderr


@pytest.fixture(
    params=[
        "dom0.profile", "dom0.nixos.system", "dom0.nixos.kernel", "templates.test",
        "system", "kernel", "initrd", "modules", "root", "store",
    ]
)
def evaluate_source_fields(request, repo_root):
    context = request.param
    library = "system" if context.startswith("dom0.") else "topology"

    def evaluate(fields):
        if library == "system":
            expression = f"mkSystem {{ {context} = {{ {fields} }}; }}"
            selector = f".{context}"
        elif context == "templates.test":
            expression = f"mkTopology {{ {context} = {{ {fields} }}; }}"
            selector = f".{context}"
        else:
            expression = f"""mkTopology {{
              domains.test = {{
                label = "blue";
                backend = {{
                  type = "nix-closure";
                  system.source = "/nix/store/test-system";
                  kernel.name = "test-kernel";
                  root.source = "/nix/store/test-root";
                  store.source = "/nix/store/test-store";
                }} // {{ {context} = {{ {fields} }}; }};
              }};
            }}"""
            selector = f".domains.test.backend.{context}"
        return evaluate_nix(
            repo_root, library, expression, selector, f'{{ build = "{OUTPUT}"; }}',
        )

    return evaluate


@pytest.mark.parametrize("field", ["source", "sourceRef"])
def test_authored_source_reference_preserves_normalized_source_ref(
    evaluate_source_fields, field,
):
    result = evaluate_source_fields(f"{field} = ref;")
    assert result.returncode == 0, result.stderr
    normalized = json.loads(result.stdout)
    assert normalized["source"] is None
    assert normalized["source_ref"] == {
        "type": "flake-output", "attr": OUTPUT, "subpath": "",
    }


def test_concrete_source_with_null_source_ref_is_preserved(evaluate_source_fields):
    result = evaluate_source_fields(
        'source = "/nix/store/test-source"; sourceRef = null;'
    )
    assert result.returncode == 0, result.stderr
    normalized = json.loads(result.stdout)
    assert normalized["source"] == "/nix/store/test-source"
    assert normalized["source_ref"] is None


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ("source_ref = ref;", "unknown option(s): source_ref"),
        ("source_ref = null;", "unknown option(s): source_ref"),
        (
            "sourceRef = ref; source_ref = ref;",
            "unknown option(s): source_ref",
        ),
        (
            'source = "/nix/store/test-source"; sourceRef = ref;',
            "source and sourceRef cannot both be set",
        ),
    ],
)
def test_invalid_authored_source_fields_are_rejected(
    evaluate_source_fields, fields, error,
):
    result = evaluate_source_fields(fields)
    assert result.returncode != 0
    assert error in result.stderr
