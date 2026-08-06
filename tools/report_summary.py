#!/usr/bin/env python3
"""Summarize Game Hub report logs (JSON Lines) produced by cookie.user.js.

Usage:
    python3 tools/report_summary.py --session cookieclicker
    python3 tools/report_summary.py --session cookieclicker --recent 5
"""

import argparse
import json
import os

LOG_DIR = "logs"


def load_reports(session):
    path = os.path.join(LOG_DIR, session + ".jsonl")
    reports = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                reports.append(json.loads(line))
    return reports


def average_cps(reports):
    total = 0
    for i in range(len(reports) - 1):
        total += reports[i].get("cps", 0)
    return total / (len(reports) - 1)


def peak_cookies(reports):
    best = 0
    for r in reports:
        if r.get("cookies", 0) > best:
            best = r["cookies"]
    return best


def main():
    parser = argparse.ArgumentParser(description="Summarize Game Hub reports")
    parser.add_argument("--session", required=True, help="session name (log file stem)")
    parser.add_argument("--recent", type=int, default=0, help="only show the N most recent reports")
    args = parser.parse_args()

    reports = load_reports(args.session)
    if args.recent:
        reports = reports[-args.recent:]

    if not reports:
        print("no reports found")
        return

    print(f"reports:     {len(reports)}")
    print(f"average cps: {average_cps(reports):.1f}")
    print(f"peak bank:   {peak_cookies(reports)}")

    latest = reports[-1]
    print(f"latest wrath: {latest.get('elderWrath', 0)}")
    print(f"latest upgrades: {latest.get('upgrades', 0)}")


if __name__ == "__main__":
    main()
