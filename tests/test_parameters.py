import pytest

from agentcfd import parameters


def test_parameter_specs_are_explicit_serializable_metadata():
    flow = parameters.number(
        "Mass flow",
        unit="kg/s",
        minimum=0,
        exclusive_minimum=True,
        nullable=True,
        description="Requested constant-density inlet mass flow.",
    )

    assert flow.to_dict() == {
        "kind": "number",
        "label": "Mass flow",
        "description": "Requested constant-density inlet mass flow.",
        "unit": "kg/s",
        "minimum": 0.0,
        "maximum": None,
        "exclusive_minimum": True,
        "choices": [],
        "nullable": True,
    }


def test_parameter_descriptions_must_target_real_factory_inputs():
    with pytest.raises(ValueError, match="unknown factory inputs"):

        @parameters.describe(
            missing=parameters.number(
                "Missing", description="Invalid metadata target."
            )
        )
        def build(*, real=1.0):
            return real


def test_choice_metadata_rejects_empty_or_duplicate_values():
    with pytest.raises(ValueError, match="non-empty string choices"):
        parameters.choice("Mode", (), description="Solver mode.")
    with pytest.raises(ValueError, match="unique"):
        parameters.choice("Mode", ("a", "a"), description="Solver mode.")
