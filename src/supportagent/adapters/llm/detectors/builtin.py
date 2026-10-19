"""Built-in secret detectors (01 §7.13): the nine closed classes.

Each detector implements the ``SecretDetector`` protocol (``ports/secrets.py``):
``detector_class`` + ``find(text) -> list[SecretSpan]``. Detectors are pure
functions of their input — no I/O, no randomness, no network — so they are
safe to run over untrusted ticket text (INV-06: redaction precedes every
LLM call, and the placeholder map never leaves the process).

Thresholds are locked by the golden set (``tests/unit/test_detectors.py``):
entropy >= 3.5 (generic key), bearer length >= 16, card 13-19 Luhn-valid,
JWT >= 3 segments, phone leading ``+``, conn-string ``user:pass@``,
AWS ``(AKIA|ASIA|A3T[A-Z0-9])[A-Z0-9]{16}``, email standard, PEM
``BEGIN ... PRIVATE KEY``.
"""

from __future__ import annotations

import re

from supportagent.adapters.llm.detectors.base import luhn_valid, shannon_entropy
from supportagent.domain.detectors import DetectorClass
from supportagent.ports.secrets import SecretDetector, SecretSpan


class EmailDetector:
    """Standard email addresses (01 §7.13)."""

    detector_class = DetectorClass.EMAIL
    _RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class AwsAccessKeyDetector:
    """AWS access key IDs (AKIA/ASIA/A3T prefix + 16 trailing chars)."""

    detector_class = DetectorClass.AWS_ACCESS_KEY
    _RE = re.compile(r"\b(?:AKIA|ASIA|A3T[A-Z0-9])[A-Z0-9]{16}\b")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class GenericApiKeyDetector:
    """``key=value`` pairs whose value is a high-entropy secret (>= 3.5 bits/char).

    The name must contain one of ``key``/``token``/``secret``/``api`` (case-
    insensitive) to avoid flagging arbitrary ``a=b`` pairs; the value must be
    >= 20 chars and >= 3.5 bits/char of entropy so low-entropy literals
    (``aaaa...``) are not flagged (golden negative).
    """

    detector_class = DetectorClass.GENERIC_API_KEY
    _RE = re.compile(
        r"(?i)\b[A-Za-z0-9_]*(?:key|token|secret|api)[A-Za-z0-9_]*\s*[=:]\s*([A-Za-z0-9_-]{20,})"
    )
    _MIN_ENTROPY = 3.5

    def find(self, text: str) -> list[SecretSpan]:
        spans: list[SecretSpan] = []
        for m in self._RE.finditer(text):
            value = m.group(1)
            if shannon_entropy(value) >= self._MIN_ENTROPY:
                spans.append(
                    SecretSpan(start=m.start(1), end=m.end(1), detector_class=self.detector_class)
                )
        return spans


class BearerTokenDetector:
    """Bearer tokens (``Authorization: Bearer <token>``), token >= 16 chars."""

    detector_class = DetectorClass.BEARER_TOKEN
    _RE = re.compile(r"Bearer\s+([A-Za-z0-9_-]{16,})", re.IGNORECASE)

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(1), end=m.end(1), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class JwtDetector:
    """JSON Web Tokens (>= 3 dot-separated base64url segments)."""

    detector_class = DetectorClass.JWT
    _RE = re.compile(r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class PrivateKeyBlockDetector:
    """PEM private-key headers (``-----BEGIN ... PRIVATE KEY-----``)."""

    detector_class = DetectorClass.PRIVATE_KEY_BLOCK
    _RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class ConnectionStringDetector:
    """Connection strings with embedded credentials (``scheme://user:pass@host``)."""

    detector_class = DetectorClass.CONNECTION_STRING
    _RE = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:@/]+:[^\s@/]+@\S+")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


class CardNumberDetector:
    """Card numbers (13 to 19 digits, Luhn-valid)."""

    detector_class = DetectorClass.CARD_NUMBER
    _RE = re.compile(r"\b\d{13,19}\b")

    def find(self, text: str) -> list[SecretSpan]:
        spans: list[SecretSpan] = []
        for m in self._RE.finditer(text):
            if luhn_valid(m.group(0)):
                spans.append(
                    SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
                )
        return spans


class PhoneE164Detector:
    """E.164 phone numbers (leading ``+`` and 10 to 15 digits)."""

    detector_class = DetectorClass.PHONE_E164
    _RE = re.compile(r"\+\d{10,15}\b")

    def find(self, text: str) -> list[SecretSpan]:
        return [
            SecretSpan(start=m.start(), end=m.end(), detector_class=self.detector_class)
            for m in self._RE.finditer(text)
        ]


_BUILTIN: list[type] = [
    EmailDetector,
    AwsAccessKeyDetector,
    GenericApiKeyDetector,
    BearerTokenDetector,
    JwtDetector,
    PrivateKeyBlockDetector,
    ConnectionStringDetector,
    CardNumberDetector,
    PhoneE164Detector,
]


def built_in_detectors() -> list[SecretDetector]:
    """One instance of each built-in detector (01 §7.13, INV-06)."""
    return [cls() for cls in _BUILTIN]
