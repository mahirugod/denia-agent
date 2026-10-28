#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：book_intake
把用户放进「书架」的书变成可分批阅读的文本块，供达妮娅按章节读（配合 cangjie-skill 拆书）。

投喂方式：把 .epub / .txt / .md 放进 D:\\denia_terminal\\books\\，然后说「读一下 xxx」。
工具：
- list_books：看看书架上有哪些书
- feed_book：抽取正文 → 写入 bot/books/<slug>/source.md + chunks/NNN.txt
- read_book_chunk：读第 N 块（默认 4000 字一块）
- continue_reading：从上次读到的位置接着往下读 N 块（自动记进度）
- note_book_chunk：把读完的一块提炼成要点笔记存进 books/<slug>/notes/，供之后被问起时检索

阅读进度存在 meta.json 的 read_upto 里，重读旧块不倒退，读完全书记到 chunks 总数。
笔记会作为「书本笔记」被 knowledge_base 检索，是跨轮次记住书中内容的唯一来源。
"""
import html
import json
import os
import re
import time
import zipfile

from skills import skill
from tool_registry import tool

skill("book_intake", "接收并按块阅读书籍",
      tools=["list_books", "feed_book", "read_book_chunk", "continue_reading",
             "note_book_chunk"])

SKILLS = ["list_books", "feed_book", "read_book_chunk", "continue_reading",
          "note_book_chunk"]
SKILL_DESCRIPTION = "接收并按块阅读书籍"

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INBOX = os.path.join(_ROOT, "books")          # 用户投喂目录
WORKSPACE = os.path.join(_ROOT, "bot", "books")  # 拆书工作区（cangjie 脚本同样使用 books/）
EXTS = (".txt", ".md", ".epub")
CHUNK_SIZE = 4000


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", os.path.splitext(name)[0]).strip("-")
    return s[:60] or "book"


# ── 阅读进度（meta.json 的 read_upto）────────────────────────────
def _meta_path(slug: str) -> str:
    return os.path.join(WORKSPACE, _slug(slug or ""), "meta.json")


def _load_meta(slug: str) -> dict:
    path = _meta_path(slug)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_meta(slug: str, meta: dict):
    with open(_meta_path(slug), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def notes_dir(slug: str) -> str:
    return os.path.join(WORKSPACE, _slug(slug or ""), "notes")


def _mark_read(slug: str, meta: dict, upto: int):
    """记录读到第几块：只前进不后退（重读旧块不会把进度冲回去）"""
    if upto > int(meta.get("read_upto") or 0):
        meta["read_upto"] = upto
        meta["last_read"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_meta(slug, meta)


def _shelf() -> list:
    """工作区里已拆好的书（各自 meta.json）"""
    out = []
    if not os.path.isdir(WORKSPACE):
        return out
    for d in sorted(os.listdir(WORKSPACE)):
        p = os.path.join(WORKSPACE, d, "meta.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            continue
        m.setdefault("slug", d)
        out.append(m)
    return out


def _read_text(path: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def _read_epub(path: str) -> str:
    texts = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        opf = next((n for n in names if n.lower().endswith(".opf")), "")
        if not opf:
            return ""
        raw = z.read(opf).decode("utf-8", "ignore")
        base = os.path.dirname(opf)
        manifest = dict(re.findall(r'<item[^>]+id="([^"]+)"[^>]+href="([^"]+)"', raw))
        for idref in re.findall(r'<itemref[^>]+idref="([^"]+)"', raw):
            href = manifest.get(idref)
            if not href or not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            name = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
            if name not in names:
                continue
            body = z.read(name).decode("utf-8", "ignore")
            body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", body)
            body = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", body)
            body = html.unescape(re.sub(r"<[^>]+>", "", body))
            body = re.sub(r"[ \t\u3000]+", " ", body)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            if body:
                texts.append(body)
    return "\n\n".join(texts)


def _split_long(p: str, limit: int) -> list:
    """把超长段落按句子切到 limit 以内（中文书常见整章无空行）"""
    if len(p) <= limit:
        return [p]
    pieces, cur = [], ""
    for sent in re.split(r"(?<=[。！？!?；;])", p):
        if not sent:
            continue
        if len(cur) + len(sent) > limit and cur:
            pieces.append(cur)
            cur = ""
        if len(sent) > limit:
            for i in range(0, len(sent), limit):
                pieces.append(sent[i:i + limit])
        else:
            cur += sent
    if cur:
        pieces.append(cur)
    return pieces


def _find(name: str) -> str:
    if not os.path.isdir(INBOX):
        return ""
    key = (name or "").strip().lower()
    cands = [f for f in sorted(os.listdir(INBOX)) if f.lower().endswith(EXTS)]
    if not key:
        return os.path.join(INBOX, cands[0]) if cands else ""
    for f in cands:
        if key in f.lower() or key == os.path.splitext(f)[0].lower():
            return os.path.join(INBOX, f)
    return ""


@tool(
    name="list_books",
    description=(
        "看看书架上有什么书（书架目录 D:\\denia_terminal\\books）。"
        "用户问「有哪些书」「能读什么」时用。"
    ),
    keywords=["书架", "有哪些书"],
    parameters={"type": "object", "properties": {}},
)
def list_books() -> str:
    if not os.path.isdir(INBOX):
        return "（书架还是空的，把 epub/txt/md 放到 D:\\denia_terminal\\books 就行）"
    files = [f for f in sorted(os.listdir(INBOX)) if f.lower().endswith(EXTS)]
    if not files:
        return "（书架还是空的，把 epub/txt/md 放到 D:\\denia_terminal\\books 就行）"
    rows = []
    for f in files:
        size = os.path.getsize(os.path.join(INBOX, f))
        rows.append(f"- {f}（{size / 1024 / 1024:.1f}MB）")
    return "\n".join(rows)


@tool(
    name="feed_book",
    description=(
        "把书架上的某本书切成分批阅读的文本块：生成 bot/books/<slug>/source.md 与 chunks/001.txt…。"
        "用户说「读一下 XX」「拆一下 XX」时调用；调用后用 read_book_chunk 逐块读。"
        "支持 epub/txt/md；不传 name 时取书架第一本。"
    ),
    keywords=["读一下这本书", "拆一下这本书", "读这本书", "拆这本书"],
    parameters={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "书名或文件名的一部分"}},
    },
)
def feed_book(name: str = "") -> str:
    path = _find(name)
    if not path or not os.path.isfile(path):
        return "（书架上没找到这本书，先调 list_books 看看）"
    ext = os.path.splitext(path)[1].lower()
    text = _read_epub(path) if ext == ".epub" else _read_text(path)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "（没读到正文，可能是加密 EPUB 或编码异常）"

    title = os.path.splitext(os.path.basename(path))[0]
    slug = _slug(title)
    work = os.path.join(WORKSPACE, slug)
    chunks_dir = os.path.join(work, "chunks")
    os.makedirs(chunks_dir, exist_ok=True)

    with open(os.path.join(work, "source.md"), "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{text}")

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    pieces = []
    for p in paragraphs:
        pieces.extend(_split_long(p, CHUNK_SIZE))
    chunks, cur, size = [], [], 0
    for p in pieces:
        if size + len(p) > CHUNK_SIZE and cur:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(p)
        size += len(p)
    if cur:
        chunks.append("\n\n".join(cur))

    for i, body in enumerate(chunks, 1):
        with open(os.path.join(chunks_dir, f"{i:03d}.txt"), "w", encoding="utf-8") as f:
            f.write(body)

    meta = {
        "title": title,
        "slug": slug,
        "source": os.path.basename(path),
        "chars": len(text),
        "chunks": len(chunks),
        "chunk_size": CHUNK_SIZE,
        "read_upto": 0,
    }
    with open(os.path.join(work, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return (f"（《{title}》收好了：{len(text)} 字，切成 {len(chunks)} 块，"
            f"放在 bot/books/{slug}/chunks/。"
            f"先读第 1 块做整书概览，之后用 continue_reading 一路往下读。）")


@tool(
    name="read_book_chunk",
    description="读某本书的第 N 块正文（1 开始）。配合 feed_book 使用，一块约 4000 字。",
    keywords=[],
    parameters={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "feed_book 返回的 slug（书名）"},
            "index": {"type": "integer", "description": "第几块，从 1 开始"},
        },
        "required": ["slug", "index"],
    },
)
def read_book_chunk(slug: str = "", index: int = 1) -> str:
    meta = _load_meta(slug)
    if not meta:
        return "（这本书还没收进来，先调 feed_book）"
    total = int(meta.get("chunks") or 0)
    i = max(1, min(total or 1, int(index or 1)))
    path = os.path.join(WORKSPACE, _slug(slug), "chunks", f"{i:03d}.txt")
    if not os.path.isfile(path):
        return "（没有这一块）"
    with open(path, encoding="utf-8") as f:
        body = f.read()
    _mark_read(slug, meta, i)
    if len(body) > 6000:
        body = body[:6000] + "\n……（这一块太长，先读到这里，剩下的下次再读）"
    return (f"《{meta.get('title', slug)}》第 {i}/{total} 块：\n{body}"
            f"\n\n（读完请调用 note_book_chunk 记要点：slug=\"{_slug(slug)}\"，index={i}）")


@tool(
    name="continue_reading",
    description=(
        "接着上次读到的地方往下读，一次 N 块（默认 3 块，最多 5 块），读完自动记住进度。"
        "用户说「接着读」「继续读」「往下读」时用；不传 slug 时，工作区只有一本书就直接读它，"
        "有多本会把进度列出来让你挑。"
    ),
    keywords=["接着读", "继续读", "往下读", "接着看书"],
    parameters={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "书名（feed_book 返回的 slug），可省略"},
            "count": {"type": "integer", "description": "这次读几块，1-5，默认 3"},
        },
    },
)
def continue_reading(slug: str = "", count: int = 3) -> str:
    meta = _load_meta(slug) if slug else {}
    if not meta:
        # 没指定书名，或书名对不上（关键词快路径会把整句话当 slug 传进来）
        books = _shelf()
        if not books:
            return "（工作区里还没有拆好的书，先调 feed_book）"
        if len(books) > 1:
            rows = [f"- {b.get('slug')}：已读 {int(b.get('read_upto') or 0)}/"
                    f"{int(b.get('chunks') or 0)} 块" for b in books]
            return "（工作区有好几本书，说一声读哪本）\n" + "\n".join(rows)
        slug = books[0].get("slug") or ""
        meta = books[0]

    total = int(meta.get("chunks") or 0)
    if not total:
        return "（这本书没有分块，重新 feed_book 一下）"
    done = int(meta.get("read_upto") or 0)
    if done >= total:
        return f"（《{meta.get('title', slug)}》{total} 块已经全部读完了）"

    try:
        n = int(count or 3)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 5))

    start = done + 1
    end = min(total, start + n - 1)
    parts, budget, last = [], 12000, start - 1
    for i in range(start, end + 1):
        path = os.path.join(WORKSPACE, _slug(slug), "chunks", f"{i:03d}.txt")
        if not os.path.isfile(path):
            break
        with open(path, encoding="utf-8") as f:
            body = f.read()
        block = f"【第 {i} 块】\n{body}"
        if parts and len(block) > budget:
            break
        parts.append(block)
        budget -= len(block)
        last = i

    if not parts:
        return "（没读到内容，检查一下 chunks 目录）"
    _mark_read(slug, meta, last)
    return (f"《{meta.get('title', slug)}》第 {start}-{last}/{total} 块"
            f"（已读到 {last}/{total}）：\n\n" + "\n\n".join(parts)
            + f"\n\n（这几块请分别调用 note_book_chunk 记要点：slug=\"{_slug(slug)}\"，"
              f"index 依次为 {start}…{last}）")


@tool(
    name="note_book_chunk",
    description=(
        "把刚读完的一块正文提炼成要点笔记，永久保存在 books/<slug>/notes/。"
        "每读完一块都要调用（不要跳过）——笔记是之后被问起这本书时唯一的记忆来源。"
        "note 写 120-300 字：核心论点 + 关键事实/数字/人名 + 一句你自己的理解。"
        "只写要点，不要抄原文长句。"
    ),
    keywords=[],
    parameters={
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "书名（feed_book 返回的 slug）"},
            "index": {"type": "integer", "description": "第几块，从 1 开始"},
            "note": {"type": "string", "description": "这一块的要点笔记，120-300 字"},
        },
        "required": ["slug", "index", "note"],
    },
)
def note_book_chunk(slug: str = "", index: int = 1, note: str = "") -> str:
    meta = _load_meta(slug)
    if not meta:
        return "（这本书还没收进来，先调 feed_book）"
    body = (note or "").strip()
    if not body:
        return "（笔记是空的，说说这一块讲了什么再记）"
    try:
        i = max(1, int(index or 1))
    except (TypeError, ValueError):
        i = 1

    d = notes_dir(slug)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{i:03d}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"# 《{meta.get('title', slug)}》第 {i}/{meta.get('chunks', '?')} 块 要点\n\n"
            f"- 记录时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 原文块：chunks/{i:03d}.txt\n\n"
            f"{body}\n"
        )

    # 记一下已写笔记的块数，方便 list_books / continue_reading 判断进度
    saved = {int(x) for x in (meta.get("note_upto") or []) if str(x).isdigit()}
    saved.add(i)
    meta["note_upto"] = sorted(saved)
    _save_meta(slug, meta)
    return f"（已记下《{meta.get('title', slug)}》第 {i} 块的要点，共 {len(body)} 字）"
