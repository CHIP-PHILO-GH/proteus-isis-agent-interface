# Proteus ISIS 自动化接口工具集

提供 51 工程编译封装、Proteus ISIS 窗口控制、批量任务和确定性验证脚本。

## Audience
Developers, students, and maintainers working with 8051/C51 projects.

## Prerequisites
- Python 3.9+; offline entry points use the standard library only.
- Windows and a local Proteus ISIS installation.

## Usage
Run from the repository root:

    python scripts/proteus_ctl.py --probe

Configure tool paths with command-line options or environment variables; do not commit absolute paths.

## Layout
- `scripts/`: scripts.
- `references/`: design, porting, and verification notes.
- `templates/`: project templates, when present.
- `examples/`: examples, when present.

## Limits
需要 Windows、Proteus ISIS 及可用工程；CI 仅检查 Python 语法，不启动桌面程序。
Generated HEX files, images, archives, and proprietary installers are excluded.

## Tests
    python -m compileall -q scripts

## License
MIT, Copyright (c) 2026 CHIP-PHILO-GH.
