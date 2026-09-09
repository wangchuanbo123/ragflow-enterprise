# -*- coding: utf-8 -*-
"""把 docs/系统总体说明.md 转换为独立 HTML 页面。

- markdown 渲染（表格/围栏代码/目录扩展）
- Mermaid 图用 CDN 渲染
- 自带侧边目录、返回顶部、亮色排版
"""
import sys
from pathlib import Path

import markdown
from markdown.extensions.toc import slugify_unicode

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "系统总体说明.md"
OUT = ROOT / "docs" / "系统总体说明.html"

text = SRC.read_text(encoding="utf-8")

md = markdown.Markdown(
    extensions=["tables", "fenced_code", "toc", "sane_lists"],
    extension_configs={
        "toc": {
            "slugify": slugify_unicode,
            "permalink": False,
        }
    },
)
body = md.convert(text)

# fenced_code 会把 ```mermaid 渲染为 <pre><code class="language-mermaid">，
# mermaid.js 只认 <div class="mermaid">，这里做后处理替换。
import re as _re

def _to_mermaid_div(m: "_re.Match") -> str:
    inner = m.group(1)
    # 循环反转义 HTML 实体，处理 &amp;lt; 这类双重转义
    prev = None
    while prev != inner:
        prev = inner
        inner = (inner
                 .replace("&lt;", "<")
                 .replace("&gt;", ">")
                 .replace("&quot;", '"')
                 .replace("&amp;", "&"))
    return '<div class="mermaid">' + inner + "</div>"

body = _re.sub(
    r'<pre><code class="language-mermaid">\n?(.*?)</code></pre>',
    _to_mermaid_div,
    body,
    flags=_re.S,
)

html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Enterprise RAG 系统总体说明</title>
<style>
  :root {
    --bg: #ffffff;
    --text: #1f2937;
    --heading: #111827;
    --muted: #6b7280;
    --border: #e5e7eb;
    --accent: #2563eb;
    --accent-soft: #eff6ff;
    --code-bg: #f3f4f6;
    --table-stripe: #f9fafb;
    --quote-bg: #fffbeb;
    --quote-border: #f59e0b;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.75;
    font-size: 15.5px;
  }
  .layout { display: flex; min-height: 100vh; }

  /* 侧边目录 */
  aside.toc {
    width: 280px;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    background: #fcfdff;
    padding: 28px 18px;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
  }
  aside.toc .toc-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--muted);
    letter-spacing: .12em;
    margin: 0 0 14px;
    text-transform: uppercase;
  }
  aside.toc ul { list-style: none; margin: 0; padding: 0; }
  aside.toc li { margin: 2px 0; }
  aside.toc a {
    display: block;
    padding: 5px 10px;
    border-radius: 6px;
    color: #374151;
    text-decoration: none;
    font-size: 13.5px;
    border-left: 2px solid transparent;
  }
  aside.toc a:hover { background: var(--accent-soft); color: var(--accent); }
  aside.toc > ul > li > a { font-weight: 600; margin-top: 10px; }
  aside.toc ul ul { padding-left: 14px; }
  aside.toc ul ul a { font-size: 12.8px; color: #4b5563; }

  /* 正文 */
  main { flex: 1; min-width: 0; padding: 48px 56px 96px; }
  .content { max-width: 940px; margin: 0 auto; }

  h1 {
    font-size: 30px; color: var(--heading);
    border-bottom: 3px solid var(--accent);
    padding-bottom: 14px; margin: 8px 0 20px;
    letter-spacing: .01em;
  }
  h2 {
    font-size: 23px; color: var(--heading);
    margin: 52px 0 18px;
    padding: 10px 16px;
    background: linear-gradient(90deg, var(--accent-soft), transparent 70%);
    border-left: 4px solid var(--accent);
    border-radius: 0 8px 8px 0;
  }
  h3 { font-size: 18.5px; color: #1e3a8a; margin: 34px 0 12px; }
  h4 { font-size: 16px; color: #334155; margin: 26px 0 10px; }
  p { margin: 12px 0; }

  a { color: var(--accent); }
  blockquote {
    margin: 18px 0;
    padding: 12px 18px;
    background: var(--quote-bg);
    border-left: 4px solid var(--quote-border);
    border-radius: 0 8px 8px 0;
    color: #78350f;
  }
  blockquote p { margin: 6px 0; }

  code {
    font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
    background: var(--code-bg);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: .88em;
    color: #b91c1c;
    word-break: break-all;
  }
  pre {
    background: #1e293b;
    color: #e2e8f0;
    padding: 18px 20px;
    border-radius: 10px;
    overflow-x: auto;
    line-height: 1.6;
    font-size: 13px;
    margin: 18px 0;
  }
  pre code { background: none; color: inherit; padding: 0; font-size: inherit; word-break: normal; }

  table {
    border-collapse: collapse;
    width: 100%;
    margin: 18px 0;
    font-size: 14px;
    box-shadow: 0 1px 2px rgba(0,0,0,.04);
  }
  th, td {
    border: 1px solid var(--border);
    padding: 8px 12px;
    text-align: left;
    vertical-align: top;
  }
  th { background: #f1f5f9; color: #0f172a; font-weight: 600; white-space: nowrap; }
  tr:nth-child(even) td { background: var(--table-stripe); }
  table code { white-space: nowrap; }

  ul, ol { padding-left: 26px; }
  li { margin: 4px 0; }
  hr { border: none; border-top: 1px solid var(--border); margin: 44px 0; }

  .mermaid {
    text-align: center;
    margin: 22px 0;
    padding: 16px;
    background: #fafbff;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow-x: auto;
  }

  /* 返回顶部 */
  #backTop {
    position: fixed; right: 28px; bottom: 28px;
    width: 42px; height: 42px;
    border-radius: 50%;
    border: 1px solid var(--border);
    background: #fff;
    color: var(--accent);
    font-size: 18px;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0,0,0,.12);
    display: none;
    z-index: 99;
  }
  #backTop:hover { background: var(--accent-soft); }

  @media (max-width: 1100px) {
    aside.toc { display: none; }
    main { padding: 32px 22px 72px; }
  }
  @media print {
    aside.toc, #backTop { display: none !important; }
    main { padding: 0; }
    pre { background: #f1f5f9; color: #0f172a; }
  }
</style>
</head>
<body>
<div class="layout">
  <aside class="toc" id="tocSidebar">
    <p class="toc-title">目录</p>
    __TOC__
  </aside>
  <main>
    <div class="content">
      __BODY__
    </div>
  </main>
</div>
<button id="backTop" title="返回顶部">&#8679;</button>

<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script>
  mermaid.initialize({ startOnLoad: true, theme: "neutral", securityLevel: "loose", flowchart: { curve: "basis" } });

  // 返回顶部
  const btn = document.getElementById("backTop");
  window.addEventListener("scroll", () => {
    btn.style.display = window.scrollY > 500 ? "block" : "none";
  });
  btn.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
</script>
</body>
</html>
"""

toc_html = md.toc
html = html.replace("__TOC__", toc_html).replace("__BODY__", body)
OUT.write_text(html, encoding="utf-8")
print(f"written: {OUT} ({len(html)} chars, body {len(body)} chars, toc {len(toc_html)} chars)")
