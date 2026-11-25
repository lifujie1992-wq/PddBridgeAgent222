# -*- coding: utf-8 -*-
"""图片落地：把"图片地址或本地路径"变成可以交给工作台发送的本地文件。

工作台的 SendImageMsg 只吃本地文件路径（宽字符），所以：
  * 中心下发 URL → 必须由桥接先下载到本地；
  * 中心下发本地路径 → 直接校验存在与大小。
下载走普通 urllib（远端地址允许走系统代理），本地回环请用 bridge.netlocal。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.parse
import urllib.request
import uuid

MAX_IMAGE_BYTES = 15 * 1024 * 1024           # 单图上限，防止误下发大文件
ALLOWED_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def _suffix_from(value: str, content_type: str = "") -> str:
    guess = os.path.splitext(urllib.parse.urlparse(value).path)[1].lower()
    if guess in ALLOWED_SUFFIX:
        return guess
    mapping = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
        "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), ".png")


def image_cache_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "pdd_bridge_images")
    os.makedirs(path, exist_ok=True)
    return path


def _download(url: str, timeout: float = 15.0) -> tuple[str | None, str]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "pdd-bridge/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length and length > MAX_IMAGE_BYTES:
                return None, "image too large: %d bytes" % length
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return None, "image too large (>%d bytes)" % MAX_IMAGE_BYTES
            suffix = _suffix_from(url, response.headers.get("Content-Type") or "")
    except Exception as exc:                  # 网络/证书/超时
        return None, "download failed: %s" % exc
    if not data:
        return None, "download empty"
    name = hashlib.sha256(data).hexdigest()[:16] + suffix
    path = os.path.join(image_cache_dir(), name)
    try:
        if not os.path.isfile(path):
            with open(path, "wb") as handle:
                handle.write(data)
    except OSError as exc:
        return None, "write failed: %s" % exc
    return path, ""


def resolve_image(value: str) -> tuple[str | None, str]:
    """把 URL 或本地路径解析成本地文件。返回 (路径, 错误说明)。"""
    text = str(value or "").strip()
    if not text:
        return None, "empty image value"
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return _download(text)
    if lowered.startswith("file://"):
        text = urllib.request.url2pathname(urllib.parse.urlparse(text).path)
    try:
        if not os.path.isfile(text):
            return None, "local image not found: %s" % text
        size = os.path.getsize(text)
    except OSError as exc:
        return None, "stat failed: %s" % exc
    if size <= 0:
        return None, "local image is empty"
    if size > MAX_IMAGE_BYTES:
        return None, "local image too large: %d" % size
    return text, ""


def localize_many(values) -> tuple[list[str], list[str]]:
    """批量解析，返回 (可用路径列表, 失败说明列表)。"""
    paths, errors = [], []
    for value in values or ():
        path, error = resolve_image(value)
        if path:
            paths.append(path)
        else:
            errors.append("%s: %s" % (str(value)[:80], error))
    return paths, errors
