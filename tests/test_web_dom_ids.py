# -*- coding: utf-8 -*-
"""界面脚本引用的 DOM id，必须在 index.html 里真实存在。

删元素时最容易漏掉 JS 里的 $(...) 引用：只要顶层赋值踩到 null，
后面的按钮绑定和首屏加载会被整段截断，现象就是「列表空白 + 按钮全没反应」
（0.7.4.0 删搜索框时漏了 $('search').oninput，就是这么挂的）。
"""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web" / "index.html"
# shopFilter 已下线，renderShopFilter 里用 `if (!box) return;` 做了守卫
ALLOWED_MISSING = {"shopFilter"}


def _inline_script(html: str) -> str:
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    return "\n".join(blocks)


def test_every_dollar_id_exists_in_dom():
    html = WEB.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", _inline_script(html)))
    missing = sorted(name for name in used if name not in ids and name not in ALLOWED_MISSING)
    assert missing == [], "脚本引用了 HTML 里不存在的 id：%s" % missing
