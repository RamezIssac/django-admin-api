# /// script
# requires-python = ">=3.12"
# dependencies = ["griffe==2.4.1"]
# ///
"""List the public names of a package added, removed or changed since BASE, as the PR comment.

    uv run scripts/api_surface.py [BASE] [--package admin_api]

Compares the merge base of BASE and HEAD against the working tree; writes api-surface/comment.md.
Report only: exits 0 whatever changed, non-zero only when the tool fails. See CONTRIBUTING.md.
"""

import argparse
import ast
import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

import griffe

MARKER = "<!-- api-surface -->"
VALUE_KINDS = {
    "ExprDict": "dict", "ExprDictComp": "dict", "ExprList": "list", "ExprListComp": "list",
    "ExprTuple": "tuple", "ExprSet": "set", "ExprSetComp": "set", "ExprCall": "call",
}

parser = argparse.ArgumentParser()
parser.add_argument("base", nargs="?", default="origin/main")
parser.add_argument("--package", default="admin_api")
args = parser.parse_args()


def git(*cmd):
    return subprocess.run(["git", *cmd], capture_output=True, check=True).stdout


def value_kind(value):
    if isinstance(value, str):
        try:
            return type(ast.literal_eval(value)).__name__
        except (ValueError, SyntaxError):
            return "name"
    return VALUE_KINDS.get(type(value).__name__, "expression")


def describe(obj):
    """Return (kind, signature) for one public object."""
    if obj.is_alias:
        return "alias", obj.target_path
    if obj.is_function:
        return ("method" if obj.parent.is_class else "function"), obj.signature()
    if obj.is_class:
        return "class", ", ".join(map(str, obj.bases))
    if obj.is_attribute:
        scope = "constant" if obj.parent.is_module else "attribute"
        return (f"{scope} ({value_kind(obj.value)})" if obj.value is not None else scope), str(obj.annotation or "")
    return obj.kind.value, ""


def surface(obj, found):
    """Map every public path under obj to (kind, signature)."""
    for member in obj.members.values():
        skipped = member.name == "migrations" or member.name.startswith("test")
        if not member.is_public or (not member.is_alias and member.is_module and skipped):
            continue
        found[member.path] = describe(member)
        if not member.is_alias and (member.is_module or member.is_class):
            surface(member, found)
    return found


def load(root):
    return surface(griffe.load(args.package, search_paths=[root], try_relative_path=False, resolve_aliases=False), {})


base_sha = git("merge-base", args.base, "HEAD").decode().strip()
with tempfile.TemporaryDirectory() as tmp:
    with tarfile.open(fileobj=io.BytesIO(git("archive", base_sha, args.package))) as tar:
        tar.extractall(tmp, filter="data")
    before = load(tmp)
after = load(".")


def code(text):
    return "`" + text.replace("|", "\\|") + "`" if text else ""


def without_class_members(paths, kinds):
    """Drop the members of a class that is itself in paths: a new class lists once, not once per method."""
    parents = {p: p.rpartition(".")[0] for p in paths}
    return sorted(p for p, parent in parents.items() if parent not in paths or kinds[parent][0] != "class")


added = without_class_members(after.keys() - before.keys(), after)
removed = without_class_members(before.keys() - after.keys(), before)
changed = sorted(p for p in after.keys() & before.keys() if after[p] != before[p])
rows = [f"| added | {code(p)} | {after[p][0]} | {code(after[p][1])} |" for p in added]
rows += [f"| removed | {code(p)} | {before[p][0]} | {code(before[p][1])} |" for p in removed]
for p in changed:
    (old_kind, old_sig), (new_kind, new_sig) = before[p], after[p]
    kind = new_kind if old_kind == new_kind else f"{old_kind} to {new_kind}"
    rows.append(f"| changed | {code(p)} | {kind} | {code(old_sig) or '_(none)_'} to {code(new_sig) or '_(none)_'} |")

if rows:
    head = os.environ.get("PR_HEAD_SHA") or git("rev-parse", "HEAD").decode()
    counts = f"**{len(added)} added, {len(removed)} removed, {len(changed)} changed**"
    rerun = f"uv run scripts/api_surface.py {args.base} --package {args.package}"
    footer = f"Advisory: this report never blocks a merge. Commit `{head[:7]}`. A new or removed class is listed once,"
    footer += f" without its members. Rerun locally: `{rerun}`."
    lines = [
        "## Public API changes",
        "",
        f"{counts} in `{args.package}` since `{args.base}` (merge base `{base_sha[:7]}`).",
        "",
        "| Change | Name | Kind | Signature |",
        "|---|---|---|---|",
        *rows,
        "",
        "---",
        f"<sub>{footer}</sub>",
    ]
else:
    lines = ["No public API changes."]

text = "\n".join([MARKER, *lines]) + "\n"
out = Path("api-surface/comment.md")
out.parent.mkdir(exist_ok=True)
out.write_text(text)
with Path(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull)).open("a") as summary:
    summary.write(text)
print(text)
