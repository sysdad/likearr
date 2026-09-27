"""The examples in the core's docstrings are tests too (#166).

pytest collects only `tests/`, so a doctest that stopped being true would never fail CI. Running
them here keeps `testpaths` as it is and covers exactly the modules whose examples are the spec.
"""

from __future__ import annotations

import doctest
from types import ModuleType

import pytest

from likearr.core import match, normalize


@pytest.mark.parametrize("module", [normalize, match], ids=lambda m: m.__name__)
def test_docstring_examples_hold(module: ModuleType) -> None:
    result = doctest.testmod(module, optionflags=doctest.ELLIPSIS)
    assert result.attempted > 0, f"{module.__name__} has no examples left to run"
    assert result.failed == 0
