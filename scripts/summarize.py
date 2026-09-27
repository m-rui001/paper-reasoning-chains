#!/usr/bin/env python3
"""summarize.py -- one Ark Flash call per paper, N concurrent workers.

Reads out/in/<sha>/ prepared inputs, writes out/md/<sha>.md + .review.json.
Resume-aware; built-in retry/backoff; no harness overhead.
"""
from __future__ import annotations
import argparse, json, os, re, sys, threading, time
import urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import prepare_inputs as P

BASE = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").rstrip("/")
MODEL = os.environ.get("ARK_MODEL", "deepseek-v4.1-flash")
KEY = os.environ.get("ARK_API_KEY", "").strip()
PROMPT_VERSION = "mdflow-v1"

ISSUE_TYPES = {"metadata_identity_mismatch", "ocr_text_error", "formula_symbol_loss",
               "table_structure_damage", "reading_order_damage", "text_truncated_or_missing",
               "other_extraction_error"}

_lock = threading.Lock()


def normalize_markdown_flow(md: str) -> str:
    out, skip_blank, h2 = [], False, 0
    for line in md.rstrip().splitlines():
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        if line.startswith("## "):
            h2 += 1
            if h2 > 4:
                line = "**" + line[3:].strip() + "**"
                skip_blank = True
        elif line.startswith("### "):
            line = "**" + line[4:].strip() + "**"
            skip_blank = True
        out.append(line)
    first = next((i for i, l in enumerate(out) if l.strip()), None)
    if first is not None and out[first].startswith("## "):
        out[first] = "# " + out[first][3:]
    return "\n".join(out).rstrip() + "\n"


def min_summary_chars(src: int) -> int:
    if src >= 50000:
        return 1000
    if src >= 10000:
        return 600
    if src >= 2000:
        return 300
    return 120


def call(prompt: str, max_tokens: int = 12000) -> dict:
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": max_tokens, "thinking": {"type": "disabled"}}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(8):
        req = urllib.request.Request(BASE + "/chat/completions", data=data,
            headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:400]
            last = "HTTP %d: %s" % (e.code, txt)
            if e.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt == 7:
                raise RuntimeError(last)
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            if attempt == 7:
                raise RuntimeError(last)
        time.sleep(min(40, 1.5 ** attempt + 0.5 * os.urandom(1)[0] / 255))
    raise RuntimeError(last or "unknown")


def build_prompt(meta: dict) -> str:
    d = os.path.join("out/in", meta["sha1"])
    parts = []
    for name in meta["parts"]:
        parts.append(open(os.path.join(d, name), encoding="utf-8").read())
    text = "\n".join(parts)
    head = P.PROMPT + "\n\n"
    head += ("## 本任务\n- 登记题名：%s\n- 年份：%s\n- DOI：%s\n"
             % (meta.get("title") or "", meta.get("year") or "", meta.get("doi") or ""))
    if meta.get("truncated"):
        head += "- 注意：源文本过长已截断，请在 self_check 标注。\n"
    return head + "\n以下是论文提取文本：\n<EXTRACTED_TEXT>\n" + text + "\n</EXTRACTED_TEXT>"


def parse_json(content: str) -> dict:
    a, b = content.find("{"), content.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in model output")
    try:
        return json.loads(content[a:b + 1])
    except json.JSONDecodeError:
        from json_repair import loads as repair
        obj = repair(content[a:b + 1])
        if not isinstance(obj, dict) or not str(obj.get("markdown") or "").strip():
            raise
        return obj


def run_one(meta: dict) -> dict:
    sha = meta["sha1"]
    md_path = os.path.join("out/md", sha + ".md")
    rev_path = os.path.join("out/md", sha + ".review.json")
    prompt = build_prompt(meta)
    payload = call(prompt)
    choice = payload["choices"][0]
    content = choice.get("message", {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        prompt2 = (prompt + "\n\n上次答复触发输出上限被截断。只输出一个合法 JSON 对象且只含 markdown 字段，"
                   "正文压缩到约 1800 个中文字符以内，优先删减背景与重复解释，保证 JSON 完整闭合。")
        payload = call(prompt2, max_tokens=4500)
        choice = payload["choices"][0]
        content = choice.get("message", {}).get("content") or ""
    try:
        result = parse_json(content)
    except Exception:
        payload = call(prompt + "\n\n上次输出不是合法 JSON。请重新只输出一个合法 JSON 对象（markdown 字段含完整正文）。")
        choice = payload["choices"][0]
        content = choice.get("message", {}).get("content") or ""
        result = parse_json(content)
    md = normalize_markdown_flow(str(result.get("markdown") or ""))
    if not md.strip():
        raise RuntimeError("empty markdown")
    src = meta["source_chars"]
    need = min_summary_chars(src)
    retried = False
    if len(md) < need:
        prompt3 = prompt + ("\n\n质量复核：上一版仅 %d 字符，而正文有 %d 字符，明显遗漏骨架。"
                            "请重新总结，至少 %d 个中文字符，按原文顺序覆盖研究问题、方法、关键证据与结论。"
                            % (len(md), src, need))
        payload = call(prompt3)
        result2 = parse_json(payload["choices"][0].get("message", {}).get("content") or "")
        md2 = normalize_markdown_flow(str(result2.get("markdown") or ""))
        if md2.strip():
            md, result = md2, result2
            retried = True
    issues = result.get("extraction_issues") or []
    if not isinstance(issues, list):
        issues = []
    issues = [x for x in issues if isinstance(x, dict) and x.get("type") in ISSUE_TYPES]
    served = payload.get("model")
    usage = payload.get("usage", {})
    os.makedirs("out/md", exist_ok=True)
    tmp = md_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, md_path)
    review = {"sha1": sha, "title": meta.get("title"), "doi": meta.get("doi"),
              "year": meta.get("year"), "field": meta.get("field"), "url": meta.get("url"),
              "model": served, "prompt_version": PROMPT_VERSION,
              "source_chars": src, "markdown_chars": len(md),
              "heading_h2_count": sum(1 for l in md.splitlines() if l.startswith("## ")),
              "heading_h3_count": sum(1 for l in md.splitlines() if l.startswith("### ")),
              "record_match": result.get("record_match", "uncertain"),
              "ocr_title": re.sub(r"\s+", " ", str(result.get("ocr_title") or "")).strip(),
              "extraction_issues": issues, "self_check": result.get("self_check", ""),
              "usage": usage, "length_retry": retried}
    tmp = rev_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)
    os.replace(tmp, rev_path)
    return {"sha1": sha, "status": "done", "markdown_chars": len(md),
            "issues": len(issues), "record_match": review["record_match"], "model": served}


def done(sha: str) -> bool:
    return (os.path.exists("out/md/%s.md" % sha) and os.path.exists("out/md/%s.review.json" % sha)
            and os.path.getsize("out/md/%s.md" % sha) > 200)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="out/in/index.jsonl")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if not KEY:
        raise SystemExit("ARK_API_KEY is not set")
    metas = [json.loads(l) for l in open(a.index, encoding="utf-8") if l.strip()]
    todo = [m for m in metas if a.force or not done(m["sha1"])]
    if a.limit:
        todo = todo[:a.limit]
    todo.sort(key=lambda m: m["source_chars"])
    print("[sum] %d total | %d todo | %d workers | model=%s"
          % (len(metas), len(todo), a.workers, MODEL), file=sys.stderr, flush=True)
    tally, t0, n = {}, time.time(), 0
    mfh = open("out/md_manifest.jsonl", "a", encoding="utf-8", buffering=1)
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(run_one, m): m for m in todo}
        for f in as_completed(futs):
            m = futs[f]
            try:
                res = f.result()
                st = "done"
            except Exception as e:
                res = {"sha1": m["sha1"], "status": "error",
                       "error": ("%s: %s" % (type(e).__name__, e))[:400]}
                st = "error"
            tally[st] = tally.get(st, 0) + 1
            mfh.write(json.dumps(res, ensure_ascii=False) + "\n")
            n += 1
            if n % 10 == 0 or n == len(todo):
                print("[sum] %d/%d %s %.2f/s" % (n, len(todo), tally, n / max(time.time() - t0, 1e-6)),
                      file=sys.stderr, flush=True)
    mfh.close()
    print("[sum] DONE %s in %.0fs" % (tally, time.time() - t0), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
