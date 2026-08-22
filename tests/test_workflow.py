from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

# Read but deliberately NOT wired into the workflow, with the reason. An entry here is a
# claim that the run is correct without the variable — not a place to park an oversight.
_OPTIONAL_ENV = {
    # Defaults to 587. Wiring it as `${{ secrets.SMTP_PORT }}` when no such secret exists
    # sets it to the EMPTY STRING rather than leaving it unset, and `int("")` raises — so
    # adding it "for completeness" would break delivery instead of documenting it.
    "SMTP_PORT": "smtp.py defaults it to 587",
}


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "VALUE"`, so an indirect read resolves. tarsasjatekos.py does
    `os.environ.get(_API_KEY_ENV)`, which a text search for the variable name never finds
    — and that source is exactly the one whose variable went missing from the spec."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def _resolve(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _reads_env(node: ast.AST) -> ast.expr | None:
    """`os.environ.get(X)`, `os.getenv(X)` and `os.environ[X]` — the three shapes this
    codebase uses to read the process environment."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
        target = node.func
        reads_environ_get = (
            target.attr == "get"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "environ"
        )
        if reads_environ_get or target.attr == "getenv":
            return node.args[0]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
    ):
        return node.slice
    return None


def environment_variables_read(src_dir: Path) -> dict[str, set[str]]:
    """Every env var the shipped code reads, mapped to the files that read it."""
    found: dict[str, set[str]] = {}
    for path in sorted(src_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            argument = _reads_env(node)
            if argument is None:
                continue
            name = _resolve(argument, constants)
            if name:
                found.setdefault(name, set()).add(path.name)
    return found


@pytest.fixture
def workflow_env(repo_root: Path) -> dict[str, str]:
    workflow = yaml.safe_load(
        (repo_root / ".github/workflows/digest.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["run"]["steps"]
    (run_step,) = [step for step in steps if step.get("name") == "Run digest"]
    return run_step["env"]


def test_every_environment_variable_the_code_reads_reaches_the_workflow(
    repo_root: Path, workflow_env: dict[str, str]
) -> None:
    """A secret that exists in the repo settings but is not named in the step's `env:`
    never reaches the process — the run just behaves as if it were unset. That is silent:
    the source raises ConfigError, the per-source guard logs it, and the digest is simply
    one source lighter. This test is what turns the next such gap into a red build.

    It is the same failure class as a config switch with no effect: the configuration says
    the capability is there, and the system behaves as though it is not."""
    read = environment_variables_read(repo_root / "src")

    assert read, "no environment reads found — the AST walk stopped matching the code"
    missing = {
        name: sorted(files)
        for name, files in read.items()
        if name not in workflow_env and name not in _OPTIONAL_ENV
    }

    assert missing == {}, (
        f"read by the code but absent from the `Run digest` env block: {missing}. "
        "Add it to .github/workflows/digest.yml, or to _OPTIONAL_ENV with the reason it "
        "is safe to leave unset."
    )


def test_the_workflow_does_not_pass_secrets_nothing_reads(
    repo_root: Path, workflow_env: dict[str, str]
) -> None:
    # The other direction, and the reason the two are separate assertions: an env entry no
    # code reads is dead configuration that reads as a working capability.
    read = environment_variables_read(repo_root / "src")

    unused = sorted(set(workflow_env) - set(read))

    assert unused == [], f"passed to the run but never read: {unused}"


def test_the_optional_list_only_holds_variables_the_code_actually_reads(
    repo_root: Path,
) -> None:
    # Keeps the exemption list from outliving the code it exempts.
    read = environment_variables_read(repo_root / "src")

    stale = sorted(name for name in _OPTIONAL_ENV if name not in read)

    assert stale == [], f"listed as optional but nothing reads it any more: {stale}"
