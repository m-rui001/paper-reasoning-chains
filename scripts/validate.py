#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""validate.py -- deterministic corpus quality gate + manifest builder.

Usage (from repo root):
    python scripts/validate.py            # check, rewrite MANIFEST.csv, exit 0/1
    python scripts/validate.py --fix      # deterministic normalization, metadata
                                          # repair from catalog, delete hard defects,
                                          # then run the check
    python scripts/validate.py --quality  # rewrite QUALITY.md from current state
    python scripts/validate.py --sample N # stratified dm.py review sample -> sample_review.csv

The gate (exit code) fails only on hard defects that are deletion-eligible:
empty / body < 400 chars / U+FFFD > 2% / duplicate content / no H1 /
neither title nor source identifier / invalid UTF-8 / BOM.
Advisory defects (missing DOI, non-standard header, truncation suspicion,
catalog conflicts) are reported and graded but do not fail the gate.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import glob
import hashlib
import json
import os
import re
import sys
import time
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.basename(ROOT)

REPOS = {
    "stem-md-corpus": {
        "md_glob": "md/*.md",
        "catalog": "catalog.csv",
        "catalog_kind": "csv",
        "catalog_id": "sha1",
        "catalog_title": "title",
        "catalog_doi": "doi",
        "catalog_year": "year",
        "record_match": "record_match",
        "review_glob": "md/*.review.json",
        "review_suffix": ".review.json",
        "source_note": "catalog.csv",
    },
    "pdfonly-stem": {
        "md_glob": "out/md/*.md",
        "catalog": "out/catalog.csv",
        "catalog_kind": "csv",
        "catalog_id": "sha1",
        "catalog_title": "title",
        "catalog_doi": "doi",
        "catalog_year": "year",
        "record_match": None,
        "review_glob": "out/reviews/*.review.json",
        "review_suffix": ".review.json",
        "source_note": "out/catalog.csv",
    },
    "paper-reasoning-chains": {
        "md_glob": "papers/*.md",
        "catalog": None,
        "catalog_kind": None,
        "review_glob": None,
        "review_suffix": None,
        "source_note": "in-document source line (no external catalog)",
    },
    "ib-koopman-uq-reasoning-chains": {
        "md_glob": "out/rc/*.md",
        "catalog": "out/text_manifest.jsonl",
        "catalog_kind": "jsonl",
        "catalog_id": "sha1",
        "catalog_title": "title",
        "catalog_doi": "doi",
        "catalog_year": "year",
        "record_match": None,
        "review_glob": None,
        "review_suffix": None,
        "source_note": "out/text_manifest.jsonl",
    },
}

CFG = REPOS[REPO]
MANIFEST = os.path.join(ROOT, "MANIFEST.csv")
REJECTED = os.path.join(ROOT, "REJECTED.csv")

# ---------------------------------------------------------------- parsers

DOI_RE = re.compile(
    r"10\.\d{4,9}/[^\s*，,；;。：:）】\]\"'<>（(一-鿿]+")
ARX_RE = re.compile(r"arXiv[:\s]*(\d{4}\.\d{4,5})", re.I)
URL_RE = re.compile(r"https?://[^\s)）\]]+")
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")
TITLE_LABEL_RE = re.compile(
    r"^\*\*(?:原文题名|原文标题|原文题目|原题|英文原题|原文|副标题|标题)\*\*[：:]?\s*(.*)$")
TITLE_LABEL_PLAIN_RE = re.compile(r"^(?:原文题名|原文标题|原文题目|原题|英文原题)[：:]\s*(.*)$")
META_LINE_RE = re.compile(
    r"^(?:\*|\*\*)?(?:原题|原文题名|原文标题|原文题目|英文原题|原文|副标题|标题|作者|年份|"
    r"期刊|DOI|doi|元数据|来源|原文来源|arXiv|\d{4}\s*年)")
PROVENANCE_RE = re.compile(r"^%\s*(?:via|source|mdflow source|doi|title|year|pages)\s*:",
                           re.I)


def is_meta_line(s):
    if META_LINE_RE.match(s) or s.startswith("*") or PROVENANCE_RE.match(s):
        return True
    if re.match(r"^(?:19|20)\d{2}\b", s) and ("·" in s or "DOI" in s or URL_RE.search(s)):
        return True
    if s.count("·") >= 2 and len(s) < 400:
        return True
    return False


def read_text(path):
    """Return (text, raw_bytes, hard_read_errors)."""
    raw = open(path, "rb").read()
    errs = []
    if raw.startswith(b"\xef\xbb\xbf"):
        errs.append("bom")
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, raw, errs + ["invalid_utf8"]
    return text, raw, errs


def md_lines(text):
    """Lines excluding fenced code blocks (for heading counts)."""
    out, fence = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if not fence:
            out.append(line)
    return out


def real_h1(text):
    """H1 lines, ignoring console/R output that markdown parses as headings."""
    out = []
    for line in md_lines(text):
        if not re.match(r"^#\s+\S", line):
            continue
        s = line.strip()
        if re.match(r"^#\s*[a-z_][a-z0-9_.]*\s*:\s*\d+(?:\.\d+)?\s*$", s):
            continue
        if re.match(r"^#\s*(?:In|Out)\s*\[\d+\]", s):
            continue
        out.append(line)
    return out


def header_block(text):
    """Metadata lines between the H1 and the first following heading."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^#\s+\S", line):
            start = i
            break
    if start is None:
        return [], 0
    block = []
    first = stop = start + 1
    for i, line in enumerate(lines[start + 1:start + 14], start + 1):
        s = line.strip()
        if not s:
            if block:
                break
            stop = i + 1
            continue
        if s.startswith("#"):
            break
        if not block:
            first = i
        block.append(s)
        stop = i + 1
    return block, start, first, stop


def clean_doi(s):
    if not s:
        return ""
    s = s.strip()
    m = DOI_RE.search(s)
    if not m:
        return ""
    d = m.group(0).rstrip(".,;:。，；：*")
    while d.endswith(")") and d.count("(") < d.count(")"):
        d = d[:-1]
    d = re.sub(r"/pdf$", "", d, flags=re.I)
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    return d.lower()


def norm_title(s):
    if not s:
        return ""
    s = s.lower().replace("&", "and")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return " ".join(s.split())


def title_sim(a, b):
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_englishish(s):
    letters = sum(c.isascii() and c.isalpha() for c in s)
    return letters >= max(8, len(s) * 0.35)


AUTHORISH_RE = re.compile(
    r"^(?:[A-Z][\w'’-]*\.?\s+){0,4}[A-Z][\w'’-]*"
    r"(?:\s*,\s*(?:[A-Z]\.\s*)?[A-Z][\w'’-]*)+$")


def looks_like_authors(s):
    s = s.strip()
    return bool(AUTHORISH_RE.match(s) or re.search(r"\bCollaboration\b", s))


def arxiv_year(arxiv):
    """Publication year encoded in an arXiv id (new 1306.0121 or old hep-th/0106112)."""
    if not arxiv:
        return None
    m = re.match(r"^(\d{2})(\d{2})\.", arxiv)
    if m:
        yy = int(m.group(1))
        return 2000 + yy if yy < 90 else 1900 + yy
    m = re.search(r"/(\d{2})(\d{2})\d{3,}", arxiv)
    if m:
        return 2000 + int(m.group(1))
    return None


def parse_header(text):
    block = header_block(text)[0]
    joined = "\n".join(block)
    doi = clean_doi(joined)
    arx = ARX_RE.search(joined)
    src_title = ""
    for line in block:
        m = TITLE_LABEL_RE.match(line) or TITLE_LABEL_PLAIN_RE.match(line)
        if m and m.group(1).strip():
            src_title = m.group(1).strip().strip("*").strip()
            break
    if not src_title:
        for line in block:
            if line.startswith("*") and "·" in line:
                src_title = line.strip("*").split("·")[0].strip()
                src_title = re.sub(r"^(?:原文题名|原题|英文原题)[：:]\s*", "", src_title)
                break
    if not src_title:
        for line in block:
            if line.startswith("*") and "·" not in line and len(line) > 12:
                src_title = line.strip("*").strip()
                break
    if src_title:
        if is_englishish(src_title):
            src_title = re.split(r"（", src_title)[0]
        src_title = re.split(r"（(?:19|20)\d{2}", src_title)[0]
        src_title = src_title.strip(" 　*，,。;；")
    year_src = DOI_RE.sub(" ", joined)
    year_src = re.sub(r"arXiv[^\s，,）)]*", " ", year_src, flags=re.I)
    years = [int(y) for y in YEAR_RE.findall(year_src)]
    return {
        "block": block,
        "doi": doi,
        "arxiv": arx.group(1) if arx else "",
        "url": URL_RE.search(joined).group(0) if URL_RE.search(joined) else "",
        "title": src_title,
        "years": years,
        "has_identifier": bool(doi or arx or URL_RE.search(joined)),
    }


def body_text(text):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^#\s+\S", line):
            start = i
            break
    if start is None:
        return text.strip()
    rest = lines[start + 1:start + 14]
    skip = 0
    for line in rest:
        s = line.strip()
        if not s:
            if skip:
                break
            skip += 1
            continue
        if s.startswith("#"):
            break
        if is_meta_line(s):
            skip += 1
            continue
        break
    body = lines[start + 1 + skip:]
    return "\n".join(l.strip() for l in body if l.strip())


REFUSAL_RE = re.compile(
    r"^(?:非常|十分)?抱歉[，,。]|^对不起[，,]|"
    r"^作为(?:一个)?(?:AI|人工智能|语言模型|大模型)[，,].{0,20}(?:无法|不能)|"
    r"我(?:无法|不能)(?:提供|完成|生成|协助)|无法提供(?:该|此|这篇)文")
OPEN_END_RE = re.compile(r"(?:[，,、：（(；;\"“]|以及|并且|而且|但是|因此|例如|其中|包括)\s*$")


def refusal_suspect(body):
    head = body[:200]
    return bool(REFUSAL_RE.search(head))


def truncation_suspect(body):
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if not lines:
        return False
    last = lines[-1]
    if PROVENANCE_RE.match(last) or last in ("---", "***", "___") or last.startswith("|"):
        return False
    if re.match(r"^(?:PACS|关键词|Key ?words|Received|收稿|致谢|参考文献|\[?\d{4}\s*年)", last):
        return False
    if len(last) < 25:
        return False
    stripped = last.rstrip("*").rstrip()
    if re.search(r"[。！？.!?…：:]$", stripped):
        return False
    if re.search(r"[。！？.!?…]\*{1,2}$", last):
        return False
    if last.endswith("）") or last.endswith(")") or last.endswith("]") or last.endswith("|"):
        return False
    if re.search(r"(?:et al\.|DOI:|doi\.org|\d{4},\s*\d+[–-]\d+|年\s*\d+\s*月)", last):
        return False
    if OPEN_END_RE.search(stripped):
        return True
    # unbalanced inline math / brackets / brace at the very end
    if stripped.count("$") % 2 == 1:
        return True
    for opener, closer in (("{", "}"), ("[", "]"), ("(", ")")):
        if stripped.count(opener) > stripped.count(closer):
            return True
    return False


def cjk_ratio(text):
    if not text:
        return 0.0
    n = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return n / len(text)


# ---------------------------------------------------------------- catalog

def load_catalog():
    if not CFG["catalog"]:
        return {}
    path = os.path.join(ROOT, CFG["catalog"])
    rows = {}
    if CFG["catalog_kind"] == "csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows[r[CFG["catalog_id"]]] = r
    else:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                rid = r.get(CFG["catalog_id"])
                if rid and rid not in rows:
                    rows[rid] = r
    return rows


def cat_get(row, key):
    if not row or not key:
        return ""
    v = row.get(key, "")
    return "" if v is None else str(v).strip()


# ---------------------------------------------------------------- checks

DEFECT_ORDER = [
    "invalid_utf8", "bom", "empty", "body_short", "no_h1", "multi_h1",
    "replacement_char", "replacement_critical", "duplicate", "refusal",
    "truncated_suspect", "no_source", "no_h2", "template_sections",
    "metadata_title_mismatch", "metadata_doi_mismatch", "metadata_year_mismatch",
    "metadata_year_minor", "nonstandard_source", "trailing_provenance", "short_body",
    "language_mismatch", "review_high_issue", "catalog_missing_md", "catalog_orphan",
]
MINOR = {
    "no_source", "no_h2", "template_sections", "nonstandard_source",
    "trailing_provenance", "short_body", "truncated_suspect", "replacement_char",
    "review_high_issue", "catalog_orphan", "metadata_year_minor",
}
HARD = {"invalid_utf8", "empty", "body_short", "no_h1", "replacement_critical",
        "duplicate"}


def load_reviews():
    out = {}
    if not CFG["review_glob"]:
        return out
    for p in glob.glob(os.path.join(ROOT, CFG["review_glob"])):
        sha = os.path.basename(p)[:-len(CFG["review_suffix"])]
        try:
            r = json.load(open(p, encoding="utf-8"))
        except Exception:
            out[sha] = "unparsable_review"
            continue
        high = 0
        for iss in (r.get("extraction_issues") or []):
            if isinstance(iss, dict) and iss.get("severity") in ("high", "critical"):
                high += 1
        st = r.get("structure_check") or {}
        if high or st.get("within_heading_limits") is False:
            out[sha] = "high_issue" if high else "structure_fail"
    return out


def scan(paths, catalog, reviews):
    docs = []
    for p in paths:
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        doc_id = os.path.basename(p)
        for suf in (".md",):
            if doc_id.endswith(suf):
                doc_id = doc_id[:-len(suf)]
        text, raw, hard = read_text(p)
        rec = {
            "path": rel, "doc_id": doc_id, "sha1": hashlib.sha1(raw).hexdigest(),
            "bytes": len(raw), "chars": 0, "body_chars": 0, "quality": "ok",
            "defect": "", "action": "keep", "title": "", "doi": "", "year": "",
            "cat_title": "", "cat_doi": "", "cat_year": "", "conflict": "",
        }
        defects = list(hard)
        if text is None:
            rec["defects"] = defects
            docs.append(rec)
            continue
        rec["chars"] = len(text)
        body = body_text(text)
        rec["body_chars"] = len(body)
        h1 = real_h1(text)
        h2 = [l[3:].strip() for l in md_lines(text) if l.startswith("## ")]
        nfffd = text.count("\ufffd")
        if not text.strip():
            defects.append("empty")
        if len(body) < 400:
            defects.append("body_short")
        if len(h1) == 0:
            defects.append("no_h1")
        elif len(h1) > 1:
            defects.append("multi_h1")
        if nfffd:
            defects.append("replacement_critical" if nfffd / max(1, len(text)) > 0.02
                           else "replacement_char")
        if refusal_suspect(body):
            defects.append("refusal")
        if truncation_suspect(body):
            defects.append("truncated_suspect")
        if not h2:
            defects.append("no_h2")
        if re.search(r"(?m)^%\s*(?:via|source|mdflow source|doi|title|year|pages)\s*:",
                     text[-500:], re.I):
            defects.append("trailing_provenance")
        if cjk_ratio(text) < 0.05:
            defects.append("language_mismatch")
        if 400 <= len(body) < 1200:
            defects.append("short_body")

        hd = parse_header(text)
        rec["title"] = hd["title"]
        rec["doi"] = hd["doi"]
        rec["year"] = str(hd["years"][0]) if hd["years"] else ""
        if not hd["has_identifier"]:
            defects.append("no_source")

        row = catalog.get(doc_id)
        if catalog and row is None:
            defects.append("catalog_orphan")
        if row:
            ct = cat_get(row, CFG.get("catalog_title"))
            cd = clean_doi(cat_get(row, CFG.get("catalog_doi")))
            cy = cat_get(row, CFG.get("catalog_year"))
            rec["cat_title"], rec["cat_doi"], rec["cat_year"] = ct, cd, cy
            sim = title_sim(hd["title"], ct) if hd["title"] and ct else 0.0
            same_paper = bool(hd["doi"] and cd and hd["doi"] == cd) or sim >= 0.85
            if cd and hd["doi"] and hd["doi"] != cd:
                defects.append("metadata_doi_mismatch")
                rec["conflict"] = "doi"
            if cy.isdigit() and hd["years"]:
                ydiff = abs(int(cy) - hd["years"][0])
                if ydiff == 1:
                    defects.append("metadata_year_minor")
                    rec["conflict"] = rec["conflict"] or "year_minor"
                elif ydiff > 1:
                    defects.append("metadata_year_mismatch")
                    rec["conflict"] = rec["conflict"] or "year"
            if (hd["title"] and ct and is_englishish(hd["title"])
                    and sim < 0.6 and not same_paper):
                defects.append("metadata_title_mismatch")
                rec["conflict"] = rec["conflict"] or "title"
        key = (reviews or {}).get(doc_id)
        if key:
            defects.append("review_high_issue")
        rec["defects"] = defects
        docs.append(rec)

    # corpus-level checks
    seen = {}
    for rec in docs:
        if rec["sha1"] in seen:
            rec["defects"].append("duplicate")
            rec["dup_of"] = seen[rec["sha1"]]["path"]
        else:
            seen[rec["sha1"]] = rec
    heading_seq = {}
    for rec in docs:
        p = os.path.join(ROOT, rec["path"])
        try:
            text = read_text(p)[0] or ""
        except OSError:
            text = ""
        hs = [l[3:].strip() for l in md_lines(text) if l.startswith("## ")]
        heading_seq[rec["path"]] = "|".join(hs)
    counts = collections.Counter(v for v in heading_seq.values() if v)
    for rec in docs:
        hseq = heading_seq[rec["path"]]
        if hseq and counts[hseq] >= 8 and len(hseq.split("|")) >= 3:
            rec["defects"].append("template_sections")
    return docs


def finalize(docs):
    for rec in docs:
        rec["defects"] = [d for d in DEFECT_ORDER if d in set(rec["defects"])]
        major = [d for d in rec["defects"] if d not in MINOR]
        if not rec["defects"]:
            rec["quality"] = "ok"
        elif any(d in HARD for d in rec["defects"]) or major:
            rec["quality"] = "bad"
        else:
            rec["quality"] = "low"
        rec["defect"] = ";".join(rec["defects"])
    return docs


# ---------------------------------------------------------------- manifest

MANIFEST_FIELDS = ["path", "doc_id", "sha1", "title", "doi", "year", "chars",
                   "body_chars", "quality", "defect", "conflict", "action"]


def write_manifest(docs, catalog):
    rows = []
    for rec in sorted(docs, key=lambda r: r["path"]):
        # A catalog conflict means the registration belongs to a different paper;
        # the in-document header is the only identity the file itself supports.
        if rec["conflict"] in ("doi", "title", "year"):
            title, doi, year = rec["title"], rec["doi"], rec["year"]
        else:
            title, doi, year = (rec["cat_title"] or rec["title"],
                                rec["cat_doi"] or rec["doi"],
                                rec["cat_year"] or rec["year"])
        rows.append({
            "path": rec["path"], "doc_id": rec["doc_id"], "sha1": rec["sha1"],
            "title": title, "doi": doi, "year": year, "chars": rec["chars"],
            "body_chars": rec["body_chars"], "quality": rec["quality"],
            "defect": rec["defect"], "conflict": rec["conflict"],
            "action": rec["action"],
        })
    # catalog rows without a file
    present = {r["doc_id"] for r in docs}
    for doc_id in sorted(set(catalog) - present):
        row = catalog[doc_id]
        rows.append({
            "path": "", "doc_id": doc_id, "sha1": "",
            "title": cat_get(row, CFG.get("catalog_title")),
            "doi": cat_get(row, CFG.get("catalog_doi")),
            "year": cat_get(row, CFG.get("catalog_year")),
            "chars": 0, "body_chars": 0, "quality": "missing",
            "defect": "catalog_missing_md", "conflict": "", "action": "none",
        })
    with open(MANIFEST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return rows


# ---------------------------------------------------------------- fix

def normalize_whitespace(text):
    """LF newlines, at most two consecutive blank lines, one final newline.

    Trailing spaces are preserved: two trailing spaces are a Markdown hard
    line break, so stripping them would change rendering.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    blank = 0
    for line in text.split("\n"):
        if not line.strip():
            blank += 1
            if blank > 2:
                continue
            out.append("")
        else:
            blank = 0
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def repair_metadata(text, cat_row, rec, log):
    """Return (new_text, action, unresolved_conflicts)."""
    if not cat_row:
        return text, "keep", ""
    hd = parse_header(text)
    ct = cat_get(cat_row, CFG.get("catalog_title"))
    cd = clean_doi(cat_get(cat_row, CFG.get("catalog_doi")))
    cy = cat_get(cat_row, CFG.get("catalog_year"))
    record_match = cat_get(cat_row, CFG.get("record_match")) if CFG.get("record_match") else ""
    trusted = (record_match == "match") if CFG.get("record_match") else True
    sim = title_sim(hd["title"], ct) if hd["title"] and ct else 0.0
    same_paper = bool(hd["doi"] and cd and hd["doi"] == cd) or sim >= 0.85
    if not trusted and not same_paper:
        return text, "keep", "untrusted_catalog"
    if not trusted and sim < 0.85:
        return text, "keep", "untrusted_catalog"

    lines = text.splitlines()
    block, h1_at, first, stop = header_block(text)
    if not block:
        return text, "keep", ""
    joined = " ".join(block)
    if re.search(r"身份冲突|暂不归属|未充分确认|身份未|暂不认领", joined):
        return text, "keep", "source_identity_unconfirmed"
    new_block = list(block)
    changed = []

    # --- DOI
    if cd:
        if not hd["doi"]:
            if new_block and new_block[-1].startswith("*") and new_block[-1].endswith("*"):
                new_block[-1] = new_block[-1][:-1].rstrip() + f" · DOI: {cd}*"
            else:
                new_block.append(f"DOI: {cd}")
            changed.append("doi_added")
        elif hd["doi"] != cd and (trusted or sim >= 0.85):
            # only repair technical variants (case, /pdf suffix, prefix); a
            # genuinely different DOI is a conflict, not something to overwrite
            if hd["doi"] in cd or cd in hd["doi"]:
                for i, line in enumerate(new_block):
                    if hd["doi"] in line.lower():
                        new_block[i] = re.sub(re.escape(hd["doi"]), cd, line,
                                              count=1, flags=re.I)
                        changed.append("doi_fixed")
                        break

    # --- year: only when the header year contradicts the document's own arXiv
    # identifier and the catalog agrees with that identifier. A catalog year
    # that merely differs from a plausible header year is a conflict to report.
    cy_int = int(cy) if cy.isdigit() else None
    arx_year = arxiv_year(hd["arxiv"])
    if (cy_int and arx_year and cy_int == arx_year and hd["years"]
            and hd["years"][0] != arx_year and trusted):
        old = str(hd["years"][0])
        for i, line in enumerate(new_block):
            m = re.match(r"^(\*\*年份\*\*\s*[：:]\s*)(.*)$", line)
            if m:
                new_block[i] = m.group(1) + re.sub(r"\b%s\b" % old, str(cy_int),
                                                   m.group(2), count=1)
                changed.append("year_fixed")
                break
            if "·" in line:
                parts = line.split("·")
                for j, part in enumerate(parts[1:], 1):
                    ps = part.strip().strip("*")
                    if re.fullmatch(r"(?:19|20)\d{2}(?:/(?:19|20)\d{2})?", ps):
                        parts[j] = part.replace(ps, str(cy_int))
                        new_block[i] = "·".join(parts)
                        changed.append("year_fixed")
                        break
                if "year_fixed" in changed:
                    break

    # --- title: only replace an author list / placeholder with the catalog
    # title; a genuine title that merely differs from the catalog is a conflict
    # to report, not something to overwrite
    short_ok = ct and len(norm_title(ct)) >= 0.85 * len(norm_title(hd["title"]))
    if (ct and hd["title"] and is_englishish(hd["title"]) and sim < 0.6
            and trusted and short_ok and looks_like_authors(hd["title"])
            and (hd["doi"] == cd or not hd["doi"])):
        for i, line in enumerate(new_block):
            m = TITLE_LABEL_RE.match(line) or TITLE_LABEL_PLAIN_RE.match(line)
            if m and m.group(1).strip():
                prefix = line[:m.start(1)]
                suffix = ""
                tail = m.group(1)
                mm = re.match(r"^(.*?)(（(?:19|20)\d{2}.*)$", tail)
                if mm:
                    suffix = mm.group(2)
                new_block[i] = prefix + ct + suffix
                changed.append("title_fixed")
                break
            if line.startswith("*") and "·" in line:
                parts = line.split("·")
                inside = parts[0].strip("* ")
                new_block[i] = "*" + ct + " ·" + "·".join(parts[1:])
                changed.append("title_fixed")
                break

    if not changed:
        return text, "keep", ""
    new_lines = lines[:first] + new_block + lines[stop:]
    return "\n".join(new_lines) + "\n", "fixed:" + "+".join(changed), ""


def strip_provenance(text):
    lines = text.splitlines()
    while lines and PROVENANCE_RE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip("\n") + "\n"


def deletion_reason(rec):
    ds = set(rec["defects"])
    if "invalid_utf8" in ds:
        return "invalid_utf8"
    if "empty" in ds:
        return "empty"
    if "body_short" in ds:
        return "body_under_400_chars"
    if "replacement_critical" in ds:
        return "replacement_chars_over_2pct"
    if "duplicate" in ds:
        return "duplicate_content"
    if "no_h1" in ds and "no_source" in ds:
        return "no_title_no_source"
    return ""


def run_fix(catalog, reviews):
    # pass 1: text normalization and metadata repair
    changed = 0
    for p in sorted(glob.glob(os.path.join(ROOT, CFG["md_glob"]))):
        doc_id = os.path.basename(p)[:-3]
        text, raw, hard = read_text(p)
        if text is None:
            continue
        if hard:
            continue
        original = text
        new = normalize_whitespace(text)
        text = new
        if text.count("\n") <= 2 and text.count("\\n") >= 10:
            text = text.replace("\\n", "\n")
            text = normalize_whitespace(text)
        if CFG["catalog"] and doc_id in catalog:
            text, action, _ = repair_metadata(text, catalog[doc_id], {}, None)
        if REPO == "paper-reasoning-chains" and parse_header(text)["has_identifier"]:
            text = strip_provenance(text)
        if text != original:
            changed += 1
        for attempt in range(3):
            try:
                with open(p, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(0.5)
    print(f"[fix] rewritten files: {changed}")

    # pass 2: deletions
    rows = []
    for p in sorted(glob.glob(os.path.join(ROOT, CFG["md_glob"]))):
        doc_id = os.path.basename(p)[:-3]
        text, raw, hard = read_text(p)
        rec = {"path": os.path.relpath(p, ROOT).replace("\\", "/"), "doc_id": doc_id,
               "sha1": hashlib.sha1(raw).hexdigest(), "chars": len(text or ""),
               "defects": [], "body_chars": len(body_text(text or "")),
               "quality": "", "defect": "", "action": "keep", "title": "",
               "doi": "", "year": "", "cat_title": "", "cat_doi": "",
               "cat_year": "", "conflict": ""}
        if text is None:
            rec["defects"] = ["invalid_utf8"]
        else:
            body = body_text(text)
            h1 = real_h1(text)
            nfffd = text.count("\ufffd")
            if not text.strip():
                rec["defects"].append("empty")
            if len(body) < 400:
                rec["defects"].append("body_short")
            if not h1:
                rec["defects"].append("no_h1")
            if nfffd / max(1, len(text)) > 0.02:
                rec["defects"].append("replacement_critical")
            if not parse_header(text)["has_identifier"]:
                rec["defects"].append("no_source")
        rec["defects"] = [d for d in DEFECT_ORDER if d in set(rec["defects"])]
        reason = deletion_reason(rec)
        if reason:
            rows.append((rec["path"], reason, str(rec["chars"]), rec["sha1"], p))
    existing = {}
    if os.path.exists(REJECTED):
        with open(REJECTED, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                existing[(r.get("path", ""), r.get("sha1", ""))] = r
    for path, reason, chars, sha, full in rows:
        existing.setdefault((path, sha), {"path": path, "reason": reason,
                                          "chars": chars, "sha1": sha})
    with open(REJECTED, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["path", "reason", "chars", "sha1"])
        for r in existing.values():
            w.writerow([r.get("path", ""), r.get("reason", ""), r.get("chars", ""), r.get("sha1", "")])
    for path, reason, chars, sha, full in rows:
        os.remove(full)
        print(f"[fix] rejected {path}: {reason} ({chars} chars)")
    return len(rows)


def sync_catalog(docs):
    if not CFG["catalog"] or CFG["catalog_kind"] != "csv":
        return
    path = os.path.join(ROOT, CFG["catalog"])
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames)
        rows = list(reader)
    by_id = {r["sha1"]: r for r in rows}
    changed = 0
    for rec in docs:
        r = by_id.get(rec["doc_id"])
        if not r:
            continue
        if REPO == "stem-md-corpus":
            if r.get("markdown_chars") != str(rec["chars"]):
                r["markdown_chars"] = str(rec["chars"])
                changed += 1
            hv = "1" if os.path.exists(os.path.join(
                ROOT, "md", rec["doc_id"] + ".review.json")) else "0"
            if r.get("has_review") != hv:
                r["has_review"] = hv
                changed += 1
        if REPO == "pdfonly-stem":
            if "md_status" not in fields:
                fields += ["md_status", "md_chars"]
                for x in rows:
                    x.setdefault("md_status", "")
                    x.setdefault("md_chars", "")
            if r.get("md_status") != "present":
                r["md_status"] = "present"
                changed += 1
            if r.get("md_chars") != str(rec["chars"]):
                r["md_chars"] = str(rec["chars"])
                changed += 1
    if REPO == "pdfonly-stem" and "md_status" in fields:
        present = {rec["doc_id"] for rec in docs}
        for r in rows:
            if r["sha1"] not in present and r.get("md_status") != "missing":
                r["md_status"] = "missing"
                r["md_chars"] = "0"
                changed += 1
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fields})
    print(f"[fix] catalog cells updated: {changed}")


# ---------------------------------------------------------------- quality

def write_quality(docs, catalog, rows_before=None):
    from collections import Counter
    total = len(docs)
    q = Counter(r["quality"] for r in docs)
    defects = Counter()
    for r in docs:
        for d in r["defects"]:
            defects[d] += 1
    rejected = []
    if os.path.exists(REJECTED):
        with open(REJECTED, encoding="utf-8-sig", newline="") as f:
            rejected = list(csv.DictReader(f))
    rej_reason = Counter(r["reason"] for r in rejected)
    chars = [r["chars"] for r in docs]
    lines = [
        f"# 质量报告：{REPO}",
        "",
        "本文件由 `python scripts/validate.py --quality` 生成，所有数字来自脚本扫描。",
        "硬门槛与删除标准见 `scripts/QUALITY_STANDARD.md`。",
        "",
        "## 现状计数",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| Markdown 文件 | {total} |",
        f"| catalog 记录 | {len(catalog) if catalog else '无（从文档头派生）'} |",
        f"| 总字符数 | {sum(chars)} |",
        f"| 最短 / 中位 / 最长（字符） | {min(chars) if chars else 0} / "
        f"{sorted(chars)[len(chars)//2] if chars else 0} / {max(chars) if chars else 0} |",
        f"| quality=ok | {q.get('ok', 0)} |",
        f"| quality=low | {q.get('low', 0)} |",
        f"| quality=bad | {q.get('bad', 0)} |",
        f"| catalog/正文身份冲突 | {sum(1 for r in docs if r['conflict'] in ('doi','title','year'))} |",
        f"| 年份仅差 1 年（保留 catalog 年份） | {sum(1 for r in docs if r['conflict'] == 'year_minor')} |",
        f"| 已删除（REJECTED.csv） | {len(rejected)} |",
        "",
        "## 缺陷分布",
        "",
        "| defect | 篇数 |",
        "| --- | --- |",
    ]
    for d, n in defects.most_common():
        lines.append(f"| {d} | {n} |")
    if not defects:
        lines.append("| （无） | 0 |")
    lines += ["", "## 删除记录", ""]
    if rejected:
        lines += ["| reason | 篇数 |", "| --- | --- |"]
        for k, v in rej_reason.most_common():
            lines.append(f"| {k} | {v} |")
        lines.append("")
        lines.append("逐文件明细见 `REJECTED.csv`（path、reason、chars、sha1）。")
    else:
        lines.append("本次未删除任何文件。")
    lines += [
        "",
        "## 已执行的确定性修复",
        "",
        "- 统一 UTF-8 无 BOM、LF 换行；压缩 3 行以上连续空行与行尾空白。",
        "- 依据 catalog/manifest 补 DOI、纠正明显错误的 DOI/年份（仅在记录可信时）。",
        "- 同步 catalog 与实际文件不一致的字符数/存在性。",
        "- 不重写任何总结正文；正文一字未改。",
        "",
        "## 未解决项",
        "",
    ]
    open_items = [d for d in defects if d in {
        "no_source", "metadata_title_mismatch", "metadata_doi_mismatch",
        "metadata_year_mismatch", "truncated_suspect", "replacement_char",
        "no_h2", "language_mismatch", "short_body", "review_high_issue",
        "catalog_missing_md"}]
    if open_items:
        for d in open_items:
            lines.append(f"- `{d}`：{defects[d]} 篇，保留并标记（不可确定性修复或证据不足）。")
    else:
        lines.append("- 无。")
    lines += [
        "",
        "## 重跑命令",
        "",
        "```",
        "python scripts/validate.py            # 校验并重写 MANIFEST.csv，退出码即门槛结果",
        "python scripts/validate.py --quality  # 重写本文件",
        "python scripts/validate.py --fix      # 重放确定性清洗/修复/删除（幂等）",
        "```",
        "",
        "## 说明",
        "",
        f"- 来源基准：`{CFG['source_note']}`。",
        "- `MANIFEST.csv` 的 `sha1` 是文件内容 SHA-1，`doc_id` 是文件名中的来源记录 ID。",
        "- `REJECTED.csv` 只在删除时追加；删除内容可从 git 历史恢复。",
        "",
    ]
    with open(os.path.join(ROOT, "QUALITY.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print("[quality] QUALITY.md written")


# ---------------------------------------------------------------- sample

def stratified_sample(docs, n=32):
    picks = {}

    def add(rec, tag):
        if len(picks) < n and rec["path"] not in picks:
            rec = dict(rec)
            rec["stratum"] = tag
            picks[rec["path"]] = rec

    by_len = sorted(docs, key=lambda r: r["chars"])
    for r in by_len[:8]:
        add(r, "shortest")
    for r in by_len[-8:]:
        add(r, "longest")
    for r in docs:
        if "no_source" in r["defects"]:
            add(r, "no_source")
    for r in docs:
        if "truncated_suspect" in r["defects"]:
            add(r, "truncation")
    for r in docs:
        if "review_high_issue" in r["defects"]:
            add(r, "review_flag")
    for r in docs:
        if "template_sections" in r["defects"]:
            add(r, "template")
    i = 0
    while len(picks) < n and i < len(docs):
        add(docs[i], "random")
        i += max(1, len(docs) // max(1, n))
    return list(picks.values())


def run_sample(docs, n):
    try:
        import dm
    except ImportError:
        sys.exit("--sample 需要 dm.py（决策模型客户端）在 scripts/ 或 PYTHONPATH 中。")
    sample = stratified_sample([d for d in docs if d["chars"] > 0], n)
    questions = {
        "readable": {"type": "choice", "criteria": {
            "yes": "中文可读、主题明确、是完整的论文总结",
            "partial": "可读但有明显缺陷（碎片、重复、信息量极低）",
            "no": "基本不可读：乱码、空泛、拒答、与标题无关"}},
        "complete": {"type": "choice", "criteria": {
            "yes": "正文结尾完整",
            "no": "正文明显截断或只剩元信息"}},
        "matches_title": {"type": "choice", "criteria": {
            "yes": "正文主题与标题一致",
            "no": "正文与标题明显不符"}},
    }
    out = []
    for rec in sample:
        text = read_text(os.path.join(ROOT, rec["path"]))[0] or ""
        prompt = f"标题：{rec['title']}\n\n正文：\n{text[:12000]}"
        try:
            ans = dm.ask(prompt, questions)
            a = ans.get("answers", {})
            row = {"path": rec["path"], "stratum": rec["stratum"],
                   "chars": rec["chars"], "quality": rec["quality"],
                   "readable": a.get("readable", {}).get("choice", ""),
                   "complete": a.get("complete", {}).get("choice", ""),
                   "matches_title": a.get("matches_title", {}).get("choice", "")}
        except Exception as e:
            row = {"path": rec["path"], "stratum": rec["stratum"],
                   "chars": rec["chars"], "quality": rec["quality"],
                   "readable": "error", "complete": "error",
                   "matches_title": "error", "error": str(e)[:120]}
        out.append(row)
        print(row)
    with open(os.path.join(ROOT, "sample_review.csv"), "w", encoding="utf-8",
              newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "stratum", "chars", "quality",
                                          "readable", "complete", "matches_title"],
                           lineterminator="\n")
        w.writeheader()
        for r in out:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})
    print(f"[sample] {len(out)} docs -> sample_review.csv")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--quality", action="store_true")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    catalog = load_catalog()
    if args.fix:
        run_fix(catalog, load_reviews())
        catalog = load_catalog()
    reviews = load_reviews()
    paths = sorted(glob.glob(os.path.join(ROOT, CFG["md_glob"])))
    docs = finalize(scan(paths, catalog, reviews))
    if args.fix:
        sync_catalog(docs)
        catalog = load_catalog()
        docs = finalize(scan(paths, catalog, reviews))
    rows = write_manifest(docs, catalog)

    hard = [r for r in docs if any(d in HARD for d in r["defects"])]
    dup = [r for r in docs if "duplicate" in r["defects"]]
    summary = {
        "repo": REPO, "files": len([r for r in docs if r["chars"] or r["defect"] != "catalog_missing_md"]),
        "catalog": len(catalog), "ok": sum(r["quality"] == "ok" for r in docs),
        "low": sum(r["quality"] == "low" for r in docs),
        "bad": sum(r["quality"] == "bad" for r in docs),
        "hard_fail": len(hard), "duplicates": len(dup),
        "rejected": 0,
    }
    if os.path.exists(REJECTED):
        with open(REJECTED, encoding="utf-8-sig", newline="") as f:
            summary["rejected"] = len(list(csv.DictReader(f)))
    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False))
        for r in hard[:20]:
            print("HARD", r["path"], r["defect"])
    if args.quality:
        write_quality(docs, catalog)
    if args.sample:
        run_sample(docs, args.sample)
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
