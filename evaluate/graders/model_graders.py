"""
Model-Based Graders for DAA Evaluation
========================================
LLM-as-judge graders using the Anthropic API.
Each grader runs N times (default 3) and returns the median score per dimension.
"""

import json
import statistics
from pathlib import Path
from typing import Any


# ============================================================
# Configuration
# ============================================================

DEFAULT_GRADER_MODEL = "claude-sonnet-4-20250514"
DEFAULT_TEMPERATURE = 0
DEFAULT_RUNS = 3


# ============================================================
# API Call Wrapper
# ============================================================

def call_grader_llm(system_prompt: str, user_prompt: str,
                    model: str = DEFAULT_GRADER_MODEL,
                    temperature: float = DEFAULT_TEMPERATURE) -> dict | None:
    """
    Call Claude API for grading. Returns parsed JSON response.
    
    In production, replace this with your actual API call.
    This is the interface contract — adapt the implementation 
    to your agent SDK's HTTP client.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        response = client.messages.create(
            model=model,
            max_tokens=2000,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        text = response.content[0].text
        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text.strip())

    except Exception as e:
        print(f"  [grader_llm] Error: {e}")
        return None


# ============================================================
# 1. Rubric-Based Grader
# ============================================================

def grade_with_rubric(agent_output: str, rubric_file: str,
                      ground_truth: dict = None,
                      task_description: str = "",
                      config: dict = None) -> dict:
    """
    Grade agent output using a structured rubric.
    Runs multiple times and returns median scores.

    Args:
        agent_output: The agent's full response text
        rubric_file: Path to the .md rubric file
        ground_truth: Dict with known correct answers/insights
        task_description: Description of the task
        config: Override defaults (model, temperature, n_runs)

    Returns:
        {
            "grader_id": "model_rubric",
            "passed": bool,
            "scores": {"dimension": median_score, ...},
            "all_runs": [run1_scores, run2_scores, ...],
            "reasoning": str
        }
    """
    config = config or {}
    n_runs = config.get("grader_runs_per_trial", DEFAULT_RUNS)
    model = config.get("grader_model", DEFAULT_GRADER_MODEL)
    temperature = config.get("grader_temperature", DEFAULT_TEMPERATURE)
    pass_thresholds = config.get("pass_threshold", {})

    # Load rubric
    rubric_path = Path(rubric_file)
    if not rubric_path.exists():
        return _error_result("model_rubric", f"Rubric file not found: {rubric_file}")
    rubric_text = rubric_path.read_text()

    # Build grader prompt
    system_prompt = (
        "You are an expert evaluator for a Data Analyst Agent. "
        "Score the agent's output strictly according to the rubric provided. "
        "Return ONLY valid JSON matching the format specified in the rubric. "
        "If you cannot evaluate a dimension, score it 0 and explain in reasoning. "
        "Do not be generous — anchor to the scoring criteria."
    )

    user_prompt = f"""## Task Description
{task_description}

## Ground Truth (Known Correct Answers)
{json.dumps(ground_truth, indent=2) if ground_truth else "Not provided — grade based on rubric only."}

## Agent Output to Evaluate
{agent_output}

## Rubric
{rubric_text}
"""

    # Run N times
    all_runs = []
    for i in range(n_runs):
        result = call_grader_llm(system_prompt, user_prompt, model=model, temperature=temperature)
        if result:
            all_runs.append(result)

    if not all_runs:
        return _error_result("model_rubric", "All grader runs failed")

    # Compute median scores per dimension
    dimensions = [k for k in all_runs[0].keys() if k != "reasoning"]
    median_scores = {}
    for dim in dimensions:
        values = [run.get(dim, 0) for run in all_runs if isinstance(run.get(dim), (int, float))]
        if values:
            median_scores[dim] = statistics.median(values)
        else:
            median_scores[dim] = 0

    # Check pass thresholds
    passed = True
    for dim, threshold in pass_thresholds.items():
        if median_scores.get(dim, 0) < threshold:
            passed = False
            break

    # Collect reasoning from the run closest to median
    reasoning = all_runs[0].get("reasoning", "No reasoning provided")

    return {
        "grader_id": "model_rubric",
        "passed": passed,
        "scores": median_scores,
        "all_runs": all_runs,
        "reasoning": reasoning,
        "n_runs": len(all_runs)
    }


# ============================================================
# 2. Natural Language Assertion Grader
# ============================================================

def grade_assertions(agent_output: str, assertions: list[str],
                     config: dict = None) -> dict:
    """
    Check whether specific claims hold true about the agent's output.

    Args:
        agent_output: The agent's full response text
        assertions: List of natural-language assertions to check
        config: Override defaults

    Returns:
        {
            "grader_id": "model_assertion",
            "passed": bool,
            "score": float (0-1),
            "assertion_results": {"assertion": True/False, ...}
        }
    """
    config = config or {}
    n_runs = config.get("grader_runs_per_trial", DEFAULT_RUNS)
    model = config.get("grader_model", DEFAULT_GRADER_MODEL)

    system_prompt = (
        "You are an evaluator. For each assertion, determine if it is TRUE or FALSE "
        "based on the agent's output. Return ONLY valid JSON: "
        '{"results": [{"assertion": "...", "verdict": true/false, "evidence": "..."}]}'
    )

    user_prompt = f"""## Agent Output
{agent_output}

## Assertions to Check
{json.dumps(assertions, indent=2)}

Evaluate each assertion against the agent output above.
"""

    # Run N times and take majority vote per assertion
    all_verdicts = {a: [] for a in assertions}

    for _ in range(n_runs):
        result = call_grader_llm(system_prompt, user_prompt, model=model)
        if result and "results" in result:
            for item in result["results"]:
                assertion_text = item.get("assertion", "")
                verdict = item.get("verdict", False)
                # Match to closest assertion
                for a in assertions:
                    if a.lower()[:50] in assertion_text.lower() or assertion_text.lower()[:50] in a.lower():
                        all_verdicts[a].append(verdict)
                        break

    # Majority vote
    final_results = {}
    for assertion, verdicts in all_verdicts.items():
        if verdicts:
            final_results[assertion] = sum(verdicts) > len(verdicts) / 2
        else:
            final_results[assertion] = False

    passed_count = sum(final_results.values())
    total = len(assertions)

    return {
        "grader_id": "model_assertion",
        "passed": passed_count == total,
        "score": passed_count / total if total > 0 else 0.0,
        "assertion_results": final_results,
        "details": f"Passed {passed_count}/{total} assertions"
    }


# ============================================================
# 3. Hallucination Detector
# ============================================================

def grade_hallucination(agent_output: str, source_data_summary: str,
                        config: dict = None) -> dict:
    """
    Check if the agent cited data that doesn't exist in the source.

    Args:
        agent_output: The agent's full response text
        source_data_summary: Summary of what's actually in the data
        config: Override defaults

    Returns:
        {
            "grader_id": "model_hallucination",
            "passed": bool,
            "hallucinated_claims": list[str]
        }
    """
    config = config or {}
    model = config.get("grader_model", DEFAULT_GRADER_MODEL)

    system_prompt = (
        "You are a fact-checker. Compare the agent's output against the source data summary. "
        "Identify any specific numbers, column names, sheet names, or facts that the agent "
        "claims exist but are NOT present in the source data. "
        'Return JSON: {"hallucinated_claims": ["claim1", ...], "verified_claims_count": N}'
    )

    user_prompt = f"""## Source Data Summary
{source_data_summary}

## Agent Output
{agent_output}

List any claims in the agent output that cannot be verified from the source data.
"""

    result = call_grader_llm(system_prompt, user_prompt, model=model)
    if not result:
        return _error_result("model_hallucination", "Grader call failed")

    hallucinated = result.get("hallucinated_claims", [])
    verified = result.get("verified_claims_count", 0)

    return {
        "grader_id": "model_hallucination",
        "passed": len(hallucinated) == 0,
        "score": 1.0 if len(hallucinated) == 0 else max(0, 1.0 - len(hallucinated) * 0.2),
        "hallucinated_claims": hallucinated,
        "verified_claims_count": verified
    }


# ============================================================
# 4. Pairwise Comparison (for A/B testing agent versions)
# ============================================================

def grade_pairwise(output_a: str, output_b: str, task_description: str,
                   config: dict = None) -> dict:
    """
    Compare two agent outputs and determine which is better.

    Returns:
        {"winner": "A" | "B" | "tie", "reasoning": str, "confidence": float}
    """
    config = config or {}
    model = config.get("grader_model", DEFAULT_GRADER_MODEL)

    system_prompt = (
        "You are comparing two analyst outputs for the same task. "
        "Determine which is better based on: accuracy, insight quality, "
        "recommendation specificity, and overall usefulness. "
        'Return JSON: {"winner": "A" or "B" or "tie", "reasoning": "...", '
        '"scores": {"A": 1-5, "B": 1-5}}'
    )

    # Randomize order to reduce position bias
    import random
    if random.random() < 0.5:
        first, second = output_a, output_b
        label_first, label_second = "A", "B"
    else:
        first, second = output_b, output_a
        label_first, label_second = "B", "A"

    user_prompt = f"""## Task
{task_description}

## Output 1
{first}

## Output 2
{second}
"""

    result = call_grader_llm(system_prompt, user_prompt, model=model)
    if not result:
        return _error_result("model_pairwise", "Grader call failed")

    # Remap labels if order was swapped
    winner = result.get("winner", "tie")
    if winner == "1":
        winner = label_first
    elif winner == "2":
        winner = label_second

    return {
        "grader_id": "model_pairwise",
        "winner": winner,
        "reasoning": result.get("reasoning", ""),
        "scores": result.get("scores", {})
    }


# ============================================================
# Helpers
# ============================================================

def _error_result(grader_id: str, message: str) -> dict:
    return {
        "grader_id": grader_id,
        "passed": False,
        "score": 0.0,
        "details": f"ERROR: {message}"
    }
