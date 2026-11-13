"""Chinese-aware FTS. Bigrams are a lexical fallback, never semantic vectors."""

import re
import unicodedata
from collections import Counter

STOP_WORDS = set(
    "的 了 是 我 在 把 这个 那个 上次 一下 为什么 找到 时候 之前 之后 怎么 什么 哪里 最后 请 帮 忘记 记录 接着".split()
)


def tokens(text):
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", text):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            out.extend(part[i : i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                out.append(part)
        else:
            out.append(part)
    return [t for t in dict.fromkeys(out) if t not in STOP_WORDS]


def fts_text(text):
    return " ".join(tokens(text))


def fts_query(text):
    terms = tokens(text)
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:64])


def overlap(query, text):
    q, t = set(tokens(query)), set(tokens(text))
    return len(q & t) / max(1, len(q))
