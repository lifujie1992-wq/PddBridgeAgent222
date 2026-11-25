# -*- coding: utf-8 -*-
"""发图冒烟：用一个不存在的买家 id 调 IMAGE，验证参数不会把客户端搞崩。"""
import os
import sys

sys.path.insert(0, r"C:\Users\Administrator\Documents\Codex\2026-09-29\x20\work\github-pdd\tools")
from probe_recv_hook import command  # noqa: E402

PNG = os.path.join(os.environ["TEMP"], "pdd_test_img.png")
CS = "cs_447787481:184712850"

print("图片存在:", os.path.isfile(PNG), "大小:", os.path.getsize(PNG) if os.path.isfile(PNG) else -1)
print("STATE   ->", command("STATE"))
print("IMAGE(不存在买家) ->", command("IMAGE|%s|99999999999999|%s" % (CS, PNG)))
print("STATE   ->", command("STATE"))
