import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / "tools/managed_update_accept.py"
    spec = importlib.util.spec_from_file_location("fault_result_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def process(records, status=0):
    output = "\n".join(json.dumps(record) for record in records)
    return SimpleNamespace(returncode=status, communicate=lambda **_: (output, None))


@pytest.mark.parametrize("records,status", [
    ([{"armed": "interruption"}], 0),
    ([{"armed": "interruption"}], 1),
    ([{"fault": "interruption", "job": "one", "state": "failed"}], 0),
    ([{"injected": "interruption", "job": "one"},
      {"fault": "interruption", "job": "other", "state": "failed"}], 0),
    ([{"injected": "interruption", "job": "one"},
      {"fault": "withheld", "job": "one", "state": "failed"}], 0),
    ([{"injected": "interruption", "job": "one"},
      {"fault": "interruption", "job": "one", "state": "rolled_back"}], 0),
    ([{"injected": "interruption", "job": "one"},
      {"fault": "interruption", "job": "one", "state": "failed"}], 1),
])
def test_incomplete_or_unsettled_injection_is_not_a_result(runner, records, status):
    with pytest.raises(runner.AcceptanceFailure):
        runner.read_fault(process(records, status))


def test_completed_fault_returns_the_job_it_settled(runner):
    terminal = {"fault": "withheld", "job": "one", "state": "failed"}
    assert runner.read_fault(process([
        {"armed": "withheld"}, {"injected": "withheld", "job": "one"}, terminal,
    ])) == terminal
