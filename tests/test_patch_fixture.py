import hashlib
import importlib.util
from pathlib import Path

from agent_py.patches import FileEdit, apply_patch


def test_fixed_trusted_fixture_reproduces_and_repairs_threshold(tmp_path):
    # These are checked-in trusted fixture bytes, not untrusted model-generated code.
    source = Path("examples/demo_service/src/queue_rules.py").read_bytes()
    path = tmp_path / "src" / "queue_rules.py"
    path.parent.mkdir()
    path.write_bytes(source)

    def load():
        spec = importlib.util.spec_from_file_location("fixture_rules", path)
        module = importlib.util.module_from_spec(spec)
        # Avoid pycache reusing a same-size candidate in this fixture test.
        exec(compile(path.read_text(), str(path), "exec"), module.__dict__)
        return module

    assert load().overdue(60, 60) is False
    candidate = source.decode().replace(
        "age_seconds > threshold_seconds", "age_seconds >= threshold_seconds"
    )
    apply_patch(
        tmp_path,
        [
            FileEdit(
                path="src/queue_rules.py",
                original_sha256=hashlib.sha256(source).hexdigest(),
                content=candidate,
            )
        ],
    )
    assert load().overdue(60, 60) is True
    assert load().overdue(59, 60) is False
