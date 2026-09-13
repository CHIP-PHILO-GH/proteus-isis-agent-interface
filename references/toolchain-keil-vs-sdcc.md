# 编译后端：Keil C51 与 SDCC 的差异与切换

`scripts/build51.py` 支持两种编译后端。没有 Keil 的用户（或不想装商业软件的用户）用 SDCC 也能编译出 hex。

---

## 一、怎么选后端

```bash
python -B scripts/build51.py --print-tools          # 只读探测本机可用后端，不编译
python -B scripts/build51.py main.c                 # 默认 auto
python -B scripts/build51.py main.c --toolchain keil
python -B scripts/build51.py main.c --toolchain sdcc
```

| `--toolchain` | 行为 |
|---|---|
| `auto`（默认） | 先找 Keil，找不到 Keil 再用 SDCC |
| `keil` | 只用 Keil；找不到就报错退出（退出码 2） |
| `sdcc` | 只用 SDCC；配 `--proj` 会被拒绝（SDCC 只支持单文件模式） |

**环境变量**

| 变量 | 指向 | 说明 |
|---|---|---|
| `KEIL_DIR` | Keil 安装根目录 | 其下应有 `C51\BIN\C51.exe`、`C51\BIN\BL51.exe`、`C51\BIN\OH51.exe`、`UV4\Uv4.exe` |
| `KEIL_C51_BIN` | 同上 | 旧变量名，仍然兼容 |
| `SDCC_DIR` | SDCC 安装根目录 | 其下或 `bin\` 下有 `sdcc`、`packihx`；不设则从 `PATH` 找 |

**没有 SDCC 怎么装（免费，不需要管理员权限）**：从 SDCC 官网/SourceForge 项目页下载 Windows 版压缩包，解压到任意英文目录（例如 `<SDCC 安装目录>`），然后二选一 —— ① 把 `<SDCC 安装目录>\bin` 加进 `PATH`；② 或设 `SDCC_DIR=<SDCC 安装目录>`。之后跑 `python -B scripts/build51.py --print-tools`，`sdcc` 与 `packihx` 都应显示找到、`packihx_found=true`。**本包不会替你下载或安装任何东西**，也不需要联网。

**单文件模式只要求 C51/BL51/OH51**，只有工程模式（`--proj`）才要求 `Uv4.exe`。所以只装了命令行工具链、没装 IDE 的用户也能编译单文件。

`--print-tools` 会输出单行 JSON，写明每个后端解析到的路径、哪些工具存在、以及 `auto` 最终选中了谁。换机后先跑它，比盲猜省事：

```json
{"keil":{"root":"...","c51":"...","c51_found":true,"bl51_found":true,"oh51_found":true,
"uv4_found":true,"single_file_ready":true,"project_ready":true,"error":null},
"sdcc":{"root":"","sdcc":"...","packihx":"...","packihx_found":true,"single_file_ready":true},
"selected":"keil","error":null,"env":{"KEIL_DIR":"...","KEIL_C51_BIN":"","SDCC_DIR":""}}
```

---

## 二、实测记录

以下是**用本文件第三节那份骨架源码、逐字照抄**在两个后端上跑出来的结果（Windows + Keil C51 V9.00 + SDCC 4.6.0），不是设想。

**Keil 单文件模式**（`python -B scripts/build51.py dual.c --toolchain keil`）

```text
C51 COMPILATION COMPLETE.  0 WARNING(S),  0 ERROR(S)
LINK/LOCATE RUN COMPLETE.  0 WARNING(S),  0 ERROR(S)
OBJECT TO HEX FILE CONVERTER OH51 V2.6
GENERATING INTEL HEX FILE: ...\DUAL.hex
OBJECT TO HEX CONVERSION COMPLETED.
HEX 数据字节数: 40
=== 结果: errors=0 warnings=0 hex=...\DUAL.hex ===
```
hex 文件 171 字节，退出码 0。

**SDCC 单文件模式**（`python -B scripts/build51.py dual.c --toolchain sdcc`）

```text
packihx: read 17 lines, wrote 22: OK.
HEX 数据字节数: 197
=== 结果: errors=0 warnings=0 hex=...\dual.hex ===
```
hex 文件 719 字节，退出码 0。

**Keil 工程模式**（`python -B scripts/build51.py --proj template.uvproj`）

```text
"template" - 0 Error(s), 1 Warning(s).
HEX 数据字节数: 28
=== 结果: errors=0 warnings=1 hex=...\template.hex ===
```
退出码 0。那个 `1 Warning` 是链接器的 `WARNING L16: UNCALLED SEGMENT`（工程里有没被调用的段），**警告不影响出 hex，也不影响退出码**（见第四节第 5 条）。

**结论**：同一份源码在 Keil 与 SDCC 下都能编出 hex、都是 0 error；两边产物体积不同（40 / 197 数据字节），不要用体积推断功能差异。

**同一个 hex 能否在电路里真的跑起来（上一轮记录，本轮未复跑）**

把 SDCC 编出的 hex 按电路引用的固件名覆盖进电路副本，再跑 `verify.py`：

```text
level=alive, running=True, verdict_basis=run_marker,
cpu_delta_seconds=0.375, window_alive=True, errors=[],
main_window=<电路名> - ISIS Professional（仿真中......）
```

主窗口标题带「仿真中」= 仿真确实在跑。**结论：没有 Keil 的用户用 SDCC 能走通「编译 → 装进电路 → 判定在跑」整条链路。**（该条未在独立审查轮复跑。）

> 后续的独立审查轮里，**Keil** 编出的一份「固定输出已知文本」的固件确实按电路引用的固件名装进副本并跑通了整条链路（`level=correct`，见 `SKILL.md` 十二节 A8 与 `references/verifier-notes.md` 第八节）；但**上面这条 SDCC 产物进电路**的记录本身仍未复跑，仍属未复跑项。

---

## 三、语法差异：一份两边都能编译的写法

两边都用同一套 C 语法是不可能的，差异集中在**关键字拼写**上。用条件编译把它们收敛到一组宏，源码主体就完全共用。

| 要表达的事 | Keil C51 | SDCC |
|---|---|---|
| 头文件 | `#include <reg52.h>`（或 `reg51.h`、`at89x52.h`） | `#include <8051.h>`（也有 `<mcs51/8051.h>`） |
| 识别编译器 | `__C51__` 有定义 | `__SDCC` 有定义（`__SDCC_VERSION_MAJOR` 等可用） |
| 中断函数 | `void t0_isr(void) interrupt 1` | `void t0_isr(void) __interrupt(1)` |
| 位变量声明 | `sbit LED0 = P1^0;` | `__sbit __at (0x90) LED0;` |
| 绝对地址变量 | `xdata unsigned char v _at_ 0x1000;` | `__xdata __at (0x1000) unsigned char v;` |
| 代码空间数组 | `code unsigned char t[] = {...};` | `__code const unsigned char t[] = {...};` |
| 空操作 | `#include <intrins.h>` 后用 `_nop_()` | 没有 `_nop_()`，用 `__asm NOP __endasm` |
| 内存模型 | 由工程选项决定（默认 small） | 命令行 `--model-small`（本脚本已加） |

**可直接套用的骨架**（这份源码已实测在两个后端下各编译出 0 error 0 warning）：

```c
/* Dual-toolchain 8051 demo: one source file, Keil C51 and SDCC both compile it.
   Comments stay ASCII on purpose (C51 encoding pitfall). */

#ifdef __SDCC
#include <8051.h>
#define ISR_T0   __interrupt(1)
#define CODE     __code
#define NOP()    __asm NOP __endasm
__sbit __at (0x90) LED0;          /* P1.0 */
#else
#include <reg52.h>
#include <intrins.h>
#define ISR_T0   interrupt 1
#define CODE     code
#define NOP()    _nop_()
sbit LED0 = P1 ^ 0;
#endif

CODE const unsigned char pattern[4] = {0x01, 0x02, 0x04, 0x08};

void t0_isr(void) ISR_T0
{
    LED0 = 1;
}

void main(void)
{
    unsigned char index = 0;
    while (1)
    {
        P1 = pattern[index & 0x03];
        index++;
        NOP();
    }
}
```

### 写这段骨架时踩到的两个真坑

1. **Keil 用 `_nop_()` 必须 `#include <intrins.h>`**。只写 `_nop_()` 而不包含头文件，Keil 报 `ERROR C264: intrinsic '_nop_': declaration/activation error` 外加一条 `WARNING C206: missing function-prototype`。SDCC 压根没有 `_nop_()` 这个函数，只能用内联汇编。
2. **SDCC 对放进代码空间的数组要求 `const`**。只写 `__code unsigned char pattern[]` 会得到 `warning 356: object in read-only code space should be const`；加上 `const` 后为零警告。Keil 侧对 `code const` 也接受。

---

## 四、切换后端时的其它注意点

- **hex 文件名大小写不同**。Keil 经 OH51 产出的文件名常常被转成**大写**（`main.c` → `MAIN.hex`），SDCC 走 `packihx` 产出的是**原名小写**（`main.hex`）。脚本会自动找两种名字，但你自己复制进电路时要注意大小写。Windows 文件系统大小写不敏感，所以本地一般不炸；一旦把工作副本放到大小写敏感的文件系统或打包分发，就会出问题。
  —— 顺带一提：正因为大小写不敏感，同一目录里先用 Keil 编、再用 SDCC 编，后一次的产物会**覆盖**前一次（`DUAL.hex` 与 `dual.hex` 是同一个文件）。要对比两边产物就分目录编。
- **hex 体积不可比**。实测同一份骨架源码，Keil 的 hex 数据段 40 字节、SDCC 197 字节。两者启动代码与库代码组织不同，不要用「hex 变大/变小」推断功能变化。
- **SDCC 不支持工程模式**。`--toolchain sdcc --proj ...` 会被直接拒绝（退出码 2）。需要工程模式的用户要用 Keil。
- **Keil 工具的退出码不能当失败判据（实测踩过）**。`BL51.exe` 在`1 WARNING(S), 0 ERROR(S)` 时**退出码是 1**；只看退出码会把一份只有链接警告的合法源码判成编译失败、拿不到 hex（旧版本就是这样，已修）。现在的规则是：**输出里有 `N ERROR(S)` 汇总就以错误数判定**；退出码非零且连汇总都没有（工具压根没跑起来）才算失败。同理 UV4 工程模式也只按 `0 Error` 判，1 条警告不影响成功。
- **工程模式要防「读到上一次的日志」**。UV4 没真正跑起来时不会重写日志，而工程目录里常留着上一次构建的 `build.log` 与 hex；照旧文件判定就会把「压根没编译」报成 `0 Error(s)` 成功、并把旧 hex 当成本次产物（实测：把 `uv4` 指向一个立即退出的程序，旧代码返回退出码 0 与遗留的 `aaa_stale.hex`）。现在脚本先清掉旧日志，跑完后没有新日志即判失败；hex 也只认**本次构建新产出**的那个（按文件时间筛）。真实工程回归实测仍为 `0 Error(s), 0 Warning(s).`、`HEX 数据字节数: 64`、退出码 0。
- **SDCC 的错误/警告计数已按 SDCC 的真实输出格式修正**。SDCC 打印的是 `file.c:18: warning 356: ...` 与 `file.c:12: error 101: ...`，编号夹在关键字和冒号之间；语法错误打印 `file.c:24: syntax error: ...`、缺头文件打印 `file.c:2:10: fatal error: xxx.h: No such file or directory`（这两种没有编号）。计数正则必须覆盖这三种形态，否则会把错误数报成 0。已用四份坏源码实测：删掉一个分号 → `syntax error`、`errors=1`、退出码 1；只包含 Keil 专有头文件 → `fatal error`、`errors=1`、退出码 1；重复定义局部变量 → 两条带编号的 `error 0` / `error 177`、`errors=2`、退出码 1；给 `const` 变量赋值 → `error 33`、`errors=1`、退出码 1。（对照：只带 `warning 356` 的源码是 `errors=0 warnings=1`、正常出 hex、**退出码 0** —— SDCC 不会因为警告返回非零。）
- **C 代码注释一律用 ASCII**。这是本包原有约定，仍然有效：C51 与 SDCC 在中文编码下的行为不一致，注释里的非 ASCII 字符容易踩编码坑。

---

## 五、SDCC 侧尚未实测的部分

- 本机实测的 SDCC 版本为 `SDCC : mcs51/... 4.6.0`。**其它 SDCC 大版本的命令行开关与警告编号可能不同**（例如 `--model-small`、`--out-fmt-ihx` 的拼写），换版本后请先跑 `--print-tools`，再用一份最小源码跑一次 `build51.py` 确认。
- **SDCC 编出的 hex 只在 AT89C51 类电路上实测过**（上面第二节的实跑记录）。换单片机型号（STC89C52、AT89C2051 等）时需要自行确认芯片 Flash 容量与启动代码是否兼容。
- **SDCC 的中断向量布局与 Keil 的一致性**未做逐字节比对。上面的实跑只证明了「hex 能加载、仿真能跑起来、无错误弹窗」，没有证明中断行为与 Keil 版逐拍一致。

### 用户自验步骤（换到没跑过的机器、或对后端没把握时照这个顺序做）

1. `python -B scripts/build51.py --print-tools` —— 确认 `sdcc` 与 `packihx` 都被找到，`selected` 为 `sdcc`。
2. 拿本节的骨架源码存成 `dual.c`，`python -B scripts/build51.py dual.c --toolchain sdcc` —— 应当 `errors=0 warnings=0` 且真的生成 `.hex`。
3. 故意改坏一行（例如删掉一个分号）再跑一次 —— 应当报出**非零**错误数并退出码 1。**能正确报错的后端才可信**。
4. 把 hex 复制进一个英文工作目录下电路副本的固件位置（按电路引用的文件名，注意大小写），跑：
   `python -B scripts/verify.py --dsn <电路.DSN> --wait 8`
   看到 `level=alive` / `verdict_basis=run_marker` 且主窗口标题带「仿真中」，说明 SDCC 产物真在跑。
