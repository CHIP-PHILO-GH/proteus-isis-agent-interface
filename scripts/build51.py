# -*- coding: utf-8 -*-
"""51 单片机命令行编译封装。

支持单文件 Keil C51、单文件 SDCC，以及 Keil UV4 工程模式。默认后端为
auto：优先使用 KEIL_DIR，其次兼容旧的 KEIL_C51_BIN，最后探测 SDCC。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time


def _decode(data):
    if isinstance(data, str):
        return data
    for encoding in ("utf-8", "gbk", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run(cmd, cwd=None):
    """执行命令，返回 (returncode, stdout)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, cwd=cwd)
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, _decode(p.stdout or b"") + _decode(p.stderr or b"")


def _keil_tools(root=None):
    root = root or os.environ.get("KEIL_DIR") or os.environ.get("KEIL_C51_BIN")
    if not root:
        return None
    return {
        "root": root,
        "c51": os.path.join(root, r"C51\BIN\C51.exe"),
        "bl51": os.path.join(root, r"C51\BIN\BL51.exe"),
        "oh51": os.path.join(root, r"C51\BIN\OH51.exe"),
        "uv4": os.path.join(root, r"UV4\Uv4.exe"),
    }


def _resolve_executable(root, names):
    candidates = []
    if root:
        root = os.path.abspath(root)
        for name in names:
            candidates.extend(
                [os.path.join(root, name), os.path.join(root, "bin", name)]
            )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _sdcc_tools():
    root = os.environ.get("SDCC_DIR")
    sdcc = _resolve_executable(root, ("sdcc.exe", "sdcc"))
    if not sdcc:
        return None
    packihx = _resolve_executable(root, ("packihx.exe", "packihx"))
    return {"root": root, "sdcc": sdcc, "packihx": packihx}


def inspect_keil(project=False):
    """只读检查 Keil：返回 (工具路径字典或 None, 错误信息)。

    单文件模式只需要 C51/BL51/OH51；只有工程模式才要求 Uv4.exe。
    这样只装了命令行工具链、没装 IDE 的用户仍然能编译单文件。
    """
    tools = _keil_tools()
    if not tools:
        return None, "未设置 KEIL_DIR（兼容变量 KEIL_C51_BIN 也可用）"
    needed = ["c51", "bl51", "oh51"] + (["uv4"] if project else [])
    missing = [tools[name] for name in needed if not os.path.isfile(tools[name])]
    if missing:
        return tools, "Keil 工具缺失: " + ", ".join(missing)
    return tools, None


def detect_toolchain(requested="auto", project=False):
    """返回 (名称, 工具信息, 错误信息)。"""
    if requested in ("auto", "keil"):
        tools, keil_error = inspect_keil(project=project)
        if not keil_error:
            return "keil", tools, None
        if requested == "keil":
            return None, None, keil_error
    else:
        keil_error = ""

    if requested == "sdcc":
        if project:
            return None, None, "SDCC 后端只支持单文件模式，工程模式请使用 Keil"
        tools = _sdcc_tools()
        if not tools:
            return None, None, "未找到 SDCC，请设置 SDCC_DIR 或把 sdcc 放进 PATH"
        return "sdcc", tools, None

    if project:
        return None, None, "工程模式需要 Keil：" + keil_error
    sdcc = _sdcc_tools()
    if sdcc:
        return "sdcc", sdcc, None
    return None, None, "未找到可用编译后端；请设置 KEIL_DIR/SDCC_DIR 或配置 PATH"


def tool_report():
    """只读探测本机可用的 51 编译后端，返回可直接 JSON 序列化的字典。"""
    keil_tools, keil_error = inspect_keil(project=False)
    keil_info = None
    if keil_tools:
        keil_info = {
            "root": keil_tools["root"],
            "c51": keil_tools["c51"],
            "c51_found": os.path.isfile(keil_tools["c51"]),
            "bl51": keil_tools["bl51"],
            "bl51_found": os.path.isfile(keil_tools["bl51"]),
            "oh51": keil_tools["oh51"],
            "oh51_found": os.path.isfile(keil_tools["oh51"]),
            "uv4": keil_tools["uv4"],
            "uv4_found": os.path.isfile(keil_tools["uv4"]),
            "single_file_ready": keil_error is None,
            "project_ready": bool(keil_tools) and os.path.isfile(keil_tools["uv4"]),
            "error": keil_error,
        }
    sdcc_tools = _sdcc_tools()
    sdcc_info = None
    if sdcc_tools:
        sdcc_info = {
            "root": sdcc_tools.get("root") or "",
            "sdcc": sdcc_tools["sdcc"],
            "packihx": sdcc_tools.get("packihx") or "",
            "packihx_found": bool(sdcc_tools.get("packihx")),
            "single_file_ready": True,
        }
    selected, _tools, error = detect_toolchain("auto")
    return {
        "keil": keil_info,
        "sdcc": sdcc_info,
        "selected": selected,
        "error": error,
        "env": {
            "KEIL_DIR": os.environ.get("KEIL_DIR") or "",
            "KEIL_C51_BIN": os.environ.get("KEIL_C51_BIN") or "",
            "SDCC_DIR": os.environ.get("SDCC_DIR") or "",
        },
    }


def _count_keil_errors(output):
    matches = re.findall(r"(\d+)\s+ERROR\s*\(S\)", output, re.I)
    return int(matches[-1]) if matches else 0


def _has_error_summary(output):
    return bool(re.search(r"\d+\s+ERROR\s*\(S\)", output, re.I))


def _keil_failed(rc, output, errors):
    """判断 Keil 某一阶段是否真的失败。

    **不能只看退出码**：Keil 命令行工具在只有**警告**时也会返回非零退出码
    （实测 BL51 输出 `1 WARNING(S), 0 ERROR(S)` 时退出码就是 1）。若按
    `rc != 0` 判失败，一份只有链接警告的合法源码会被报成编译失败、拿不到 hex，
    这与本包「0 Error 即通过、只有警告不算失败」的口径冲突。
    规则：只要输出里有 `N ERROR(S)` 汇总就以错误数为准；退出码非零且连汇总都
    没有（工具根本没启动、输出不可解析）才算失败。
    """
    if errors:
        return True
    return rc != 0 and not _has_error_summary(output)


def _count_keil_warnings(output):
    matches = re.findall(r"(\d+)\s+WARNING\s*\(S\)", output, re.I)
    return int(matches[-1]) if matches else 0


def _count_sdcc_errors(output):
    """SDCC 实际输出形如 `file.c:18: error 101: ...`，编号在 error 与冒号之间。

    缺头文件一类会打印 `fatal error: xxx.h: No such file or directory`，语法错误打印
    `file.c:24: syntax error: ...`，这两种都没有编号，也要算作错误，否则会把错误数
    报成 0。
    """
    return len(
        re.findall(r"error\s+\d+\s*:|\bfatal\s+error\b|\bsyntax\s+error\b", output, re.I)
    )


def _count_sdcc_warnings(output):
    """SDCC 实际输出形如 `file.c:18: warning 356: ...`。"""
    return len(re.findall(r"\bwarning\s+\d+\s*:", output, re.I))


def _hex_data_bytes(path):
    total = 0
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line.startswith(":") or len(line) < 11:
                    continue
                count = int(line[1:3], 16)
                record_type = int(line[7:9], 16)
                if record_type == 0:
                    total += count
    except (OSError, ValueError):
        return None
    return total


def _report_hex(path):
    if not path or not os.path.isfile(path):
        return False
    size = _hex_data_bytes(path)
    if size is None:
        print("ERR: HEX 文件无法按 Intel HEX 格式读取")
        return False
    print(f"HEX 数据字节数: {size}")
    return True


def build_single(c_file, toolchain="keil", tools=None):
    """编译单个 C 文件，返回 (返回码, hex 路径, 错误数, 警告数)。"""
    c_file = os.path.abspath(c_file)
    if not os.path.exists(c_file):
        print(f"ERR: 源文件不存在 {c_file}")
        return 1, None, 0, 0
    if toolchain == "sdcc":
        return build_single_sdcc(c_file, tools or _sdcc_tools())

    tools = tools or _keil_tools()
    workdir = os.path.dirname(c_file)
    base = os.path.splitext(os.path.basename(c_file))[0]
    rc, output = run([tools["c51"], c_file], cwd=workdir)
    print(output[-1200:])
    errors = _count_keil_errors(output)
    warnings = _count_keil_warnings(output)
    if _keil_failed(rc, output, errors):
        return 1, None, errors, warnings

    obj = os.path.join(workdir, base + ".OBJ")
    if not os.path.exists(obj):
        obj = os.path.join(workdir, base + ".obj")
    if not os.path.isfile(obj):
        print("ERR: C51 未产出 OBJ")
        return 1, None, 1, warnings
    rc, output = run(
        [tools["bl51"], obj, "RAMSIZE(256)", "IDATA(80H)"], cwd=workdir
    )
    print(output[-1200:])
    link_errors = _count_keil_errors(output)
    warnings += _count_keil_warnings(output)
    if _keil_failed(rc, output, link_errors):
        return 1, None, link_errors, warnings

    absfile = os.path.join(workdir, base.upper())
    rc, output = run([tools["oh51"], absfile], cwd=workdir)
    print(output[-800:])
    hex_path = os.path.join(workdir, base.upper() + ".hex")
    if not os.path.exists(hex_path):
        hex_path = os.path.join(workdir, base + ".hex")
    if not _report_hex(hex_path):
        print("ERR: 未产出 HEX")
        return 1, None, link_errors, warnings
    return 0, hex_path, link_errors, warnings


def build_single_sdcc(c_file, tools=None):
    """使用 SDCC 单文件生成 Intel HEX。"""
    tools = tools or _sdcc_tools()
    if not tools:
        print("ERR: 未找到 SDCC")
        return 1, None, 1, 0
    workdir = os.path.dirname(c_file)
    base = os.path.splitext(os.path.basename(c_file))[0]
    ihx_path = os.path.join(workdir, base + ".ihx")
    hex_path = os.path.join(workdir, base + ".hex")
    rc, output = run(
        [tools["sdcc"], "--model-small", "--out-fmt-ihx", "-o", ihx_path, c_file],
        cwd=workdir,
    )
    print(output[-1600:])
    errors = _count_sdcc_errors(output)
    warnings = _count_sdcc_warnings(output)
    if rc != 0 or errors > 0:
        return 1, None, errors or 1, warnings
    if not os.path.isfile(ihx_path):
        fallback = os.path.join(workdir, base + ".ihx")
        ihx_path = fallback if os.path.isfile(fallback) else ihx_path
    if not os.path.isfile(ihx_path):
        print("ERR: SDCC 未产出 IHX")
        return 1, None, 1, warnings

    if tools.get("packihx"):
        rc, packed = run([tools["packihx"], ihx_path], cwd=workdir)
        print(packed[-1200:])
        if rc == 0 and any(line.strip().startswith(":") for line in packed.splitlines()):
            with open(hex_path, "w", encoding="ascii", newline="\n") as handle:
                handle.write(packed)
        else:
            shutil.copyfile(ihx_path, hex_path)
    else:
        shutil.copyfile(ihx_path, hex_path)
    if not _report_hex(hex_path):
        print("ERR: 未产出有效 HEX")
        return 1, None, 1, warnings
    return 0, hex_path, errors, warnings


def build_project(proj_file, out_log=None, tools=None):
    """使用 UV4 -b 编译 Keil 工程。"""
    proj_file = os.path.abspath(proj_file)
    if not os.path.exists(proj_file):
        print(f"ERR: 工程不存在 {proj_file}")
        return 1, None, 0, 0
    tools = tools or _keil_tools()
    log = out_log or os.path.join(os.path.dirname(proj_file), "build.log")
    # UV4 没能真正跑起来时不会重写日志。若把上一次构建留下的日志读进来，
    # 「0 Error(s)」就会把「压根没编译」报成成功 —— 实测把 uv4 指向一个立即
    # 退出的程序，脚本照样返回退出码 0，并把上一次构建遗留的 hex 报成本次产物。
    # 所以先把旧日志清掉，UV4 跑完后没有新日志即判失败。
    try:
        if os.path.exists(log):
            os.remove(log)
    except OSError as exc:
        print(f"WARN: 无法删除旧日志 {log}: {exc}（将按文件时间判断新旧）")
    log_mtime_before = os.path.getmtime(log) if os.path.exists(log) else None
    started_at = time.time()
    try:
        process = subprocess.Popen(
            [tools["uv4"], "-b", proj_file, "-o", log, "-j0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        print(f"ERR: 无法启动 UV4: {exc}")
        return 1, None, 1, 0

    for _ in range(60):
        time.sleep(1)
        if os.path.exists(log) and os.path.getsize(log) > 0:
            break
        if process.poll() is not None:
            break
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    log_is_fresh = False
    if os.path.exists(log) and os.path.getsize(log) > 0:
        log_is_fresh = log_mtime_before is None or os.path.getmtime(log) > log_mtime_before
    if os.path.exists(log):
        with open(log, "rb") as handle:
            text = _decode(handle.read())
    else:
        text = ""
    print(text[-1500:])
    if not log_is_fresh:
        print(f"ERR: UV4 没有写出新的编译日志 {log}（UV4 可能没跑起来）")
        return 1, None, 0, 0

    def last_count(word):
        matches = re.findall(r"(\d+)\s+" + word, text, re.I)
        return int(matches[-1]) if matches else 0

    errors = last_count("Error")
    warnings = last_count("Warning")
    # 与单文件模式同一口径：只看错误数，警告不算失败。
    ok = errors == 0 and "0 Error" in text
    hexdir = os.path.dirname(proj_file)
    # 只认**这次构建新产出**的 hex。工程目录里往往留着上一次的 hex，按目录顺序
    # 取第一个会把旧产物报成本次结果（实测会挑到 aaa_stale.hex）。用文件时间过滤。
    fresh_hexes = []
    for name in os.listdir(hexdir):
        if not name.lower().endswith(".hex"):
            continue
        full = os.path.join(hexdir, name)
        try:
            if os.path.getmtime(full) >= started_at - 1:
                fresh_hexes.append(full)
        except OSError:
            continue
    fresh_hexes.sort(key=os.path.getmtime, reverse=True)
    hex_path = fresh_hexes[0] if fresh_hexes else None
    if hex_path:
        _report_hex(hex_path)
    else:
        print(
            "ERR: 本次构建没有产出新的 hex（UV4 可能没跑起来、工程缺少源文件，"
            "或工程把 hex 输出到了别的目录 —— 看 uvproj 里的 <OutputDirectory>）"
        )
    if not text:
        print("ERR: UV4 没有写出编译日志 " + log)
    return (0 if ok and hex_path else 1), hex_path, errors, warnings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", help="单文件 .c 路径")
    parser.add_argument("--proj", help=".uvproj 工程路径")
    parser.add_argument("--log", help="工程编译日志输出路径")
    parser.add_argument(
        "--toolchain",
        choices=("auto", "keil", "sdcc"),
        default="auto",
        help="编译后端；默认 auto，优先 Keil，找不到时使用 SDCC",
    )
    parser.add_argument(
        "--print-tools",
        action="store_true",
        help="只读探测本机可用的编译后端并打印单行 JSON，不编译",
    )
    args = parser.parse_args()

    if args.print_tools:
        report = tool_report()
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
        sys.exit(0 if report["selected"] else 2)

    if not args.file and not args.proj:
        print("用法: python build51.py <file.c> | --proj <file.uvproj>")
        sys.exit(2)
    if args.file and args.proj:
        print("ERR: file 与 --proj 只能二选一")
        sys.exit(2)
    # 源文件/工程不存在属于参数错误（退出码 2），与「编译出错」（退出码 1）区分开：
    # 自动化流水线靠退出码分流时，两者含义完全不同。
    if args.file and not os.path.isfile(os.path.abspath(args.file)):
        print(f"ERR: 源文件不存在 {os.path.abspath(args.file)}")
        sys.exit(2)
    if args.proj and not os.path.isfile(os.path.abspath(args.proj)):
        print(f"ERR: 工程不存在 {os.path.abspath(args.proj)}")
        sys.exit(2)

    name, tools, error = detect_toolchain(args.toolchain, project=bool(args.proj))
    if not name:
        print("ERR: " + error)
        sys.exit(2)
    print(f"编译后端: {name}")
    if args.proj:
        rc, hex_path, errors, warnings = build_project(args.proj, args.log, tools)
    else:
        rc, hex_path, errors, warnings = build_single(args.file, name, tools)
    print(f"\n=== 结果: errors={errors} warnings={warnings} hex={hex_path} ===")
    sys.exit(rc)


if __name__ == "__main__":
    main()
