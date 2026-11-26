"""
Split questions.jsonl into a dev set and a held-out test set.

About 60/40, stratified by type, with a fixed seed so the split is
reproducible. Items listed in PIN_TO_DEV always go to dev. Tune retrieval
on dev only; the test file is for the final reported numbers.

Usage, from backend/:
    python eval/split_questions.py
"""
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
SOURCE = EVAL_DIR / "questions.jsonl"
DEV = EVAL_DIR / "questions_dev.jsonl"
TEST = EVAL_DIR / "questions_test.jsonl"
SEED = 20261124
DEV_SHARE = 0.6
PIN_TO_DEV = {"cross-09"}


def split(items, seed=SEED):
    by_type = defaultdict(list)
    for q in items:
        by_type[q["type"]].append(q)
    rng = random.Random(seed)
    dev_ids = set()
    for qtype in sorted(by_type):
        group = sorted(by_type[qtype], key=lambda q: q["id"])
        n_dev = round(DEV_SHARE * len(group))
        pinned = [q for q in group if q["id"] in PIN_TO_DEV]
        rest = [q for q in group if q["id"] not in PIN_TO_DEV]
        rng.shuffle(rest)
        chosen = pinned + rest[:max(n_dev - len(pinned), 0)]
        dev_ids.update(q["id"] for q in chosen)
    dev = [q for q in items if q["id"] in dev_ids]          # keep file order
    test = [q for q in items if q["id"] not in dev_ids]
    return dev, test


def write(path, items):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for q in items:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")


def main():
    items = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    dev, test = split(items)
    write(DEV, dev)
    write(TEST, test)
    types = sorted({q["type"] for q in items})
    print(f"dev {len(dev)}, test {len(test)} (seed {SEED})")
    for t in types:
        print(f"  {t}: dev {sum(q['type'] == t for q in dev)}, "
              f"test {sum(q['type'] == t for q in test)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
