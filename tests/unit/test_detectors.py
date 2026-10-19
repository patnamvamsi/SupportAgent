"""WP-007: detector classes — golden positive/negative per class (01 §7.13).

Canonical test ID (10-wp WP-007), parametrized over the nine detector classes:

- ``test_detectors_golden_positive_and_negative``

Each class ships with a golden *positive* (a secret the detector must find) and
a golden *negative* (a same-shape string the detector must not flag). The
``CUSTOM`` sentinel is not a built-in detector and is excluded.
"""

from __future__ import annotations

import pytest

from supportagent.domain.detectors import DetectorClass
from supportagent.ports.secrets import SecretDetector

# (positive, negative) per detector class — the negative is same-shape but not a
# secret (01 §7.13). Values are low-entropy fakes, safe for gitleaks.
_GOLDEN: dict[DetectorClass, tuple[str, str]] = {
    DetectorClass.EMAIL: (
        "contact support+ticket@acme-corp.example for help",
        "support at acme dot corp (email redacted)",
    ),
    DetectorClass.AWS_ACCESS_KEY: (
        "aws key AKIAIOSFODNN7EXAMPLE in config",
        "aws key AKIA12345 (truncated, too short)",
    ),
    DetectorClass.GENERIC_API_KEY: (
        "api_key=abcdef1234567890abcdef",
        "api_key=aaaaaaaaaaaaaaaaaaaa",  # low entropy — not a key
    ),
    DetectorClass.BEARER_TOKEN: (
        "Authorization: Bearer ghsA2mN8pR5tY6wZ1c",
        "Authorization: Bearer abc",  # too short — not a token
    ),
    DetectorClass.JWT: (
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "eyJabc.def",  # only two base64url parts — not a JWT
    ),
    DetectorClass.PRIVATE_KEY_BLOCK: (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA",
        "-----BEGIN CERTIFICATE-----\nMIID",  # a certificate, not a key
    ),
    DetectorClass.CONNECTION_STRING: (
        "DSN postgres://sa:secret@localhost:5432/sa",
        "postgres://localhost:5432/sa",  # no user:pass@ — not a DSN secret
    ),
    DetectorClass.CARD_NUMBER: (
        "card 4242424242424242",
        "card 4242 (too short, and not Luhn-valid)",
    ),
    DetectorClass.PHONE_E164: (
        "call +14155552671 now",
        "call 4155552671 now",  # no leading + — not E.164
    ),
}

# The nine built-in detector classes (CUSTOM is a sentinel, not a detector).
_BUILTIN_CLASSES = [
    DetectorClass.EMAIL,
    DetectorClass.AWS_ACCESS_KEY,
    DetectorClass.GENERIC_API_KEY,
    DetectorClass.BEARER_TOKEN,
    DetectorClass.JWT,
    DetectorClass.PRIVATE_KEY_BLOCK,
    DetectorClass.CONNECTION_STRING,
    DetectorClass.CARD_NUMBER,
    DetectorClass.PHONE_E164,
]


def _detector_for(cls: DetectorClass) -> SecretDetector:
    from supportagent.adapters.llm.detectors import built_in_detectors  # noqa: PLC0415

    for detector in built_in_detectors():
        if detector.detector_class == cls:
            return detector
    raise LookupError(f"no built-in detector for {cls}")  # noqa: TRY003


@pytest.mark.parametrize("cls", _BUILTIN_CLASSES)
def test_detectors_golden_positive_and_negative(cls: DetectorClass) -> None:
    """01 §7.13: each detector flags its golden positive and not its negative."""
    detector = _detector_for(cls)
    positive, negative = _GOLDEN[cls]

    positive_spans = detector.find(positive)
    assert positive_spans, f"{cls.value}: positive not detected in {positive!r}"
    # The detected span lies within the input.
    for span in positive_spans:
        assert 0 <= span.start < span.end <= len(positive)

    assert detector.find(negative) == [], f"{cls.value}: negative was detected: {negative!r}"
