from __future__ import annotations

import re

from tests.conftest import ROOT

LINUX = ROOT / ".github" / "workflows" / "linux.yml"


def _job_block(text: str, job: str) -> str:
    match = re.search(rf"^  {job}:\n(.*?)(?=^  \w+:\n|\Z)", text, re.M | re.S)
    assert match, f"job {job} missing"
    return match.group(1)


def _work_env(job_block: str) -> dict[str, str]:
    match = re.search(
        r"^      - id: work\n(.*?)(?=^      - |\Z)", job_block, re.M | re.S
    )
    assert match, "work step missing"
    env = re.search(r"^        env:\n((?:^          .*\n)+)", match.group(1), re.M)
    assert env, "work step has no env"
    pairs = {}
    for line in env.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(": ")
        pairs[key] = value
    return pairs


def test_linux_work_steps_get_job_token_and_command_only():
    # mise and similar tools hit GitHub's 60/hour anonymous limit without a
    # token, and a caller cannot pass github.token through `with`.
    text = LINUX.read_text(encoding="utf-8")
    for job in ("primary", "fallback", "last"):
        assert _work_env(_job_block(text, job)) == {
            "WORK_COMMAND": "${{ inputs.run }}",
            "GITHUB_TOKEN": "${{ github.token }}",
        }, job


def test_linux_token_stays_read_only():
    text = LINUX.read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read\n(?! )", text, re.M)
    assert len(re.findall(r"^\s*permissions:", text, re.M)) == 1
    assert "secrets." not in text
