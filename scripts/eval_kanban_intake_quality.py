#!/usr/bin/env python3
"""Evaluate conversational Kanban intake classifier/title quality."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gateway.kanban_intake import (
    IntakeDetectionRequest,
    KeywordHeuristicDetector,
    evaluate_title_quality,
)

THRESHOLDS = {
    "candidate_precision": 0.90,
    "candidate_recall": 0.70,
    "generic_title_rate": 0.05,
    "raw_copy_rate": 0.0,
    "sensitive_title_leak": 0,
    "unsafe_false_positive": 0,
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        data = json.loads(stripped)
        for key in ("id", "input", "expected"):
            if key not in data:
                raise ValueError(f"{path}:{lineno}: missing {key}")
        expected = data["expected"]
        if "candidate_class" not in expected or "card_worthy" not in expected or "title_policy" not in expected:
            raise ValueError(f"{path}:{lineno}: incomplete expected contract")
        cases.append(data)
    if not cases:
        raise ValueError(f"{path}: no cases")
    return cases


def _contains_all(title: str, needles: list[str]) -> bool:
    lowered = title.lower()
    return all(str(needle).lower() in lowered for needle in needles)


def _contains_any(title: str, needles: list[str]) -> bool:
    lowered = title.lower()
    return any(str(needle).lower() in lowered for needle in needles)


def evaluate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    detector = KeywordHeuristicDetector()
    predicted_positive = 0
    expected_positive = 0
    true_positive = 0
    false_positive = 0
    generic_failures = 0
    raw_copy_failures = 0
    sensitive_leak_failures = 0
    title_contract_failures: list[str] = []
    class_failures: list[str] = []
    titles_checked = 0

    details: list[dict[str, Any]] = []
    for case in cases:
        request = IntakeDetectionRequest(
            platform="discord",
            session_key="eval",
            source_ref=f"kp_eval_{case['id']}",
            user_summary=case["input"].get("user_summary", ""),
            assistant_summary=case["input"].get("assistant_summary", ""),
            default_board="lifelog-control",
            default_tenant="lifelog",
        )
        expected = case["expected"]
        decision = detector.detect(request)
        expected_card = bool(expected["card_worthy"])
        predicted_card = bool(decision.card_worthy)
        expected_positive += int(expected_card)
        predicted_positive += int(predicted_card)
        true_positive += int(predicted_card and expected_card)
        false_positive += int(predicted_card and not expected_card)
        if decision.candidate_class != expected["candidate_class"]:
            class_failures.append(case["id"])
        quality = evaluate_title_quality(decision.title, request) if predicted_card else None
        if predicted_card:
            titles_checked += 1
            if quality and "generic_title" in quality.reason_codes:
                generic_failures += 1
            if quality and "raw_user_copy" in quality.reason_codes:
                raw_copy_failures += 1
            if quality and "sensitive_leak" in quality.reason_codes:
                sensitive_leak_failures += 1
            policy = expected.get("title_policy") or {}
            allow = list(policy.get("allow_contains") or [])
            forbid = list(policy.get("forbid_contains") or [])
            if allow and not _contains_all(decision.title, allow):
                title_contract_failures.append(case["id"])
            if forbid and _contains_any(decision.title, forbid):
                title_contract_failures.append(case["id"])
            if policy.get("quality_pass") and quality and not quality.passed:
                title_contract_failures.append(case["id"])
        details.append({
            "id": case["id"],
            "expected_card_worthy": expected_card,
            "predicted_card_worthy": predicted_card,
            "expected_candidate_class": expected["candidate_class"],
            "candidate_class": decision.candidate_class,
            "title": decision.title,
            "title_quality_reasons": list(quality.reason_codes) if quality else [],
        })

    precision = true_positive / predicted_positive if predicted_positive else 1.0
    recall = true_positive / expected_positive if expected_positive else 1.0
    generic_title_rate = generic_failures / titles_checked if titles_checked else 0.0
    raw_copy_rate = raw_copy_failures / titles_checked if titles_checked else 0.0
    thresholds_passed = (
        precision >= THRESHOLDS["candidate_precision"]
        and recall >= THRESHOLDS["candidate_recall"]
        and false_positive == THRESHOLDS["unsafe_false_positive"]
        and generic_title_rate <= THRESHOLDS["generic_title_rate"]
        and raw_copy_rate <= THRESHOLDS["raw_copy_rate"]
        and sensitive_leak_failures == THRESHOLDS["sensitive_title_leak"]
        and not title_contract_failures
        and not class_failures
    )
    return {
        "cases": len(cases),
        "candidate_precision": precision,
        "candidate_recall": recall,
        "unsafe_false_positive": false_positive,
        "generic_title_rate": generic_title_rate,
        "raw_copy_rate": raw_copy_rate,
        "sensitive_title_leak": sensitive_leak_failures,
        "thresholds_passed": thresholds_passed,
        "title_contract_failures": sorted(set(title_contract_failures)),
        "candidate_class_failures": class_failures,
        "details": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="tests/fixtures/kanban_intake_golden_cases.jsonl")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = evaluate(load_cases(Path(args.corpus)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print("PASS" if result["thresholds_passed"] else "FAIL")
    return 0 if result["thresholds_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
