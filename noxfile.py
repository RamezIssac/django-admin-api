"""Test matrix and lint, run with nox (https://nox.thea.codes).

    uvx nox                                  # everything: lint + the whole matrix
    uvx nox -s lint
    uvx nox -s tests                         # the whole matrix
    uvx nox -s "tests(django=6.1, mcp=latest)"   # one combination (see `uvx nox -l`)
    uvx nox -s tests -- -k subadmin          # extra args go to pytest

Each combination gets its own venv (built by uv), with the dev dependencies of pyproject.toml
and the Django / MCP SDK / django-subadmin versions of that row. ``uv run pytest`` keeps using
the lock file (one combination); this is for the supported range.
"""

from __future__ import annotations

import nox

nox.needs_version = ">=2025.2.9"
nox.options.default_venv_backend = "uv"
nox.options.reuse_venv = "yes"
nox.options.sessions = ["lint", "tests"]

PYPROJECT = nox.project.load_toml("pyproject.toml")

# Django version → (Python, django-subadmin spec). Python: the lowest that version supports,
# within admin_api's range, so old Pythons are covered too. django-subadmin follows Django's
# version and has no 4.2 release (tests/test_subadmin.py skips there).
DJANGOS = {
    "4.2": ("3.10", None),
    "5.2": ("3.12", "django-subadmin>=5.2,<6"),
    "6.1": ("3.13", "django-subadmin>=6.1,<6.2"),
}
# MCP SDK: the lowest allowed and the latest allowed.
MCPS = {"2.3.0": "mcp==2.3.0", "latest": "mcp>=2.3,<3"}

MATRIX = [
    nox.param(python, django, mcp, id=f"django={django}, mcp={mcp}")
    for django, (python, _subadmin) in DJANGOS.items()
    for mcp in MCPS
]


def _dev_dependencies() -> list[str]:
    """The dev group minus the packages pinned per row."""
    pinned = ("django-subadmin", "mcp")
    return [dep for dep in nox.project.dependency_groups(PYPROJECT, "dev") if not dep.lower().startswith(pinned)]


@nox.session
@nox.parametrize("python, django, mcp", MATRIX)
def tests(session: nox.Session, django: str, mcp: str) -> None:
    """The test suite on one Django x MCP SDK combination."""
    major, minor = django.split(".")
    deps = ["-e", ".", *_dev_dependencies(), f"django>={django},<{major}.{int(minor) + 1}", MCPS[mcp]]
    subadmin = DJANGOS[django][1]
    if subadmin:
        deps.append(subadmin)
    session.install(*deps)
    if not subadmin:
        # a reused venv may still have it from an older run
        session.run("uv", "pip", "uninstall", "--python", session.python, "django-subadmin", silent=True)
    session.run("pytest", "-q", "-rs", *session.posargs)


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    """ruff check + ruff format --check."""
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", "admin_api", "tests", "examples", "noxfile.py")
