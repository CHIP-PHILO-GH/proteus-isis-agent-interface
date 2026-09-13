# -*- coding: utf-8 -*-
"""Proteus ISIS 窗口控制与只读探测。

默认只使用 ctypes 调用 Win32。若安装了 pywin32 与 Pillow，截图会优先走
兼容路径；缺少这些可选依赖时仍可完成窗口控制、文本探测和 PNG 截图。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

try:
    import win32api
    import win32gui
    import win32ui
    from PIL import Image
except Exception:
    win32api = None
    win32gui = None
    win32ui = None
    Image = None


TITLE_CANDIDATES = (
    "ISIS Professional",
    "Proteus ISIS",
    "Labcenter Proteus",
    "Proteus",
)
CLASS_CANDIDATES = (
    "LX_ISIS_DClkNoDC",
    "LX_ISIS_NoDC",
    "LX_ISIS",
)
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_F12 = 0x7B
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_GETTEXT = 0x000D
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
IDOK = 1
DLG_CLASS = "#32770"
ERROR_KEYWORDS = (
    "internal error",
    "cannot open",
    "simulation failed",
    "0x0000e008",
    "no message found",
    "fatal simulator",
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32


def _proc_id(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _class_of(hwnd):
    buffer = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _title_of(hwnd):
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_size(hwnd):
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def _is_candidate(title, class_name):
    title_lower = title.lower()
    class_lower = class_name.lower()
    return any(mark.lower() in title_lower for mark in TITLE_CANDIDATES) or any(
        mark.lower() in class_lower for mark in CLASS_CANDIDATES
    )


def _enumerate_windows(pid=None, candidates_only=False):
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None and _proc_id(hwnd) != pid:
            return True
        title = _title_of(hwnd)
        class_name = _class_of(hwnd)
        if candidates_only and not _is_candidate(title, class_name):
            return True
        width, height = _window_size(hwnd)
        found.append((hwnd, title, class_name, width, height))
        return True

    user32.EnumWindows(callback, 0)
    found.sort(key=lambda item: item[3] * item[4], reverse=True)
    return found


def find_windows(pid=None, title_mark=None):
    """枚举候选 ISIS 顶层窗口，返回兼容旧版的四元组。"""
    windows = _enumerate_windows(pid, candidates_only=True)
    if title_mark:
        windows = [item for item in windows if title_mark in item[1]]
    return [(hwnd, title, width, height) for hwnd, title, _cls, width, height in windows]


def wait_main_hwnd(pid, timeout=25):
    """等待并返回 (hwnd, title)。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = find_windows(pid)
        if windows:
            return windows[0][0], windows[0][1]
        time.sleep(0.5)
    return None, None


def _process_path(pid):
    access = 0x1000 | 0x0400
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        query = getattr(kernel32, "QueryFullProcessImageNameW", None)
        if not query:
            return ""
        if query(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def probe():
    """只读列出当前可识别的 Proteus/ISIS 窗口。"""
    result = []
    for hwnd, title, class_name, width, height in _enumerate_windows(candidates_only=True):
        pid = _proc_id(hwnd)
        result.append(
            {
                "hwnd": int(hwnd),
                "pid": int(pid),
                "title": title,
                "class": class_name,
                "width": width,
                "height": height,
                "process": _process_path(pid),
            }
        )
    return {"ok": True, "windows": result, "state": "probe-ok" if result else "no-window"}


def resolve_isis_exe():
    configured = os.environ.get("PROTEUS_ISIS_EXE")
    candidates = [configured] if configured else []
    for variable in ("PROTEUS_BIN", "PROTEUS_DIR"):
        root = os.environ.get(variable)
        if root:
            candidates.extend(
                [
                    os.path.join(root, "ISIS.EXE"),
                    os.path.join(root, "BIN", "ISIS.EXE"),
                ]
            )
    for name in ("ISIS.EXE", "isis.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def resolve_temp_dir():
    configured = os.environ.get("PROTEUS_TEMP_DIR")
    if configured:
        return os.path.abspath(configured)
    system_temp = tempfile.gettempdir()
    if system_temp.isascii():
        return os.path.join(system_temp, "proteus-isis-agent-interface")
    return None


def bring_foreground(hwnd):
    """尽力把窗口置前。"""
    if win32gui:
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            pass
    try:
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        window_thread = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(foreground_thread, window_thread, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(foreground_thread, window_thread, False)
        return True
    except Exception:
        return False


def send_key_combo(hwnd, ctrl=False, shift=False, vk=VK_F12):
    """向窗口投递快捷键消息。"""
    bring_foreground(hwnd)
    time.sleep(0.1)

    def post(message, key):
        user32.PostMessageW(hwnd, message, key, 0)

    if ctrl:
        post(WM_KEYDOWN, VK_CONTROL)
    if shift:
        post(WM_KEYDOWN, VK_SHIFT)
    post(WM_KEYDOWN, vk)
    post(WM_KEYUP, vk)
    if shift:
        post(WM_KEYUP, VK_SHIFT)
    if ctrl:
        post(WM_KEYUP, VK_CONTROL)
    time.sleep(0.2)


def run_sim(hwnd):
    send_key_combo(hwnd, ctrl=True)
    time.sleep(0.5)


def pause_sim(hwnd):
    send_key_combo(hwnd)
    time.sleep(0.3)


def stop_sim(hwnd):
    send_key_combo(hwnd, shift=True)


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _write_png(path, width, height, rgba):
    rows = []
    stride = width * 4
    for offset in range(0, len(rgba), stride):
        rows.append(b"\x00" + rgba[offset : offset + stride])
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    payload += _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
    payload += _png_chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(payload)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", wt.LONG),
        ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def _capture_with_ctypes(hwnd, out_path):
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("无法读取窗口尺寸")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"无效窗口尺寸 {width}x{height}")
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    if not window_dc or not memory_dc or not bitmap:
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if window_dc:
            user32.ReleaseDC(hwnd, window_dc)
        raise RuntimeError("无法创建截图缓冲区")
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        rendered = bool(user32.PrintWindow(hwnd, memory_dc, 2))
        if not rendered:
            rendered = bool(
                gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, 0x00CC0020)
            )
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        raw = ctypes.create_string_buffer(width * height * 4)
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            raw,
            ctypes.byref(info),
            0,
        )
        if copied != height:
            raise RuntimeError("GetDIBits 未返回完整像素")
        pixels = raw.raw
        rgba_rows = []
        stride = width * 4
        for row in reversed(range(height)):
            source = pixels[row * stride : (row + 1) * stride]
            rgba_rows.append(b"".join(source[i : i + 3][::-1] + b"\xff" for i in range(0, stride, 4)))
        _write_png(out_path, width, height, b"".join(rgba_rows))
        return out_path, rendered
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def _capture_with_pywin32(hwnd, out_path):
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source_dc, width, height)
    save_dc.SelectObject(bitmap)
    rendered = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    if not rendered:
        rendered = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
    info = bitmap.GetInfo()
    data = bitmap.GetBitmapBits(True)
    image = Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]), data, "raw", "BGRX", 0, 1
    )
    image.save(out_path)
    save_dc.DeleteDC()
    source_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    win32gui.DeleteObject(bitmap.GetHandle())
    return out_path, bool(rendered)


def capture_window(hwnd, out_path):
    """截图；优先可选 pywin32/Pillow，失败后使用纯 ctypes。"""
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if win32gui and win32ui and Image:
        try:
            return _capture_with_pywin32(hwnd, out_path)
        except Exception:
            pass
    return _capture_with_ctypes(hwnd, out_path)


def _child_windows(parent):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        out.append(hwnd)
        return True

    user32.EnumChildWindows(parent, callback, 0)
    return out


def _dialog_text(hwnd):
    parts = [_title_of(hwnd)]
    for child in _child_windows(hwnd):
        if _class_of(child) == "Static":
            buffer = ctypes.create_unicode_buffer(512)
            user32.SendMessageW(
                child, WM_GETTEXT, len(buffer), ctypes.cast(buffer, ctypes.c_void_p)
            )
            if buffer.value:
                parts.append(buffer.value)
    return " ".join(part for part in parts if part)


def dismiss_error_dialogs(pid, timeout=20):
    """只关闭当前 ISIS 进程中命中已知错误关键词的标准对话框。"""
    dismissed = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = None
        for hwnd, title, class_name, _width, _height in _enumerate_windows(pid):
            if class_name != DLG_CLASS:
                continue
            text = (title + " " + _dialog_text(hwnd)).lower()
            if any(keyword in text for keyword in ERROR_KEYWORDS):
                found = hwnd
                break
        if not found:
            break
        user32.PostMessageW(found, WM_COMMAND, IDOK, 0)
        dismissed += 1
        time.sleep(0.8)
    return dismissed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--probe", action="store_true", help="只读探测 Proteus/ISIS 窗口并输出 JSON")
    parser.add_argument("--run-seconds", type=float, default=6)
    parser.add_argument("--shot")
    parser.add_argument("--shots", type=int, default=0)
    parser.add_argument("--no-run", action="store_true")
    parser.add_argument("--kill", action="store_true", help="结束后关闭 ISIS")
    args = parser.parse_args()

    if args.probe:
        print(json.dumps(probe(), ensure_ascii=False, separators=(",", ":")))
        return
    if args.list:
        for hwnd, title, width, height in find_windows():
            print(f"hwnd={hwnd} title=[{title}] size={width}x{height}")
        return
    if not args.dsn:
        print("ERR: --dsn、--list、--probe 三者至少提供一个")
        sys.exit(2)

    dsn = os.path.abspath(args.dsn)
    if not os.path.isfile(dsn):
        print(f"ERR: DSN 不存在 {dsn}")
        sys.exit(2)
    isis_exe = resolve_isis_exe()
    if not isis_exe:
        print("ERR: 未找到 ISIS.EXE，请设置 PROTEUS_ISIS_EXE 或 PROTEUS_DIR")
        sys.exit(2)
    temp_dir = resolve_temp_dir()
    if not temp_dir:
        print("ERR: 当前临时目录含非 ASCII 字符，请设置 PROTEUS_TEMP_DIR")
        sys.exit(2)
    os.makedirs(temp_dir, exist_ok=True)
    child_env = os.environ.copy()
    child_env["TEMP"] = temp_dir
    child_env["TMP"] = temp_dir
    try:
        process = subprocess.Popen([isis_exe, dsn], env=child_env)
    except OSError as exc:
        print(f"ERR: 无法启动 ISIS: {exc}")
        sys.exit(3)
    print(f"ISIS PID={process.pid}")
    hwnd, title = wait_main_hwnd(process.pid)
    if hwnd is None:
        print("ERR: 未找到 ISIS 主窗口")
        process.kill()
        process.wait()
        sys.exit(3)
    print(f"主窗口 hwnd={hwnd} title=[{title}]")
    time.sleep(3)
    dismissed = dismiss_error_dialogs(process.pid)
    if dismissed:
        print(f"打开阶段自动消除弹窗 {dismissed} 个")

    if not args.no_run:
        print("运行仿真 (Ctrl+F12)...")
        run_sim(hwnd)
        dismissed = dismiss_error_dialogs(process.pid)
        if dismissed:
            print(f"运行期自动消除弹窗 {dismissed} 个")
        time.sleep(max(0, args.run_seconds))
        if args.shots > 0:
            base = args.shot or os.path.join(os.path.dirname(dsn), "shot")
            for index in range(args.shots):
                output = f"{os.path.splitext(base)[0]}_{index + 1}.png"
                _, rendered = capture_window(hwnd, output)
                print(f"连拍[{index + 1}] {'OK' if rendered else 'Fallback'} -> {output}")
                time.sleep(2)
        elif args.shot:
            output, rendered = capture_window(hwnd, args.shot)
            print(f"截图 {'OK' if rendered else 'Fallback'} -> {output}")
        print("停止仿真 (Shift+F12)...")
        stop_sim(hwnd)
        time.sleep(1)
    elif args.shot:
        output, rendered = capture_window(hwnd, args.shot)
        print(f"截图 {'OK' if rendered else 'Fallback'} -> {output}")

    if args.kill:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        print("ISIS 已关闭")
    else:
        print(f"ISIS 保持运行 PID={process.pid}")


if __name__ == "__main__":
    main()



