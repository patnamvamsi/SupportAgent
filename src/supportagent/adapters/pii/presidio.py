"""Presidio PII analyser (ADR-0021 §4a): local detection, nothing leaves the deployment.

Presidio and its spaCy model are an optional install (``supportagent[pii]``
plus ``python -m spacy download <model>``). Both are imported on first use;
anything missing raises ``PiiUnavailable`` naming what to install, so the
caller fails closed. Nothing is fetched from the network: Presidio would
download a missing model, so it is checked first, and its email recogniser's
domain check (``tldextract``) would fetch the public suffix list, so it is
pointed at the list bundled with the package.

A deployment adds recognisers for its own identifiers as regexes.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from supportagent.domain.errors import PiiUnavailable
from supportagent.domain.pii import PiiSpan


@dataclass(frozen=True, slots=True)
class PiiRecognizerSpec:
    """A deployment's own identifier: ``entity`` wherever ``regex`` matches."""

    entity: str
    regex: str
    score: float


class PresidioAnalyzer:
    """``PiiAnalyzer`` over Presidio's ``AnalyzerEngine`` (built once, lazily)."""

    def __init__(
        self,
        *,
        entities: Sequence[str],
        min_score: float,
        language: str,
        model: str,
        recognizers: Sequence[PiiRecognizerSpec] = (),
    ) -> None:
        self._entities = sorted({*entities, *(r.entity for r in recognizers)})
        self._min_score = min_score
        self._language = language
        self._model = model
        self._recognizers = tuple(recognizers)
        self._engine: Any = None
        self._lock = threading.Lock()

    def analyze(self, text: str) -> list[PiiSpan]:
        engine = self._load()
        try:
            found = engine.analyze(
                text=text,
                language=self._language,
                entities=self._entities,
                score_threshold=self._min_score,
            )
        except Exception as exc:
            raise PiiUnavailable(f"presidio failed: {type(exc).__name__}") from exc  # noqa: TRY003
        return [PiiSpan(r.start, r.end, r.entity_type, float(r.score)) for r in found]

    def _load(self) -> Any:  # noqa: ANN401 — presidio is untyped and optional
        with self._lock:
            if self._engine is None:
                self._engine = self._build()
            return self._engine

    def _build(self) -> Any:  # noqa: ANN401
        try:
            analyzer = importlib.import_module("presidio_analyzer")
            nlp = importlib.import_module("presidio_analyzer.nlp_engine")
            spacy_util = importlib.import_module("spacy.util")
            tld = importlib.import_module("tldextract.tldextract")
        except ImportError as exc:
            raise PiiUnavailable(  # noqa: TRY003
                "presidio is not installed: install supportagent[pii]"
            ) from exc
        if not spacy_util.is_package(self._model):
            raise PiiUnavailable(  # noqa: TRY003
                f"spaCy model {self._model!r} is not installed: "
                f"python -m spacy download {self._model}"
            )
        # The module-level extractor fetches the suffix list; the snapshot is offline.
        vars(tld)["TLD_EXTRACTOR"] = tld.TLDExtract(suffix_list_urls=(), cache_dir=None)
        try:
            provider = nlp.NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": self._language, "model_name": self._model}],
                }
            )
            engine = analyzer.AnalyzerEngine(
                nlp_engine=provider.create_engine(), supported_languages=[self._language]
            )
            for i, spec in enumerate(self._recognizers):
                engine.registry.add_recognizer(
                    analyzer.PatternRecognizer(
                        supported_entity=spec.entity,
                        supported_language=self._language,
                        patterns=[analyzer.Pattern(f"custom_{i}", spec.regex, spec.score)],
                    )
                )
        except Exception as exc:
            raise PiiUnavailable(f"presidio failed to start: {type(exc).__name__}") from exc  # noqa: TRY003
        return engine


__all__ = ["PiiRecognizerSpec", "PresidioAnalyzer"]
