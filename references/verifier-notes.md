# 验证器实现说明（verify.py）

本文说明 `verify.py` 内部**怎么认窗口、怎么读状态、怎么下判定**，以及为什么在拿不到文本信号时要退回进程 CPU 时间、而 CPU 时间又为什么只能当弱证据。需要排查「为什么判成 false / 为什么判成 true」或移植到别的 Proteus 版本时看这份。

配套阅读：`references/legacy-desktop-automation.md`（四条路线与迁移）、`references/toolchain-keil-vs-sdcc.md`（编译后端）。

## 一、总体思路

`verify.py` **完全不用图像识别**。它通过 Win32 枚举目标进程的顶层窗口与子控件，读取窗口标题、控件文本、状态栏与弹窗文本，再叠加进程级信号，最后在标准输出打印**单行 ASCII JSON**。

判定信号分四类：**主窗口文本**、**弹窗文本**、**失败词黑名单**、**进程 CPU 时间 + 主窗口存活**。

## 二、窗口定位（这一步错了后面全错）

1. **不能用「最大的可见窗口」当主窗口。** ISIS 启动时会先出现一个**无标题的启动画面**（实测 499×316），它随后被销毁。若在它还在时取到它，会连锁出四个错：Ctrl+F12 投进死窗口（仿真没启动）、控件文本永远读到空、失败截图报「无法读取窗口尺寸」、而 CPU 却因设计还在加载渲染而上涨 —— 把「没在跑」判成 `running=true`。
2. **先按窗口特征过滤，再按面积取最大。** `is_main_candidate(title, class)` 用标题候选（`ISIS Professional` / `Proteus ISIS` / `Labcenter Proteus` / `Proteus`）与窗口类候选（`LX_ISIS_DClkNoDC` / `LX_ISIS_NoDC` / `LX_ISIS`）。这两个列表与 `proteus_ctl.py` 共用一份；单独拷走 `verify.py` 时用内置副本。
3. `wait_main()` **只接受**命中特征的窗口；`--startup-timeout` 内等不到就返回空，脚本报 `ISIS main window not found`（退出码 4）。宁可明确报「没找到主窗口」，也不拿启动画面顶替。
4. 运行期间主窗口可能被重建，所以判定前会再确认一次句柄有效性，失效则重新等待 1 秒。**主窗口不存在时 `running` 一律为 false**，并给出 `basis=window_gone`。

## 三、状态读取方式

1. **主窗口文本**：`EnumChildWindows` 遍历全部子控件，对每个控件先试 `GetWindowTextW`，为空再试 `SendMessageW(WM_GETTEXT)`。所有非空文本去重后合并成 `log_text`（截断到末尾 12000 字符）。
2. **状态栏**：识别控件类名 `msctls_statusbar32` / `statusbar`，取窗口文本之外，还用 `SB_GETTEXTW` 逐栏（0–7）读取每格文字，合并成 `status_text`。标准状态栏把每格文字存在内部分栏里，`GetWindowTextW` / `WM_GETTEXT` 都取不到，必须用 `SB_*` 消息。
3. **弹窗文本**：`dialog_texts()` 收集同一进程里**除主窗口以外**的可见顶层窗口文字（含 `#32770` 对话框）。错误弹窗通常不在主窗口的子控件树里，只看主窗口会漏掉「Cannot open ...」这类真正的致命提示。实测：把电路固件删掉再跑，弹窗文字 `SIMULATION ERROR LOG` 正是这一次判定的决定性证据。
   **仪器窗口要排除**：虚拟终端窗口的文字是**用户程序自己的输出**，程序打印一句 `ERROR: ...` 就会被失败词表命中，把一次运行正常的仿真判成 `failed`（实测带 Virtual Terminal 的电路上，该窗口的文字确实会出现在这里）。所以标题命中串口/终端特征的顶层窗口不进 `dialog_text`。
   **`--serial-window` 给的额外关键字必须同样参与这个排除**：否则会出现自相矛盾的结果 —— 抓取侧认得那个终端、断言能通过，判定侧却仍把它的文字当弹窗扫描，程序自己打印的 `ERROR:` 立刻把这次运行判成失败。离线复核：造一个标题为 `MyTermConsole` 的真实顶层窗口加一个写着 `ERROR: sensor timeout` 的真实子控件，不传关键字时 `dialog_text` 是 `"MyTermConsole ERROR: sensor timeout"`，传入 `extra_marks=["MyTermConsole"]` 后为 `""`。
4. **进程信号**：`OpenProcess(PROCESS_QUERY_INFORMATION)` + `GetProcessTimes` 取内核态 + 用户态累计 CPU 时间（秒）。

## 四、CPU 基线为什么要「等静止」

ISIS 打开电路后还要画一会儿图，这段 CPU 增长与「仿真正在跑」无关。若把基线打在加载期，**坏固件也能被判成 running**（实测：固件缺失时 CPU 仍涨 0.125s，超过 0.05s 阈值）。

所以顺序是：

```text
1) 等主窗口出现（窗口特征匹配）
2) wait_cpu_idle(): 等 CPU 连续 0.4 秒不再增长（上限 6 秒）→ 结果记入 cpu_baseline_settled
3) 记 CPU 基线 cpu_before
4) 投 Ctrl+F12
5) 观察 --wait 秒（期间反复抓串口文本，命中期望就提前结束）
6) 记 cpu_after，算 cpu_delta
```

实测对照：设计加载完成后的**真静止**状态 CPU 增长为 `0.000s`；对着真主窗口按 Ctrl+F12 后为 `0.859s`。阈值 `0.05s` 两边都有很大余量。

## 五、判定词表

| 类别 | 匹配内容（不区分大小写） |
|---|---|
| 失败（一票否决） | `cannot open`、`simulation failed`、`fatal simulator`、`\berror\b` |
| 运行 | `simulation started` / `simulation running`、`animation`、`\brunning\b`、`time =` 或 `time:`、`simulating`、`仿真中` |
| 加载中（视为未运行） | `loading design` / `loading project`、`正在加载` |

命中失败词的原文会被收进 `errors` 数组；**`errors` 非空一律判 `running=false`**。

`仿真中` 与 `simulating` 是实测加进来的：Proteus 7.8 汉化版在仿真运行时，主窗口标题会变成「<电路名> - ISIS Professional（仿真中......）」。这是目前**最强的正向证据**（文本级、语义明确）。

## 六、判定顺序

```text
1) 失败词命中（含弹窗文字）？            → running=false，basis=failure_marker
2) 状态栏在「加载中」？                  → 视为未运行，basis=loading
3) 主窗口死了？                          → running=false，basis=window_gone
4) 有运行标志（标题/日志/状态栏）？      → running=true，basis=run_marker
5) CPU 时间增长 ≥0.05s 且主窗口存活？    → running=true，basis=cpu_fallback（弱证据）
6) 都不满足                              → running=false，basis=no_signal
```

**注意第 4 步取代了「状态栏非空就算在跑」这条旧规则。** 状态栏里常驻坐标、网格设置、提示语
这类与仿真无关的文字，把「有字」当「在跑」是明确的假阳性来源，这条判定分支已删除：
状态栏只有**命中运行词表**（`RUN_RE`）时才计入运行标志。`running` 的充要条件现在是
「没有失败词 + 不在加载 + **主窗口句柄有效** +（命中运行标志 或 CPU 兜底）」。

`evidence` 字段按实际走到的分支拼出可读证据，例如：

```text
run marker in log/status; no failure markers; status=<empty>;
cpu_delta=0.484s; basis=run_marker; main_window=<电路名> - ISIS Professional（仿真中......）
```

## 七、为什么要标 verdict_basis：CPU 时间只是弱证据

**CPU 时间增长不是充分证据。** 实测：电路固件被删掉时，仿真没启动，但 CPU 仍增长 `0.125s` —— 因为 ISIS 在弹错误框、开错误日志窗口，这些同样耗 CPU。若只靠 CPU 判据，这一次会被误判成「在跑」。

所以脚本用 `verdict_basis` 如实标出每一票的证据强度：

| `verdict_basis` | 强度 | 含义 |
|---|---|---|
| `run_marker` | 强 | 标题/日志/状态栏明确写着仿真在运行（命中运行词表） |
| `failure_marker` | 强（否定） | 命中失败词或错误弹窗 |
| `window_gone` | 强（否定） | 主窗口句柄已失效 |
| `loading` | 强（否定） | 状态栏仍在加载 |
| `cpu_fallback` | **弱** | 只有 CPU 增长 |
| `no_signal` | —— | 什么信号都没有 |

（早期版本还有一个 `status_text` = 「状态栏读到了文字就算在跑」，已删除，见第六节。）

**用法建议**：自动化流水线里对 `alive` + `basis=run_marker` 可以放心；看到 `alive` + `basis=cpu_fallback` 时，说明这个 Proteus 版本没暴露运行标志，结论只是「进程在算且没看到失败词」，**不要当成功能正确的证明**。

同理，`--expect-serial` / `--expect-regex` 满足时得到的是 `level=correct`，那才是「输出对不对」层面的结论。

## 八、串口（Virtual Terminal）读取

**候选规则（三条，都是实测踩出来的）**

1. **标题必须命中词边界特征**：`virtual terminal` / `vterm` / `serial` / `uart` / `comN`（正则带 `\b`）。
   为什么不能用子串：电路文件叫 `softuart.DSN` 时，**主窗口标题里就含 `uart`**，于是主窗口的所有子控件都成了候选——实测该电路 `serial_candidates=64`，工具条下拉框里的 `"0"`、窗口标题、面板标签全被当成串口文本，`--expect-serial "0"` 会**假通过**。加词边界后 `softuart` 不再命中。
2. **主窗口自身及其子控件一律不是串口候选**，也不继承主窗口的标题特征。
   虚拟终端是**独立顶层窗口**（实测标题就是 `Virtual Terminal`），所以这条排除不会误伤真终端。
3. **虚拟终端窗口内部的显示控件可以继承「非主窗口」祖先的标题特征**——终端里的 Edit 控件自己通常没有标题，特征只挂在父窗口上。ISIS 左侧对象选择面板的分组标题（`TERMINALS` 等）另用黑名单 `SERIAL_PANE_LABELS` 排除。

**为什么不按类名认**：Proteus 工具条上的下拉框同样是 `Edit` 控件，里面放的是选项值（例如 `"0"`）。早期版本按类名给分，结果在没有虚拟终端的电路上把它当成了 `serial_text`。现在类名只用于加分。

**实测对比（同一条带虚拟终端的电路、同一条命令）**

| 版本 | `serial_candidates` | `serial_text` |
|---|---|---|
| 修正前 | 64 | 混入工具条 `"0"` + 主窗口标题 + 面板标签 + `Virtual Terminal` |
| 修正后 | 2 | 只剩虚拟终端窗口内 Edit 控件的文本 |

修正后 `serial_source="Edit|"`、`serial_sources=["Edit|","LX_ISIS_DClkNoDC|Virtual Terminal"]`，并且虚拟终端窗口的文字**不再进入 `dialog_text`**（否则程序自己打印 `ERROR: ...` 会污染失败词判定）。

> **那条「两次运行只拿到 3 个 / 2 个 `0x7F`」的旧观察，后来解释清楚了**：这条电路自带的**原固件**（例程源码里的发送表）是 `{0xFE,0xFD,0xFB,0xF7,0xEF,0xDF,0xBF,0x7F}` 循环发送 —— 其中只有 `0x7F` 是终端会留在显示区里的可见字符。也就是说**抓取链一直是通的，只是原文本身没有可读内容**，不是抓取坏了。换成一份输出可读文本的固件后，抓到的就是那段文本本身（见下面的端到端记录）。

**实际验证到哪一层（据实说）**

| 层 | 状态 |
|---|---|
| 候选识别（认对虚拟终端窗口、不误抓工具条下拉框） | **已实测**（修正前 `serial_candidates=64` → 修正后 `2`） |
| 取文本这条链 | **已实测**：抓到过 `serial_text="REVIEW-OK-4211\r\n"`（16 字节），与程序实际发出的内容逐字一致 |
| `--expect-serial` 断言判**通过**（`level=correct`） | **已实测通过**（见下面的端到端记录） |
| `--expect-regex` | **已实测通过**（同一次运行内） |
| 断言判**不通过**（`level=failed`） | **已实测**（给不可能出现的期望值 → 退出码 1；另有一次故意的波特率错配，抓到乱码 `ffff`x€ffff`x€ffff`，同样判失败） |

**端到端实测记录**

在一份自带虚拟终端、电路本身走硬件串口（`SBUF` / `SCON`、`TH1=0xFD`，时钟 11.0592 MHz、9600 8N1，与终端设置一致）的电路上，把固件换成一份「每秒固定输出 `REVIEW-OK-4211` + CRLF」的已知程序（改的是**系统临时目录里的副本**，原电路一个字节未动）：

```text
python -B scripts/verify.py --dsn <临时副本>\<电路>.DSN ^
    --startup-timeout 12 --wait 8 --timeout 18 ^
    --expect-serial "REVIEW-OK-4211" --expect-regex "^REVIEW-OK-\d{4}\r?$" ^
    --dump-serial serial.txt
```

```text
level=correct, running=true, serial_match=true, exit=0,
serial_text="REVIEW-OK-4211\r\n", serial_candidates=2,
serial_source="Edit|", serial_method="wm_gettext",
serial_sources=["Edit|","LX_ISIS_DClkNoDC|Virtual Terminal"],
verdict_basis=run_marker, errors=[], expectation_errors=[],
dialog_text="", cpu_delta_seconds=0.172, elapsed_seconds≈7.0
```

两条要点：
- **可读窗口很窄**：程序在这段时间里重复输出了很多行，但抓到的是**一整行**（16 字节）。断言要针对此刻仍在显示区内的内容，别断言几十行以前的东西。
- **波特率必须与电路的 `CLOCK=` 一致**（11.0592 MHz 下 9600 8N1 用 `TH1=0xFD`）。一旦对不上，终端显示的是乱码，判定会（正确地）判 `failed`；但乱码文本仍可能碰巧命中**很短**的断言，所以断言尽量写长、带上不常见的数字或前缀。

## 八·附：串口断言的端到端自验配方

没有现成的「已知输出」电路时，可以**现造一份**，全程只碰临时副本：

1. 把带虚拟终端仪器的电路**整个目录**复制到系统临时目录（路径要纯 ASCII）。
2. 用文本方式**只读**搜原 `.DSN` 里的 `PROGRAM=`（形如 `PROGRAM=xxx.hex`）拿到固件名，顺带搜 `CLOCK=` 拿到单片机时钟频率。
3. 编译一份**固定输出已知文本**的固件（源码见下），hex 名字无所谓。
4. 把编出的 hex 按第 2 步的名字**覆盖进副本**（原文件另存 `.bak`）。**原电路与原件一个字节都不要动。**
5. 先只抓不判：`python -B scripts/verify.py --dsn <副本.DSN> --wait 8 --dump-serial serial.txt`，看 `serial.txt` 里有没有你写的那段文本，并核对 `serial_source` / `serial_sources` 是不是虚拟终端窗口。
6. 再加断言：`--expect-serial "<你的输出片段>"` 应得 `level=correct`、退出码 0；换成一段不可能出现的文本应得 `level=failed`、退出码 1、`expectation_errors` 非空。

**已知输出固件模板**（Keil 与 SDCC 都能编译；注释保持 ASCII 是本包约定）。
**关键点**：`TH1` 必须按你电路的 `CLOCK=` 算 —— 11.0592 MHz、9600 8N1、`SMOD=0` 时是 `0xFD`；写成 `0xFA` 就是 4800，终端只会显示乱码。

```c
/* Known-output UART firmware: prints a fixed line so the verifier can assert it.
   Target example: AT89C51 @ 11.0592 MHz, 9600 8N1 on P3.1/TXD (Timer1 mode 2). */

#ifdef __SDCC
#include <8051.h>
#define NOP()  __asm NOP __endasm
#else
#include <reg52.h>
#include <intrins.h>
#define NOP()  _nop_()
#endif

static const char banner[] = "REVIEW-OK-4211\r\n";

static void uart_init(void)
{
    TMOD = 0x20;        /* timer1 mode 2, 8-bit auto reload (baud generator) */
    SCON = 0x50;        /* mode 1, 8-bit UART, REN = 1 */
    TH1 = 0xFD;         /* 9600 bps @ 11.0592 MHz, SMOD = 0 */
    TL1 = 0xFD;
    PCON = 0x00;
    TR1 = 1;
    TI = 0;
    RI = 0;
}

static void uart_putc(char c)
{
    SBUF = c;
    while (!TI)
    {
        NOP();
    }
    TI = 0;
}

static void uart_puts(const char *s)
{
    unsigned char i = 0;
    while (s[i] != 0)
    {
        uart_putc(s[i]);
        i++;
    }
}

static void delay_rough(unsigned int ms)
{
    volatile unsigned int i;
    volatile unsigned char j;
    for (i = 0; i < ms; i++)
    {
        for (j = 0; j < 200; j++)
        {
            NOP();
        }
    }
}

void main(void)
{
    uart_init();
    while (1)
    {
        uart_puts(banner);
        delay_rough(500);
    }
}
```

**跟本包脚本的关系**：`run_batch.py` 的「复制电路目录到临时目录 + 覆盖 `target_hex`」就是第 1、4 步的自动化版本；把上面这份固件编出的 hex 当成任务清单里的 `hex`、`target_hex` 写成电路引用的那个名字，就能一条命令跑完整套自验。

**取材方式**：优先 `WM_GETTEXT`/`GetWindowTextW`；取不到时对文本型控件走剪贴板（`Ctrl+A` + `Ctrl+C`，用完恢复原剪贴板内容）。顶层窗口自己的 `GetWindowText` 读到的是**标题栏文字**，不是终端内容，会被跳过。

`serial_source` 是**实际贡献文本**的那个控件（`类名|标题`）；`serial_sources` 列出**识别到的全部候选来源**（至多 8 个，没取到文本时也列出），两个字段一起看才能判断识别对不对。

**窗口标题猜不到时**：用 `--serial-window <关键字>`（可重复）手工指定（按子串匹配）。脚本不做猜测 —— 猜不到就返回空文本，而不是随便抓一个文本框顶上。

## 九、失败现场留存

判定为 `failed` / `timeout` 时，把现场写进 `--evidence-dir`（默认 DSN 同目录下 `verify_evidence`）：

- `simulation.log.txt`：`log_text` + `STATUS: <status_text>`，并附一行 `EVIDENCE screenshot: ok` 或 `EVIDENCE screenshot failed: <异常类型>: <原因>`；
- `simulation.png`：主窗口截图（有 pywin32+Pillow 时走它们，否则走纯 ctypes）。

**截图失败也要留痕**，不再静默吞掉 —— 曾经的静默捕获让「句柄失效」这个根本原因被藏了很久。

**截图只作人工复核，不参与任何判定分支。**

## 十、收尾

验证结束后发送 `Shift+F12` 停止仿真，然后 `WM_CLOSE` 请求关闭主窗口；若进程仍未退出，再 `terminate()`（最后 `kill()`）兜底。默认自动关闭是为了避免残留 ISIS 进程导致下一次验证撞上旧窗口；需要人工复查时用 `--keep` 保留。实测每次运行结束后 `Get-Process ISIS` 均为空。

## 十一、已知限制

- 依赖目标软件的窗口类名/文本行为。**换 Proteus 版本可能需要在窗口候选列表与判定词表上微调**；`proteus_ctl.py --probe` 可只读列出本机识别到的窗口与类名，用它来补候选。
- `\berror\b` 是宽匹配：电路文件名、元件名、设计标题里若含独立 `error` 字样可能被误判为失败，改个名字即可。
- 判定的是「仿真进程在跑且无失败词」，**不校验电路功能是否正确**（灯该亮没亮、数码管数值对不对）。
- 状态栏自绘的版本（如 Proteus 7.8 汉化版，类名 `LX_ISIS_NoDC`）只能靠标题运行标志或 CPU 时间兜底；此时 `status_text` 可能为空，属于预期现象，不是脚本故障。
- **「没找到主窗口」（退出码 4）在少数版本上是可能的**：如果你的 Proteus 标题/类名都不在候选列表里，请用 `--probe` 读出真实类名并补进候选列表，而不是把候选放宽到「任意窗口」——那会退回到启动画面误判。
- 串口读取在下列情况下会失败（详见 SKILL.md 能力边界一节）：电路里没放虚拟终端、终端窗口没打开、终端缓冲区被覆盖、换行/大小写与期望不一致、输出速率极快导致采样漏窗。
- **串口候选只按标题特征筛，不验语义**：任何标题命中 `virtual terminal` / `vterm` / `serial` / `uart` / `comN` 的仪器窗口都会被当成串口源。请用 `serial_sources` 核对来源；不对就用 `--serial-window` 手工指定（那是子串匹配，别给太短的关键字）。
- **断言是「对显示区里的文本做子串/正则匹配」，不是「对程序的完整输出做匹配」**。跑错波特率、别的仪器串了文本、显示区里残留着上一次的内容，都可能让很短的断言（单个字符、`OK` 这种通用词）**假通过**。断言尽量写长、带上不常见的数字或前缀，并先按 SKILL.md 第十节第 6 条走一遍。
- **`--wait` 必须明显小于 `--timeout`**：两者相等或倒挂，观察窗口会被总时长上限截断，`timed_out` 必然为真，一次好端端的仿真会被判 `timeout`。脚本会在 stderr 提醒并把 `note=--wait >= --timeout cut the run short` 写进 `evidence`。
- **`running=true` 也不等于「你按的快捷键被响应了」**：`run_marker` 靠标题/状态栏文字，`cpu_fallback` 靠进程 CPU 增长，两者都不保证 Ctrl+F12 一定生效。要落到「输出对不对」这一层，用 `--expect-serial` / `--expect-regex` 拿 `level=correct`（并先按 SKILL.md 第十节第 6 条自验）。
- 同一条 `cpu_fallback` 判据在不同电路上的数值可能差一个数量级（实测 `0.125s` / `0.172s` / `0.188s` / `0.234s` / `0.344s` / `0.859s`），所以它只能当兜底。
