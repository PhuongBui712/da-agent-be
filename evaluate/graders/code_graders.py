"""
Code-Based Graders for DAA Evaluation
======================================
Deterministic graders that check exact values, schema, transcripts, and process gates.
"""

import re
import json
import math
from typing import Any


class GraderResult:
    """Standardized grader output."""
    def __init__(self, grader_id: str, passed: bool, score: float,
                 details: str = "", metadata: dict = None):
        self.grader_id = grader_id
        self.passed = passed
        self.score = score        # 0.0 - 1.0
        self.details = details
        self.metadata = metadata or {}

    def to_dict(self):
        return {
            "grader_id": self.grader_id,
            "passed": self.passed,
            "score": self.score,
            "details": self.details,
            "metadata": self.metadata
        }


# ============================================================
# 1. Value Match Grader
# ============================================================

def grade_value_match(output: dict, config: dict) -> GraderResult:
    """
    Check if agent's output contains a value matching the expected answer.

    Config:
        field: str - key in output dict to check
        expected: float - expected value
        tolerance: float - acceptable absolute difference
    """
    field = config.get("field", "answer_value")
    expected = config["expected"]
    tolerance = config.get("tolerance", 0.01)

    actual = _extract_numeric(output, field)
    if actual is None:
        return GraderResult(
            grader_id="code_value_match",
            passed=False, score=0.0,
            details=f"Could not extract numeric value for '{field}' from output"
        )

    # Handle percentage values: if expected is < 1 and actual > 1, assume agent reported as %
    if abs(expected) < 1 and abs(actual) > 1:
        actual = actual / 100.0

    diff = abs(actual - expected)
    passed = diff <= tolerance

    return GraderResult(
        grader_id="code_value_match",
        passed=passed,
        score=1.0 if passed else max(0, 1.0 - diff / (abs(expected) + 1e-9)),
        details=f"Expected {expected} ± {tolerance}, got {actual} (diff={diff:.6f})",
        metadata={"expected": expected, "actual": actual, "diff": diff}
    )


def _extract_numeric(output: dict, field: str) -> float | None:
    """Try to extract a numeric value from various output formats."""
    # Direct field lookup
    if field in output:
        val = output[field]
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            return _parse_number_from_text(val)

    # Try extracting from the full text response
    text = output.get("response_text", "") or output.get("final_answer", "")
    return _parse_number_from_text(text)


def _parse_number_from_text(text: str) -> float | None:
    """Extract the most prominent number from a text response."""
    if not text:
        return None
    # Find all numbers (including negatives and decimals)
    numbers = re.findall(r'-?[\d,]+\.?\d*', str(text))
    if not numbers:
        return None
    # Return the first substantive number (skip very small ones like "1." or "2.")
    for n in numbers:
        cleaned = n.replace(',', '')
        try:
            val = float(cleaned)
            if abs(val) > 0.0001 or val == 0:
                return val
        except ValueError:
            continue
    return None


# ============================================================
# 2. Contains All Grader
# ============================================================

def grade_contains_all(output: dict, config: dict) -> GraderResult:
    """
    Check that output text contains all expected strings.

    Config:
        expected_strings: list[str]
        case_sensitive: bool (default False)
    """
    text = output.get("response_text", "") or output.get("final_answer", "")
    expected = config["expected_strings"]
    case_sensitive = config.get("case_sensitive", False)

    if not case_sensitive:
        text = text.lower()

    found = []
    missing = []
    for s in expected:
        check = s if case_sensitive else s.lower()
        if check in text:
            found.append(s)
        else:
            missing.append(s)

    score = len(found) / len(expected) if expected else 1.0

    return GraderResult(
        grader_id="code_contains_all",
        passed=len(missing) == 0,
        score=score,
        details=f"Found {len(found)}/{len(expected)}. Missing: {missing}",
        metadata={"found": found, "missing": missing}
    )


# ============================================================
# 3. Transcript Grader
# ============================================================

def grade_transcript(transcript: list[dict], config: dict) -> GraderResult:
    """
    Check transcript for required/forbidden tool calls or markers.

    Config:
        assert_present: list[str] - strings that MUST appear in transcript
        assert_absent: list[str] - strings that must NOT appear
    """
    # Flatten transcript to text
    transcript_text = json.dumps(transcript, default=str)

    assert_present = config.get("assert_present", [])
    assert_absent = config.get("assert_absent", [])

    present_results = {}
    for marker in assert_present:
        present_results[marker] = marker in transcript_text

    absent_results = {}
    for marker in assert_absent:
        absent_results[marker] = marker not in transcript_text  # True = good (absent)

    all_present_ok = all(present_results.values()) if present_results else True
    all_absent_ok = all(absent_results.values()) if absent_results else True
    passed = all_present_ok and all_absent_ok

    details_parts = []
    if present_results:
        missing = [k for k, v in present_results.items() if not v]
        if missing:
            details_parts.append(f"Missing required markers: {missing}")
        else:
            details_parts.append(f"All required markers found: {list(present_results.keys())}")

    if absent_results:
        found_bad = [k for k, v in absent_results.items() if not v]
        if found_bad:
            details_parts.append(f"Forbidden markers found: {found_bad}")
        else:
            details_parts.append(f"No forbidden markers found")

    total_checks = len(assert_present) + len(assert_absent)
    passed_checks = sum(present_results.values()) + sum(absent_results.values())

    return GraderResult(
        grader_id="code_transcript",
        passed=passed,
        score=passed_checks / total_checks if total_checks > 0 else 1.0,
        details=" | ".join(details_parts),
        metadata={"present_results": present_results, "absent_results": absent_results}
    )


# ============================================================
# 4. Phase Gate Grader
# ============================================================

PHASE_MARKERS = {
    "business_understanding": [
        "business question", "kpi", "metric", "dimension", "hypothesis",
        "AskUserQuestion", "business understanding"
    ],
    "data_understanding": [
        "data profiling", "schema", "grain", "null", "duplicate",
        "data type", "column", "sheet", "data understanding"
    ],
    "cleaning": [
        "clean", "missing value", "impute", "standardize", "deduplic",
        "remove duplicate", "data preparation", "data cleaning"
    ],
    "analysis": [
        "hypothesis", "H1", "H2", "H3", "segment", "compare", "trend",
        "correlation", "root cause", "analysis"
    ],
    "synthesis": [
        "recommendation", "action", "suggest", "finding", "conclusion",
        "synthesis", "root cause"
    ],
    "delivery": [
        "executive summary", "summary", "overview", "deliverable",
        "key finding"
    ]
}


def grade_phase_gate(transcript: list[dict], output: dict, config: dict) -> GraderResult:
    """
    Verify the agent completed all required DA phases.

    Config:
        required_phases: list[str]
        required_calls: list[str] - tool calls that must appear
        max_hypotheses: int
    """
    full_text = json.dumps(transcript, default=str).lower()
    full_text += " " + json.dumps(output, default=str).lower()

    required_phases = config.get("required_phases", list(PHASE_MARKERS.keys()))
    required_calls = config.get("required_calls", [])
    max_hypotheses = config.get("max_hypotheses", 3)

    # Check phases
    phase_results = {}
    for phase in required_phases:
        markers = PHASE_MARKERS.get(phase, [])
        phase_results[phase] = any(m.lower() in full_text for m in markers)

    # Check required tool calls
    call_results = {}
    transcript_raw = json.dumps(transcript, default=str)
    for call in required_calls:
        call_results[call] = call in transcript_raw

    # Check hypothesis count
    h_pattern = re.findall(r'\bh[1-9]\b', full_text)
    hypothesis_count = len(set(h_pattern))
    hypothesis_ok = hypothesis_count <= max_hypotheses

    phases_passed = sum(phase_results.values())
    calls_passed = sum(call_results.values())
    total_checks = len(required_phases) + len(required_calls) + 1  # +1 for hypothesis
    passed_checks = phases_passed + calls_passed + (1 if hypothesis_ok else 0)

    passed = (phases_passed == len(required_phases) and
              calls_passed == len(required_calls) and
              hypothesis_ok)

    details = (
        f"Phases: {phases_passed}/{len(required_phases)} | "
        f"Calls: {calls_passed}/{len(required_calls)} | "
        f"Hypotheses: {hypothesis_count} (max {max_hypotheses})"
    )

    return GraderResult(
        grader_id="code_phase_gate",
        passed=passed,
        score=passed_checks / total_checks if total_checks > 0 else 0.0,
        details=details,
        metadata={
            "phase_results": phase_results,
            "call_results": call_results,
            "hypothesis_count": hypothesis_count
        }
    )


# ============================================================
# 5. Efficiency Metrics Collector
# ============================================================

def collect_efficiency_metrics(transcript: list[dict]) -> dict:
    """
    Extract efficiency metrics from the transcript.
    Returns dict with: n_turns, n_toolcalls, n_total_tokens, redundant_tool_calls
    """
    n_turns = 0
    n_toolcalls = 0
    n_total_tokens = 0
    tool_calls_log = []

    for entry in transcript:
        if entry.get("role") == "assistant":
            n_turns += 1
        if entry.get("type") == "tool_call" or "tool_use" in str(entry.get("type", "")):
            n_toolcalls += 1
            tool_name = entry.get("name", entry.get("tool", ""))
            tool_input = str(entry.get("input", entry.get("parameters", "")))
            tool_calls_log.append(f"{tool_name}:{tool_input[:100]}")
        # Token counting (if available in metadata)
        usage = entry.get("usage", {})
        n_total_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

    # Detect redundant calls (exact same tool+input)
    redundant = len(tool_calls_log) - len(set(tool_calls_log))

    return {
        "n_turns": n_turns,
        "n_toolcalls": n_toolcalls,
        "n_total_tokens": n_total_tokens,
        "redundant_tool_calls": redundant
    }


# ============================================================
# 6. Dataframe Comparison Grader (for operation tasks producing tables)
# ============================================================

def grade_dataframe_compare(output_df: 'pd.DataFrame', reference_df: 'pd.DataFrame',
                            config: dict) -> GraderResult:
    """
    Compare two dataframes for structural and value equality.

    Config:
        tolerance: float - numeric comparison tolerance
        check_columns: bool
        check_dtypes: bool
        check_row_count: bool
    """
    import pandas as pd

    tolerance = config.get("tolerance", 0.01)
    check_columns = config.get("check_columns", True)
    check_dtypes = config.get("check_dtypes", False)
    check_row_count = config.get("check_row_count", True)

    issues = []
    checks_passed = 0
    total_checks = 0

    # Column check
    if check_columns:
        total_checks += 1
        if list(output_df.columns) == list(reference_df.columns):
            checks_passed += 1
        else:
            extra = set(output_df.columns) - set(reference_df.columns)
            missing = set(reference_df.columns) - set(output_df.columns)
            issues.append(f"Column mismatch. Extra: {extra}, Missing: {missing}")

    # Row count check
    if check_row_count:
        total_checks += 1
        if len(output_df) == len(reference_df):
            checks_passed += 1
        else:
            issues.append(f"Row count: expected {len(reference_df)}, got {len(output_df)}")

    # Value comparison (for shared columns and rows)
    shared_cols = list(set(output_df.columns) & set(reference_df.columns))
    min_rows = min(len(output_df), len(reference_df))
    value_matches = 0
    value_total = 0

    for col in shared_cols:
        for i in range(min_rows):
            val_out = output_df[col].iloc[i]
            val_ref = reference_df[col].iloc[i]
            value_total += 1

            if pd.isna(val_out) and pd.isna(val_ref):
                value_matches += 1
            elif isinstance(val_ref, (int, float)) and isinstance(val_out, (int, float)):
                if abs(val_out - val_ref) <= tolerance:
                    value_matches += 1
            elif str(val_out) == str(val_ref):
                value_matches += 1

    if value_total > 0:
        total_checks += 1
        value_accuracy = value_matches / value_total
        if value_accuracy >= 0.95:
            checks_passed += 1
        else:
            issues.append(f"Value accuracy: {value_accuracy:.2%} ({value_matches}/{value_total})")

    score = checks_passed / total_checks if total_checks > 0 else 0.0

    return GraderResult(
        grader_id="code_dataframe_compare",
        passed=len(issues) == 0,
        score=score,
        details=f"Passed {checks_passed}/{total_checks}. Issues: {issues if issues else 'None'}",
        metadata={"value_accuracy": value_matches / value_total if value_total > 0 else 1.0}
    )


# ============================================================
# Dispatcher
# ============================================================

GRADER_REGISTRY = {
    "code_value_match": grade_value_match,
    "code_contains_all": grade_contains_all,
    "code_transcript": grade_transcript,
    "code_phase_gate": grade_phase_gate,
    "code_dataframe_compare": grade_dataframe_compare,
}


def run_code_grader(grader_type: str, **kwargs) -> GraderResult:
    """Dispatch to the appropriate code grader."""
    if grader_type not in GRADER_REGISTRY:
        return GraderResult(
            grader_id=grader_type,
            passed=False, score=0.0,
            details=f"Unknown grader type: {grader_type}"
        )
    fn = GRADER_REGISTRY[grader_type]
    return fn(**kwargs)
