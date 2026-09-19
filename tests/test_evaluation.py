import pytest

from agent_py.evaluation import dataset, run_case


@pytest.mark.parametrize("case", dataset()[:6], ids=lambda c: c["family"])
def test_conformance_families(env, case):
    assert run_case(env[0], case)["passed"]
