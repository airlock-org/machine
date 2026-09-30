"""Authored flake references use build; normalized references retain attr."""

import json
import subprocess

import pytest

pytestmark = pytest.mark.nix

OUTPUT = "packages.x86_64-linux.test-source"


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
    schema = repo_root / f"lifecycle/misc/lib/{library}.nix"

    def evaluate(reference):
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
