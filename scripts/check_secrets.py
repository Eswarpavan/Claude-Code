#!/usr/bin/env python3
"""Scan the repository AND its git history for API keys. Prints only where something was
found, never the value. Exit code 1 if anything is found.

Checks:
  1. the actual values of the key variables in your environment (FINNHUB_API_KEY, ...),
     if they are set, in every tracked file and in every commit's diff;
  2. key-shaped text: common provider prefixes (sk-, re_, SG., ghp_, AKIA...) and
     `token=` / `apikey=` / `api_key=` assignments followed by a long random-looking value.

    python3 scripts/check_secrets.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

KEY_VARS = ("FINNHUB_API_KEY", "TIINGO_API_KEY", "MARKETAUX_API_KEY", "ALPHAVANTAGE_API_KEY", "FRED_API_KEY",
            "OPENFDA_API_KEY", "STOOQ_API_KEY", "RESEND_API_KEY", "SENDGRID_API_KEY", "SMTP_PASSWORD",
            "APP_PASSWORD", "APP_SECRET", "HF_TOKEN")
PATTERNS = [
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|re_[A-Za-z0-9]{20,}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}|"
               r"ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|hf_[A-Za-z0-9]{30,})\b"),
    re.compile(r"(?i)\b(?:token|apikey|api_key|secret|password)\s*[=:]\s*[\"']?(?=[A-Za-z0-9]*\d)"
               r"(?=[A-Za-z0-9]*[a-z])[A-Za-z0-9]{20,}\b"),
]
ALLOW = re.compile(r"(?i)example|placeholder|your[_-]|xxxx|changeme|<[^>]+>|\$\{|os\.environ")


def run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout


def main() -> int:
    values = {v: os.environ[v] for v in KEY_VARS if len(os.environ.get(v, "")) >= 8}
    problems: list[str] = []
    files = [f for f in run("git", "ls-files").splitlines() if f]
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for name, val in values.items():
            if val in text:
                problems.append(f"{f}: contains the value of ${name}")
        for i, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in PATTERNS) and not ALLOW.search(line):
                problems.append(f"{f}:{i}: key-shaped text")
    history = run("git", "log", "--all", "-p", "--no-color")
    for name, val in values.items():
        if val in history:
            problems.append(f"git history: contains the value of ${name} (rewrite history and rotate the key)")
    for line in history.splitlines():
        if line.startswith("+") and any(p.search(line) for p in PATTERNS) and not ALLOW.search(line):
            problems.append("git history: key-shaped text in an added line (search with: git log -p --all -S <prefix>)")
            break
    ignored = run("git", "check-ignore", ".env", "backend/.env").split()
    if ".env" not in ignored:
        problems.append(".gitignore does not cover .env")
    print(f"Checked {len(files)} tracked files, full git history, {len(values)} key values from the environment.")
    if problems:
        print("\n".join(sorted(set(problems))))
        return 1
    print("No keys found. .env is ignored by git.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
