from hypothesis import given, settings
from hypothesis import strategies as st

from agent_py.adapters.simulation import SimulatedSystem, UnknownOutcome
from agent_py.domain import digest


@settings(max_examples=30)
@given(st.lists(st.sampled_from(["submit", "query", "lose"]), min_size=1, max_size=30))
def test_remote_idempotency_under_arbitrary_retry_sequences(actions):
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as root:
        remote = SimulatedSystem(Path(root) / "remote.db")
        for action in actions:
            if action == "lose":
                remote.inject("t", "op", "response_lost")
            elif action == "query":
                remote.query("t", "op")
            else:
                try:
                    remote.execute(
                        "t",
                        "op",
                        "create_pr",
                        "resource",
                        {"candidate_sha": digest("candidate"), "base_sha": digest("base")},
                    )
                except UnknownOutcome:
                    pass
            assert remote.snapshot("t", "resource")["effect_count"] <= 1
