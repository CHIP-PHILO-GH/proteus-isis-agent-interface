# -*- coding: utf-8 -*-
"""批量运行 Proteus 仿真验证任务。

输入支持 JSON 与 CSV。每个任务都会复制 DSN 所在目录到独立临时目录，
再把指定 HEX 放入副本后调用 verify.py；失败或超时不会阻断后续任务。
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


VERIFY_SCRIPT = Path(__file__).with_name("verify.py")


def _value(row, *names):
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _list_value(value):
    value = str(value or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            decoded = json.loads(value)
            return [str(item) for item in decoded]
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in value.split("|") if item.strip()]


def _seconds(value, field, index, allow_zero=True):
    """把清单里的秒数字段收敛成字符串；写错就给出可读的报错而不是崩栈。

    这些字段以前一路传到 `float()`，清单里写成 `"30s"` 就会抛未捕获的
    ValueError，整批任务连同已跑完的结果一起丢掉（实测直接打印 traceback）。
    批改场景下一次崩栈意味着前面跑过的作业白跑，所以这里在读清单阶段就拦下。
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(
            "第 {} 项的 {} 不是数字：{!r}（只写秒数，例如 30）".format(index, field, raw)
        )
    if number < 0 or (number == 0 and not allow_zero):
        raise ValueError(
            "第 {} 项的 {} 必须{}：{}".format(
                index, field, "大于 0" if not allow_zero else "不小于 0", raw
            )
        )
    return raw


def load_tasks(path):
    path = Path(path).resolve()
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for row_number, row in enumerate(reader, 2):
                # 列数不匹配时 csv 会静默丢列/塞进 None 键，导致断言被张冠李戴。
                # 批改场景下这种静默错位比直接报错危险得多，所以这里硬性拦下。
                extras = row.pop(None, None)
                if extras or any(value is None for value in row.values()):
                    raise ValueError(
                        "CSV 第 {} 行列数与表头不一致（表头 {} 列：{}）；"
                        "请补齐字段或删掉多余的分隔符".format(
                            row_number,
                            len(reader.fieldnames or []),
                            ",".join(reader.fieldnames or []),
                        )
                    )
                rows.append(row)
    else:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload.get("tasks", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("任务清单必须是数组，或包含 tasks 数组的对象")
    tasks = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 项不是对象")
        dsn = _value(row, "dsn", "circuit")
        if not dsn:
            raise ValueError(f"第 {index} 项缺少 dsn/circuit")
        tasks.append(
            {
                "id": _value(row, "id", "name") or str(index),
                "dsn": Path(dsn).expanduser().resolve(),
                "hex": Path(_value(row, "hex", "firmware")).expanduser().resolve()
                if _value(row, "hex", "firmware")
                else None,
                "target_hex": _value(row, "target_hex", "hex_name"),
                "expect_serial": _list_value(
                    _value(row, "expect_serial", "expect")
                ),
                "expect_regex": _list_value(_value(row, "expect_regex", "regex")),
                "wait": _seconds(_value(row, "wait"), "wait", index),
                "startup_timeout": _seconds(
                    _value(row, "startup_timeout"), "startup_timeout", index, allow_zero=False
                ),
                "timeout": _seconds(
                    _value(row, "timeout"), "timeout", index, allow_zero=False
                ),
            }
        )
    return tasks


def _add_option(command, name, value):
    if value:
        command.extend([name, value])


def _parse_json_output(stdout):
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _safe_name(value):
    """把任务 id 收敛成可用作目录名的 ASCII 安全片段。"""
    cleaned = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "")).strip("._")
    return cleaned[:48] or "task"


def _run_one(task, index, temp_root, default_timeout, evidence_root=None):
    dsn = task["dsn"]
    if not dsn.is_file():
        return {
            "index": index,
            "id": task["id"],
            "dsn": str(dsn),
            "level": "failed",
            "running": False,
            "serial_match": None,
            "elapsed_seconds": 0.0,
            "errors": ["DSN not found: " + str(dsn)],
            "expectation_errors": [],
            "returncode": None,
            "evidence_dir": "",
        }
    if task["hex"] and not task["hex"].is_file():
        return {
            "index": index,
            "id": task["id"],
            "dsn": str(dsn),
            "level": "failed",
            "running": False,
            "serial_match": None,
            "elapsed_seconds": 0.0,
            "errors": ["HEX not found: " + str(task["hex"])],
            "expectation_errors": [],
            "returncode": None,
            "evidence_dir": "",
        }

    stage = Path(temp_root) / f"task-{index:04d}"
    shutil.copytree(dsn.parent, stage)
    staged_dsn = stage / dsn.name
    if task["hex"]:
        target_name = task["target_hex"] or task["hex"].name
        shutil.copy2(task["hex"], stage / target_name)

    # 失败现场与串口抓取必须落在临时目录之外，否则默认清理后就查不到了。
    evidence_dir = ""
    if evidence_root:
        evidence_dir = str(
            Path(evidence_root) / f"{index:04d}-{_safe_name(task['id'])}"
        )

    timeout_value = task["timeout"] or str(default_timeout)
    # 一律带 -B：verify.py 会 import proteus_ctl，不禁止写字节码就会在
    # scripts/ 下生成 __pycache__，把二进制 .pyc 带进包里。
    command = [sys.executable, "-B", str(VERIFY_SCRIPT), "--dsn", str(staged_dsn)]
    _add_option(command, "--wait", task["wait"])
    _add_option(command, "--startup-timeout", task["startup_timeout"])
    _add_option(command, "--timeout", timeout_value)
    for expected in task["expect_serial"]:
        command.extend(["--expect-serial", expected])
    for expression in task["expect_regex"]:
        command.extend(["--expect-regex", expression])
    if evidence_dir:
        # 目录由 verify.py 按需创建；这里不预建，避免留下空目录。
        command.extend(["--evidence-dir", evidence_dir])
        command.extend(["--dump-serial", str(Path(evidence_dir) / "serial.txt")])

    try:
        child_env = os.environ.copy()
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(10.0, float(timeout_value) + 8.0),
            env=child_env,
        )
        parsed = _parse_json_output(completed.stdout)
        if parsed is None:
            return {
                "index": index,
                "id": task["id"],
                "dsn": str(dsn),
                "level": "failed",
                "running": False,
                "serial_match": None,
                "elapsed_seconds": 0.0,
                "errors": ["verify.py 未输出可解析 JSON"],
                "expectation_errors": [],
                "returncode": completed.returncode,
                "evidence_dir": evidence_dir,
            }
        parsed.update(
            {"index": index, "id": task["id"], "dsn": str(dsn), "evidence_dir": evidence_dir}
        )
        parsed["returncode"] = completed.returncode
        return parsed
    except subprocess.TimeoutExpired:
        return {
            "index": index,
            "id": task["id"],
            "dsn": str(dsn),
            "level": "timeout",
            "running": False,
            "serial_match": None,
            "elapsed_seconds": float(timeout_value),
            "errors": ["批量任务超出外层超时"],
            "expectation_errors": [],
            "returncode": None,
            "evidence_dir": evidence_dir,
        }


def _safe_json(value):
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return "" if value is None else str(value)


def write_csv(path, results):
    fields = [
        "index",
        "id",
        "dsn",
        "level",
        "verdict_basis",
        "running",
        "serial_match",
        "elapsed_seconds",
        "returncode",
        "errors",
        "expectation_errors",
        "evidence_dir",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            row = {field: _safe_json(result.get(field)) for field in fields}
            writer.writerow(row)


def write_markdown(path, results, input_path, evidence_root=None):
    counts = {}
    for result in results:
        level = result.get("level", "failed")
        counts[level] = counts.get(level, 0) + 1
    summary = ", ".join(f"{key}={counts[key]}" for key in sorted(counts))
    lines = [
        "# Proteus 批量仿真汇总",
        "",
        f"- 任务清单：`{input_path}`",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 结果计数：{summary or '无任务'}",
    ]
    if evidence_root:
        lines.append(f"- 失败现场与串口抓取：`{evidence_root}`")
    lines.extend(
        [
            "",
            "| 序号 | 任务 | 判定 | running | 串口断言 | 耗时（秒） | 错误 |",
            "|---:|---|---|---|---|---:|---|",
        ]
    )
    for result in results:
        errors = _safe_json(result.get("errors")) or _safe_json(
            result.get("expectation_errors")
        )
        lines.append(
            "| {index} | {id} | {level} | {running} | {serial_match} | {elapsed} | {errors} |".format(
                index=result.get("index", ""),
                id=result.get("id", ""),
                level=result.get("level", "failed"),
                running=result.get("running", False),
                serial_match=result.get("serial_match", ""),
                elapsed=result.get("elapsed_seconds", ""),
                errors=errors.replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## 判定含义",
            "",
            "- `correct`：仿真运行且串口断言满足。",
            "- `alive`：仿真运行，但未提供串口断言。",
            "- `failed`：启动、运行或断言失败。",
            "- `timeout`：达到任务超时上限。",
            "",
            "> 判定为 `failed` / `timeout` 的任务，其现场日志与截图在“失败现场与串口抓取”目录下",
            "> 以 `<序号>-<任务名>/` 为单位保存（`simulation.log.txt` / `simulation.png` / `serial.txt`）。",
            "",
        ]
    )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_list", help="JSON 或 CSV 任务清单")
    parser.add_argument("--report", help="Markdown 汇总路径")
    parser.add_argument("--csv", dest="csv_path", help="CSV 汇总路径")
    parser.add_argument("--timeout", type=float, default=45, help="未单独指定任务超时时的默认秒数")
    parser.add_argument(
        "--evidence-dir",
        help="失败现场与串口抓取的保存目录；默认是任务清单同目录下的 <清单名>.evidence",
    )
    parser.add_argument("--keep-temp", action="store_true", help="保留临时仿真副本供人工排查")
    parser.add_argument("--stop-on-error", action="store_true", help="首个失败后停止，不建议批改场景使用")
    args = parser.parse_args()
    try:
        tasks = load_tasks(args.task_list)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERR: 无法读取任务清单: {exc}")
        return 2
    if args.timeout <= 0:
        print("ERR: --timeout 必须为正数")
        return 2

    input_path = Path(args.task_list).resolve()
    report_path = Path(args.report).resolve() if args.report else input_path.with_suffix(".report.md")
    csv_path = Path(args.csv_path).resolve() if args.csv_path else input_path.with_suffix(".report.csv")
    evidence_root = (
        Path(args.evidence_dir).resolve()
        if args.evidence_dir
        else input_path.with_name(input_path.stem + ".evidence")
    )
    temp_root = Path(tempfile.mkdtemp(prefix="proteus-batch-"))
    results = []
    try:
        for index, task in enumerate(tasks, 1):
            print(f"[{index}/{len(tasks)}] {task['id']}")
            result = _run_one(task, index, temp_root, args.timeout, evidence_root)
            results.append(result)
            print(
                f"  level={result.get('level', 'failed')} "
                f"running={result.get('running', False)} "
                f"elapsed={result.get('elapsed_seconds', 0)}"
            )
            if args.stop_on_error and result.get("level") not in ("alive", "correct"):
                break
        report_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report_path, results, input_path, evidence_root)
        write_csv(csv_path, results)
        print(f"Markdown 汇总: {report_path}")
        print(f"CSV 汇总: {csv_path}")
        # 只有真的写了现场才报路径：DSN/HEX 不存在这类开跑前就失败的，
        # 没有现场可存，报一个不存在的目录会让人白找。
        if evidence_root.exists():
            print(f"失败现场目录: {evidence_root}")
    finally:
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            print(f"临时副本保留: {temp_root}")
    return 0 if all(result.get("level") in ("alive", "correct") for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
