"""Read final pytest outcomes without exposing JUnit exception text."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

TEST_IDS = {
    "baseline": ("knowmate.tests.test_ppt_rename_probe", "test_ppt_native_rename_comparison"),
    "actual_writer": ("knowmate.tests.test_ppt_request_writer", "test_ppt_production_request_writer_matrix"),
    "acl": ("knowmate.tests.test_ppt_acl_native", "test_ppt_native_legacy_directory_lease"),
    "stdout": ("knowmate.tests.test_ppt_installation", "test_windows_helper_stdout_survives_observer_close_and_deletes_on_last_close"),
    "cleanup_contract": ("knowmate.tests.test_ppt_request_writer", "test_ppt_writer_cleanup_native_contract"),
}
NODE_IDS = {key: classname.replace(".", "/") + ".py::" + name
            for key, (classname, name) in TEST_IDS.items()}


@dataclass(frozen=True)
class FinalOutcome:
    """One known test's validated final pytest status and failure phases."""

    result: str
    body_failure: bool = False
    teardown_error: bool = False


def junit_outcomes(data: bytes) -> dict[str, FinalOutcome]:
    """Validate known testcase identities, accepting only pytest's teardown pair."""
    incomplete = {key: FinalOutcome("INCOMPLETE") for key in TEST_IDS}
    # DTD/entity declarations have no role in pytest JUnit input.
    try:
        text = data.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            return incomplete
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError, UnicodeError):
        return incomplete
    suites = [root] if root.tag == "testsuite" else list(root) if root.tag == "testsuites" else []
    if not suites or any(suite.tag != "testsuite" for suite in suites):
        return incomplete
    found: dict[str, list[tuple[int, ET.Element]]] = {key: [] for key in TEST_IDS}
    for index, suite in enumerate(suites):
        if any(child.tag not in {"testcase", "properties", "system-out", "system-err"} for child in suite):
            return incomplete
        for case in suite.findall("testcase"):
            identity = (case.get("classname"), case.get("name"))
            for key, expected in TEST_IDS.items():
                if identity == expected:
                    found[key].append((index, case))
    results = incomplete.copy()
    for key, cases in found.items():
        if not cases or len(cases) > 2 or len({suite for suite, _ in cases}) != 1:
            continue
        outcomes = []
        for _, case in cases:
            children = list(case)
            if any(child.tag not in {"failure", "error", "skipped", "properties", "system-out", "system-err"}
                   for child in children):
                break
            phase = [child for child in children if child.tag in {"failure", "error", "skipped"}]
            if len(phase) > 1:
                break
            outcomes.append(phase[0] if phase else None)
        if len(outcomes) != len(cases):
            continue
        if len(outcomes) == 1:
            outcome = outcomes[0]
            result = "PASS" if outcome is None else "UNEXECUTED" if outcome.tag == "skipped" else "FAIL"
            results[key] = FinalOutcome(result, outcome is not None and outcome.tag == "failure",
                                        outcome is not None and outcome.tag == "error" and
                                        outcome.get("message", "").startswith("failed on teardown with "))
        else:
            failure = [child for child in outcomes if child is not None and child.tag == "failure"]
            error = [child for child in outcomes if child is not None and child.tag == "error"]
            if len(failure) == len(error) == 1 and error[0].get("message", "").startswith("failed on teardown with "):
                results[key] = FinalOutcome("FAIL", True, True)
    return results
