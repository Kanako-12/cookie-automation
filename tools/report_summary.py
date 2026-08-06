#!/usr/bin/env python3
"""Summarize Game Hub report logs (JSON Lines) produced by cookie.user.js.

Usage:
    python3 tools/report_summary.py --session cookieclicker
    python3 tools/report_summary.py --session cookieclicker --recent 5
"""

import argparse
import json
import math
import os
import re
import sys
from collections import deque

LOG_DIR = "logs"


def num(value):
    """JSONのnull・bool・非有限値(NaN/Infinity)等、有限の数値以外は0として扱う。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    # 巨大intはfloat変換なしで常に有限なので、floatのみ有限性を確認する
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return value


def load_reports(session, limit=0):
    path = os.path.join(LOG_DIR, session + ".jsonl")
    # まだ一度も報告が来ていないセッションはログファイル自体が無い
    if not os.path.exists(path):
        return []
    # limit指定時は直近limit件だけを保持し、長期セッションのログでもメモリを食わない
    reports = deque(maxlen=limit) if limit else []
    # テキストモードだと行イテレータ自体がUnicodeDecodeErrorを投げ得るため、
    # バイナリで読み、行単位で独立にデコードして壊れた行だけをスキップする
    with open(path, "rb") as f:
        for lineno, raw in enumerate(f, start=1):
            try:
                line = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                print(f"warning: {path}:{lineno}: skipping non-UTF-8 line", file=sys.stderr)
                continue
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
    return list(reports)


def average_cps(reports):
    if not reports:
        return 0.0
    # 単純合計だと巨大値(クリッカー系は指数的に伸びる)でinfに飽和し得るため逐次平均で計算する
    mean = 0.0
    for n, r in enumerate(reports, start=1):
        mean += (num(r.get("cps")) - mean) / n
    return mean


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

    reports = load_reports(args.session, args.recent)

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
