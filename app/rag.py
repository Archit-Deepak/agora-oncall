"""Tiny deterministic retriever over the knowledge base.

Chunks the KB markdown by heading and scores chunks by query-term overlap. No
embeddings, no network, no extra dependency — for ~10 small files this is plenty
and can't fail on stage. (BM25 is a drop-in upgrade later if wanted.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

KB_DIR = Path(__file__).resolve().parent / "kb"
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@dataclass
class Chunk:
    source: str  # file name
    heading: str
    text: str
    _tokens: set[str]


class KnowledgeBase:
    def __init__(self, kb_dir: Path = KB_DIR):
        self.chunks: list[Chunk] = []
        for md in sorted(kb_dir.glob("*.md")):
            self._load(md)

    def _load(self, path: Path) -> None:
        heading = path.stem
        buf: list[str] = []

        def flush():
            if buf:
                body = "\n".join(buf).strip()
                if body:
                    toks = set(_tokens(heading + " " + body))
                    self.chunks.append(Chunk(path.name, heading, body, toks))

        for line in path.read_text().splitlines():
            if line.startswith("#"):
                flush()
                buf = []
                heading = line.lstrip("# ").strip()
            buf.append(line)
        flush()

    def top_k(self, query: str, k: int = 3) -> list[Chunk]:
        q = set(_tokens(query))
        if not q:
            return []
        scored = [(len(q & c._tokens), c) for c in self.chunks]
        scored = [(s, c) for s, c in scored if s > 0]
        scored.sort(key=lambda sc: sc[0], reverse=True)
        return [c for _, c in scored[:k]]
