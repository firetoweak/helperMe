from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Sequence


PRINCIPLE_PATH = "docs/原则.md"
PROMPT_PATH = Path(__file__).with_name("review_prompt.md")
DEFAULT_BRANCH = "main"


class ReviewInputError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewEvidence:
    repository: Path
    design_revision: str
    head_revision: str
    principles: str
    design: str


def _git(
    repository: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def find_repository(start: Path) -> Path:
    result = _git(start, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip())


def collect_evidence(repository: Path, revision: str) -> ReviewEvidence:
    branch = _git(repository, "branch", "--show-current").stdout.strip()
    if branch == DEFAULT_BRANCH:
        raise ReviewInputError(
            f"draft_review_branch_violation: candidate implementation is on {DEFAULT_BRANCH}"
        )

    resolved = _git(
        repository,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    if resolved.returncode != 0:
        raise ReviewInputError(f"invalid design_revision: {revision}")
    design_revision = resolved.stdout.strip()

    ancestor = _git(
        repository,
        "merge-base",
        "--is-ancestor",
        design_revision,
        "HEAD",
        check=False,
    )
    if ancestor.returncode == 1:
        raise ReviewInputError("design_revision is not an ancestor of HEAD")
    if ancestor.returncode != 0:
        raise subprocess.CalledProcessError(
            ancestor.returncode,
            ancestor.args,
            ancestor.stdout,
            ancestor.stderr,
        )

    status = _git(repository, "status", "--porcelain", "--untracked-files=all")
    if status.stdout:
        raise ReviewInputError("working tree must be clean before Draft PR review")

    principle_change = _git(
        repository,
        "diff",
        "--quiet",
        f"{design_revision}..HEAD",
        "--",
        PRINCIPLE_PATH,
        check=False,
    )
    if principle_change.returncode == 1:
        raise ReviewInputError(
            f"principle_boundary_violation: {PRINCIPLE_PATH} changed after design_revision"
        )
    if principle_change.returncode != 0:
        raise subprocess.CalledProcessError(
            principle_change.returncode,
            principle_change.args,
            principle_change.stdout,
            principle_change.stderr,
        )

    frozen_principles = _git(
        repository,
        "show",
        f"{design_revision}:{PRINCIPLE_PATH}",
        check=False,
    )
    if frozen_principles.returncode != 0:
        raise ReviewInputError(
            f"{PRINCIPLE_PATH} does not exist at design_revision"
        )
    principles = frozen_principles.stdout
    design = _git(
        repository,
        "show",
        "-s",
        "--format=%B",
        design_revision,
    ).stdout
    head_revision = _git(repository, "rev-parse", "HEAD").stdout.strip()

    return ReviewEvidence(
        repository=repository,
        design_revision=design_revision,
        head_revision=head_revision,
        principles=principles,
        design=design,
    )


def build_prompt(template: str, evidence: ReviewEvidence) -> str:
    return "\n\n".join(
        (
            template.rstrip(),
            "## 冻结输入",
            f"design_revision: {evidence.design_revision}\n"
            f"head_revision: {evidence.head_revision}",
            f"### 项目原则\n\n{evidence.principles.rstrip()}",
            f"### 原始设计提交\n\n{evidence.design.rstrip()}",
        )
    ) + "\n"


def render_review_prompt(repository: Path, revision: str) -> str:
    evidence = collect_evidence(repository, revision)
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return build_prompt(template, evidence)


def main(argv: Sequence[str] | None = None) -> int:
    args = tuple(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python tools/review_draft.py <design_revision>", file=sys.stderr)
        return 2

    try:
        repository = find_repository(Path.cwd())
        prompt = render_review_prompt(repository, args[0])
    except ReviewInputError as error:
        print(error, file=sys.stderr)
        return 2

    print(prompt, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
