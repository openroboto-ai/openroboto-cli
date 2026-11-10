"""Exercise the release event guard without dispatching a publishing workflow."""

import re
from pathlib import Path

import pytest
import yaml


def _matches_guard(expression: str, event: str, ref: str) -> bool:
    """Evaluate only the two reviewed GitHub expression terms, fail otherwise."""
    terms = []
    for term in expression.split(" && "):
        if term == "github.event_name == 'push'":
            terms.append(event == "push")
        elif match := re.fullmatch(r"startsWith\(github\.ref, '([^']+)'\)", term):
            terms.append(ref.startswith(match[1]))
        else:
            raise AssertionError(f"unreviewed release condition: {term}")
    return all(terms)


@pytest.mark.parametrize(
    "event,ref,publishes",
    [
        ("push", "refs/tags/v1.8.0", True),
        ("push", "refs/tags/v1.8.0rc1", True),
        ("push", "refs/heads/main", False),
        ("workflow_dispatch", "refs/heads/main", False),
        ("workflow_dispatch", "refs/tags/v1.8.0", False),
        ("pull_request", "refs/pull/1/merge", False),
        ("pull_request", "refs/tags/v1.8.0", False),
        ("schedule", "refs/tags/v1.8.0", False),
        ("workflow_call", "refs/tags/v1.8.0", False),
    ],
)
def test_real_pypi_requires_a_pushed_version_tag(event, ref, publishes):
    workflow = Path(__file__).parents[1] / ".github/workflows/release.yml"
    expression = yaml.safe_load(workflow.read_text())["jobs"]["publish"]["if"]
    assert _matches_guard(expression, event, ref) is publishes
