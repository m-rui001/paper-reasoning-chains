#!/usr/bin/env python3
"""pi_rc.py -- 论文 -> 推理链（reasoning chain）生成器（pi 侧，Ark）。

与旧「总结类 md」不同，产物是**论证结构**：
  1. claim 抽取（主结论 / 支撑性结论）
  2. 证据关联（claim -> 实验/数据/图表/推导）
  3. 推理步骤重建（补全作者省略的中间推理）
  4. 限定条件提取（"在 X 下成立" / "不适用于 Y"）
  5. 反例与失败模式（局限、负面结果、边界）
  6. 术语对齐（构念 -> 标准术语/本体，处理同名不同义）

产物：
  out/rc/<sha>.md     可读推理链（仅此论文产物）

用法：
  python pi_rc.py --scan --limit 20 --workers 4         # 自动取还没有 rc 的论文
  python pi_rc.py --shas 058924d9 --workers 4
  python pi_rc.py --queue out/rc_queue.json --workers 4
"""
from __future__ import annotations
import argparse, glob, json, os, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import prepare_inputs as P
from pi_summarize import call_ark
import summarize as S

RC_PROMPT = """你是严谨而善于讲清论证的学术分析者。请只依据下面的论文提取文本，用流利、自然的中文写一篇可读的论证分析，让读者看懂作者主张什么、证据如何支撑结论、推理中关键的一步是什么，以及结论在哪些条件下成立。

以下是分析视角，不是固定栏目或必须逐项填满的模板：
- 辨认核心结论和必要的支撑结论；把重要主张与具体实验、数据、图表、公式推导或证明联系起来，给出原文图表/公式编号和 [page N] 页码。
- 把论证讲顺：说明从证据到结论的关键推理；作者省略的桥接可以补足，但要让读者看得出哪些是原文明说、哪些是基于文本的分析重建。
- 交代影响结论的假设、适用条件、边界、负面结果和作者承认的局限。论文没有讨论的限制不要冒充作者结论；确有价值的分析推断要明确标为推断。
- 只在有助于理解时解释术语或消除同名异义；标准术语映射是分析者提供的对照，不是原文事实。无歧义、无必要时可略过。

写作方式：
- 让文章结构服从论文论证本身。优先用连贯叙述串起关键推理；确有助于阅读时再加少量小标题，标题默认不编号，除非原文结构或论证顺序确实需要编号。避免流水账，以及反复使用“作者主张什么”“结论成立的条件与边界”“总体评价”等跨论文通用栏目名；不同论文应有不同的组织方式。六项分析视角不是要逐项填满的清单。
- 不要把全文固定写成“问题—主张—证据—推理—限制—总体评价”的标准骨架。某篇论文若没有对应内容就略去；若算法步骤或文中分节本来适合编号，才保留编号。标题尽量描述这篇论文特有的机制、对照或论证转折，避免以六项分析视角直接充当标题。
- 除非论文原文明确采用编号章节，正文小标题不要使用“一、二、三”式连续编号。避免通用槽位标题“作者要证明什么”“证据如何支撑结论”“结论成立的条件与边界”“总体判断”；把适用条件、限制和推理解释融入相关证据段落，只在确有必要时单独成节。复杂论文也不需要把所有分析视角逐项列成章节。
- 控制篇幅，集中讲清核心论证和关键边界；删去教科书式背景、重复数值和与主张无关的逐项罗列。综述论文可以较长，但应按其真正的论证主题组织，避免把目录机械改写成十几节摘要。
- 综述主题较多时，优先合并相互支撑的论点；只在分节能帮助读者理解推理关系时才拆开，不要求每个技术主题各占一节。
- 保持启发性，但启发必须来自论文真正的论证转折、建模简化、关键对照、量级关系或证据选择；不要添加泛泛的“可迁移意义”。
- 因果力度和限定语严格贴合原文。相关性不写成因果；保留 may、might、suggest、likely 等谨慎语气，不能把可能解释升级为确定结论。
- 使用通顺的学术中文，避免翻译腔。必要时首次出现保留英文术语。
- 公式、符号、变量定义、参数、数值和单位照录，不擅自修复、换算或替换缩写。关键判断尽量标注 [page N]。
- 若 OCR 或抽文使数值、单位、符号可疑，不要将其当作确定证据或推理前提；简要标出疑点。若结论依赖疑值，降低结论强度并说明不确定性。
- 不引入论文文本以外的事实，不把分析者重建写成作者原话。

只输出 Markdown 正文，不要输出 JSON、代码围栏或前言。开头写中文题名；有用时附原文题名、年份和 DOI。"""


def load_source(sha: str):
    """Return (text, source_kind) or (None, None)."""
    parts = sorted(glob.glob(os.path.join("out/in", sha, "part-*.txt")))
    if parts:
        return "\n".join(open(p, encoding="utf-8").read() for p in parts), "in"
    txt = os.path.join("out/txt", sha + ".txt")
    if os.path.exists(txt):
        lines = P.load_body(txt)
        lines, _ = P.strip_refs(lines)
        lines = P.collapse(lines)
        body = "\n".join(lines)
        if len(body) > P.MAX_TOTAL:
            body = body[:P.MAX_TOTAL]
        return body, "txt"
    return None, None


def run_one(sha: str, meta: dict) -> dict:
    text, kind = load_source(sha)
    if not text:
        raise RuntimeError("no source text (txt/in both missing)")
    head = RC_PROMPT + "\n\n## 论文登记信息（仅供核对）\n"
    if meta:
        head += "- 登记题名：%s\n- 年份：%s\n- DOI：%s\n" % (
            meta.get("title") or "", meta.get("year") or "", meta.get("doi") or "")
        head += "若正文题名与登记信息不符，以正文为准，并在文中简要指出疑点。\n"
    prompt = head + "\n以下是论文提取文本：\n<EXTRACTED_TEXT>\n" + text + "\n</EXTRACTED_TEXT>"
    payload = call_ark(prompt, max_tokens=20000)
    choice = payload["choices"][0]
    content = choice.get("message", {}).get("content") or ""
    if choice.get("finish_reason") == "length" or not content.strip() or content.lstrip().startswith("{"):
        retry_note = "上次回复未能作为完整 Markdown 正文使用。请重新依据原论文写作，压缩背景和重复内容，保留关键论证与限定；只输出 Markdown 正文，不要 JSON、代码围栏或解释。"
        payload = call_ark(head + "\n" + retry_note + "\n以下是论文提取文本：\n" + text,
                           max_tokens=20000)
        choice = payload["choices"][0]
        content = choice.get("message", {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        raise RuntimeError("Markdown output still truncated after retry")
    md = content.strip()
    if md.startswith("```markdown"):
        md = md[len("```markdown"):].strip()
        if md.endswith("```"):
            md = md[:-3].rstrip()
    if not md or md.startswith("{") or md.startswith("```json"):
        raise RuntimeError("model returned empty content or non-Markdown output")
    os.makedirs("out/rc", exist_ok=True)
    mp = os.path.join("out/rc", sha + ".md")
    with open(mp + ".tmp", "w", encoding="utf-8") as f:
        f.write(md)
    os.replace(mp + ".tmp", mp)
    return {"sha1": sha, "status": "done", "source": kind, "md_chars": len(md)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="out/in/index.jsonl")
    ap.add_argument("--shas", nargs="*", default=[])
    ap.add_argument("--queue", help="JSON list of sha1 (only entries lacking rc are processed)")
    ap.add_argument("--scan", action="store_true", help="todo = every txt/index entry lacking rc")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    metas = {}
    if os.path.exists(a.index):
        for l in open(a.index, encoding="utf-8"):
            if l.strip():
                m = json.loads(l)
                metas[m["sha1"]] = m
    todo = []
    if a.scan:
        have_in = {os.path.basename(d) for d in glob.glob("out/in/*") if os.path.isdir(d)}
        cand = {os.path.basename(p)[:-4] for p in glob.glob("out/txt/*.txt")} | have_in
        claimed = set()
        for claim_path in ("out/rc_claimed_B.json", "out/rc_queue_B.json"):
            if os.path.exists(claim_path):
                try:
                    raw = json.load(open(claim_path, encoding="utf-8"))
                    claimed |= set(raw if isinstance(raw, list) else (raw.get("shas") or []))
                except Exception:
                    pass
        todo = sorted(s for s in cand if s not in claimed and not os.path.exists(os.path.join("out/rc", s + ".md")))
    elif a.queue:
        raw = json.load(open(a.queue, encoding="utf-8"))
        shas = raw if isinstance(raw, list) else (raw.get("shas") or [])
        todo = [s for s in shas if not os.path.exists(os.path.join("out/rc", s + ".md"))]
    else:
        todo = [s for s in a.shas if not os.path.exists(os.path.join("out/rc", s + ".md"))]
    if a.limit:
        todo = todo[:a.limit]
    print("[rc] %d papers | workers=%d" % (len(todo), a.workers), file=sys.stderr, flush=True)
    log = open("out/rc_run.log", "a", encoding="utf-8", buffering=1)
    tally = {}
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(run_one, s, metas.get(s) or {}): s for s in todo}
        for f in as_completed(futs):
            s = futs[f]
            try:
                res = f.result()
            except Exception as e:
                res = {"sha1": s, "status": "error", "error": ("%s: %s" % (type(e).__name__, e))[:300]}
            log.write("{sha1}\t{status}\tclaims={claims}\tsteps={steps}\tchars={chars}\t{error}\n".format(
                sha1=res.get("sha1", s), status=res.get("status", "error"),
                claims=res.get("claims", ""), steps=res.get("steps", ""),
                chars=res.get("md_chars", ""), error=res.get("error", "").replace("\t", " ").replace("\n", " ")))
            tally[res["status"]] = tally.get(res["status"], 0) + 1
            print("[rc]", res.get("status"), s[:8], res.get("md_chars") or res.get("error", "")[:90],
                  file=sys.stderr, flush=True)
    log.close()
    print("[rc] DONE %s" % tally, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


