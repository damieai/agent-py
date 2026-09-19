"""Run the local CI gate in disposable simulation state, without production configuration."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_py.adapters.simulation import SimulatedSystem
from agent_py.config import Settings
from agent_py.db import Database
from agent_py.evaluation import evaluate
from agent_py.evaluation_gate import GatePolicy, load_json, run_gate
from agent_py.retrieval_evaluation import evaluate_retrieval
from agent_py.service import Service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=Path("examples/evaluation-gate-policy.json"))
    parser.add_argument("--fixture", type=Path, default=Path("examples/retrieval-development.json"))
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/gate"))
    args = parser.parse_args()
    outputs = [
        args.output_dir / name for name in ("simulation.json", "retrieval.json", "gate.json")
    ]
    if {p.resolve() for p in outputs} & {args.policy.resolve(), args.fixture.resolve()}:
        parser.error("Output paths must not overwrite policy or fixture")
    policy = GatePolicy.model_validate(load_json(args.policy))
    with TemporaryDirectory(prefix="agent-gate-") as temporary:
        root = Path(temporary)
        settings = Settings(
            environment="test",
            execution_mode="simulation",
            database_url=f"sqlite:///{root}/state.db",
            artifact_root=root / "artifacts",
            _env_file=None,
        )
        db = Database(settings.database_url)
        try:
            db.create_schema()
            service = Service(db, settings, SimulatedSystem(root / "remote.db"))
            evaluate(service, policy.simulation_split, outputs[0])
        finally:
            db.engine.dispose()
    evaluate_retrieval(args.fixture, outputs[1])
    result = run_gate(args.policy, outputs[0], outputs[1], outputs[1], args.fixture, outputs[2])
    status = result["body"]["status"]
    print(f"Local evaluation gate: {status}; report: {outputs[2]}; not production authorization")
    return {"PASS": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
