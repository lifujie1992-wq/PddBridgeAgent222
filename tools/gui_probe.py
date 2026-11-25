"""GUI 冒烟测试：不靠人点，直接把配置对话框搭出来，把界面上出现的文案/选项打印出来核对。"""
import sys
import tkinter as tk
from types import SimpleNamespace

sys.path.insert(0, r"C:\Users\Administrator\Documents\Codex\2026-09-29\x20\work\github-pdd")

from bridge.gui import ConfigDialog  # noqa: E402


def probe(cfg: dict) -> None:
    root = tk.Tk()
    root.withdraw()
    app = SimpleNamespace(
        root=root,
        cfg=cfg,
        cfg_path=None,
        platform=SimpleNamespace(header_color="#1f6feb", name="pdd"),
        status_bar=SimpleNamespace(set=lambda *_: None),
        _phase="stopped",
        _refresh_view=lambda: None,
        open_config_file=lambda: None,
    )
    dlg = ConfigDialog(app)
    texts, combos = [], []

    def walk(widget):
        for child in widget.winfo_children():
            cls = child.winfo_class()
            if cls in ("Label", "Checkbutton", "Button"):
                try:
                    t = child.cget("text")
                    if t:
                        texts.append(t)
                except tk.TclError:
                    pass
            if cls == "TCombobox":
                combos.append((child.get(), tuple(child.cget("values"))))
            walk(child)

    walk(dlg.window)
    dlg.window.update_idletasks()
    print("当前选中:", combos)
    print("界面文案:")
    for t in texts:
        print("   ", t)
    dlg.window.destroy()
    root.destroy()


if __name__ == "__main__":
    print("=== 新装（默认配置） ===")
    probe({"data_source": "tanyu_logs"})
    print("\n=== 老配置（还写着 cdp） ===")
    probe({"data_source": "cdp"})
