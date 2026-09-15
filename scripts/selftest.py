# -*- coding: utf-8 -*-
"""离线自检：零外部依赖、退出码可信。

本仓库的脚本分两类：
  * 纯标准库：build51.py、run_batch.py——任何装了 Python 3 的机器都能直接测。
  * 面向 Windows 桌面自动化：proteus_ctl.py、verify.py——可选依赖 pywin32 + Pillow，
    两个文件都在顶部用 try/except 做了降级（缺依赖时把 win32*/Image 置为 None）。

本自检只 import Python 标准库，在 Windows 与 Linux 上都能跑，
**不需要** Proteus、Keil、pywin32、Pillow，也不需要图形界面。

三条硬规则：
1. 零外部依赖：只用标准库。
2. 失败必须非 0：任何一条断言不成立，进程退出码就是 1。
   `--prove-failure` 用来证明这一点——它跑一条故意失败的断言，退出码必须是 1，
   所以这个闸门不是「恒绿」的摆设。
3. 不允许「跳过即通过」：每条检查要么真跑并断言，要么被显式标成 UNAVAILABLE
   并把原因原样打印；UNAVAILABLE 从不计入通过数。`--strict` 会把 UNAVAILABLE
   直接升级为失败（Windows 开发机建议用 --strict）。

退出码：0 = 全部断言通过；1 = 有断言失败（或 --strict 下有不可用项）；2 = 用法错误。
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ("build51.py", "run_batch.py", "proteus_ctl.py", "verify.py")

# 允许被「模拟缺失」的可选依赖：它们缺失时 proteus_ctl / verify 必须降级而不是崩掉。
OPTIONAL_DEPS = ("win32api", "win32gui", "win32ui", "PIL")

# 环境中立的能力缺失原因：命中则记 UNAVAILABLE（不是通过）。
ENV_CAUSE_MARKERS = (
    "ctypes.wintypes",
    "wintypes",
    "No module named 'win32",
    "No module named 'PIL'",
    "not supported",
)

if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _enable_utf8_output():
    """把 stdout/stderr 切到 UTF-8。

    Windows 的默认控制台编码是 GBK，脚本里凡是打印非 GBK 字符（例如
    `_decode(b"\xff")` 的 repr）都会抛 UnicodeEncodeError 把自检自己搞崩——
    那会变成一个假的「失败」。这里先切成 UTF-8，让输出与断言结果无关。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老解释器没有 reconfigure 就退化为下面的兜底
            pass


def _emit(line):
    """打印一行；万一编码仍然不行，就退化成 ASCII 转义，绝不因打印而崩。"""
    try:
        print(line)
    except UnicodeEncodeError:
        print(str(line).encode("ascii", "backslashreplace").decode("ascii"))


class _Result(object):
    """一次自检的账本：通过 / 失败 / 不可用三类分开记，绝不混算。"""

    def __init__(self):
        self.passed = []
        self.failed = []
        self.unavailable = []

    def check(self, name, ok, detail=""):
        if ok:
            self.passed.append(name)
            _emit("PASS  {}{}".format(name, (" - " + detail) if detail else ""))
        else:
            self.failed.append(name)
            _emit("FAIL  {}{}".format(name, (" - " + detail) if detail else ""))
        return bool(ok)

    def equal(self, name, actual, expected):
        return self.check(
            name, actual == expected, "expected {!r}, got {!r}".format(expected, actual)
        )

    def unavailable(self, name, reason):
        self.unavailable.append((name, reason))
        _emit("UNAVAILABLE  {} - {}".format(name, reason))


class _BlockModules(object):
    """import 钩子：让指定顶层模块 import 失败，用来模拟「本机没装这些依赖」。

    这是本自检的关键手段——它让「缺依赖时会不会降级」这件事在本机就能真验证，
    而不是靠假设。
    """

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError("blocked by selftest (simulated missing dependency): " + fullname)
        return None


def _fresh_import(module_name, blocked=()):
    """在「blocked 里的依赖不可用」的前提下重新 import 一个脚本模块。

    返回 (module, None) 或 (None, 异常)。会先清掉 sys.modules 里的缓存，
    保证真的重跑一次模块顶部的 import 逻辑。
    """
    for key in list(sys.modules):
        if key == module_name or key.startswith(module_name + "."):
            sys.modules.pop(key, None)
    for key in ("proteus_ctl", "verify"):
        sys.modules.pop(key, None)

    hook = _BlockModules(blocked) if blocked else None
    if hook is not None:
        sys.meta_path.insert(0, hook)
    try:
        module = __import__(module_name)
        return module, None
    except Exception as exc:  # noqa: BLE001 - 这里就是要看清是哪种失败
        return None, exc
    finally:
        if hook is not None:
            try:
                sys.meta_path.remove(hook)
            except ValueError:
                pass


def _is_env_cause(exc):
    text = "{}: {}".format(type(exc).__name__, exc)
    return any(marker in text for marker in ENV_CAUSE_MARKERS)


def check_syntax(result):
    """语法闸门：逐文件真跑 py_compile，取子进程自己的退出码。"""
    for name in SCRIPTS:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            result.check("syntax:" + name, False, "missing file " + path)
            continue
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True,
            text=True,
        )
        result.check(
            "syntax:" + name,
            proc.returncode == 0,
            "exit={} {}".format(proc.returncode, (proc.stderr or "").strip()),
        )


def check_build51(result):
    """build51 的纯逻辑：解码兜底、Keil/SDCC 错误计数、警告不算失败。"""
    module, exc = _fresh_import("build51")
    if module is None:
        result.unavailable("logic:build51", "import failed: {}: {}".format(type(exc).__name__, exc))
        return

    result.equal("build51._decode(str passthrough)", module._decode("abc"), "abc")
    result.equal("build51._decode(utf-8 bytes)", module._decode("中文".encode("utf-8")), "中文")
    result.check(
        "build51._decode(非法字节不抛错)",
        isinstance(module._decode(b"\xff\xfe\x00"), str),
        repr(module._decode(b"\xff\xfe\x00")),
    )
    result.equal("build51._count_keil_errors(0)", module._count_keil_errors("0 Error(s)"), 0)
    result.equal("build51._count_keil_errors(3)", module._count_keil_errors("3 ERROR(S)"), 3)
    result.equal("build51._count_keil_warnings(2)", module._count_keil_warnings("2 WARNING(S)"), 2)
    result.check(
        "build51._has_error_summary", module._has_error_summary("1 WARNING(S), 0 ERROR(S)")
    )
    # 这条是本仓库最重要的口径：只有警告时 Keil 也会返回非零退出码，但那不算失败。
    result.check(
        "build51._keil_failed(只有警告 -> 不算失败)",
        module._keil_failed(1, "1 WARNING(S), 0 ERROR(S)", 0) is False,
        repr(module._keil_failed(1, "1 WARNING(S), 0 ERROR(S)", 0)),
    )
    result.check(
        "build51._keil_failed(有错误 -> 失败)",
        module._keil_failed(1, "1 ERROR(S)", 1) is True,
    )
    result.check(
        "build51._keil_failed(非零且无汇总 -> 失败)",
        module._keil_failed(1, "tool did not start", 0) is True,
    )
    result.equal(
        "build51._count_sdcc_errors(带编号)", module._count_sdcc_errors("a.c:18: error 101: x"), 1
    )
    result.equal(
        "build51._count_sdcc_errors(fatal error)",
        module._count_sdcc_errors("fatal error: reg52.h: No such file or directory"),
        1,
    )
    result.equal(
        "build51._count_sdcc_warnings", module._count_sdcc_warnings("a.c:18: warning 356: y"), 1
    )


def check_run_batch(result):
    """run_batch 的清单解析与汇总纯逻辑（含「写错秒数不崩栈」的既有修复）。"""
    module, exc = _fresh_import("run_batch")
    if module is None:
        result.unavailable("logic:run_batch", "import failed: {}: {}".format(type(exc).__name__, exc))
        return

    result.equal("run_batch._value(去空白)", module._value({"a": "  x "}, "a"), "x")
    result.equal("run_batch._value(缺字段)", module._value({}, "a", "b"), "")
    result.equal("run_batch._list_value(JSON 数组)", module._list_value("[1,2]"), ["1", "2"])
    result.equal("run_batch._list_value(竖线分隔)", module._list_value("a|b"), ["a", "b"])
    result.equal("run_batch._list_value(空)", module._list_value(""), [])
    result.equal("run_batch._seconds(合法)", module._seconds("30", "timeout", 1), "30")
    try:
        module._seconds("30s", "timeout", 1)
        result.check("run_batch._seconds('30s' 必须报错)", False, "没有抛 ValueError")
    except ValueError:
        result.check("run_batch._seconds('30s' 必须报错)", True)
    except Exception as exc2:  # noqa: BLE001
        result.check("run_batch._seconds('30s' 必须报错)", False, "抛了别的异常 " + repr(exc2))
    try:
        module._seconds("-1", "timeout", 1)
        result.check("run_batch._seconds(负数必须报错)", False, "没有抛 ValueError")
    except ValueError:
        result.check("run_batch._seconds(负数必须报错)", True)
    result.equal(
        "run_batch._parse_json_output(取末行 JSON)",
        module._parse_json_output('noise\n{"a": 1}\n'),
        {"a": 1},
    )
    result.equal("run_batch._parse_json_output(无 JSON -> None)", module._parse_json_output("noise"), None)
    result.equal("run_batch._safe_name(净化)", module._safe_name("a b/c"), "a_b_c")
    result.equal("run_batch._safe_name(空 -> task)", module._safe_name(""), "task")
    result.equal("run_batch._safe_json(list)", module._safe_json(["a", "b"]), "a; b")
    result.equal("run_batch._safe_json(None)", module._safe_json(None), "")
    command = []
    module._add_option(command, "--x", "")
    result.equal("run_batch._add_option(空值不加)", command, [])
    module._add_option(command, "--x", "1")
    result.equal("run_batch._add_option(有值才加)", command, ["--x", "1"])


def check_optional_dependency_degradation(result):
    """核心断言：把 pywin32/Pillow 全部屏蔽掉，两个 Windows 脚本仍必须能 import 并降级。

    这直接证明「零外部依赖」不是声明，而是行为：缺依赖时它们不崩、不假装有依赖。
    """
    module, exc = _fresh_import("proteus_ctl", blocked=OPTIONAL_DEPS)
    if module is None:
        if _is_env_cause(exc):
            result.unavailable(
                "degrade:proteus_ctl",
                "本机环境无法 import（{}: {}）".format(type(exc).__name__, exc),
            )
        else:
            result.check(
                "degrade:proteus_ctl(屏蔽依赖后仍可 import)",
                False,
                "{}: {}".format(type(exc).__name__, exc),
            )
    else:
        result.check("degrade:proteus_ctl(屏蔽依赖后仍可 import)", True)
        result.check(
            "degrade:proteus_ctl.win32gui 降级为 None",
            module.win32gui is None,
            repr(module.win32gui),
        )
        result.check("degrade:proteus_ctl.Image 降级为 None", module.Image is None, repr(module.Image))
        result.check(
            "degrade:proteus_ctl 仍保留窗口特征常量",
            isinstance(getattr(module, "CLASS_CANDIDATES", None), (list, tuple))
            and len(module.CLASS_CANDIDATES) > 0,
        )

    module, exc = _fresh_import("verify", blocked=OPTIONAL_DEPS)
    if module is None:
        if _is_env_cause(exc):
            result.unavailable(
                "degrade:verify", "本机环境无法 import（{}: {}）".format(type(exc).__name__, exc)
            )
        else:
            result.check(
                "degrade:verify(屏蔽依赖后仍可 import)", False, "{}: {}".format(type(exc).__name__, exc)
            )
    else:
        result.check("degrade:verify(屏蔽依赖后仍可 import)", True)
        # verify 单独拷走 proteus_ctl 时必须有内置副本，这里验证兜底真的生效。
        result.check(
            "degrade:verify 有内置窗口特征兜底",
            isinstance(module._CLASS_CANDIDATES, (list, tuple))
            and isinstance(module._TITLE_CANDIDATES, (list, tuple))
            and len(module._TITLE_CANDIDATES) > 0,
        )
        # 这两条是本仓库真实修过的假通过 bug 的回归断言。
        result.check(
            "verify._has_serial_mark(文件名含 uart 不算串口标题)",
            module._has_serial_mark("softuart.DSN - ISIS Professional") is False,
            repr(module._has_serial_mark("softuart.DSN - ISIS Professional")),
        )
        result.check(
            "verify._has_serial_mark(空白关键字被忽略)",
            module._has_serial_mark("随便什么标题", ["", "   "]) is False,
        )
        ok, failures = module._match_serial("GOOD", ["GOOD"], [])
        result.check("verify._match_serial(命中)", ok is True and failures == [], repr(failures))
        ok, failures = module._match_serial("GOOD", ["MISSING"], [])
        result.check("verify._match_serial(未命中 -> 失败)", ok is False and len(failures) == 1)
        ok, failures = module._match_serial("GOOD", [], ["["])
        result.check("verify._match_serial(非法正则 -> 失败而非崩栈)", ok is False and len(failures) == 1)


def check_cli_help(result):
    """CLI 契约：四个脚本的 --help 都必须能跑，且退出码为 0。

    这条在 Windows 与 Linux 上都成立（缺 pywin32/Pillow 时脚本会自行降级），
    所以它是真正的命令行闸门，而不是平台相关的摆设。
    """
    for name in SCRIPTS:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            result.check("cli:" + name + " --help", False, "missing file " + path)
            continue
        proc = subprocess.run(
            [sys.executable, path, "--help"], capture_output=True, text=True
        )
        if proc.returncode == 0:
            result.check("cli:" + name + " --help", True, "exit=0")
        elif _is_env_cause(proc.stderr or ""):
            result.unavailable(
                "cli:" + name + " --help", "环境不支持：" + (proc.stderr or "").strip()[:160]
            )
        else:
            result.check(
                "cli:" + name + " --help",
                False,
                "exit={} {}".format(proc.returncode, (proc.stderr or "").strip()[:200]),
            )


def run_all(result):
    check_syntax(result)
    check_build51(result)
    check_run_batch(result)
    check_optional_dependency_degradation(result)
    check_cli_help(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线自检（零外部依赖）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把 UNAVAILABLE（环境不支持）也当成失败",
    )
    parser.add_argument(
        "--prove-failure",
        action="store_true",
        help="跑一条故意失败的断言，用来证明失败时退出码确实是 1",
    )
    args = parser.parse_args(argv)

    _enable_utf8_output()
    result = _Result()

    if args.prove_failure:
        # 故意失败：证明这个闸门不是恒绿的。
        result.check("intentional-failure(for exit-code proof)", False, "deliberate")
    else:
        run_all(result)

    _emit("")
    _emit("verified: {} assertion(s) passed".format(len(result.passed)))
    _emit("unavailable: {} (not counted as passed)".format(len(result.unavailable)))
    for name, reason in result.unavailable:
        _emit("  - {}: {}".format(name, reason))
    _emit("failed: {}".format(len(result.failed)))
    for name in result.failed:
        _emit("  - {}".format(name))

    if result.failed:
        _emit("RESULT: FAIL")
        return 1
    if args.strict and result.unavailable:
        _emit("RESULT: FAIL (--strict: UNAVAILABLE counts as failure)")
        return 1
    _emit("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
