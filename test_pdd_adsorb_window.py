import pdd_adsorb_window as adsorb
from pdd_adsorb_window import (
    DockedWindow,
    ProcessInfo,
    Rect,
    WindowInfo,
    Win32,
    choose_dock_rect,
    select_dock_window,
    select_target_window,
)


def window(pid: int, title: str, class_name: str = "Qt5152QWindowIcon") -> WindowInfo:
    return WindowInfo(pid, pid, title, class_name, Rect(100, 80, 1200, 800), True, False)


def test_dock_uses_free_space_on_right() -> None:
    assert choose_dock_rect(Rect(0, 10, 1200, 900), Rect(0, 0, 1920, 1040), 320) == Rect(
        1204, 10, 1524, 900
    )


def test_dock_uses_left_when_right_is_full() -> None:
    assert choose_dock_rect(Rect(500, 0, 1800, 1000), Rect(0, 0, 1920, 1040), 320) == Rect(
        176, 0, 496, 1000
    )


def test_target_prefers_pdd_and_excludes_qianniu() -> None:
    rows = [window(10, "shop-接待中心"), window(20, "mall-接待中心")]
    processes = {
        10: ProcessInfo("AliWorkbench.exe", r"D:\qianniu\AliWorkbench.exe"),
        20: ProcessInfo("PddWorkbench.exe", r"D:\tanyu\PddWorkbench.exe"),
    }
    assert select_target_window(rows, processes).pid == 20


def test_target_does_not_guess_generic_qt_reception_window() -> None:
    rows = [window(10, "shop-接待中心")]
    processes = {10: ProcessInfo("Workbench.exe", r"D:\other\Workbench.exe")}
    assert select_target_window(rows, processes) is None


def test_target_accepts_pdd_path_compatibility_fallback() -> None:
    rows = [window(30, "拼多多商家工作台")]
    processes = {30: ProcessInfo("Workbench.exe", r"D:\tanyu\cnpdd\Workbench.exe")}
    assert select_target_window(rows, processes).pid == 30


def test_target_prefers_titled_pdd_window_over_loading_surface() -> None:
    rows = [window(40, ""), window(40, "拼多多工作台")]
    processes = {40: ProcessInfo("PddWorkbench.exe", r"D:\tanyu\PddWorkbench.exe")}
    assert select_target_window(rows, processes).title == "拼多多工作台"


def test_dock_prefers_rendered_page_over_blank_chrome_host() -> None:
    app = DockedWindow({})
    app.edge_pids = lambda: {50}
    blank = WindowInfo(1, 50, "", "Chrome_WidgetWin_0", Rect(0, 0, 320, 630), True, False)
    page = WindowInfo(2, 50, "聚合接待", "Chrome_WidgetWin_1", Rect(0, 0, 320, 630), True, False)
    assert app.find_dock_window([blank, page]) == page


def test_minimized_dock_is_not_replaced_by_black_chrome_host() -> None:
    black = WindowInfo(1, 50, "", "Chrome_WidgetWin_0", Rect(0, 0, 320, 630), True, False)
    page = WindowInfo(
        2, 50, "聚合接待", "Chrome_WidgetWin_1",
        Rect(-32000, -32000, -31840, -31972), True, True,
    )

    assert select_dock_window([black, page], {50}) == page


def test_black_chrome_host_is_hidden_after_dock_is_found(monkeypatch) -> None:
    app = DockedWindow({})
    app.dock_hwnd = 2
    app.edge_pids = lambda: {50}
    black = WindowInfo(1, 50, "", "Chrome_WidgetWin_0", Rect(0, 0, 320, 630), True, False)
    page = WindowInfo(2, 50, "聚合接待", "Chrome_WidgetWin_1", Rect(0, 0, 320, 630), True, False)
    calls = []
    monkeypatch.setattr(Win32.user32, "ShowWindow", lambda hwnd, command: calls.append((hwnd, command)))

    app.hide_black_host_windows([black, page])

    assert calls == [(1, Win32.SW_HIDE)]


def test_operator_minimized_dock_stays_minimized(monkeypatch) -> None:
    app = DockedWindow({})
    app.dock_hwnd = 42
    calls = []
    monkeypatch.setattr(Win32.user32, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(Win32.user32, "IsIconic", lambda _hwnd: True)
    monkeypatch.setattr(Win32.user32, "ShowWindow", lambda hwnd, command: calls.append((hwnd, command)))

    app.set_visible(True)

    assert calls == []


def test_edge_pids_include_same_profile_processes(monkeypatch) -> None:
    app = DockedWindow({})
    app.edge = None
    monkeypatch.setattr(adsorb, "profile_browser_processes", lambda _profile: [type("P", (), {"pid": 77})()])

    assert app.edge_pids() == {77}


def test_failed_controller_preserves_live_dock(monkeypatch) -> None:
    app = DockedWindow({})
    app.dock_hwnd = 42
    app.find_dock_window = lambda _windows: WindowInfo(
        42, 50, "聚合接待", "Chrome_WidgetWin_1", Rect(0, 0, 320, 630), True, False
    )
    monkeypatch.setattr(Win32, "windows", lambda: [])
    calls = []
    monkeypatch.setattr(adsorb, "stop_profile_browser", lambda _profile: calls.append("stop"))
    monkeypatch.setattr(Win32.user32, "PostMessageW", lambda *_args: calls.append("close"))

    app.close(preserve_live_dock=True)

    assert calls == []


def test_pin_off_applies_not_topmost(monkeypatch) -> None:
    app = DockedWindow({})
    app.dock_hwnd = 42
    dock = WindowInfo(42, 50, "聚合接待", "Chrome_WidgetWin_1", Rect(0, 0, 320, 630), True, False)
    calls = []
    monkeypatch.setattr(
        Win32.user32,
        "SetWindowPos",
        lambda *args: calls.append(args) or True,
    )

    app.apply_window_state(dock, dock.rect, {"pin": False, "adsorb": True})

    assert calls[0][1].value == Win32.HWND_NOTOPMOST.value


def _fake_exe(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")


def test_bundled_chromium_wins_over_system_browsers(tmp_path, monkeypatch) -> None:
    bundled = tmp_path / "chromium" / "chrome.exe"
    _fake_exe(bundled)
    monkeypatch.setattr(adsorb, "ROOT", tmp_path)

    assert adsorb.find_browser({}) == bundled


def test_bundled_chromium_wins_over_config(tmp_path, monkeypatch) -> None:
    """显式配置优先级最高。"""
    bundled = tmp_path / "chromium" / "chrome.exe"
    _fake_exe(bundled)
    explicit = tmp_path / "my-chrome.exe"
    _fake_exe(explicit)
    monkeypatch.setattr(adsorb, "ROOT", tmp_path)

    assert adsorb.find_browser({"browser_path": str(explicit)}) == explicit


def test_missing_configured_path_falls_through_to_bundle(tmp_path, monkeypatch) -> None:
    bundled = tmp_path / "chromium" / "chrome.exe"
    _fake_exe(bundled)
    monkeypatch.setattr(adsorb, "ROOT", tmp_path)

    assert adsorb.find_browser({"browser_path": str(tmp_path / "gone.exe")}) == bundled


def test_falls_back_to_system_browser_when_bundle_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adsorb, "ROOT", tmp_path)
    root = tmp_path / "system"
    _fake_exe(root / "Google" / "Chrome" / "Application" / "chrome.exe")
    monkeypatch.setenv("PROGRAMFILES", str(root))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert adsorb.find_browser({}).name == "chrome.exe"


def test_no_browser_anywhere_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adsorb, "ROOT", tmp_path)
    monkeypatch.setattr(adsorb, "SYSTEM_BROWSER_ROOTS", ())

    import pytest

    with pytest.raises(FileNotFoundError):
        adsorb.find_browser({})


def test_profile_dir_is_per_browser(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(adsorb, "STATE_DIR", tmp_path)
    chrome = tmp_path / "chrome.exe"
    _fake_exe(chrome)
    monkeypatch.setattr(adsorb, "_BROWSER", chrome)

    assert adsorb.profile_dir() == tmp_path / "pdd-adsorb-chrome-profile"
