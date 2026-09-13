# -*- coding: utf-8 -*-
"""Proteus ISIS 确定性仿真验证器。

判定顺序是失败词、加载状态、运行信号、CPU 时间与窗口存活。若指定串口
断言，则在运行期间读取 Virtual Terminal 等文本控件，并把结果提升为
correct 或 failed。默认只依赖 Python 标准库与 Windows ctypes。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import time

try:  # 与 proteus_ctl 共用同一份窗口特征；单独拷走 verify.py 时用下面的内置副本
    from proteus_ctl import CLASS_CANDIDATES as _CLASS_CANDIDATES
    from proteus_ctl import TITLE_CANDIDATES as _TITLE_CANDIDATES
except Exception:
    _TITLE_CANDIDATES = (
        "ISIS Professional",
        "Proteus ISIS",
        "Labcenter Proteus",
        "Proteus",
    )
    _CLASS_CANDIDATES = (
        "LX_ISIS_DClkNoDC",
        "LX_ISIS_NoDC",
        "LX_ISIS",
    )


ISIS_EXE = os.environ.get("PROTEUS_ISIS_EXE")
TEMP_DIR = os.environ.get("PROTEUS_TEMP_DIR")
FAIL_RE = re.compile(
    r"cannot open|simulation\s+failed|fatal simulator|\berror\b", re.I
)
RUN_RE = re.compile(
    r"simulation\s*(started|running)|animation|\brunning\b|time\s*[=:]|simulating|仿真中",
    re.I,
)
LOAD_RE = re.compile(r"loading\s+(design|project)|正在加载", re.I)
SERIAL_TITLE_RE = re.compile(
    r"virtual\s*terminal|\bvterm\d*\b|\bserial\d*\b|\buart\d*\b|\bcom\d+\b", re.I
)
SERIAL_CLASS_RE = re.compile(r"edit|rich|scintilla|terminal|text", re.I)
# ISIS 左侧对象选择面板的分组标题。这些标签在每个设计里都存在，且文本恰好
# 命中 "terminal"（如 TERMINALS），会被误当成串口输出，必须排除。
SERIAL_PANE_LABELS = {
    "DEVICES",
    "TERMINALS",
    "PORTS",
    "PINS",
    "INSTRUMENTS",
    "SYMBOLS",
    "MARKERS",
    "GRAPHS",
    "GENERATORS",
    "GRAPHIC STYLES",
}
WM_GETTEXT = 0x000D
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
SB_GETTEXTLENGTHW = 0x040C
SB_GETTEXTW = 0x040D
STATUS_BAR_PARTS = 8
VK_CONTROL = 0x11
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def pid_of(hwnd):
    process_id = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return process_id.value


def _title_of(hwnd):
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _class_of(hwnd):
    buffer = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_size(hwnd):
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def _window_records(pid=None, visible_only=True):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if visible_only and not user32.IsWindowVisible(hwnd):
            return True
        if pid is not None and pid_of(hwnd) != pid:
            return True
        width, height = _window_size(hwnd)
        out.append((hwnd, _title_of(hwnd), _class_of(hwnd), width, height))
        return True

    user32.EnumWindows(callback, 0)
    return sorted(out, key=lambda item: item[3] * item[4], reverse=True)


def enum_windows(pid):
    """兼容旧接口：返回当前进程的可见顶层窗口。"""
    return [
        (hwnd, title, width, height)
        for hwnd, title, _class_name, width, height in _window_records(pid)
    ]


def is_main_candidate(title, class_name):
    """标题或类名命中已知 ISIS 主窗口特征。

    **不能只挑「最大的可见窗口」**：ISIS 启动时会先弹一个无标题的启动画面
    （实测 499x316），它随后被销毁。占用这个句柄会连锁出三个错：Ctrl+F12 投进
    一个已死窗口（仿真压根没启动）、控件文本永远读到空、失败截图报「无法读取窗
    口尺寸」。而与此同时 CPU 时间仍在涨（设计还在加载渲染），于是把「没在跑」
    判成 running=true。必须先认窗口特征，再取最大的那个。
    """
    lowered_title = (title or "").lower()
    lowered_class = (class_name or "").lower()
    return any(mark.lower() in lowered_title for mark in _TITLE_CANDIDATES) or any(
        mark.lower() in lowered_class for mark in _CLASS_CANDIDATES
    )


def find_main_windows(pid):
    """按窗口特征挑出候选主窗口，面积大的在前。"""
    return [
        (hwnd, title, width, height)
        for hwnd, title, class_name, width, height in _window_records(pid)
        if is_main_candidate(title, class_name)
    ]


def children(parent):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        out.append(hwnd)
        return True

    user32.EnumChildWindows(parent, callback, 0)
    return out


def text_of(hwnd):
    buffer = ctypes.create_unicode_buffer(4096)
    length = user32.GetWindowTextW(hwnd, buffer, len(buffer))
    if length:
        return buffer.value
    try:
        user32.SendMessageW(
            hwnd, WM_GETTEXT, len(buffer), ctypes.cast(buffer, ctypes.c_void_p)
        )
        return buffer.value
    except Exception:
        return ""


def status_parts(hwnd):
    """用 SB_GETTEXTW 逐栏读取标准状态栏文字。

    标准状态栏（msctls_statusbar32）把每格文字存在内部分栏里，
    GetWindowTextW / WM_GETTEXT 都取不到，必须用 SB_* 消息逐栏读。
    """
    parts = []
    for index in range(STATUS_BAR_PARTS):
        length = user32.SendMessageW(hwnd, SB_GETTEXTLENGTHW, index, 0)
        if not length:
            continue
        buffer = ctypes.create_unicode_buffer(512)
        copied = user32.SendMessageW(
            hwnd, SB_GETTEXTW, index, ctypes.cast(buffer, ctypes.c_void_p)
        )
        text = buffer.value.strip()
        if copied and text:
            parts.append(text)
    return parts


def snapshot_text(hwnd):
    """返回 (全部控件文本, 状态栏文本, 非空控件数)。"""
    values = []
    status = []
    for handle in [hwnd] + children(hwnd):
        class_name = _class_of(handle)
        text = text_of(handle)
        if text:
            values.append(text)
        if class_name.lower() in ("msctls_statusbar32", "statusbar"):
            if text:
                status.append(text)
            status.extend(status_parts(handle))
    return "\n".join(dict.fromkeys(values)), " | ".join(dict.fromkeys(status)), len(values)


def wait_main(pid, timeout):
    """等待并返回真正的 ISIS 主窗口句柄。

    只接受命中窗口特征的窗口；等不到就返回 None（宁可明确报「没找到主窗口」，
    也不要把启动画面当成主窗口 —— 那会导致假阳性）。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = find_main_windows(pid)
        if windows:
            return windows[0][0]
        time.sleep(0.3)
    return None


def key(hwnd, vk=0x7B, ctrl=False, shift=False):
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    keys_down = ([VK_CONTROL] if ctrl else []) + ([0x10] if shift else []) + [vk]
    keys_up = [vk] + ([0x10] if shift else []) + ([VK_CONTROL] if ctrl else [])
    for value in keys_down:
        user32.PostMessageW(hwnd, WM_KEYDOWN, value, 0)
    for value in keys_up:
        user32.PostMessageW(hwnd, WM_KEYUP, value, 0)


def cpu_seconds(pid):
    handle = kernel32.OpenProcess(0x0400, False, pid)
    if not handle:
        return None
    creation = wt.FILETIME()
    exit_time = wt.FILETIME()
    kernel = wt.FILETIME()
    user = wt.FILETIME()
    try:
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None

        def value(filetime):
            return ((filetime.dwHighDateTime << 32) | filetime.dwLowDateTime) / 10000000.0

        return value(kernel) + value(user)
    finally:
        kernel32.CloseHandle(handle)


def wait_cpu_idle(pid, quiet=0.4, max_wait=6.0):
    """等 CPU 累计时间连续 quiet 秒不再增长，即设计加载/渲染已经结束。

    这一步是判据成立的前提：ISIS 打开电路后还要画一会儿图，这段 CPU 增长与
    「仿真正在跑」无关。若不等它静止就把基线打在这里，坏固件也能被判成 running。
    返回 (是否已静止, 静止时的 CPU 秒数)。
    """
    last = cpu_seconds(pid)
    stable_since = None
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(0.25)
        current = cpu_seconds(pid)
        if current is None or last is None:
            return False, last
        if current - last <= 0.005:
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= quiet:
                return True, current
        else:
            stable_since = None
        last = current
    return False, last


def dialog_texts(pid, exclude_hwnd=None, extra_marks=()):
    """收集同一进程里其它可见顶层窗口（弹窗、错误框）的文字。

    错误弹窗通常不在主窗口的子控件树里，只看主窗口会漏掉「Cannot open ...」
    这类真正的致命提示。

    **虚拟终端等仪器窗口要排除**：那里面显示的是用户程序自己的输出，程序打印
    一句 `ERROR: ...` 就会被失败词表命中，把一次运行正常的仿真判成 `failed`。
    实测：带 Virtual Terminal 的电路上，该窗口的文字会出现在这里。

    `extra_marks` 必须和 `capture_serial` 用同一份（即 `--serial-window` 给的
    关键字）。否则会出现这种自相矛盾的结果：用户用 `--serial-window` 指定了
    一个标题不在默认候选里的终端，抓取那边认到了它、断言能通过，判定这边却仍
    把它的文字当弹窗扫描 —— 程序自己打印的 `ERROR:` 立刻把这次运行判成失败。
    """
    collected = []
    for hwnd, title, _class_name, _width, _height in _window_records(pid):
        if exclude_hwnd is not None and hwnd == exclude_hwnd:
            continue
        if _has_serial_mark(title, extra_marks):
            continue
        parts = [title]
        for child in children(hwnd):
            child_text = text_of(child)
            if child_text:
                parts.append(child_text)
        joined = " ".join(part for part in parts if part)
        if joined:
            collected.append(joined)
    return "\n".join(dict.fromkeys(collected))


def _has_serial_mark(text, extra_marks=()):
    """判断一段标题文字是否带串口/虚拟终端特征。

    特征匹配必须带**词边界**：电路文件名叫 `softuart.DSN` 时，主窗口标题里就含
    `uart` 子串；按子串匹配会让主窗口的所有子控件都变成串口候选，实测就是这样
    把工具条下拉框里的 `"0"`、以及窗口标题本身当成 `serial_text` 的
    （`--expect-serial "0"` 会因此假通过）。
    `--serial-window` 给的额外关键字按子串匹配即可，那是使用者显式指定的。

    **空白关键字一律忽略**：`"" in 任意字符串` 恒为真，一个空的 `--serial-window`
    会让**每一个**窗口都变成串口候选，同时把**每一个**其它窗口都从失败词扫描里
    排除掉（等价于把早期那个 `serial_candidates=64` 的假通过 bug 原样放回来）。
    """
    if SERIAL_TITLE_RE.search(text or ""):
        return True
    lowered = (text or "").lower()
    return any(
        mark.strip() and mark.strip().lower() in lowered
        for mark in extra_marks
    )


def _marked_ancestor(hwnd, stop_hwnd, extra_marks=()):
    """子控件的祖先里（不含 stop_hwnd 本身）是否有带串口特征的窗口。

    虚拟终端窗口里的显示控件**自己往往没有标题**，特征只挂在父窗口上，所以
    要允许继承；但主窗口的子控件不能继承主窗口的标题特征，否则工具条会被误认。
    """
    parent = user32.GetParent(hwnd)
    depth = 0
    while parent and parent != stop_hwnd and depth < 8:
        if _has_serial_mark(_title_of(parent), extra_marks):
            return True
        parent = user32.GetParent(parent)
        depth += 1
    return False


def capture_serial(pid, extra_marks=(), exclude_hwnd=None):
    """读取 Virtual Terminal 文本。

    返回 (文本, 方法, 候选数, 文本来源, 候选来源列表)。三条排除规则都是实测
    踩出来的：

    1. **主窗口（`exclude_hwnd`）自身永远不是串口候选**，它的子控件也不继承它
       的标题特征 —— 否则电路名叫 softuart 时，工具条下拉框的 `"0"` 会被当成
       串口输出（假通过）。
    2. 特征匹配用词边界，见 `_has_serial_mark`。
    3. 虚拟终端窗口里的显示控件可以继承**非主窗口**祖先的标题特征；软件自身的
       对象选择面板标签（`TERMINALS` 等）另用黑名单排除。

    `extra_marks` 是使用者用 `--serial-window` 指定的额外标题关键字。
    不做任何猜测：认不出终端就返回空文本，而不是随便抓一个文本框顶上。
    """
    candidates = []
    for hwnd, title, class_name, _width, _height in _window_records(pid):
        is_main = exclude_hwnd is not None and hwnd == exclude_hwnd
        window_hit = (not is_main) and _has_serial_mark(title, extra_marks)
        if window_hit:
            candidates.append((5, hwnd, title, class_name))
        for child in children(hwnd):
            child_title = _title_of(child)
            child_class = _class_of(child)
            own_hit = _has_serial_mark(child_title, extra_marks)
            if not own_hit and not window_hit:
                if not _marked_ancestor(child, hwnd, extra_marks):
                    continue
            score = 10 if own_hit else 5
            if SERIAL_CLASS_RE.search(child_class or ""):
                score += 2
            candidates.append((score, child, child_title, child_class))
    candidates.sort(key=lambda item: item[0], reverse=True)

    def usable(text):
        """排除软件自身面板标题等不具备串口语义的文本。"""
        stripped = text.strip()
        return bool(stripped) and stripped.upper() not in SERIAL_PANE_LABELS

    candidate_sources = []
    for _score, _hwnd, title, class_name in candidates:
        source = f"{class_name}|{title}"
        if source not in candidate_sources:
            candidate_sources.append(source)

    texts = []
    sources = []
    for _score, hwnd, title, class_name in candidates:
        text = text_of(hwnd)
        # 顶层窗口用 GetWindowText 读到的是标题栏文字本身，不是终端内容。
        if text.strip() and text.strip() == (title or "").strip():
            continue
        if not usable(text):
            continue
        texts.append(text)
        sources.append(f"{class_name}|{title}")
    if texts:
        return (
            "\n".join(dict.fromkeys(texts)),
            "wm_gettext",
            len(candidates),
            sources[0],
            candidate_sources[:8],
        )

    for _score, hwnd, title, class_name in candidates:
        if not SERIAL_CLASS_RE.search(class_name or ""):
            continue
        try:
            text = _clipboard_capture(hwnd)
        except Exception:
            text = ""
        if usable(text):
            return (
                text,
                "clipboard",
                len(candidates),
                f"{class_name}|{title}",
                candidate_sources[:8],
            )
    return "", "none", len(candidates), "", candidate_sources[:8]


def _clipboard_text():
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _save_clipboard_text():
    return _clipboard_text()


def _restore_clipboard_text(text):
    if text is None or not user32.OpenClipboard(None):
        return
    try:
        encoded = (text + "\x00").encode("utf-16-le")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not handle:
            return
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return
        ctypes.memmove(pointer, encoded, len(encoded))
        kernel32.GlobalUnlock(handle)
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_capture(hwnd):
    saved = _save_clipboard_text()
    try:
        user32.SetForegroundWindow(hwnd)
        key(hwnd, vk=0x41, ctrl=True)
        key(hwnd, vk=0x43, ctrl=True)
        time.sleep(0.15)
        return _clipboard_text()
    finally:
        _restore_clipboard_text(saved)


def _match_serial(text, expected_serial, expected_regex):
    failures = []
    for expected in expected_serial:
        if expected not in text:
            failures.append("serial substring not found: " + expected)
    for expression in expected_regex:
        try:
            matched = re.search(expression, text, re.MULTILINE) is not None
        except re.error as exc:
            failures.append(f"invalid serial regex {expression}: {exc}")
            continue
        if not matched:
            failures.append("serial regex not matched: " + expression)
    return not failures, failures


def _write_serial(path, text):
    if not path:
        return
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_failure_evidence(directory, log, status, hwnd=None):
    """把失败现场写入目录；截图失败也要留痕，不能静默吞掉。"""
    os.makedirs(directory, exist_ok=True)
    notes = []
    if hwnd:
        try:
            from proteus_ctl import capture_window

            capture_window(hwnd, os.path.join(directory, "simulation.png"))
            notes.append("screenshot: ok")
        except Exception as exc:
            notes.append(
                "screenshot failed: {}: {}".format(type(exc).__name__, exc)
            )
    else:
        notes.append("screenshot skipped: no main window handle")
    with open(os.path.join(directory, "simulation.log.txt"), "w", encoding="utf-8") as handle:
        handle.write(log + "\nSTATUS: " + status + "\n")
        for note in notes:
            handle.write("EVIDENCE " + note + "\n")


def _close_process(process, hwnd, keep):
    if keep:
        return
    if hwnd:
        key(hwnd, shift=True)
        time.sleep(0.5)
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--wait", type=float, default=6)
    parser.add_argument("--startup-timeout", type=float, default=25)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--expect-serial", action="append", default=[])
    parser.add_argument("--expect-regex", action="append", default=[])
    parser.add_argument("--dump-serial")
    parser.add_argument(
        "--serial-window",
        action="append",
        default=[],
        help="额外指定串口窗口标题关键字（可重复）；用于窗口标题不在默认候选里的版本",
    )
    parser.add_argument("--evidence-dir")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    started = time.monotonic()
    result = {
        "running": False,
        "log_text": "",
        "status_text": "",
        "errors": [],
        "evidence": "",
        "pid": None,
        "level": "failed",
        "serial_text": "",
        "serial_method": "none",
        "serial_match": None,
        "serial_candidates": 0,
        "serial_source": "",
        "serial_sources": [],
        "main_window": None,
        "dialog_text": "",
        "window_alive": None,
        "cpu_delta_seconds": None,
        "cpu_baseline_settled": None,
        "verdict_basis": "",
        "expectation_errors": [],
        "elapsed_seconds": 0.0,
        "timeout_seconds": args.timeout,
    }
    process = None
    hwnd = None

    def finish(code):
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return code

    if args.timeout <= 0 or args.wait < 0 or args.startup_timeout <= 0:
        result["errors"] = ["timeout and startup-timeout must be positive; wait must be non-negative"]
        return finish(2)
    # 空串断言是明确的假阳性来源：`"" in text` 恒为真，`re.search("", text)` 恒匹配，
    # 于是「什么都没断言」会被报成 `level=correct`。这在 shell 里很常见 ——
    # `--expect-serial "$EXPECT"` 的变量没设时就会传进一个空串。同理空的
    # `--serial-window` 会让所有窗口都成为串口候选。一律按参数错误拒绝。
    for flag, values in (
        ("--expect-serial", args.expect_serial),
        ("--expect-regex", args.expect_regex),
        ("--serial-window", args.serial_window),
    ):
        if any(not value.strip() for value in values):
            result["errors"] = [
                flag + " 不接受空字符串（会变成恒真的假阳性）；"
                "请给一段真实内容，或干脆不要传这个参数"
            ]
            return finish(2)
    # --wait >= --timeout 时观察窗口会被总时长上限截断，随后 timed_out 必然为真：
    # 一次跑得好好的仿真也会被报成 timeout（exit 1）。这里只提醒、不改变行为
    # （老命令行照旧），并把提醒写进 evidence，免得调用方对着 timeout 找不到原因。
    wait_exceeds_timeout = args.wait >= args.timeout
    if wait_exceeds_timeout:
        print(
            "WARN: --wait (%g s) >= --timeout (%g s); the observation window is cut short "
            "by the total timeout, so a healthy run may be reported as timeout. "
            "Keep --wait clearly below --timeout." % (args.wait, args.timeout),
            file=sys.stderr,
        )
    dsn = os.path.abspath(args.dsn)
    if not os.path.isfile(dsn):
        result["errors"] = [f"DSN not found: {dsn}"]
        return finish(2)
    isis_exe = os.environ.get("PROTEUS_ISIS_EXE") or ISIS_EXE
    if not isis_exe or not os.path.isfile(isis_exe):
        result["errors"] = ["ISIS_EXE not found; set PROTEUS_ISIS_EXE"]
        return finish(3)
    temp_dir = os.environ.get("PROTEUS_TEMP_DIR") or TEMP_DIR
    if not temp_dir:
        temp_dir = os.path.join(os.environ.get("TEMP", ""), "proteus-isis-agent-interface")
    if not temp_dir or not os.path.abspath(temp_dir).isascii():
        result["errors"] = ["temporary directory contains non-ASCII characters; set PROTEUS_TEMP_DIR"]
        return finish(3)
    os.makedirs(temp_dir, exist_ok=True)
    environment = os.environ.copy()
    environment["TEMP"] = temp_dir
    environment["TMP"] = temp_dir
    try:
        process = subprocess.Popen([isis_exe, dsn], env=environment)
        result["pid"] = process.pid
    except OSError as exc:
        result["errors"] = [f"launch failed: {exc}"]
        return finish(3)

    try:
        remaining = max(0.0, args.timeout - (time.monotonic() - started))
        hwnd = wait_main(process.pid, min(args.startup_timeout, remaining))
        if not hwnd:
            result["level"] = "timeout" if time.monotonic() - started >= args.timeout else "failed"
            result["errors"] = ["ISIS main window not found"]
            return finish(4)

        # 先等设计与渲染静止（此时还没按运行键），把 CPU 基线打在真正的静止点上；
        # 否则加载期的 CPU 增长会被误算成「仿真在跑」。
        settled, _idle_cpu = wait_cpu_idle(
            process.pid,
            max_wait=min(6.0, max(0.5, args.timeout - (time.monotonic() - started))),
        )
        result["cpu_baseline_settled"] = settled
        key(hwnd, ctrl=True)
        cpu_before = cpu_seconds(process.pid)
        observation_deadline = min(
            started + args.timeout, time.monotonic() + args.wait
        )
        serial_text = ""
        serial_method = "none"
        serial_candidates = 0
        serial_source = ""
        serial_sources = []
        while time.monotonic() < observation_deadline:
            (
                current_text,
                current_method,
                current_candidates,
                current_source,
                current_sources,
            ) = capture_serial(process.pid, args.serial_window or [], exclude_hwnd=hwnd)
            if len(current_text) >= len(serial_text):
                serial_text = current_text
                serial_method = current_method
                serial_candidates = current_candidates
                serial_source = current_source
                serial_sources = current_sources
            if args.expect_serial or args.expect_regex:
                matched, _ = _match_serial(serial_text, args.expect_serial, args.expect_regex)
                if matched:
                    break
            time.sleep(min(0.5, max(0.05, observation_deadline - time.monotonic())))
        cpu_after = cpu_seconds(process.pid)

        # 主窗口可能在运行期间被重建；先确认句柄有效，失效就重新找一次。
        if not user32.IsWindow(hwnd):
            refreshed = wait_main(process.pid, 1.0)
            if refreshed:
                hwnd = refreshed
        window_alive = bool(user32.IsWindow(hwnd)) and is_main_candidate(
            _title_of(hwnd), _class_of(hwnd)
        )
        result["window_alive"] = window_alive
        result["main_window"] = {
            "hwnd": int(hwnd),
            "title": _title_of(hwnd),
            "class": _class_of(hwnd),
        }

        log, status, _ = snapshot_text(hwnd)
        dialogs = dialog_texts(
            process.pid, exclude_hwnd=hwnd, extra_marks=args.serial_window or []
        )
        result["log_text"] = log[-12000:]
        result["status_text"] = status
        result["dialog_text"] = dialogs[-4000:]
        result["serial_text"] = serial_text[-12000:]
        result["serial_method"] = serial_method
        result["serial_candidates"] = serial_candidates
        result["serial_source"] = serial_source
        result["serial_sources"] = serial_sources
        dump_note = ""
        if args.dump_serial:
            # 落盘失败只影响附带的留痕，不能反过来改写这次仿真的判定结果；
            # 但也不能静默吞掉，写进 evidence 让调用方看得见。
            try:
                _write_serial(args.dump_serial, serial_text)
            except OSError as exc:
                dump_note = "; dump-serial failed: %s: %s" % (type(exc).__name__, exc)
                print(
                    "WARN: --dump-serial 写入失败: %s: %s" % (type(exc).__name__, exc),
                    file=sys.stderr,
                )

        # 错误弹窗也算失败证据：它的文字不在主窗口子控件树里。
        combined = result["log_text"] + "\n" + status + "\n" + dialogs
        failures = sorted(set(match.group(0) for match in FAIL_RE.finditer(combined)))
        result["errors"] = failures
        has_run = bool(RUN_RE.search(combined))
        loading = bool(LOAD_RE.search(status))
        cpu_delta = (
            cpu_after - cpu_before
            if cpu_before is not None and cpu_after is not None
            else 0.0
        )
        result["cpu_delta_seconds"] = round(cpu_delta, 3)
        active_fallback = cpu_delta >= 0.05 and window_alive
        # 主窗口必须存活：窗口没了就说明进程已经崩/退出，绝不能算在跑。
        # 另外：**非空状态栏本身不构成运行证据**。状态栏常显示坐标、提示语、
        # 网格设置这类与仿真无关的文字；把它当成「在跑」会让「没在跑」被判成
        # running=true。状态栏只有命中运行词表（已被 has_run 覆盖）才算数。
        result["running"] = bool(
            not failures
            and not loading
            and window_alive
            and (has_run or active_fallback)
        )
        # 如实标注这一票是凭什么下的：run_marker 是强证据（标题/状态栏写着在运行），
        # cpu_fallback 是弱证据（错误弹窗、窗口活动本身也会让 CPU 涨）。
        if failures:
            basis = "failure_marker"
        elif loading:
            basis = "loading"
        elif not window_alive:
            basis = "window_gone"
        elif has_run:
            basis = "run_marker"
        elif active_fallback:
            basis = "cpu_fallback"
        else:
            basis = "no_signal"
        result["verdict_basis"] = basis
        if not window_alive:
            reason = "main window gone (handle invalid)"
        elif failures:
            reason = "failure marker present"
        elif has_run:
            reason = "run marker in log/status"
        elif active_fallback:
            reason = "process CPU advanced %.3fs; ISIS window alive" % cpu_delta
        else:
            reason = "no run marker"
        result["evidence"] = (
            reason
            + "; "
            + ("failure marker present" if failures else "no failure markers")
            + "; status=" + (status or "<empty>")
            + "; cpu_delta=%.3fs; basis=%s; main_window=%s"
            % (cpu_delta, basis, _title_of(hwnd) or "<untitled>")
            + dump_note
        )
        timed_out = time.monotonic() >= started + args.timeout
        if timed_out:
            result["level"] = "timeout"
            if wait_exceeds_timeout:
                result["evidence"] += "; note=--wait >= --timeout cut the run short"
        elif not result["running"]:
            result["level"] = "failed"
        elif args.expect_serial or args.expect_regex:
            serial_match, expectation_errors = _match_serial(
                serial_text, args.expect_serial, args.expect_regex
            )
            result["serial_match"] = serial_match
            result["expectation_errors"] = expectation_errors
            result["level"] = "correct" if serial_match else "failed"
            result["evidence"] += "; serial=" + (
                "matched" if serial_match else "not matched"
            ) + (" via " + serial_source if serial_source else "; no text source found")
        else:
            result["level"] = "alive"
    except Exception as exc:
        result["errors"].append(f"verification exception: {exc}")
        result["level"] = "failed"
    finally:
        if result["level"] in ("failed", "timeout") and hwnd:
            evidence_dir = args.evidence_dir or os.path.join(
                os.path.dirname(dsn), "verify_evidence"
            )
            try:
                _write_failure_evidence(
                    evidence_dir,
                    result["log_text"],
                    result["status_text"],
                    hwnd,
                )
            except Exception:
                pass
        if process:
            _close_process(process, hwnd, args.keep)
    return finish(0 if result["level"] in ("alive", "correct") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
