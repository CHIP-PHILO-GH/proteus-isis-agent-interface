# Proteus ISIS 自动化接口工具集

只想要“照着做一遍”的封装版本（含安装、配置与排错表），见同名技能包 proteus-isis-agent-interface；本仓库是代码本体。

提供 51 工程编译封装、Proteus ISIS 窗口控制、批量任务和确定性验证脚本。

## 适用对象
需要阅读、修改或验证 8051/C51 项目的开发者、学生和维护者。

## 前置条件
- Python 3.9+；离线入口仅使用标准库。
- Windows 和本机安装的 Proteus ISIS。

## 使用
在仓库根目录执行：

    python scripts/proteus_ctl.py --probe

工具路径通过命令行参数或环境变量配置，不要提交绝对路径。

## 目录结构
- `scripts/`：脚本。
- `references/`：设计、移植和验证资料。
- `templates/`：工程模板（如有）。
- `examples/`：示例（如有）。

## 能力边界与限制
需要 Windows、Proteus ISIS 及可用工程；CI 仅检查 Python 语法，不启动桌面程序。
不包含生成的 HEX、图片、压缩包或专用软件安装包。

## 测试
    python -m compileall -q scripts

## 许可
MIT，Copyright (c) 2026 CHIP-PHILO-GH。

--probe 输出 {ok:true,windows:[...],state:probe-ok|no-window}，退出码 0 表示探测调用成功；
o-window 表示当前没有匹配窗口，兼容旧版 [] 的空窗口含义。

--probe 输出结构化 JSON：ok=true 表示探测调用成功，state=probe-ok 表示发现窗口，state=no-window 表示没有窗口，windows 为窗口数组；退出码 0 表示调用成功，兼容旧版空数组语义。
