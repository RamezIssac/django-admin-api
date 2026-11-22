"""Mutation-test only the admin_api lines changed since BASE, then print a Markdown report.

    uv run --with mutmut==3.8.1 python scripts/mutate_changed.py [BASE]   # BASE defaults to origin/main

Report only: exits 0 whatever survives. The report also lands in mutants/report.md. See CONTRIBUTING.md.
"""

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import mutmut.__main__ as mutmut_cli
from mutmut.code_coverage import CoverageInfo
from mutmut.mutation.data import SourceFileMutationData
from mutmut.mutation.diff_apply import get_diff_for_mutant
from mutmut.stats import status_by_exit_code

base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
diff = subprocess.run(
    ["git", "diff", "-U0", "--merge-base", base, "--", "admin_api/*.py", ":!admin_api/migrations"],
    capture_output=True,
    text=True,
    check=True,
).stdout

changed: dict[str, set[int]] = {}
for chunk in re.split(r"^diff --git ", diff, flags=re.MULTILINE)[1:]:
    path = re.search(r"^\+\+\+ b/(.+)$", chunk, re.MULTILINE)
    if path:
        for start, count in re.findall(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", chunk, re.MULTILINE):
            changed.setdefault(path.group(1), set()).update(range(int(start), int(start) + int(count or 1)))

summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull))


def report(text):
    """Print the report, add it to the job summary and keep a copy in mutants/report.md."""
    print(text)
    with summary.open("a") as out:
        out.write(text + "\n")
    Path("mutants").mkdir(exist_ok=True)
    Path("mutants/report.md").write_text(text + "\n")


if not changed:
    report(f"## Mutation testing\n\nNo Python source under admin_api/ changed since {base}.")
    sys.exit(0)

# Re-test earlier survivors: an edited test is not a change mutmut's cache sees.
for path in changed:
    data = SourceFileMutationData(path=Path(path))
    if not Path(data.meta_path).exists():
        continue
    data.load()
    data.exit_code_by_key = {k: None if v == 0 else v for k, v in data.exit_code_by_key.items()}
    data.save()

# mutmut mutates only "covered" lines when mutate_only_covered_lines is set; the changed lines stand in for coverage.
mutated_lines = {str((Path("mutants") / path).absolute()): lines for path, lines in changed.items()}
mutmut_cli.gather_coverage = lambda runner, files: CoverageInfo(covered_lines=mutated_lines)
mutmut_cli.cli(["run"], standalone_mode=False)

counts, survivors = Counter(), []
for path, lines in sorted(changed.items()):
    data = SourceFileMutationData(path=Path(path))
    data.load()
    source = Path(path).read_text().splitlines()
    for name, code in data.exit_code_by_key.items():
        status = status_by_exit_code[code]
        counts[status] += 1
        if status in ("survived", "no tests"):
            mutant_diff = get_diff_for_mutant(name, path=path)
            removed = next(line[1:].strip() for line in mutant_diff.splitlines()[2:] if line.startswith("-"))
            lineno = min((n for n in lines if source[n - 1].strip() == removed), default="?")
            survivors.append((f"{path}:{lineno}", status, mutant_diff))

total = sum(counts.values())
survived = counts["survived"] + counts["no tests"]
out = [
    "## Mutation testing (changed lines only)",
    "",
    f"Base: `{base}`. Files: {len(changed)}. Changed lines: {sum(map(len, changed.values()))}.",
    "",
    "| mutants | killed | survived | other |",
    "|---|---|---|---|",
    f"| {total} | {counts['killed']} | {survived} | {total - counts['killed'] - survived} |",
    "",
]
if counts.keys() - {"killed", "survived", "no tests"}:
    out += ["Other: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), ""]
for where, status, mutant_diff in survivors:
    out += [f"<details><summary><code>{where}</code> {status}</summary>", "", "```diff", mutant_diff, "```", ""]
    out += ["</details>"]
report("\n".join(out))
