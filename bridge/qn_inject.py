# -*- coding: utf-8 -*-
"""Inject openbot-style bridge into 千牛 webui.zip (no 探域 required).

Mirrors openbot QNInject:
  Resources/newWebui/webui.zip -> web_chat-packer/recent.html
  Load our local bridge: http://127.0.0.1:41011/qn_imsdk_bridge.js
"""
from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import winreg  # type: ignore
except ImportError:
    winreg = None  # type: ignore

IMSUPPORT = "https://iseiya.taobao.com/imsupport"
WORKLINK_OPENBOT = "https://worklink.oss-cn-hangzhou.aliyuncs.com/5CFB5E11D17E63CDD8CB37B52FA6ACFD.js"
LOCAL_BRIDGE = "http://127.0.0.1:41011/qn_imsdk_bridge.js"
CHAT_RECENT = "web_chat-packer/recent.html"


def _install_from_command(command: str) -> Optional[Path]:
    command = str(command or "").strip()
    if not command:
        return None
    if command.startswith('"'):
        end = command.find('"', 1)
        exe = command[1:end] if end > 1 else command.strip('"')
    else:
        idx = command.lower().find(".exe")
        exe = command[: idx + 4] if idx >= 0 else command
    try:
        parent = Path(exe).resolve().parent
        if parent.parent:
            return parent.parent
    except Exception:
        return None
    return None


def find_install_candidates() -> list[Path]:
    """All plausible AliWorkbench roots (Program Files + 探域副本 + registry)."""
    found: list[Path] = []
    seen: set[str] = set()

    def _add(p: Optional[Path]) -> None:
        if not p:
            return
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            return
        if (p / "AliWorkbench.ini").is_file():
            seen.add(key)
            found.append(p)

    # Prefer official install first
    for base in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AliWorkbench",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "AliWorkbench",
        Path(r"D:\AliWorkbench"),
        Path(r"C:\AliWorkbench"),
    ):
        _add(base)

    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"aliim\Shell\Open\Command") as key:
                command, _ = winreg.QueryValueEx(key, "")
            _add(_install_from_command(str(command or "")))
        except Exception:
            pass

    # 探域缓存的千牛资源（若存在）
    for root in (Path(r"D:\tanyuAgent\data\platformSoftware\cntaobao"), Path(r"D:\kefuAgent")):
        if not root.is_dir():
            continue
        try:
            for ini in root.rglob("AliWorkbench.ini"):
                _add(ini.parent)
                if len(found) >= 8:
                    break
        except Exception:
            pass
    return found


def find_install_path() -> Optional[Path]:
    cands = find_install_candidates()
    return cands[0] if cands else None


def _read_version(ini_path: Path) -> str:
    text = ini_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*Version\s*=\s*(\S+)", text, re.I | re.M)
    return (m.group(1).strip() if m else "") or ""


def find_resource_dir(install: Optional[Path] = None) -> Optional[Path]:
    install = install or find_install_path()
    if not install:
        return None
    ini = install / "AliWorkbench.ini"
    if not ini.is_file():
        return None
    ver = _read_version(ini)
    if not ver:
        return None
    res = install / ver / "Resources"
    return res if res.is_dir() else None


def webui_zip_path(resource_dir: Optional[Path] = None) -> Optional[Path]:
    res = resource_dir or find_resource_dir()
    if not res:
        return None
    z = res / "newWebui" / "webui.zip"
    return z if z.is_file() else None


def find_all_webui_zips() -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for install in find_install_candidates():
        res = find_resource_dir(install)
        z = webui_zip_path(res) if res else None
        if z and z.is_file():
            key = str(z.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append(z)
    return out


def _read_recent_html(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        with zf.open(CHAT_RECENT) as fh:
            return fh.read().decode("utf-8", "replace")


def is_injected(zip_path: Optional[Path] = None) -> bool:
    z = zip_path or webui_zip_path()
    if not z:
        return False
    try:
        html = _read_recent_html(z)
    except Exception:
        return False
    # openbot worklink OR our local bridge both OK (both use 41010 protocol)
    if LOCAL_BRIDGE in html or "qn_imsdk_bridge" in html:
        return True
    if WORKLINK_OPENBOT in html:
        # openbot OSS script connects to ws://127.0.0.1:41010 — protocol compatible
        return True
    if "ws://127.0.0.1:41010" in html or "127.0.0.1:41010" in html:
        return True
    if IMSUPPORT in html:
        return False
    return False


def inject_status() -> Dict[str, Any]:
    zips = find_all_webui_zips()
    out: Dict[str, Any] = {
        "install_path": str(find_install_path() or ""),
        "webui_zips": [str(z) for z in zips],
        "injected": False,
        "all_injected": False,
        "injected_count": 0,
        "uninjected_count": 0,
        "uninjected_zips": [],
        "mode": "",
        "error": "",
        "details": [],
    }
    if not zips:
        out["error"] = "未找到千牛 webui.zip"
        return out
    any_inj = False
    modes = []
    for z in zips:
        try:
            html = _read_recent_html(z)
            inj = is_injected(z)
            if LOCAL_BRIDGE in html:
                mode = "local_bridge"
            elif WORKLINK_OPENBOT in html:
                mode = "openbot_worklink"
            elif IMSUPPORT in html:
                mode = "stock_imsupport"
            else:
                mode = "unknown_custom"
            out["details"].append({"zip": str(z), "injected": inj, "mode": mode})
            if inj:
                any_inj = True
                modes.append(mode)
        except Exception as exc:
            out["details"].append({"zip": str(z), "error": str(exc)})
    injected_count = sum(1 for row in out["details"] if row.get("injected") is True)
    uninjected_zips = [
        str(row.get("zip") or "")
        for row in out["details"]
        if row.get("injected") is not True and row.get("zip")
    ]
    out["injected"] = any_inj
    out["all_injected"] = bool(zips) and injected_count == len(zips)
    out["injected_count"] = injected_count
    out["uninjected_count"] = len(uninjected_zips)
    out["uninjected_zips"] = uninjected_zips
    out["mode"] = modes[0] if modes else (out["details"][0].get("mode") if out["details"] else "")
    out["webui_zip"] = str(zips[0])
    out["resource_dir"] = str(zips[0].parent.parent) if zips else ""
    return out


def _inject_one_zip(zpath: Path, *, prefer_local: bool = True, force: bool = False) -> Dict[str, Any]:
    if is_injected(zpath) and not force:
        return {"ok": True, "skipped": True, "zip": str(zpath), "reason": "already injected"}

    try:
        html = _read_recent_html(zpath)
    except Exception as exc:
        return {"ok": False, "zip": str(zpath), "error": f"read recent.html fail: {exc}"}

    target_src = LOCAL_BRIDGE if prefer_local else WORKLINK_OPENBOT
    new_html = html
    if IMSUPPORT in new_html:
        new_html = new_html.replace(IMSUPPORT, target_src)
    if WORKLINK_OPENBOT in new_html and prefer_local:
        new_html = new_html.replace(WORKLINK_OPENBOT, LOCAL_BRIDGE)
    if target_src not in new_html:
        snippet = (
            f'\n<script>\n'
            f'(function(){{var s=document.createElement("script");'
            f's.src="{target_src}";s.async=true;'
            f'document.documentElement.appendChild(s);}})();\n'
            f"</script>\n"
        )
        if "</body>" in new_html:
            new_html = new_html.replace("</body>", snippet + "</body>")
        else:
            new_html = new_html + snippet

    if new_html == html and is_injected(zpath):
        return {"ok": True, "skipped": True, "zip": str(zpath), "reason": "no change needed"}

    bak = zpath.with_suffix(zpath.suffix + ".bak-pddbridge")
    try:
        if not bak.exists():
            shutil.copy2(zpath, bak)
    except Exception:
        pass

    tmp = zpath.with_suffix(".zip.tmp-pdd")
    try:
        with zipfile.ZipFile(zpath, "r") as zin, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.replace("\\", "/") == CHAT_RECENT:
                    data = new_html.encode("utf-8")
                zout.writestr(item, data)
        os.replace(str(tmp), str(zpath))
    except PermissionError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {
            "ok": False,
            "zip": str(zpath),
            "error": f"无写权限: {exc}（请管理员运行）",
        }
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "zip": str(zpath), "error": str(exc)}

    sign = zpath.parent / "sign.json"
    try:
        if sign.is_file() and sign.stat().st_size > 0:
            sign.write_bytes(b"")
    except Exception:
        pass
    return {"ok": True, "skipped": False, "zip": str(zpath), "bridge_src": target_src}


def inject(*, prefer_local: bool = True, force: bool = False) -> Dict[str, Any]:
    """Patch all found webui.zip copies. Requires write access for Program Files installs."""
    zips = find_all_webui_zips()
    if not zips:
        status = inject_status()
        status["ok"] = False
        status["error"] = status.get("error") or "未找到千牛 webui.zip"
        return status
    results = [_inject_one_zip(z, prefer_local=prefer_local, force=force) for z in zips]
    status = inject_status()
    payload = {**status, "ok": bool(status.get("all_injected")), "results": results}
    if not payload["ok"] and not payload.get("error"):
        payload["error"] = f"仍有 {int(status.get('uninjected_count') or 0)} 个千牛版本未完成注入"
    return payload
