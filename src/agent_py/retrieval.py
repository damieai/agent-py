"""Deterministic, local sparse retrieval. Sources are data, never imported or executed."""

import ast
import math
import re
from collections import Counter

from agent_py.domain import DomainError, digest

POLICY = "context-v2-bm25-rrf"
MAX_CHUNKS = 10_000
CHUNK_BYTES = 2400


def tokens(text: str) -> list[str]:
    # Preserve exact identifiers as well as snake/camel components and Chinese bigrams.
    result = []
    for word in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text):
        if "\u4e00" <= word[0] <= "\u9fff":
            result.extend(word)
            result.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            result.append(word.lower())
            parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word).replace("_", " ").lower().split()
            if parts != [word.lower()]:
                result.extend(parts)
    return result


def chunk_document(document) -> list[dict]:
    lines = document.body.splitlines(keepends=True)
    if not lines:
        return []
    boundaries, symbols = {1, len(lines) + 1}, {}
    # Bound parser input. Invalid or large Python falls back to line windows.
    if document.source.partition("?")[0].endswith(".py") and len(document.body.encode()) <= 64_000:
        try:
            tree = ast.parse(document.body)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    boundaries.update((start, node.end_lineno + 1))
                    symbols[start] = node.name
        except (SyntaxError, ValueError, RecursionError):
            pass
    whole_digest = digest(document.body)
    chunks = []
    points = sorted(boundaries)
    for left, right in zip(points, points[1:]):
        start, size = left, 0
        for line_no in range(left, right):
            length = len(lines[line_no - 1].encode())
            if size and size + length > CHUNK_BYTES:
                chunks.append((start, line_no - 1, symbols.get(left)))
                start, size = line_no, 0
            size += length
        if size:
            chunks.append((start, right - 1, symbols.get(left)))
    return [
        {
            "id": document.id,  # Keep model citation IDs compatible with whole-document evidence.
            "source": document.source,
            "version": document.version,
            "body": "".join(lines[start - 1 : end]),
            "trust": "untrusted_source",
            "chunk_id": digest([document.id, document.version, whole_digest, start, end]),
            "document_digest": whole_digest,
            "start_line": start,
            "end_line": end,
            "symbol": symbol,
        }
        for start, end, symbol in chunks
    ]


def rank_chunks(documents, query: str) -> list[dict]:
    chunks = []
    for document in documents:
        chunks.extend(chunk_document(document))
        if len(chunks) > MAX_CHUNKS:
            raise DomainError("CONTEXT_CAPACITY", "Authorized corpus exceeds chunk limit", 413)
    if not chunks:
        return []
    q = set(tokens(query))
    counts = [Counter(tokens(c["body"])) for c in chunks]
    lengths = [sum(c.values()) for c in counts]
    average = sum(lengths) / len(chunks) or 1
    df = Counter(term for count in counts for term in count if term in q)
    body_scores, source_scores = {}, {}
    for i, (chunk, count, length) in enumerate(zip(chunks, counts, lengths)):
        score = 0.0
        for term in sorted(q & count.keys()):
            frequency = count[term]
            idf = math.log1p((len(chunks) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        if score:
            body_scores[i] = score
        source_score = len(q & set(tokens(chunk["source"] + " " + (chunk["symbol"] or ""))))
        if source_score:
            source_scores[i] = source_score
    ranks = []
    for scores in (body_scores, source_scores):
        ordered = sorted(
            scores, key=lambda i: (-scores[i], chunks[i]["id"], chunks[i]["start_line"])
        )
        ranks.append({i: rank for rank, i in enumerate(ordered, 1)})
    fused = {i: sum(1 / (60 + r[i]) for r in ranks if i in r) for i in set().union(*ranks)}
    ordered = sorted(fused, key=lambda i: (-fused[i], chunks[i]["id"], chunks[i]["start_line"]))
    return [
        {
            **chunks[i],
            "ranking": {
                "bm25_rank": ranks[0].get(i),
                "source_rank": ranks[1].get(i),
                "rrf_score": round(fused[i], 10),
            },
        }
        for i in ordered
    ]
