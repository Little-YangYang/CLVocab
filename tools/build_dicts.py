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

# ECDICT 把一些普通词的考试标签挂在首字母大写的词头上（如 Conservative 的释义
# 其实是"保守的"、CORE 是"核心"、FAX 是"传真"），这些词的通用义项占绝对主导、
# 词典标准词头是小写，生成时归一为小写。真正的专有名词（月份/国家/节日/Odyssey
# 奥德赛等）和缩写词（TV/AIDS/CD/B.C.）保持大写不动。
NORMALIZE_LOWER = frozenset({
    "Conservative", "CORE", "FAX", "Maxim", "Mister", "Perks", "Pole",
    "Polish", "SAC", "Saint", "Satanic", "Spartan", "Stoic", "Stygian",
    "Thespian", "Titanic", "Trident", "Utopia", "Utopian",
})


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
    seen = {name: set() for name in TAG_BOOKS.values()}
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
            if word in NORMALIZE_LOWER:
                word = word.lower()
            phonetic = (row.get("phonetic") or "").strip()
            entry = {
                "word": word,
                "phonetic": f"/{phonetic}/" if phonetic else "",
                "meaning": trans.replace("\\n", "；").replace("\n", "；"),
            }
            for tag in hit:
                name = TAG_BOOKS[tag]
                if word.lower() not in seen[name]:
                    seen[name].add(word.lower())
                    books[name].append(entry)

    os.makedirs(DICTS_DIR, exist_ok=True)
    for name, words in books.items():
        path = os.path.join(DICTS_DIR, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)
        print(f"{name}: {len(words)} 词, {os.path.getsize(path) // 1024}KB")


if __name__ == "__main__":
    main()
