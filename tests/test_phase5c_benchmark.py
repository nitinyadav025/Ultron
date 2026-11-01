"""Phase 5 Part C: the 25-command benchmark must be 25/25 with mocked runners."""

from __future__ import annotations

from pathlib import Path

from scripts.benchmark_phase5c import CASES, run_benchmark


def test_benchmark_is_25_of_25_and_writes_a_report(tmp_path: Path) -> None:
    report_path = tmp_path / "benchmark.json"

    report = run_benchmark(report_path=report_path, print_fn=lambda _line: None)

    assert len(CASES) == 25
    assert len({case.text for case in CASES}) == 25
    assert report["mode"] == "mocked"
    assert report["total"] == 25
    assert report["passed"] == 25
    assert report["failed"] == 0
    assert report["average_total_ms"] >= 0
    assert all(case["pass"] is True for case in report["cases"])
    assert all(case["actual_reply"] == case["expected_reply"] for case in report["cases"])
    assert report_path.is_file()


def test_benchmark_uses_no_tool_for_chat_cases(tmp_path: Path) -> None:
    report = run_benchmark(report_path=tmp_path / "benchmark.json", print_fn=lambda _line: None)
    chat_rows = [row for row in report["cases"] if row["expected_tool"] is None]

    assert chat_rows
    assert all(row["calls"] == [] for row in chat_rows)
