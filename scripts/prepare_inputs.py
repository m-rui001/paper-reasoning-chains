#!/usr/bin/env python3
"""prepare_inputs.py -- turn extracted text into bounded, chunked inputs for one
summary agent per paper.

Strips the reference list (no information loss for a summary), collapses blank
runs, caps total source at MAX_TOTAL chars, and splits into <=CHUNK chars /
<=MAX_LINES line parts so a single file read never truncates.

Output layout:
  out/in/<sha>/meta.json
  out/in/<sha>/part-01.txt ...
  out/in/<sha>/INSTRUCTIONS.md   <- the exact brief the summary agent follows
  out/in/index.jsonl
"""
from __future__ import annotations
import argparse, json, os, re, sys

REF_HEAD = re.compile(r"^\s*(references|bibliography|references and notes|"
                      r"literature cited|references cited|参考文献|引用文献)\s*$", re.I)
CHUNK = 38000
MAX_LINES = 1500
MAX_TOTAL = 180000

PROMPT = """你是严谨的学术编辑。请只依据下面给出的论文提取文本，为论文生成一份精炼、准确、自然的中文 Markdown 笔记，并同步审核抽取问题。不要读取 PDF/图片，不要使用外部知识。

目标：读者只读这份 MD，就能复原论文的研究骨架、论证顺序、方法、关键证据与结论；它是有信息密度的结构化总结，不是逐句翻译。标题用准确中文，原文题目可作为副标题。中文应简洁自然，避免翻译腔。全文以论证脉络为主线，避免拆成互不相连的模块。篇幅随复杂度调整：通常 2000–6000 个中文字符，短论文可更短，公式密集可适度增加；绝不能只输出标题或摘要。

写作要求：
1. 第一行用 `#` 写正文中明确出现的论文中文标题；无中文标题时忠实翻译正文首页的英文标题。第二行写短元数据（*原题 · 年份 · DOI*）。若正文标题与登记标题冲突，以正文为准，元数据行写明“登记题名/DOI 与正文身份冲突，暂不归属”。随后用一段交代主张，此后严格跟随原文章节与论证次序；不要切成固定栏目（背景/方法/结果/局限）。全文最多 4 个二级标题，不使用三级标题；把同一论证阶段合并成连续段落，用段首主题句过渡。避免项目符号、编号清单、一段一句式碎片（仅当原文逐项比较多个对象时才用短列表）。不要单设“核心结论/关键数值/公式/局限”清单，除非原文结构如此。每项论据只在最相关处讲一次。
2. 公式属于高信息量内容：尽量原样保留提取文本中的公式、符号、变量定义与关系；不可凭记忆补写、“修复”或把模糊术语/化学名称替换成常见缩写或推测版本。缩写只有原文明确展开时才展开。若原文抽取疑似损坏，照录可见文本并在审核中标出，不要静默改正。
3. 保留关键表格/图的结论及其关键数字（仅限正文实际描述的内容），可保留 Figure/Table 编号；不要臆测未提取出的图表信息。
4. 不列参考文献表；低信息量引用编号可省略。若来源身份对论证不可缺，再保留简短作者/年份。
5. 不引入外部知识，不把推断写成原文事实。extraction_issues 只列会影响理解、确有强文本内证据的抽取错误；页间断句、换行、参考文献缺失、图像区域提示、格式偏好均不算问题。只允许这些 type：metadata_identity_mismatch（首页题名与登记题名明显冲突）、ocr_text_error、formula_symbol_loss、table_structure_damage、reading_order_damage、text_truncated_or_missing、other_extraction_error。证据不足就不报。每条疑点的 quote 必须逐字复制输入文本，page 必须对应明确的 `[page N]` 页标。不要报告“数值已确认/无问题”。
6. 输出前逐项核对摘要、正文数据和结论，没有文本证据的内容删除或标明不确定。

只输出一个合法 JSON 对象（不要代码围栏、不要数组外文字），字段：
- markdown: 完整 Markdown 正文（连续论述，按原文结构取舍）。
- record_match: match / mismatch / uncertain（正文与登记题名、DOI 是否同一篇）。
- ocr_title: 正文首页明确出现的题名；未出现则空字符串。
- extraction_issues: 数组，每项 {page, quote, type, severity(high/medium/low), confidence(0..1), reason}。
- self_check: 简短说明已核对的关键数值/公式/结论及不确定处。"""


def load_body(path):
    t = open(path, encoding="utf-8").read()
    lines = t.splitlines()
    hi = -1
    for i, l in enumerate(lines):
        if l.startswith("% "):
            hi = i
        else:
            break
    return lines[hi + 1:]


def strip_refs(lines):
    n = len(lines)
    cut = None
    for i in range(n - 1, int(n * 0.5), -1):
        if REF_HEAD.match(lines[i] or ""):
            cut = i
            break
    if cut and cut > 0.3 * n:
        return lines[:cut], True
    return lines, False


def collapse(lines):
    out, blank = [], False
    for l in lines:
        s = l.rstrip()
        if s.strip() == "":
            if blank:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(re.sub(r"[ \t]{2,}", " ", s))
    return out


def chunk(lines):
    parts, cur, chars = [], [], 0
    for l in lines:
        if cur and (chars + len(l) + 1 > CHUNK or len(cur) >= MAX_LINES):
            parts.append(cur)
            cur, chars = [], 0
        cur.append(l)
        chars += len(l) + 1
    if cur:
        parts.append(cur)
    return parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="out/text_manifest.jsonl")
    ap.add_argument("--out-root", default="out/in")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    recs = [json.loads(l) for l in open(a.manifest, encoding="utf-8") if l.strip()]
    recs = [r for r in recs if r.get("status") == "text" and r.get("txt")]
    missing = [r for r in recs if not os.path.isfile(r["txt"])]
    if missing:
        first = ", ".join(r.get("sha1", "?") for r in missing[:5])
        sys.stderr.write("[prepare] skipping %d manifest entries with missing text files (first: %s)\n"
                         % (len(missing), first))
        recs = [r for r in recs if os.path.isfile(r["txt"])]
    if a.limit:
        recs = recs[:a.limit]

    index = []
    for r in recs:
        sha = r["sha1"]
        lines = load_body(r["txt"])
        lines, refs_stripped = strip_refs(lines)
        lines = collapse(lines)
        body = "\n".join(lines)
        truncated = False
        if len(body) > MAX_TOTAL:
            body = body[:MAX_TOTAL]
            truncated = True
        parts = chunk(body.splitlines())
        d = os.path.join(a.out_root, sha)
        os.makedirs(d, exist_ok=True)
        part_names = []
        for i, p in enumerate(parts, 1):
            name = "part-%02d.txt" % i
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write("\n".join(p))
            part_names.append(name)
        meta = {
            "sha1": sha, "title": r.get("title"), "doi": r.get("doi"),
            "year": r.get("year"), "field": r.get("field"), "url": r.get("url"),
            "pages": r.get("pages"), "source_chars": len(body),
            "refs_stripped": refs_stripped, "truncated": truncated,
            "parts": part_names,
            "md_out": os.path.abspath(os.path.join("out/md", sha + ".md")),
            "review_out": os.path.abspath(os.path.join("out/md", sha + ".review.json")),
        }
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with open(os.path.join(d, "INSTRUCTIONS.md"), "w", encoding="utf-8") as f:
            f.write(PROMPT + "\n\n")
            f.write("## 本任务\n\n")
            f.write("- 登记题名：%s\n" % (r.get("title") or ""))
            f.write("- 年份：%s\n" % (r.get("year") or ""))
            f.write("- DOI：%s\n" % (r.get("doi") or ""))
            f.write("- 文本分片（按顺序全部读取）：%s\n" % ", ".join(part_names))
            f.write("- 输出 Markdown：%s\n" % meta["md_out"])
            f.write("- 输出审核 JSON：%s\n" % meta["review_out"])
            if truncated:
                f.write("- 注意：源文本过长，已在 %d 字符处截断；请在 self_check 中标注。\n" % MAX_TOTAL)
            f.write("\n用法：先读本文件，再按顺序读取每个分片；最后把 JSON 写入输出文件"
                    "（markdown 写入 .md，其余字段写入 .review.json）。\n")
        index.append(meta)

    with open(os.path.join(a.out_root, "index.jsonl"), "w", encoding="utf-8") as f:
        for m in index:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    tot = sum(m["source_chars"] for m in index)
    sys.stderr.write("[prepare] %d papers -> %s | median? total_chars=%d truncated=%d\n"
                     % (len(index), a.out_root, tot, sum(m["truncated"] for m in index)))


if __name__ == "__main__":
    main()
