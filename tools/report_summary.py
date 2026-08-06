#!/usr/bin/env python3
"""Summarize Game Hub report logs (JSON Lines) produced by cookie.user.js.

Usage:
    python3 tools/report_summary.py --session cookieclicker
    python3 tools/report_summary.py --session cookieclicker --recent 5
"""

import argparse
import json
import os
import re
import sys

LOG_DIR = "logs"


def num(value):
    """JSONのnullやbool等、数値以外の値は0として扱う。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def load_reports(session):
    path = os.path.join(LOG_DIR, session + ".jsonl")
    # まだ一度も報告が来ていないセッションはログファイル自体が無い
    if not os.path.exists(path):
        return []
    reports = []
    with open(path) as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            # 書き込み途中のプロセス停止等で壊れた行が混ざっても、残りの集計は続ける
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"warning: {path}:{lineno}: skipping malformed JSON line", file=sys.stderr)
                continue
            if not isinstance(record, dict):
                print(f"warning: {path}:{lineno}: skipping non-object record", file=sys.stderr)
                continue
            # save退避レコード(type: "save")は集計対象外。type未設定の旧形式は報告として扱う
            if record.get("type", "report") == "report":
                reports.append(record)
    return reports


def average_cps(reports):
    if not reports:
        return 0.0
    total = 0
    for r in reports:
        total += num(r.get("cps"))
    return total / len(reports)


def peak_cookies(reports):
    best = 0
    for r in reports:
        cookies = num(r.get("cookies"))
        if cookies > best:
            best = cookies
    return best


def main():
    parser = argparse.ArgumentParser(description="Summarize Game Hub reports")
    parser.add_argument("--session", required=True, help="session name (log file stem)")
    parser.add_argument("--recent", type=int, default=0, help="only show the N most recent reports")
    args = parser.parse_args()

    # セッション名はログファイル名の一部になるため、パス区切りや".."を含む値を拒否する
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.session):
        parser.error("--session must contain only letters, digits, hyphens, or underscores")

    if args.recent < 0:
        parser.error("--recent must be a nonnegative integer")

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
    print(f"latest wrath: {num(latest.get('elderWrath'))}")
    print(f"latest upgrades: {num(latest.get('upgrades'))}")


if __name__ == "__main__":
    main()
