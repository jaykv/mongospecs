"""Consumer typing fixtures: mypy must reject exactly the lines marked ``# expect-error``."""

import pathlib
import re
import subprocess
import sys

FIXTURE = pathlib.Path(__file__).parent / "typing_fixtures" / "consumer.py"
ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_public_api_types_hold_for_a_consumer():
    expected = {
        number
        for number, line in enumerate(FIXTURE.read_text().splitlines(), start=1)
        if line.rstrip().endswith("# expect-error")
    }
    assert len(expected) >= 8
    run = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "--no-error-summary", "--no-incremental", str(FIXTURE)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    reported = {int(m.group(1)) for m in re.finditer(r"consumer\.py:(\d+): error", run.stdout)}
    assert reported == expected, (
        f"unexpected errors on lines {sorted(reported - expected)}, "
        f"missing errors on lines {sorted(expected - reported)}\n{run.stdout}{run.stderr}"
    )
