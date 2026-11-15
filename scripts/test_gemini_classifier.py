"""Exercise the real hold classifier with and without Gemini credentials."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

logging.disable(logging.CRITICAL)

PHRASES = [
    "You are number four in the queue. Your call is important to us.",
    "Press one for appointments, press two for prescriptions.",
    "Hello, Longsight Medical Practice, how can I help?",
    "Good morning, reception speaking.",
    "Hi, who am I speaking to?",
    "Sorry, we're closed at the moment. Our opening hours are Monday to Friday, eight until six thirty.",
    "Please hold, all our receptionists are busy.",
    "When you finish recording please hang up or press one for more options.",
    "The person you are calling is not available. Please leave a message after the tone.",
    "mm yeah hold on one sec... hello? yes?",
    "For appointments please visit our website or the NHS App.",
]


@dataclass
class Result:
    phrase: str
    label: str
    source: str
    milliseconds: float


def _display_phrase(phrase: str) -> str:
    compact = re.sub(r"\s+", " ", phrase)
    return compact[:57] + "..." if len(compact) > 60 else compact


async def _classify() -> list[Result]:
    from app.classifier import HoldClassifier

    classifier = HoldClassifier("Test Surgery", "fake cough")
    results: list[Result] = []
    for index, phrase in enumerate(PHRASES):
        source = "RULES"
        original_ask_llm = classifier._ask_llm

        async def tracked_ask(text: str, original=original_ask_llm):
            nonlocal source
            result = await original(text)
            if result is not None:
                source = "GEMINI"
            return result

        classifier._ask_llm = tracked_ask
        started = time.perf_counter()
        verdict = await classifier.classify(phrase)
        elapsed = (time.perf_counter() - started) * 1000
        results.append(Result(phrase, verdict.heard.value, source, elapsed))
        if index != len(PHRASES) - 1:
            await asyncio.sleep(2)
    return results


def _print_table(mode: str, results: list[Result]) -> None:
    print(f"\n{mode}")
    print(" # | phrase | label | source | ms")
    print("---|---|---|---|---")
    for index, result in enumerate(results, 1):
        print(f"{index:>2} | {_display_phrase(result.phrase)} | {result.label} | "
              f"{result.source} | {result.milliseconds:.0f}")
    rules = sum(result.source == "RULES" for result in results)
    gemini = sum(result.source == "GEMINI" for result in results)
    print(f"summary: rules={rules}, gemini={gemini}")


def _run_child(blank_key: bool) -> str:
    env = os.environ.copy()
    if blank_key:
        env["GEMINI_API_KEY"] = ""
    else:
        env.pop("GEMINI_API_KEY", None)
    command = [sys.executable, __file__, "--blank-key"] if blank_key else [sys.executable, __file__]
    return subprocess.run(command, env=env, check=True, text=True, capture_output=True).stdout


def _labels(output: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"^\s*\d+\s+\|.*\|\s+(\w+)\s+\|\s+\w+\s+\|", output, re.MULTILINE)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blank-key", action="store_true")
    parser.add_argument("--both", action="store_true")
    args = parser.parse_args()

    if args.both:
        with_key = _run_child(False)
        without_key = _run_child(True)
        print("=== WITH GEMINI_API_KEY ===")
        print(with_key, end="")
        print("=== WITH GEMINI_API_KEY BLANKED ===")
        print(without_key, end="")
        with_labels, without_labels = _labels(with_key), _labels(without_key)
        differences = [
            f"{index}: {left} -> {right}"
            for index, (left, right) in enumerate(zip(with_labels, without_labels), 1)
            if left != right
        ]
        print("comparison:")
        print(f"labels_differ: {len(differences)}")
        for difference in differences:
            print(difference)
        return

    mode = "with key" if not args.blank_key else "with key blanked"
    _print_table(mode, asyncio.run(_classify()))


if __name__ == "__main__":
    main()
