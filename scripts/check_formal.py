"""Check all pinned TLC scenarios. Provide a local, hash-verified jar and Java 11+."""

import argparse
import os
from pathlib import Path

from agent_py.formal_check import run_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jar", type=Path, default=os.environ.get("TLC_JAR"))
    parser.add_argument("--java", default=os.environ.get("JAVA", "java"))
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/formal"))
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.jar is None:
        parser.error("Provide --jar or TLC_JAR (see formal/README.md)")
    java = str(Path(args.java).resolve()) if "/" in args.java else args.java
    report = run_suite(
        Path(__file__).resolve().parents[1], args.jar, java, args.output_dir, args.timeout
    )
    for case in report["cases"]:
        print(f"{case['case']}: {case['status']}")
    print(f"Finite safety gate: {report['status']}; report: {args.output_dir / 'report.json'}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
