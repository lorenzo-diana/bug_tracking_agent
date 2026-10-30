def test_prompt_names_no_real_library_function() -> None:
    """Guard against re-priming the model with a libc name.

    An earlier prompt used `strcpy` repeatedly as a syntax example, and the
    model dutifully diagnosed a strcpy overflow in a program that had none --
    we primed it ourselves.

    This replaces a test that ALSO required an `@plt` paragraph. That guidance
    was imported from a binary-only sibling agent and did not belong here: BTA1
    compiles the target itself with debug symbols, and the transcript of a real
    run shows it anchoring on a source line in the project's own function
    (`break store_label:27`), never on a library-call PLT entry. The paragraph
    addressed a failure mode this agent does not have, while nudging it toward
    symbol-poor technique it has no reason to use.
    """
    from bta1.agent.prompts import SYSTEM_PROMPT

    for primer in ("strcpy", "strcat", "memcpy", "sprintf", "gets", "@plt"):
        assert primer not in SYSTEM_PROMPT, f"{primer} would prime the model"


def test_initial_task_carries_the_pre_run_evidence() -> None:
    """The pre-run output must REACH the agent, not just be logged.

    It was collected, exit-code checked, logged and dropped: loop.run had no
    evidence parameter at all, so the agent opened every run by calling build
    again to learn what we already knew.
    """
    from bta1.agent.prompts import initial_task

    bare = initial_task("/some/root")
    assert "start from this output" not in bare

    carried = initial_task("/some/root", 'exit_code": 0\nAN_IDENTIFIABLE_LINE')
    assert "AN_IDENTIFIABLE_LINE" in carried
    assert "Do NOT call build again" in carried

    # The path must never be named -- it made the model open with a doomed
    # list_files("examples/overflow"), one wasted turn every run.
    assert "/some/root" not in carried
