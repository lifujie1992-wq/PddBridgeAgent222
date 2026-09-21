# -*- coding: utf-8 -*-
"""客服友好的 Bridge 状态窗口（Tkinter，无黑窗口命令行）。"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Optional
from urllib.parse import urlparse

from . import __version__
from .agent import BridgeAgent
from .config import default_config_path, load_config, save_config, write_example_config
from .platforms import get_platform


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _normalize_http_url(value: str, label: str, *, loopback: bool = False) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label}必须是完整的 http:// 或 https:// 地址")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{label}端口无效") from exc
    if loopback:
        if parsed.username or parsed.password:
            raise ValueError(f"{label}无需用户名密码，请从地址中删除登录信息")
        if parsed.hostname.lower() not in _LOOPBACK_HOSTS:
            raise ValueError(f"{label}必须使用 127.0.0.1、localhost 或 ::1")
    return url


class ConfigDialog:
    def __init__(self, app: "BridgeGuiApp") -> None:
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("编辑桥接配置")
        self.window.geometry("650x570")
        self.window.minsize(600, 530)
        self.window.configure(bg="#f4f6f8")
        self.window.transient(app.root)
        self.window.grab_set()

        cfg = dict(app.cfg)
        self.vars = {
            "server_url": tk.StringVar(value=str(cfg.get("server_url") or "")),
            "agent_token": tk.StringVar(value=str(cfg.get("agent_token") or "")),
            "agent_name": tk.StringVar(value=str(cfg.get("agent_name") or "")),
            "tanyu_log_dir": tk.StringVar(value=str(cfg.get("tanyu_log_dir") or "")),
            "local_workbench_url": tk.StringVar(
                value=str(cfg.get("local_workbench_url") or "http://127.0.0.1:18767")
            ),
            "manage_local_workbench": tk.BooleanVar(
                value=bool(cfg.get("manage_local_workbench", True))
            ),
            "dual_write_local_workbench": tk.BooleanVar(
                value=bool(cfg.get("dual_write_local_workbench", True))
            ),
            "dry_run": tk.BooleanVar(value=bool(cfg.get("dry_run", False))),
            "data_source": tk.StringVar(value=str(cfg.get("data_source") or "cdp")),
            "show_token": tk.BooleanVar(value=False),
        }
        self._build(cfg)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.after(0, self._focus)

    def _build(self, cfg: dict) -> None:
        header = tk.Frame(self.window, bg=self.app.platform.header_color)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text="桥接配置",
            font=("Microsoft YaHei UI", 15, "bold"),
            fg="white",
            bg=self.app.platform.header_color,
        ).pack(anchor="w", padx=18, pady=(13, 2))
        tk.Label(
            header,
            text="保存后会写入 bridge_config.json",
            font=("Microsoft YaHei UI", 9),
            fg="#fff5eb",
            bg=self.app.platform.header_color,
        ).pack(anchor="w", padx=18, pady=(0, 12))

        form = tk.Frame(self.window, bg="white", highlightbackground="#d0d7de", highlightthickness=1)
        form.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=16, pady=14)
        form.grid_columnconfigure(1, weight=1)

        self._entry_row(form, 0, "中心服务地址", "server_url")
        self._entry_row(form, 1, "机器令牌", "agent_token", secret=True)
        self._entry_row(form, 2, "工位名称", "agent_name")
        self._entry_row(form, 3, "探域日志目录", "tanyu_log_dir", browse=True)
        self._entry_row(form, 4, "本地工作台地址", "local_workbench_url")

        tk.Label(
            form,
            text="机器编号",
            font=("Microsoft YaHei UI", 9),
            fg="#57606a",
            bg="white",
            anchor="e",
        ).grid(row=5, column=0, sticky="e", padx=(14, 10), pady=7)
        agent_id = tk.Entry(
            form,
            font=("Microsoft YaHei UI", 9),
            relief=tk.SOLID,
            bd=1,
            disabledbackground="#f6f8fa",
            disabledforeground="#57606a",
        )
        agent_id.insert(0, str(cfg.get("agent_id") or ""))
        agent_id.configure(state=tk.DISABLED)
        agent_id.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=7, ipady=5)

        options = tk.Frame(form, bg="white")
        options.grid(row=6, column=0, columnspan=3, sticky="ew", padx=16, pady=(10, 8))
        ds_row = tk.Frame(options, bg="white")
        ds_row.pack(anchor="w", pady=(0, 6))
        tk.Label(
            ds_row,
            text="消息数据源：",
            font=("Microsoft YaHei UI", 9),
            bg="white",
        ).pack(side="left")
        ds_combo = ttk.Combobox(
            ds_row,
            textvariable=self.vars["data_source"],
            values=("cdp", "tanyu_logs"),
            state="readonly",
            font=("Microsoft YaHei UI", 9),
            width=20,
        )
        ds_combo.pack(side="left")
        tk.Label(
            ds_row,
            text="  cdp=PDD 实时收发（脱离探域）；tanyu_logs=探域日志（降级）",
            font=("Microsoft YaHei UI", 9),
            fg="#57606a",
            bg="white",
        ).pack(side="left", padx=(8, 0))
        tk.Checkbutton(
            options,
            text="随桥接助手启动本地工作台",
            variable=self.vars["manage_local_workbench"],
            font=("Microsoft YaHei UI", 9),
            bg="white",
            activebackground="white",
        ).pack(anchor="w")
        tk.Checkbutton(
            options,
            text="收到消息后立即同步到本地工作台",
            variable=self.vars["dual_write_local_workbench"],
            font=("Microsoft YaHei UI", 9),
            bg="white",
            activebackground="white",
        ).pack(anchor="w", pady=(4, 0))
        tk.Checkbutton(
            options,
            text="仅测试，不执行真实发送",
            variable=self.vars["dry_run"],
            font=("Microsoft YaHei UI", 9),
            bg="white",
            activebackground="white",
        ).pack(anchor="w", pady=(4, 0))

        actions = tk.Frame(self.window, bg="#f4f6f8")
        actions.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=(0, 14))
        tk.Button(
            actions,
            text="高级：打开 JSON",
            command=self.app.open_config_file,
            font=("Microsoft YaHei UI", 9),
            relief=tk.GROOVE,
            padx=10,
            pady=6,
            cursor="hand2",
        ).pack(side=tk.LEFT)
        tk.Button(
            actions,
            text="取消",
            command=self.close,
            font=("Microsoft YaHei UI", 9),
            relief=tk.GROOVE,
            padx=16,
            pady=6,
            cursor="hand2",
        ).pack(side=tk.RIGHT)
        tk.Button(
            actions,
            text="保存配置",
            command=self.save,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            bg="#1f6feb",
            fg="white",
            activebackground="#1158c7",
            activeforeground="white",
            padx=18,
            pady=7,
            cursor="hand2",
        ).pack(side=tk.RIGHT, padx=(0, 8))

    def _entry_row(
        self,
        parent: tk.Frame,
        row: int,
        label: str,
        key: str,
        *,
        secret: bool = False,
        browse: bool = False,
    ) -> None:
        tk.Label(
            parent,
            text=label,
            font=("Microsoft YaHei UI", 9),
            fg="#24292f",
            bg="white",
            anchor="e",
        ).grid(row=row, column=0, sticky="e", padx=(14, 10), pady=7)
        entry = tk.Entry(
            parent,
            textvariable=self.vars[key],
            font=("Microsoft YaHei UI", 9),
            relief=tk.SOLID,
            bd=1,
            show="●" if secret else "",
        )
        entry.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=7, ipady=5)
        if secret:
            tk.Checkbutton(
                parent,
                text="显示",
                variable=self.vars["show_token"],
                command=lambda: entry.configure(show="" if self.vars["show_token"].get() else "●"),
                font=("Microsoft YaHei UI", 9),
                bg="white",
                activebackground="white",
            ).grid(row=row, column=2, sticky="w", padx=(0, 14))
        elif browse:
            tk.Button(
                parent,
                text="选择…",
                command=self._choose_log_dir,
                font=("Microsoft YaHei UI", 9),
                relief=tk.GROOVE,
                cursor="hand2",
            ).grid(row=row, column=2, sticky="w", padx=(0, 14))
        else:
            tk.Frame(parent, width=58, bg="white").grid(row=row, column=2)
        if key == "server_url":
            self.first_entry = entry

    def _choose_log_dir(self) -> None:
        initial = self.vars["tanyu_log_dir"].get().strip()
        selected = filedialog.askdirectory(parent=self.window, initialdir=initial or None)
        if selected:
            self.vars["tanyu_log_dir"].set(selected)

    def _focus(self) -> None:
        self.window.lift()
        self.first_entry.focus_set()

    def save(self) -> None:
        try:
            server_url = _normalize_http_url(self.vars["server_url"].get(), "中心服务地址")
            workbench_url = _normalize_http_url(
                self.vars["local_workbench_url"].get(),
                "本地工作台地址",
                loopback=True,
            )
            token = self.vars["agent_token"].get().strip()
            if not token:
                raise ValueError("机器令牌不能为空")
            if "\n" in token or "\r" in token:
                raise ValueError("机器令牌不能包含换行")
            agent_name = self.vars["agent_name"].get().strip()
            if not agent_name:
                raise ValueError("工位名称不能为空")
            log_dir = self.vars["tanyu_log_dir"].get().strip()
            if not log_dir:
                raise ValueError("探域日志目录不能为空")

            updated = dict(self.app.cfg)
            updated.update(
                {
                    "server_url": server_url,
                    "agent_token": token,
                    "agent_name": agent_name,
                    "tanyu_log_dir": log_dir,
                    "local_workbench_url": workbench_url,
                    "manage_local_workbench": self.vars["manage_local_workbench"].get(),
                    "dual_write_local_workbench": self.vars["dual_write_local_workbench"].get(),
                    "dry_run": self.vars["dry_run"].get(),
                    "data_source": str(self.vars["data_source"].get() or "cdp"),
                }
            )
            save_config(updated, self.app.cfg_path)
            self.app.cfg = load_config(self.app.cfg_path, platform=self.app.platform.name)
            self.app.cfg["platform"] = self.app.platform.name
        except Exception as exc:
            messagebox.showerror("配置未保存", str(exc), parent=self.window)
            return

        was_running = self.app._phase in {"starting", "running", "stopping"}
        self.app.status_bar.set("配置已保存")
        self.app._refresh_view()
        self.close()
        detail = "配置已保存。"
        if was_running:
            detail += "\n\n中心连接配置将在停止并重新启动桥接后生效。"
        detail += "\n本地工作台地址或自动启动设置将在关闭并重开本程序后生效。"
        messagebox.showinfo("保存成功", detail, parent=self.app.root)

    def close(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            pass


# 总体状态：stopped | starting | running | stopping
# 颜色约定：绿=正常 橙=进行中/缺依赖 红=异常 灰=停止


class BridgeGuiApp:
    def __init__(self, *, platform: str = "pdd") -> None:
        self.platform = get_platform(platform)
        self.root = tk.Tk()
        self.root.title(f"{self.platform.label}桥接助手  v{__version__}")
        self.root.geometry("540x640")
        self.root.minsize(480, 560)
        self.root.configure(bg="#f4f6f8")

        self.cfg_path = default_config_path(self.platform.name)
        if not self.cfg_path.exists():
            write_example_config(self.cfg_path, platform=self.platform.name)
        self.cfg = load_config(self.cfg_path, platform=self.platform.name)
        self.cfg["platform"] = self.platform.name

        self.agent: Optional[BridgeAgent] = None
        self.agent_thread: Optional[threading.Thread] = None
        self._stop_agent = threading.Event()
        self.log_q: queue.Queue = queue.Queue()
        self._phase = "stopped"  # stopped | starting | running | stopping
        self._last_ch_probe = 0.0
        self._ch_cache: dict = {}

        self._build_ui()
        self._setup_logging()
        self._apply_button_state()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Show window first; heavy work only after first paint
        self.root.after(200, self._tick)
        # Auto-start after UI is interactive (never block __init__)
        self.root.after(1200, self.start_agent)

    def _build_ui(self) -> None:
        # Header
        color = self.platform.header_color
        header = tk.Frame(self.root, bg=color)
        header.pack(fill=tk.X)
        tk.Label(
            header,
            text=f"{self.platform.label}桥接助手",
            font=("Microsoft YaHei UI", 16, "bold"),
            fg="white",
            bg=color,
        ).pack(anchor="w", padx=16, pady=(12, 0))
        tk.Label(
            header,
            text=f"连接中心大脑 · 监听本机探域{self.platform.label}通道 · 代发消息",
            font=("Microsoft YaHei UI", 9),
            fg="#fff5eb",
            bg=color,
        ).pack(anchor="w", padx=16, pady=(2, 12))

        body = tk.Frame(self.root, bg="#f4f6f8")
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

        # ---- 总状态大卡片 ----
        self.banner = tk.Frame(body, bg="#6e7781", highlightthickness=0)
        self.banner.pack(fill=tk.X, pady=(0, 10))
        self.var_phase_title = tk.StringVar(value="● 已停止")
        self.var_phase_hint = tk.StringVar(value="点击下方「启动桥接」开始工作")
        self.lbl_phase_title = tk.Label(
            self.banner,
            textvariable=self.var_phase_title,
            font=("Microsoft YaHei UI", 18, "bold"),
            fg="white",
            bg="#6e7781",
        )
        self.lbl_phase_title.pack(anchor="w", padx=16, pady=(14, 2))
        self.lbl_phase_hint = tk.Label(
            self.banner,
            textvariable=self.var_phase_hint,
            font=("Microsoft YaHei UI", 10),
            fg="#f0f3f6",
            bg="#6e7781",
            justify="left",
            wraplength=480,
        )
        self.lbl_phase_hint.pack(anchor="w", padx=16, pady=(0, 14))

        # ---- 分项状态 ----
        card = tk.Frame(body, bg="white", highlightbackground="#d0d7de", highlightthickness=1)
        card.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            card,
            text="分项状态",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#57606a",
            bg="white",
        ).pack(anchor="w", padx=12, pady=(10, 4))

        self.row_center = self._status_row(card, "中心大脑")
        self.row_dll = self._status_row(
            card, "通道就绪" if self.platform.name == "taobao" else "工作台发送"
        )
        self.row_watch = self._status_row(card, "消息监听")
        self.row_seat = self._status_row(card, "本机工位")
        tk.Frame(card, height=8, bg="white").pack()

        if self.platform.name == "taobao":
            tip_text = (
                "使用前请：① 打开千牛接待台并登录  ② 建议开「无障碍模式」+ 多账号接待\n"
                "发送走 openbot 同款：CDP 写入输入框 + 点「发送」。窗口可最小化。\n"
                "配置 dry_run=false 才会真发；PddBridgeAgent 不会处理淘宝消息。"
            )
        else:
            tip_text = (
                "使用前请先打开：① 探域智能体  ② 拼多多商家工作台\n"
                "窗口可最小化；直接关闭会断开桥接。"
            )
        tip = tk.Label(
            body,
            text=tip_text,
            justify="left",
            font=("Microsoft YaHei UI", 9),
            fg="#57606a",
            bg="#f4f6f8",
        )
        tip.pack(fill=tk.X, pady=(0, 8))

        # ---- 主操作按钮 ----
        btn_row = tk.Frame(body, bg="#f4f6f8")
        btn_row.pack(fill=tk.X, pady=(0, 6))

        self.btn_start = tk.Button(
            btn_row,
            text="启动桥接",
            command=self.start_agent,
            font=("Microsoft YaHei UI", 11, "bold"),
            relief=tk.FLAT,
            padx=18,
            pady=10,
            cursor="hand2",
            width=12,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = tk.Button(
            btn_row,
            text="停止桥接",
            command=self.stop_agent,
            font=("Microsoft YaHei UI", 11, "bold"),
            relief=tk.FLAT,
            padx=18,
            pady=10,
            cursor="hand2",
            width=12,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_check = tk.Button(
            btn_row,
            text="刷新检查",
            command=self.refresh_status,
            font=("Microsoft YaHei UI", 10),
            relief=tk.FLAT,
            padx=12,
            pady=10,
            cursor="hand2",
            bg="#ddf4ff",
            fg="#0550ae",
            activebackground="#b6e3ff",
            activeforeground="#0550ae",
        )
        self.btn_check.pack(side=tk.LEFT)

        btn_row2 = tk.Frame(body, bg="#f4f6f8")
        btn_row2.pack(fill=tk.X, pady=(0, 8))
        tk.Button(
            btn_row2,
            text="打开本地工作台",
            command=self.open_local_workbench,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            bg="#1a7f37",
            fg="white",
            activebackground="#116329",
            activeforeground="white",
            padx=11,
            pady=5,
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(
            btn_row2,
            text="唤醒浮窗",
            command=self.wake_dock,
            font=("Microsoft YaHei UI", 9, "bold"),
            relief=tk.FLAT,
            bg="#8250df",
            fg="white",
            activebackground="#6639ba",
            activeforeground="white",
            padx=11,
            pady=5,
            cursor="hand2",
        ).pack(side=tk.LEFT, padx=(0, 8))
        for text, cmd in (
            ("编辑配置", self.edit_config),
            ("配置目录", self.open_config_dir),
            ("复制状态", self.copy_status),
        ):
            tk.Button(
                btn_row2,
                text=text,
                command=cmd,
                font=("Microsoft YaHei UI", 9),
                relief=tk.GROOVE,
                padx=10,
                pady=4,
                cursor="hand2",
            ).pack(side=tk.LEFT, padx=(0, 8))

        tk.Label(
            body,
            text="运行日志（客服一般不用看，给技术支持）",
            anchor="w",
            font=("Microsoft YaHei UI", 9),
            fg="#57606a",
            bg="#f4f6f8",
        ).pack(fill=tk.X)

        self.log_box = scrolledtext.ScrolledText(
            body,
            height=10,
            font=("Consolas", 9),
            bg="#0d1117",
            fg="#c9d1d9",
            insertbackground="white",
            state=tk.DISABLED,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self.status_bar = tk.StringVar(value="就绪")
        tk.Label(
            self.root,
            textvariable=self.status_bar,
            anchor="w",
            font=("Microsoft YaHei UI", 8),
            bg="#eaeef2",
            fg="#57606a",
        ).pack(fill=tk.X, side=tk.BOTTOM)

    def _status_row(self, parent: tk.Frame, title: str) -> dict:
        row = tk.Frame(parent, bg="white")
        row.pack(fill=tk.X, padx=12, pady=3)
        dot = tk.Label(row, text="●", font=("Microsoft YaHei UI", 11), fg="#8c959f", bg="white", width=2)
        dot.pack(side=tk.LEFT)
        name = tk.Label(
            row,
            text=title,
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#24292f",
            bg="white",
            width=10,
            anchor="w",
        )
        name.pack(side=tk.LEFT)
        text_var = tk.StringVar(value="—")
        text = tk.Label(
            row,
            textvariable=text_var,
            font=("Microsoft YaHei UI", 10),
            fg="#57606a",
            bg="white",
            anchor="w",
            justify="left",
        )
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return {"dot": dot, "text_var": text_var, "text": text}

    def _set_row(self, row: dict, kind: str, message: str) -> None:
        colors = {
            "ok": "#1a7f37",
            "warn": "#9a6700",
            "err": "#cf222e",
            "off": "#8c959f",
            "busy": "#0969da",
        }
        color = colors.get(kind, "#8c959f")
        row["dot"].configure(fg=color)
        row["text_var"].set(message)
        row["text"].configure(fg=color)

    def _style_button(self, btn: tk.Button, *, enabled: bool, primary: str = "blue") -> None:
        if not enabled:
            btn.configure(
                state=tk.DISABLED,
                bg="#eaeef2",
                fg="#8c959f",
                activebackground="#eaeef2",
                activeforeground="#8c959f",
                disabledforeground="#8c959f",
                cursor="arrow",
            )
            return
        palettes = {
            "blue": ("#1f6feb", "white", "#1158c7"),
            "red": ("#cf222e", "white", "#a40e26"),
            "green": ("#1a7f37", "white", "#116329"),
            "gray": ("#6e7781", "white", "#555e68"),
        }
        bg, fg, active = palettes.get(primary, palettes["blue"])
        btn.configure(
            state=tk.NORMAL,
            bg=bg,
            fg=fg,
            activebackground=active,
            activeforeground=fg,
            cursor="hand2",
        )

    def _apply_button_state(self) -> None:
        phase = self._phase
        if phase == "stopped":
            self._style_button(self.btn_start, enabled=True, primary="blue")
            self.btn_start.configure(text="启动桥接")
            self._style_button(self.btn_stop, enabled=False)
            self.btn_stop.configure(text="停止桥接")
        elif phase == "starting":
            self._style_button(self.btn_start, enabled=False)
            self.btn_start.configure(text="正在启动…")
            self._style_button(self.btn_stop, enabled=True, primary="red")
            self.btn_stop.configure(text="停止桥接")
        elif phase == "running":
            self._style_button(self.btn_start, enabled=False)
            self.btn_start.configure(text="运行中")
            self._style_button(self.btn_stop, enabled=True, primary="red")
            self.btn_stop.configure(text="停止桥接")
        elif phase == "stopping":
            self._style_button(self.btn_start, enabled=False)
            self.btn_start.configure(text="启动桥接")
            self._style_button(self.btn_stop, enabled=False)
            self.btn_stop.configure(text="正在停止…")

    def _set_banner(self, kind: str, title: str, hint: str) -> None:
        colors = {
            "ok": "#1a7f37",
            "warn": "#9a6700",
            "err": "#cf222e",
            "off": "#6e7781",
            "busy": "#0969da",
        }
        bg = colors.get(kind, "#6e7781")
        self.banner.configure(bg=bg)
        self.lbl_phase_title.configure(bg=bg)
        self.lbl_phase_hint.configure(bg=bg)
        self.var_phase_title.set(title)
        self.var_phase_hint.set(hint)

    def _setup_logging(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        # 只替换“界面日志框”那个 handler：启动阶段装好的文件 handler
        # （logs/bridge-pipeline.log）必须保留，否则运行期日志（event queued /
        # event upload / 队列指标）一条都不会落盘。
        for h in list(root_logger.handlers):
            if isinstance(h, QueueLogHandler):
                root_logger.removeHandler(h)
        qh = QueueLogHandler(self.log_q)
        qh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"))
        root_logger.addHandler(qh)
        logging.getLogger("pdd.bridge").setLevel(logging.INFO)

    def _append_log(self, line: str) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.insert(tk.END, line + "\n")
        try:
            total = int(self.log_box.index("end-1c").split(".")[0])
            if total > 500:
                self.log_box.delete("1.0", f"{total - 500}.0")
        except Exception:
            pass
        self.log_box.see(tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _tick(self) -> None:
        try:
            while True:
                line = self.log_q.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        # 线程结束后同步 phase
        if self._phase in {"starting", "running"} and self.agent_thread and not self.agent_thread.is_alive():
            self._phase = "stopped"
            self.agent = None
        if self._phase == "stopping" and (not self.agent_thread or not self.agent_thread.is_alive()):
            self._phase = "stopped"
            self.agent = None
        self._refresh_view()
        self.root.after(400, self._tick)

    def _channel_snapshot(self) -> dict:
        if self.agent and self._phase in {"starting", "running"}:
            st = getattr(self.agent, "_last_status", None)
            if isinstance(st, dict) and st:
                return {
                    "dll_ready": st.get("dll_ready"),
                    "receive_ready": st.get("receive_ready"),
                    "dll_port": st.get("dll_port"),
                    "port_discovery": st.get("port_discovery"),
                    "watching": st.get("watching"),
                    "hint": st.get("channel_hint") or st.get("hint") or "",
                }
        now = time.time()
        if now - self._last_ch_probe >= 8.0:
            self._last_ch_probe = now
            # Never block Tk UI thread on port scans — probe in background
            def _probe() -> None:
                try:
                    snap = self.platform.channel_status(self.cfg)
                    self._ch_cache = snap
                except Exception as exc:
                    self._ch_cache = {"dll_ready": False, "hint": f"状态探测失败: {exc}"}

            threading.Thread(target=_probe, name="ch-probe", daemon=True).start()
        return dict(self._ch_cache or {})

    def _refresh_view(self) -> None:
        ch = self._channel_snapshot()
        dll_ok = bool(ch.get("dll_ready"))
        receive_ok = bool(ch.get("receive_ready", self.platform.name != "taobao"))
        channel_ok = bool(dll_ok and (self.platform.name != "taobao" or receive_ok))
        log_dir_ok = Path(str(self.cfg.get("tanyu_log_dir") or "")).is_dir()
        token_ok = bool(self.cfg.get("agent_token")) and not str(self.cfg.get("agent_token")).startswith("change-me")

        center_ok = False
        watching = False
        err = ""
        if self.agent and self._phase in {"starting", "running"}:
            center_ok = bool(self.agent._last_heartbeat_ok)
            if hasattr(self.agent, "_active_watching"):
                watching = bool(self.agent._active_watching())
            else:
                watching = bool(self.agent.watcher._thread and self.agent.watcher._thread.is_alive())
            src_err = ""
            if hasattr(self.agent, "pddbridge_source") and self.agent.pddbridge_source is not None:
                src_err = str(getattr(self.agent.pddbridge_source, "last_error", "") or "")
            err = str(self.agent._last_error or src_err or self.agent.watcher.last_error or "")

        # 分项
        if self._phase == "stopped":
            self._set_row(self.row_center, "off", "未连接（桥接已停止）")
            self._set_row(self.row_watch, "off", "未监听")
        elif self._phase == "starting":
            self._set_row(self.row_center, "busy", "正在连接中心…")
            self._set_row(self.row_watch, "busy", "正在启动监听…")
        elif self._phase == "stopping":
            self._set_row(self.row_center, "busy", "正在断开…")
            self._set_row(self.row_watch, "busy", "正在停止监听…")
        else:
            if center_ok:
                self._set_row(self.row_center, "ok", f"已连接  {self.cfg.get('server_url') or ''}")
            elif err and ("heartbeat" in err.lower() or "register" in err.lower()):
                self._set_row(self.row_center, "err", f"连接失败，自动重试中  {err[:60]}")
            else:
                self._set_row(self.row_center, "warn", f"尚未连上，重试中…  {self.cfg.get('server_url') or ''}")
            if watching:
                effective = str(getattr(self.agent, "_effective_source", "") or "tanyu_logs")
                configured = str(getattr(self.agent, "_data_source", "") or "cdp")
                if effective == "cdp":
                    self._set_row(self.row_watch, "ok", "CDP 监听中（PDD 实时）")
                elif configured == "cdp":
                    self._set_row(self.row_watch, "warn", "已降级：日志监听中")
                else:
                    self._set_row(
                        self.row_watch,
                        "ok" if log_dir_ok else "warn",
                        "日志监听中" if log_dir_ok else "日志监听中，但日志目录不存在",
                    )
            else:
                self._set_row(self.row_watch, "warn", "监听未就绪")

        if channel_ok:
            if self.platform.name == "taobao":
                self._set_row(self.row_dll, "ok", ch.get("hint") or "千牛通道就绪")
            else:
                self._set_row(self.row_dll, "ok", f"正常，端口 {ch.get('dll_port')}")
        else:
            if self.platform.name == "taobao":
                self._set_row(
                    self.row_dll,
                    "warn",
                    ch.get("hint") or "未就绪 — 请从探域启动千牛并确认注入",
                )
            else:
                self._set_row(self.row_dll, "warn", "未就绪 — 请打开探域 + 拼多多工作台")

        seat = f"{self.cfg.get('agent_name') or '未命名'}  ·  {self.cfg.get('agent_id') or '-'}"
        if not token_ok:
            self._set_row(self.row_seat, "err", f"{seat}  ·  令牌未配置")
        else:
            self._set_row(self.row_seat, "ok" if self._phase == "running" and center_ok else "off", seat)

        # 总横幅 + 按钮
        if self._phase == "stopped":
            if not token_ok:
                self._set_banner("err", "● 已停止 · 需配置", "请先点「编辑配置」填写 agent_token，再启动桥接")
            elif not channel_ok:
                self._set_banner("off", "● 已停止", "可启动桥接；发送功能还需先打开探域与工作台")
            else:
                self._set_banner("off", "● 已停止", "点击「启动桥接」开始连接中心")
            self.status_bar.set("状态：已停止")
        elif self._phase == "starting":
            self._set_banner("busy", "● 正在启动…", "正在连接中心并开启日志监听，请稍候")
            self.status_bar.set("状态：启动中")
        elif self._phase == "stopping":
            self._set_banner("busy", "● 正在停止…", "正在断开连接，请稍候")
            self.status_bar.set("状态：停止中")
        else:  # running
            if center_ok and channel_ok and watching:
                self._set_banner("ok", "● 运行中 · 一切正常", "可最小化本窗口；关闭窗口会断开桥接")
                self.status_bar.set("状态：运行中（中心✓ 发送✓ 监听✓）")
            elif center_ok and watching and not channel_ok:
                if self.platform.name == "taobao":
                    if dll_ok and not receive_ok:
                        self._set_banner(
                            "warn",
                            "● 运行中 · 收消息未就绪",
                            ch.get("hint")
                            or "千牛发送通道可用，但聊天页尚未连接，店铺和消息暂时不会上报",
                        )
                    else:
                        self._set_banner(
                            "warn",
                            "● 运行中 · 不能发送",
                            ch.get("hint")
                            or "已连中心并在听消息，但千牛发送通道未就绪。请打开「千牛接待台」并登录",
                        )
                else:
                    self._set_banner(
                        "warn",
                        "● 运行中 · 不能发送",
                        "已连中心并在听消息，但工作台发送口未就绪。请打开探域 + 拼多多工作台",
                    )
                if self.platform.name == "taobao" and dll_ok and not receive_ok:
                    self.status_bar.set("状态：运行中（中心✓ 发送✓ 接收✗）")
                else:
                    self.status_bar.set("状态：运行中（中心✓ 发送✗）")
            elif not center_ok:
                self._set_banner(
                    "warn",
                    "● 运行中 · 中心未连上",
                    "本机桥已启动，但连不上中心服务器，正在自动重试。请检查网络与 server_url",
                )
                self.status_bar.set("状态：运行中（中心✗）")
            else:
                self._set_banner("warn", "● 运行中 · 部分未就绪", err[:120] if err else "部分组件未就绪，请看分项状态")
                self.status_bar.set("状态：运行中（部分异常）")

        self._apply_button_state()

    def start_agent(self) -> None:
        if self._phase in {"starting", "running", "stopping"}:
            return
        try:
            self.cfg = load_config(self.cfg_path, platform=self.platform.name)
            self.cfg["platform"] = self.platform.name
        except Exception as exc:
            messagebox.showerror("配置错误", f"无法读取配置：\n{exc}")
            return
        if not self.cfg.get("agent_token") or str(self.cfg.get("agent_token")).startswith("change-me"):
            messagebox.showwarning(
                "需要配置令牌",
                f"请先编辑配置，填写中心下发的 agent_token。\n\n点「编辑配置」打开 {self.cfg_path.name}。",
            )
            self.edit_config()
            self._refresh_view()
            return

        self._stop_agent = threading.Event()
        self.agent = BridgeAgent(self.cfg)
        self._phase = "starting"
        self._apply_button_state()
        self._refresh_view()

        def runner() -> None:
            try:
                self.agent._stop = self._stop_agent  # type: ignore[attr-defined]
                # 进入主循环前切到 running（register 在 run_forever 内）
                self.root.after(0, self._mark_running)
                self.agent.run_forever()
            except SystemExit as exc:
                self.log_q.put(f"退出：{exc}")
            except Exception as exc:
                self.log_q.put(f"异常：{exc}")
            finally:
                def _done() -> None:
                    if self._phase != "stopping":
                        self._phase = "stopped"
                    self._apply_button_state()
                    self._refresh_view()

                self.root.after(0, _done)

        self.agent_thread = threading.Thread(target=runner, name="bridge-agent-gui", daemon=True)
        self.agent_thread.start()
        logging.getLogger("pdd.bridge").info("用户启动桥接")

    def _mark_running(self) -> None:
        if self._phase == "starting":
            self._phase = "running"
            self._apply_button_state()
            self._refresh_view()

    def stop_agent(self) -> None:
        if self._phase not in {"starting", "running"}:
            return
        self._phase = "stopping"
        self._apply_button_state()
        self._refresh_view()
        self._stop_agent.set()
        if self.agent:
            try:
                self.agent.stop()
            except Exception:
                pass
        logging.getLogger("pdd.bridge").info("用户停止桥接")

        def _force_stopped() -> None:
            if self._phase == "stopping":
                self._phase = "stopped"
                self.agent = None
                self._apply_button_state()
                self._refresh_view()

        # 若线程卡死，2 秒后仍切到已停止，避免按钮永久灰掉
        self.root.after(2000, _force_stopped)

    def refresh_status(self) -> None:
        try:
            self.cfg = load_config(self.cfg_path, platform=self.platform.name)
            self.cfg["platform"] = self.platform.name
        except Exception:
            pass
        self._last_ch_probe = 0.0

        def _probe_and_show() -> None:
            try:
                ch = self.platform.channel_status(self.cfg)
                self._ch_cache = ch
            except Exception as exc:
                ch = {"dll_ready": False, "hint": f"状态探测失败: {exc}"}
                self._ch_cache = ch
            phase_cn = {
                "stopped": "已停止",
                "starting": "正在启动",
                "running": "运行中",
                "stopping": "正在停止",
            }.get(self._phase, self._phase)
            channel_line = (
                (ch.get("hint") or ("通道就绪" if ch.get("dll_ready") else "通道未就绪"))
                if self.platform.name == "taobao"
                else (
                    f"工作台发送：{'正常 端口 ' + str(ch.get('dll_port')) if ch.get('dll_ready') else '未就绪'}"
                )
            )
            msg = (
                f"【总状态】{phase_cn}\n"
                f"【平台】{self.platform.label}\n\n"
                f"中心：{self.cfg.get('server_url')}\n"
                f"令牌：{'已配置' if self.cfg.get('agent_token') else '未配置'}\n"
                f"{channel_line}\n"
                f"CDP：{ch.get('cdp_port') or '无'}\n"
                f"日志目录：{self.cfg.get('tanyu_log_dir')}\n"
                f"目录存在：{Path(str(self.cfg.get('tanyu_log_dir') or '')).is_dir()}\n"
                f"工位：{self.cfg.get('agent_name')} / {self.cfg.get('agent_id')}\n"
                f"dry_run：{bool(self.cfg.get('dry_run'))}"
            )

            def _ui() -> None:
                messagebox.showinfo("当前状态", msg)
                self._refresh_view()

            self.root.after(0, _ui)

        threading.Thread(target=_probe_and_show, name="status-probe", daemon=True).start()

    def edit_config(self) -> None:
        try:
            self.cfg = load_config(self.cfg_path, platform=self.platform.name)
            self.cfg["platform"] = self.platform.name
        except Exception as exc:
            messagebox.showerror("配置错误", f"无法读取配置：\n{exc}")
            return
        ConfigDialog(self)

    def open_config_file(self) -> None:
        path = self.cfg_path
        if not path.exists():
            write_example_config(path, platform=self.platform.name)
        try:
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("配置文件", f"请手动编辑：\n{path}")

    def open_local_workbench(self) -> None:
        try:
            cfg = load_config(self.cfg_path, platform=self.platform.name)
            url = _normalize_http_url(
                str(cfg.get("local_workbench_url") or "http://127.0.0.1:18767"),
                "本地工作台地址",
                loopback=True,
            )
            if not webbrowser.open(url, new=2):
                import os

                os.startfile(url)  # type: ignore[attr-defined]
            self.status_bar.set(f"已打开本地工作台：{url}")
        except Exception as exc:
            messagebox.showerror("无法打开本地工作台", str(exc))

    def _bundle_root(self) -> Path:
        import sys

        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent

    def wake_dock(self) -> None:
        """唤醒聚合接待浮窗（隐藏/最小化后一键找回）。"""
        import subprocess
        import sys

        executable = self._bundle_root() / "PddAdsorbWindow.exe"
        try:
            if not executable.is_file():
                raise FileNotFoundError("未找到 PddAdsorbWindow.exe")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            subprocess.Popen(
                [str(executable), "--wake"],
                cwd=str(executable.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self.status_bar.set("已发送浮窗唤醒请求，若未出现请确认探域工作台已打开")
        except Exception as exc:
            messagebox.showerror("无法唤醒浮窗", str(exc))

    def open_config_dir(self) -> None:
        folder = str(self.cfg_path.parent)
        try:
            import os

            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("目录", folder)

    def copy_status(self) -> None:
        # Never scan ports on UI thread — use cache / agent snapshot
        ch = dict(self._ch_cache or {})
        if not ch and self.agent:
            st = getattr(self.agent, "_last_status", None) or {}
            if isinstance(st, dict):
                ch = {
                    "dll_ready": st.get("dll_ready"),
                    "dll_port": st.get("dll_port"),
                    "cdp_port": st.get("cdp_port"),
                    "port_discovery": st.get("port_discovery"),
                    "hint": st.get("channel_hint") or st.get("hint") or "",
                }
        payload = {
            "version": __version__,
            "phase": self._phase,
            "platform": self.platform.name,
            "config": {
                "server_url": self.cfg.get("server_url"),
                "agent_id": self.cfg.get("agent_id"),
                "agent_name": self.cfg.get("agent_name"),
                "tanyu_log_dir": self.cfg.get("tanyu_log_dir"),
                "dry_run": self.cfg.get("dry_run"),
            },
            "channel": ch,
            "last_error": (self.agent._last_error if self.agent else ""),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_bar.set("状态已复制到剪贴板（可发给技术支持）")

    def _on_close(self) -> None:
        if self._phase in {"starting", "running"}:
            if not messagebox.askyesno(
                "退出确认",
                "当前桥接正在运行。\n关闭窗口将断开连接，本机将无法代发消息。\n\n确定退出吗？\n（可点「否」后最小化）",
            ):
                self.root.iconify()
                return
            self.stop_agent()
            time.sleep(0.3)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main(platform: str = "pdd") -> int:
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        # Fail fast if another instance is already running and hung
        app = BridgeGuiApp(platform=platform)
        try:
            app.root.update_idletasks()
            app.root.deiconify()
            app.root.lift()
            app.root.attributes("-topmost", True)
            app.root.after(400, lambda: app.root.attributes("-topmost", False))
        except Exception:
            pass
        app.run()
        return 0
    except Exception as exc:
        # Windowed EXE has no console — surface startup failures to the user.
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "桥接助手启动失败",
                f"{type(exc).__name__}: {exc}\n\n"
                "常见原因：配置文件损坏或含 BOM。\n"
                "可删除同目录 bridge_config*.json 后重开，或检查 JSON 格式。",
            )
            root.destroy()
        except Exception:
            pass
        try:
            import traceback
            from pathlib import Path
            import sys as _sys

            base = Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else Path.cwd()
            (base / "bridge_startup_error.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    import sys
    plat = "pdd"
    for arg in sys.argv[1:]:
        if arg.startswith("--platform="):
            plat = arg.split("=", 1)[1]
        elif arg in {"taobao", "pdd", "qianniu"}:
            plat = "taobao" if arg == "qianniu" else arg
    raise SystemExit(main(plat))
