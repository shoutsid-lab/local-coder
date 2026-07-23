from __future__ import annotations

import warnings

DSPY_PREFIX_WARNING = (
    "The 'prefix' argument in InputField/OutputField is deprecated and has no "
    "effect in DSPy. It will be removed in a future version."
)


def test_known_dspy_prefix_warning_is_filtered() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(
            DSPY_PREFIX_WARNING,
            DeprecationWarning,
            filename="dspy/predict/avatar/signatures.py",
            lineno=12,
            module="dspy.predict.avatar.signatures",
        )

    assert caught == []


def test_unrelated_deprecation_warning_remains_visible() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.warn_explicit(
            "Unrelated repository warning.",
            DeprecationWarning,
            filename="runtime/example.py",
            lineno=1,
            module="runtime.example",
        )

    assert len(caught) == 1
    assert str(caught[0].message) == "Unrelated repository warning."
