from agentcfd import diagnostics


def observation(text: str, command: str = "pimpleFoam") -> diagnostics.LogObservation:
    return diagnostics.LogObservation(
        command=command,
        source="workspace",
        text=text,
    )


def test_diagnosis_prioritizes_resource_exhaustion_over_generic_fatal_error():
    findings = diagnostics.diagnose(
        [observation("FOAM FATAL ERROR\nwrite failed: No space left on device\n")]
    )

    assert findings[0]["code"] == "DISK_SPACE_EXHAUSTED"
    assert findings[0]["evidence"]["line"] == 2
    assert findings[0]["automatic_repair"] is False
    assert any(item["code"] == "OPENFOAM_FATAL_ERROR" for item in findings)


def test_diagnosis_identifies_dictionary_and_numerical_failures_with_evidence():
    findings = diagnostics.diagnose(
        [
            observation(
                "--> FOAM FATAL IO ERROR:\nEntry 'solver' has invalid input\n",
                "checkMesh",
            ),
            observation(
                "smoothSolver: Solving for Ux, Initial residual = nan\n"
                "Floating point exception\n"
            ),
        ]
    )

    assert [item["code"] for item in findings[:2]] == [
        "OPENFOAM_DICTIONARY_INVALID",
        "NUMERICAL_DIVERGENCE",
    ]
    assert findings[1]["evidence"]["command"] == "pimpleFoam"


def test_diagnosis_does_not_treat_ordinary_convergence_log_as_failure():
    findings = diagnostics.diagnose(
        [
            observation(
                "Time = 0.1\n"
                "Courant Number mean: 0.1 max: 0.4\n"
                "Solving for Ux, Initial residual = 0.02, Final residual = 1e-6\n"
                "ExecutionTime = 2 s\nEnd\n"
            )
        ]
    )

    assert findings == ()
