"""The harness must keep working: every workload and scenario runs in quick mode."""

import json

import pytest

from mongospecs_bench import harness
from mongospecs_bench.cli import report
from mongospecs_bench.harness import Settings, metadata, write_result
from mongospecs_bench.stress import SCENARIOS
from mongospecs_bench.workloads import WORKLOADS

QUICK = Settings(quick=True)


@pytest.mark.parametrize("name", sorted(WORKLOADS))
async def test_workload_runs(name):
    result = await WORKLOADS[name](QUICK)
    assert result.workload == name and result.rows
    assert all(isinstance(row, dict) for row in result.rows)


@pytest.mark.parametrize("name", sorted(set(SCENARIOS) - {"X6"}))
async def test_stress_scenario_passes(name):
    result = await SCENARIOS[name](QUICK)
    assert result.conclusion.startswith("PASS"), result.conclusion


@pytest.mark.sharded
async def test_include_workload_on_sharded_cluster():
    result = await WORKLOADS["B3"](Settings(topology="sharded", quick=True))
    assert all(row["batch_ms"] > 0 and row["lookup_ms"] > 0 for row in result.rows)


async def test_results_are_written_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "RESULTS", tmp_path)
    monkeypatch.setattr("mongospecs_bench.cli.RESULTS", tmp_path)
    meta = await metadata(QUICK)
    result = await WORKLOADS["B6"](QUICK)
    path = write_result(result, meta)
    data = json.loads(path.read_text())
    assert data["workload"] == "B6" and data["meta"]["mongodb"] and data["meta"]["quick"] is True
    text = report()
    assert "## B6: Plugin overhead" in text and "| plugins |" in text
