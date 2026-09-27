#!/usr/bin/env python3
"""pi_summarize.py -- pi-side summarizer (Ark). Writes ONLY md (no review.json).

New spec (2026-09-26): 简单论文 2000-4000 字；技术密集 6000-12000 字；宁全勿略。
Usage:
  python pi_summarize.py --shas 058924d9 [--target 9000] [--workers 4]
"""
from __future__ import annotations
import argparse, json, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import prepare_inputs as P
import summarize as S


def call_ark(prompt: str, max_tokens: int = 24000) -> dict:
    """Ark call with quota-aware backoff (AccountQuotaExceeded waits minutes)."""
    import json as _json, os as _os, time, random, urllib.error, urllib.request
    key = _os.environ.get("ARK_API_KEY")
    base = _os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").rstrip("/")
    model = _os.environ.get("ARK_MODEL", "deepseek-v4.1-flash")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0, "max_tokens": max_tokens, "thinking": {"type": "disabled"}}
    data = _json.dumps(body, ensure_ascii=False).encode("utf-8")
    last = None
    transient_attempt = 0
    quota_retries = 0
    while True:
        req = urllib.request.Request(base + "/chat/completions", data=data,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                payload = _json.loads(r.read().decode("utf-8"))
                if quota_retries:
                    print("[ark] quota recovered after %d retries" % quota_retries,
                          file=sys.stderr, flush=True)
                return payload
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:200]
            last = "HTTP %d: %s" % (e.code, txt)
            if "QuotaExceeded" in txt:
                quota_retries += 1
                if quota_retries > 60:   # allow one 5-hour quota window to reset
                    raise RuntimeError(last)
                if quota_retries == 1 or quota_retries % 6 == 0:
                    print("[ark] quota exceeded; retry %d/60 in 300s" % quota_retries,
                          file=sys.stderr, flush=True)
                time.sleep(300)
                continue
            if e.code not in {408, 409, 425, 429, 500, 502, 503, 504} or transient_attempt >= 11:
                raise RuntimeError(last)
            transient_attempt += 1
            time.sleep(min(90, 2.0 ** transient_attempt + random.random()))
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            last = "%s: %s" % (type(e).__name__, e)
            if transient_attempt >= 11:
                raise RuntimeError(last)
            transient_attempt += 1
            time.sleep(min(60, 2.0 ** transient_attempt + random.random()))

LEN_ADDENDUM = """

## 篇幅（本批新规）
- 本论文技术密集，目标 **6000-12000 个中文字符**；宁全勿略，绝不因求短而漏掉技术信息。
- 必须保留：全部关键公式与符号、变量定义、参数取值与单位、数据表结论、实验/数值条件、算法步骤、比较对象与数值结果。
- 但仍以连续论述为主，最多 4 个二级标题，不堆栏目；只输出一个合法 JSON 对象，字段不变。"""


def resolve(prefix: str, metas: list[dict]) -> dict:
    hits = [m for m in metas if m["sha1"].startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit("sha prefix %s matched %d" % (prefix, len(hits)))
    return hits[0]


def run_one(meta: dict, target: int) -> dict:
    d = os.path.join("out/in", meta["sha1"])
    parts = [open(os.path.join(d, n), encoding="utf-8").read() for n in meta["parts"]]
    text = "\n".join(parts)
    head = (P.PROMPT + "\n\n## 本任务\n- 登记题名：%s\n- 年份：%s\n- DOI：%s\n"
            % (meta.get("title") or "", meta.get("year") or "", meta.get("doi") or ""))
    head += LEN_ADDENDUM
    base = head + "\n以下是论文提取文本：\n<EXTRACTED_TEXT>\n" + text + "\n</EXTRACTED_TEXT>"

    def one(prompt: str, mt: int) -> dict:
        payload = call_ark(prompt, max_tokens=mt)
        choice = payload["choices"][0]
        content = choice.get("message", {}).get("content") or ""
        return payload, choice, content

    attempts = [(base, 24000, target), (base, 24000, 8000), (base, 16000, 4500)]
    last_err = None
    md, result, payload = "", {}, {}
    for prompt, mt, tgt in attempts:
        try:
            payload, choice, content = one(prompt, mt)
            if choice.get("finish_reason") == "length":
                retry = (base + "\n\n上次触发输出上限被截断。只输出一个合法 JSON 对象且只含 markdown 字段，"
                         "正文压到约 %d 个中文字符以内，优先删减背景与重复解释，确保 JSON 完整闭合，"
                         "但保留全部公式、符号、参数与关键数值。" % tgt)
                payload, choice, content = one(retry, mt)
            if choice.get("finish_reason") == "length":
                last_err = "still length-limited"
                continue
            result = S.parse_json(content)
            md = S.normalize_markdown_flow(str(result.get("markdown") or ""))
            if len(md) >= 800:
                break
            last_err = "markdown too short: %d" % len(md)
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, e)
    if len(md) < 800:
        raise RuntimeError(last_err or "failed")
    path = os.path.join("out/md", meta["sha1"] + ".md")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(tmp, path)
    return {"sha1": meta["sha1"], "status": "done", "chars": len(md),
            "h2": sum(1 for l in md.splitlines() if l.startswith("## ")),
            "model": payload.get("model"), "record_match": result.get("record_match")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="out/in/index.jsonl")
    ap.add_argument("--shas", nargs="*", default=[])
    ap.add_argument("--queue", help="JSON file with a list of sha1 (or {shas:[...]}); only missing-md are processed")
    ap.add_argument("--all-missing", action="store_true",
                    help="process every index entry whose md does not yet exist (never overwrites)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--target", type=int, default=9000)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    metas = [json.loads(l) for l in open(a.index, encoding="utf-8") if l.strip()]
    if a.all_missing:
        todo = [m for m in metas if not os.path.exists(os.path.join("out/md", m["sha1"] + ".md"))]
    elif a.queue:
        raw = json.load(open(a.queue, encoding="utf-8"))
        shas = raw if isinstance(raw, list) else (raw.get("shas") or raw.get("sha1") or [])
        want = set(shas)
        todo = [m for m in metas if m["sha1"] in want]
        got = {m["sha1"] for m in todo}
        missing_from_index = [s for s in shas if s not in got]
        if missing_from_index:
            print("[pi] WARN %d queue shas not in index" % len(missing_from_index), file=sys.stderr)
        todo = [m for m in todo if not os.path.exists(os.path.join("out/md", m["sha1"] + ".md"))]
    else:
        todo = [resolve(p, metas) for p in a.shas]
    if a.limit:
        todo = todo[:a.limit]
    print("[pi] %d papers | target=%d | workers=%d" % (len(todo), a.target, a.workers), file=sys.stderr)
    log = open("out/pi_md_manifest.jsonl", "a", encoding="utf-8", buffering=1)
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(run_one, m, a.target): m for m in todo}
        for f in as_completed(futs):
            m = futs[f]
            try:
                res = f.result()
            except Exception as e:
                res = {"sha1": m["sha1"], "status": "error", "error": ("%s: %s" % (type(e).__name__, e))[:300]}
            log.write(json.dumps(res, ensure_ascii=False) + "\n")
            print("[pi]", res.get("status"), m["sha1"][:8], res.get("chars") or res.get("error", "")[:80], file=sys.stderr)
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
