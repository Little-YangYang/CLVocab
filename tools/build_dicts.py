#!/usr/bin/env python3
"""从 ECDICT 生成/重建 dicts/ 下的考试词书（带词性的英汉释义）。

    python tools/build_dicts.py            # 自动下载 ecdict.csv（约 63MB）并生成 6 本词书
    python tools/build_dicts.py path.csv   # 用本地的 ecdict.csv

词书按 ECDICT 的考试标签筛选：cet4 / cet6 / ky(考研) / ielts / toefl / gre。
想加别的词书：往 TAG_BOOKS 里加一行（可用标签还有 zk 中考、gk 高考）。
ECDICT: https://github.com/skywind3000/ECDICT (MIT)
"""

import csv
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DICTS_DIR = os.path.join(ROOT, "dicts")
ECDICT_URL = "https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"

TAG_BOOKS = {
    "cet4": "CET4",
    "cet6": "CET6",
    "ky": "考研",
    "ielts": "雅思",
    "toefl": "托福",
    "gre": "GRE",
}


def download(dst):
    print(f"下载 {ECDICT_URL} -> {dst}（约 63MB，可能要几分钟）")
    req = urllib.request.Request(ECDICT_URL, headers={"User-Agent": "vocab-popup"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dst, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "ecdict.csv")
    if not os.path.exists(src):
        download(src)

    books = {name: [] for name in TAG_BOOKS.values()}
    csv.field_size_limit(10_000_000)
    with open(src, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            word = (row.get("word") or "").strip()
            trans = (row.get("translation") or "").strip()
            if not word or not trans:
                continue
            hit = set((row.get("tag") or "").split()) & TAG_BOOKS.keys()
            if not hit:
                continue
            phonetic = (row.get("phonetic") or "").strip()
            entry = {
                "word": word,
                "phonetic": f"/{phonetic}/" if phonetic else "",
                "meaning": trans.replace("\\n", "；").replace("\n", "；"),
            }
            for tag in hit:
                books[TAG_BOOKS[tag]].append(entry)

    os.makedirs(DICTS_DIR, exist_ok=True)
    for name, words in books.items():
        path = os.path.join(DICTS_DIR, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
        print(f"{name}: {len(words)} 词, {os.path.getsize(path) // 1024}KB")


if __name__ == "__main__":
    main()
