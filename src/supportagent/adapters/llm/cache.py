"""The eval response cache (ARCH §11, 11-wp WP-104): ``evals/.cache/``.

The key covers the **rendered prompt** (system + user, after redaction),
the **model id**, the **snapshot id**, and the output budget. Any change, a
prompt edit or a newly retrieved memory case, is a different key and forces a
live call. An entry also records the **resolved model** the provider
reported; it's only reused when that equals the pinned model id. A provider
that starts serving a different version under the same id therefore misses
the cache instead of replaying stale answers. Fixture runs never use the cache.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from supportagent.ports.llm import LLMProvider, ProviderRequest, ProviderResponse


def cache_key(request: ProviderRequest, snapshot_id: str, sample: int = 0) -> str:
    rendered = hashlib.sha256(
        json.dumps({"system": request.system, "user": request.user}, sort_keys=True).encode()
    ).hexdigest()
    material = json.dumps(
        {
            "rendered_prompt_hash": rendered,
            "model_id": request.model_id,
            "snapshot_id": snapshot_id,
            "max_output_tokens": request.max_output_tokens,
            # Sample i of N is its own entry: the quality suite's N samples
            # must be N real calls, not one answer replayed N times.
            "sample": sample,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


class CachingLLMProvider:
    def __init__(self, inner: LLMProvider, *, directory: Path, snapshot_id: str) -> None:
        self._inner = inner
        self._dir = directory
        self._snapshot = snapshot_id
        self.hits = 0
        self.misses = 0
        #: The sample index the next calls belong to (set by the quality runner).
        self.sample = 0

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        path = self._dir / f"{cache_key(request, self._snapshot, self.sample)}.json"
        if path.is_file():
            entry = json.loads(path.read_text(encoding="utf-8"))
            if entry.get("resolved_model") == request.model_id:
                self.hits += 1
                return ProviderResponse(**entry)
        self.misses += 1
        response = self._inner.complete_json(request)
        if response.resolved_model == request.model_id:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(response), sort_keys=True), encoding="utf-8")
        return response


__all__ = ["CachingLLMProvider", "cache_key"]
