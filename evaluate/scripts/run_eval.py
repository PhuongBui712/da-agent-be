"""
DAA Evaluation Runner
======================
Runs the full evaluation suite against the Data Analyst Agent.

Usage:
    python run_eval.py                          # Run all suites
    python run_eval.py --suite suite_a_operations  # Run specific suite
    python run_eval.py --task ops_extract_001   # Run specific task
    python run_eval.py --n-trials 5             # Override trial count
    python run_eval.py --dry-run                # Validate config only
"""

import argparse
import json
import time
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from graders.code_graders import (
    grade_value_match, grade_contains_all, grade_transcript,
    grade_phase_gate, collect_efficiency_metrics, GraderResult
)
from graders.model_graders import grade_with_rubric, grade_assertions, grade_hallucination


# ============================================================
# Agent Interface (ADAPT THIS to your Claude Agent SDK setup)
# ============================================================

def run_agent(excel_file: str, user_prompt: str, config: dict) -> dict:
    """
    Run the DAA agent on a single task.

    THIS IS THE INTEGRATION POINT — replace with your actual agent call.

    Args:
        excel_file: Path to the Excel input file
        user_prompt: The user's request
        config: Agent configuration (max_turns, max_tokens, etc.)

    Returns:
        {
            "response_text": str,     # Agent's final text output
            "transcript": list[dict], # Full message history
            "final_answer": str,      # Extracted answer (if any)
            "files_produced": list,   # Any output files
            "metadata": {
                "total_tokens": int,
                "latency_ms": int,
                "n_turns": int
            }
        }
    """
    # ---- PLACEHOLDER IMPLEMENTATION ----
    # Replace this with your actual Claude Agent SDK call, e.g.:
    #
    #   from agent_sdk import DAAAgent
    #   agent = DAAAgent(model="claude-sonnet-4-20250514")
    #   result = agent.run(
    #       input_file=excel_file,
    #       prompt=user_prompt,
    #       max_turns=config.get("max_turns", 30),
    #       max_tokens=config.get("max_tokens", 80000)
    #   )
    #   return {
    #       "response_text": result.final_response,
    #       "transcript": result.messages,
    #       "final_answer": result.extracted_answer,
    #       "files_produced": result.output_files,
    #       "metadata": result.usage
    #   }

    raise NotImplementedError(
        "Replace run_agent() with your actual DAA agent invocation. "
        "See the docstring for the expected interface contract."
    )


# ============================================================
# Grader Dispatcher
# ============================================================

def run_graders(task: dict, agent_result: dict, eval_root: str) -> list[dict]:
    """Run all graders defined for a task and return results."""
    results = []

    for grader_config in task.get("graders", []):
        grader_type = grader_config["type"]
        try:
            if grader_type == "code_value_match":
                r = grade_value_match(agent_result, grader_config)
                results.append(r.to_dict())

            elif grader_type == "code_contains_all":
                r = grade_contains_all(agent_result, grader_config)
                results.append(r.to_dict())

            elif grader_type == "code_transcript":
                r = grade_transcript(agent_result.get("transcript", []), grader_config)
                results.append(r.to_dict())

            elif grader_type == "code_phase_gate":
                r = grade_phase_gate(
                    agent_result.get("transcript", []),
                    agent_result,
                    grader_config
                )
                results.append(r.to_dict())

            elif grader_type == "model_rubric":
                rubric_path = os.path.join(eval_root, grader_config["rubric_file"])
                r = grade_with_rubric(
                    agent_output=agent_result.get("response_text", ""),
                    rubric_file=rubric_path,
                    ground_truth=task.get("ground_truth"),
                    task_description=task.get("description", ""),
                    config={**grader_config, "pass_threshold": grader_config.get("pass_threshold", {})}
                )
                results.append(r)

            elif grader_type == "model_assertion":
                r = grade_assertions(
                    agent_output=agent_result.get("response_text", ""),
                    assertions=grader_config.get("assertions", [])
                )
                results.append(r)

            else:
                results.append({
                    "grader_id": grader_type,
                    "passed": False,
                    "score": 0.0,
                    "details": f"Unknown grader type: {grader_type}"
                })

        except Exception as e:
            results.append({
                "grader_id": grader_type,
                "passed": False,
                "score": 0.0,
                "details": f"Grader error: {str(e)}"
            })

    return results


# ============================================================
# Score Aggregation
# ============================================================

def compute_task_score(grader_results: list[dict], weights: dict = None) -> dict:
    """
    Aggregate grader results into a single task score.

    Uses weighted combination if weights provided, else equal weights.
    A task passes if: weighted_score >= 0.6 AND all critical graders pass.
    """
    if not grader_results:
        return {"score": 0.0, "passed": False, "details": "No grader results"}

    # Compute weighted score
    total_weight = 0
    weighted_sum = 0

    for r in grader_results:
        gid = r.get("grader_id", "unknown")

        # For model_rubric, use individual dimension scores
        if gid == "model_rubric" and "scores" in r:
            for dim, score in r["scores"].items():
                if dim == "reasoning":
                    continue
                w = (weights or {}).get(dim, 1.0)
                weighted_sum += (score / 5.0) * w  # Normalize 1-5 to 0-1
                total_weight += w
        else:
            score = r.get("score", 0.0)
            w = (weights or {}).get(gid, 1.0)
            weighted_sum += score * w
            total_weight += w

    final_score = weighted_sum / total_weight if total_weight > 0 else 0.0

    # Check critical graders (phase_gate, value_match must pass)
    critical_types = {"code_phase_gate", "code_value_match"}
    critical_pass = all(
        r.get("passed", False)
        for r in grader_results
        if r.get("grader_id") in critical_types
    )

    passed = final_score >= 0.6 and critical_pass

    return {
        "score": round(final_score, 4),
        "passed": passed,
        "critical_pass": critical_pass,
        "n_graders": len(grader_results)
    }


# ============================================================
# Suite-Level Aggregation
# ============================================================

def compute_suite_metrics(task_results: list[dict]) -> dict:
    """Compute pass@1, pass@3, and other suite-level metrics."""
    if not task_results:
        return {}

    # pass@1: proportion of tasks that passed on first trial
    first_trial_results = [t["trials"][0]["passed"] for t in task_results if t.get("trials")]
    pass_at_1 = sum(first_trial_results) / len(first_trial_results) if first_trial_results else 0

    # pass@3: proportion of tasks where at least 1 of 3 trials passed
    any_pass_results = [
        any(trial["passed"] for trial in t["trials"])
        for t in task_results if t.get("trials")
    ]
    pass_at_3 = sum(any_pass_results) / len(any_pass_results) if any_pass_results else 0

    # Average score
    all_scores = [
        trial["score"]
        for t in task_results
        for trial in t.get("trials", [])
    ]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0

    return {
        "pass_at_1": round(pass_at_1, 4),
        "pass_at_3": round(pass_at_3, 4),
        "avg_score": round(avg_score, 4),
        "n_tasks": len(task_results),
        "n_trials_total": sum(len(t.get("trials", [])) for t in task_results)
    }


# ============================================================
# Report Generation
# ============================================================

def generate_report(all_results: dict, output_dir: str):
    """Generate a markdown evaluation report."""
    report_lines = [
        "# DAA Evaluation Report",
        f"**Generated:** {datetime.now().isoformat()}",
        f"**Total Tasks:** {sum(len(s.get('task_results', [])) for s in all_results.get('suites', {}).values())}",
        ""
    ]

    for suite_name, suite_data in all_results.get("suites", {}).items():
        metrics = suite_data.get("metrics", {})
        report_lines.extend([
            f"## {suite_name}",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| pass@1 | {metrics.get('pass_at_1', 'N/A')} |",
            f"| pass@3 | {metrics.get('pass_at_3', 'N/A')} |",
            f"| Avg Score | {metrics.get('avg_score', 'N/A')} |",
            f"| Tasks | {metrics.get('n_tasks', 0)} |",
            ""
        ])

        # Per-task results
        report_lines.append("### Task Results")
        report_lines.append("| Task ID | Trial 1 | Trial 2 | Trial 3 | Avg Score |")
        report_lines.append("|---------|---------|---------|---------|-----------|")

        for task_result in suite_data.get("task_results", []):
            tid = task_result["task_id"]
            trials = task_result.get("trials", [])
            trial_marks = []
            scores = []
            for i in range(3):
                if i < len(trials):
                    mark = "PASS" if trials[i]["passed"] else "FAIL"
                    trial_marks.append(mark)
                    scores.append(trials[i]["score"])
                else:
                    trial_marks.append("-")
            avg = f"{sum(scores)/len(scores):.3f}" if scores else "N/A"
            report_lines.append(f"| {tid} | {trial_marks[0]} | {trial_marks[1]} | {trial_marks[2]} | {avg} |")

        report_lines.append("")

    # Write report
    report_path = os.path.join(output_dir, "eval_report.md")
    with open(report_path, "w") as f:
        f.write("\n".join(report_lines))

    print(f"\nReport saved to: {report_path}")


# ============================================================
# Main Runner
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="DAA Evaluation Runner")
    parser.add_argument("--suite", type=str, help="Run specific suite only")
    parser.add_argument("--task", type=str, help="Run specific task only")
    parser.add_argument("--n-trials", type=int, default=None, help="Override trial count")
    parser.add_argument("--dry-run", action="store_true", help="Validate config only")
    parser.add_argument("--evals-file", default="evals.json", help="Path to evals.json")
    parser.add_argument("--output-dir", default="results", help="Output directory")
    args = parser.parse_args()

    # Load eval config
    eval_root = str(Path(__file__).parent.parent)
    evals_path = os.path.join(eval_root, args.evals_file)

    with open(evals_path) as f:
        eval_config = json.load(f)

    default_cfg = eval_config.get("default_config", {})
    if args.n_trials:
        default_cfg["n_trials"] = args.n_trials

    # Validate
    total_tasks = 0
    for suite_name, suite in eval_config.get("suites", {}).items():
        tasks = suite.get("tasks", [])
        total_tasks += len(tasks)
        for task in tasks:
            excel = task.get("input", {}).get("excel_file", "")
            full_path = os.path.join(eval_root, excel)
            if excel and not os.path.exists(full_path):
                print(f"  WARNING: Missing file for {task['id']}: {full_path}")

    print(f"Loaded {total_tasks} tasks across {len(eval_config['suites'])} suites")

    if args.dry_run:
        print("Dry run complete. Config is valid.")
        return

    # Prepare output
    output_dir = os.path.join(eval_root, args.output_dir, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_dir, exist_ok=True)

    # Run evaluations
    all_results = {"suites": {}, "config": default_cfg, "timestamp": datetime.now().isoformat()}

    for suite_name, suite in eval_config.get("suites", {}).items():
        if args.suite and suite_name != args.suite:
            continue

        print(f"\n{'='*60}")
        print(f"Suite: {suite_name}")
        print(f"{'='*60}")

        task_results = []

        for task in suite.get("tasks", []):
            if args.task and task["id"] != args.task:
                continue

            print(f"\n  Task: {task['id']} — {task.get('description', '')[:60]}...")
            n_trials = default_cfg.get("n_trials", 3)
            trials = []

            for trial_num in range(n_trials):
                print(f"    Trial {trial_num + 1}/{n_trials}...", end=" ")

                try:
                    # Run agent
                    start = time.time()
                    excel_path = os.path.join(eval_root, task["input"]["excel_file"])
                    agent_result = run_agent(
                        excel_file=excel_path,
                        user_prompt=task["input"]["user_prompt"],
                        config=default_cfg
                    )
                    latency_ms = int((time.time() - start) * 1000)
                    agent_result["metadata"] = agent_result.get("metadata", {})
                    agent_result["metadata"]["latency_ms"] = latency_ms

                    # Run graders
                    grader_results = run_graders(task, agent_result, eval_root)

                    # Aggregate
                    task_score = compute_task_score(
                        grader_results,
                        task.get("scoring_weights")
                    )

                    # Efficiency metrics
                    efficiency = collect_efficiency_metrics(agent_result.get("transcript", []))

                    trial_result = {
                        "trial": trial_num + 1,
                        "passed": task_score["passed"],
                        "score": task_score["score"],
                        "grader_results": grader_results,
                        "efficiency": efficiency,
                        "latency_ms": latency_ms
                    }
                    trials.append(trial_result)
                    status = "PASS" if task_score["passed"] else "FAIL"
                    print(f"{status} (score={task_score['score']:.3f})")

                except NotImplementedError as e:
                    print(f"SKIP — {e}")
                    trials.append({"trial": trial_num + 1, "passed": False, "score": 0.0,
                                   "details": str(e)})
                    break

                except Exception as e:
                    print(f"ERROR — {e}")
                    trials.append({"trial": trial_num + 1, "passed": False, "score": 0.0,
                                   "details": str(e)})

            task_results.append({"task_id": task["id"], "trials": trials})

        # Suite metrics
        suite_metrics = compute_suite_metrics(task_results)
        all_results["suites"][suite_name] = {
            "metrics": suite_metrics,
            "task_results": task_results
        }
        print(f"\n  Suite metrics: {json.dumps(suite_metrics, indent=2)}")

    # Save raw results
    results_path = os.path.join(output_dir, "raw_results.json")
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"\nRaw results saved to: {results_path}")

    # Generate report
    generate_report(all_results, output_dir)


if __name__ == "__main__":
    main()
