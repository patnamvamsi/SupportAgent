"""The pre-classifier: ordered ``when``/``label`` rules (ARCH §4).

The ``DomainModel`` subclasses are defined in
:mod:`supportagent.domain.model.preclass` (mypy convention: pydantic models
live in the ``domain.model`` package). This module preserves the public
``supportagent.domain.preclass`` import path.
"""

from supportagent.domain.model.preclass import PreclassRule, RuleSet

__all__ = ["PreclassRule", "RuleSet"]
