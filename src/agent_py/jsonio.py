"""Bounded JSON input with duplicate-key and nonfinite-number rejection."""

import json
from pathlib import Path


def decode_json(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Nonfinite JSON value")

    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid_constant)


def load_json(path: Path, limit=100_000):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON file exceeds limit")
    return decode_json(raw)
