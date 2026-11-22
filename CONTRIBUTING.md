# Contributing

Setup, tests and the nox matrix are in the README's [Development](README.md#development) section.

## Mutation testing

The `mutation` workflow (`.github/workflows/mutation.yml`, pull requests only) runs
[mutmut](https://github.com/boxed/mutmut) on the `admin_api/` lines the PR changed and posts the mutants, kills and
survivors (each with its `file:line` and diff) to the job summary and a PR comment. It is a report: survivors do not fail the PR. Run the same check locally:

```bash
uv run --with mutmut==3.8.1 python scripts/mutate_changed.py origin/main   # BASE: the branch the PR targets
```

The report prints at the end and stays in `mutants/comment.md`. `uv run --with mutmut==3.8.1 mutmut browse` opens the
results. A survivor means the tests ran the line and asserted nothing that caught the change: add the assertion.

An equivalent mutant (the change cannot alter behaviour) is marked on its line with a reason after a comma:

```python
chunk = rows[:limit]  # pragma: no mutate, a larger slice is cut again by the caller
```

Note: put a comma after `no mutate`, not a colon or a space, because mutmut reads a first word `block`, `start` or `end`
as a pragma mode.

Note: delete `mutants/` after weakening a test, not only after changing source, because mutmut keeps a cached kill until
the function under test changes. Survivors are always re-tested.

## Public API changes

The `API surface` workflow (`.github/workflows/api-surface.yml`, pull requests only) lists the public names of
`admin_api` the PR adds, removes or changes: modules, classes, functions, methods and module-level constants, each with
its kind (`constant (dict)`, `method`, ...) and signature. Public means not underscore-prefixed and outside `tests` and
`migrations`, and a module's `__all__` wins where present. It is a report: it fails only when the tool itself fails.
Run the same check locally:

```bash
uv run scripts/api_surface.py origin/main   # BASE: the branch the PR targets; griffe is pinned in the script header
```

The report prints and stays in `api-surface/comment.md`.

The `PR comment` workflow (`.github/workflows/pr-comment.yml`) posts both reports on the PR, fork PRs included: one
comment per report, found by the marker on its first line and updated on each push.

Note: test a change to `pr-comment.yml` after it reaches `main` and not on the PR that edits it, because `workflow_run`
workflows always run from the default branch.
