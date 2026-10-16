# 大模型文本摘要器（ai-summary-bot）

用 Python 调用 DeepSeek API，把一段长文本压缩成 3 句话以内的中文摘要。命令行工具，支持读取文件、控制摘要长度、失败自动重试、全量调用日志落盘。

> 这是一个**从零搭建**的练手项目（不是跟教程抄的），目的是把「大模型应用开发」需要的工程化环节完整走一遍：
> 依赖管理 → 面向对象设计 → 异常分级 → 装饰器重试 → 单元测试 → 日志落盘 → 版本发布。

---

## 功能特性

| 特性 | 说明 |
|---|---|
| 读取文件生成摘要 | 支持 `.txt` / `.md`，不再受 `input()` 只能读一行限制 |
| 可调摘要长度 | `--length` 指定摘要句数 |
| 失败自动重试 | API 超时 / 网络抖动 / 429 限流 / 5xx → 指数退避重试（1s → 2s → 4s） |
| 错误分级 | 401 / 402 这类不可恢复错误**立即失败**，不做无谓等待 |
| 调用日志落盘 | 每次运行的关键信息和错误写入 `logs/app.log`，跑批量任务出问题可事后排查 |

---

## 环境要求

- **Python 3.10+**（本项目在 3.14.5 上开发）
- 一个 **DeepSeek API Key**（<https://platform.deepseek.com/> 注册后在「API keys」页面创建）
- 依赖库：`requests`；跑测试需要 `pytest`

---

## 安装

```bash
git clone https://github.com/ycj200683-bit/ai-summary-bot.git
cd ai-summary-bot

# 建虚拟环境（推荐，避免污染全局环境）
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt          # 运行依赖
pip install -r requirements-dev.txt      # 想跑测试再装这个
```

### 配置 API Key

Key 通过**环境变量**读取，不写进代码，所以不会被提交到仓库。

**Windows（PowerShell）**
```powershell
$env:DEEPSEEK_API_KEY = "你的key"
```

**Windows（Git Bash）/ macOS / Linux**
```bash
export DEEPSEEK_API_KEY="你的key"
```

> 每次新开终端都要重设。想永久生效：Windows 搜「编辑系统环境变量」→ 新建用户变量；macOS/Linux 写进 `~/.zshrc`。

---

## 用法

```bash
python main.py --file sample.txt
```

### 参数表

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--file` | 路径 | 无 | 要摘要的文本文件。**不传则进入交互模式**，手动粘贴文本 |
| `--length` | 整数 | `3` | 摘要句数上限，会写进系统提示词约束模型 |
| `-h` / `--help` | — | — | 查看帮助 |

### 示例 1：摘要一个文件

```bash
python main.py --file sample.txt
```

```
14:46:27 | INFO    | 📄 已读取文件：sample.txt（359 字）

--- 摘要结果 ---

人工智能正深刻改变软件开发与产品设计流程：开发者可用自然语言生成代码初稿，
产品经理可快速产出调研问卷、分析反馈和竞品框架，大幅提升效率。
但 AI 生成内容存在缺陷风险与理解局限，需更严格的评审和人工把关。
当前 AI 本质是"增强工具"，其价值高度依赖人的专业判断、问题定义能力和业务
洞察力，无法替代经验丰富的从业者。
```

> 第一行是 `logging` 输出（自动带时间戳，同时落盘到 `logs/app.log`），
> 后面是 `print` 输出。**判据**：给开发者看的记录走 logging，给用户看的结果走 print。

### 示例 2：自定义摘要句数

```bash
python main.py --file sample.txt --length 5
```

`--length 5` 会把系统提示词里的约束改成「用 5 句话以内概括要点」。

### 示例 3：API 出错时

程序**不会崩**，会打印明确的原因，并把错误记进日志。

不可恢复错误（key 无效 / 余额不足）——立即失败，不重试：

```
14:22:05 | INFO    | 📄 已读取文件：sample.txt（359 字）
14:22:06 | ERROR   | ❌ 401/403 key 不对或没权限，别重试，去检查 key
```

可恢复错误（429 限流 / 超时 / 5xx）——自动重试，过程也记进日志：

```
14:23:10 | WARNING | 🔁 第 1 次失败（429 被限流了，等一会儿可以重试），1 秒后重试……
14:23:13 | WARNING | 🔁 第 2 次失败（429 被限流了，等一会儿可以重试），2 秒后重试……
```

---

## 运行测试

测试**不会真的调用 API**（不花钱、不联网、结果稳定）：用 `monkeypatch` 把 `requests.post` 换成假函数，人为造出 200 / 401 / 429 各种响应。

```bash
python -m pytest -v
```

预期输出：

```
tests/test_main.py::test_build_prompt_把句数写进系统提示词 PASSED    [ 25%]
tests/test_main.py::test_chat_正常返回                      PASSED    [ 50%]
tests/test_main.py::test_chat_401_不重试                    PASSED    [ 75%]
tests/test_main.py::test_chat_429_重试后成功                PASSED    [100%]
========================= 4 passed in 0.12s =========================
```

4 个用例分别覆盖：提示词拼装、正常返回、不可恢复错误不重试、可恢复错误重试成功（含指数退避验证）。

> ⚠️ 测试文件只能用 `pytest` 跑，**不要用编辑器右上角的 ▶ 运行**——那是「当脚本执行」，`sys.path` 里只有 `tests/`，会报 `ModuleNotFoundError: No module named 'main'`。

---

## 项目结构

```
ai-summary-bot/
├── main.py                  # 主程序：Config / LLMClient / Summarizer 三个类
├── sample.txt               # 示例文本
├── requirements.txt         # 运行依赖
├── requirements-dev.txt     # 开发依赖（pytest）
├── pytest.ini               # pytest 配置（pythonpath / testpaths）
├── pyrightconfig.json       # 类型检查配置（让 IDE 认识 import main）
├── conftest.py              # 把项目根加进 sys.path
├── tests/
│   ├── test_main.py         # 4 个单元测试
│   └── README.md            # 测试写法讲解
├── learn_oop.py             # 学习笔记：OOP 对照示例
├── learn_decorator.py       # 学习笔记：闭包与装饰器 6 步对照示例
└── logs/                    # 运行日志（已 gitignore，不入库）
    └── app.log
```

---

## 设计说明

这几条是面试时最容易追问的，也是这个项目真正有含量的地方。

**1. 为什么把错误分成 `RetryableError` 和 `FatalError` 两类？**

不是所有失败都值得重试。超时、网络抖动、429 限流、5xx 是**暂时性**的，等一下可能就成功；而 401（key 错）、402（余额不足）是**确定性**的，重试 100 次结果一样，只会拖慢速度、浪费额度。所以先给错误分类，再让重试逻辑只处理可恢复的那类。

**2. 为什么用装饰器实现重试，而不是在 `chat()` 里写 `for` 循环？**

重试是**横切关注点**——日志、计时、鉴权同理：任何函数都可能需要，但都不属于业务逻辑本身。写成装饰器后，`@retry(max_tries=3, backoff=1.0)` 一行就加上能力，`chat()` 内部一行都不用改；以后要给别的函数加重试，也是加一行。

实现上是三层嵌套：`retry(收参数) → decorator(收函数) → wrapper(真正执行)`。带参数的装饰器比无参的多一层，那一层只为收参数。另外用 `@functools.wraps` 保留原函数的名字和文档字符串。

**3. 为什么测试里要把 `requests.post` 换成假函数？**

三个原因：① 每次真调用都花钱，跑几轮测试就烧掉几块；② 网络会抖，测试结果不稳定（这次过下次不过，即 flaky test）；③ 想验证「429 限流时会重试」，总不能真去把服务商惹毛。

所以用 `monkeypatch.setattr(main.requests, "post", fake_post)` 在运行时把真函数换掉，由测试自己决定返回 200 还是 429，pytest 会在测试结束后自动还原。

> 这也解释了为什么主代码写 `import requests` + `requests.post(...)`，而不是 `from requests import post`——只有前者能被打补丁。

**4. 为什么日志用 `logging` 而不是 `print`？**

判据只有一条：**给用户看的输出用 `print`，给开发者看的记录用 `logging`。**

摘要正文必须 `print`（用户要看到）；「第 2 次失败，2 秒后重试」必须 `logging`（用户不关心，但排障要查）。`logging` 还带来三个 `print` 做不到的事：分级别（INFO / WARNING / ERROR）、带时间戳、能同时输出到终端和文件。程序凌晨跑批量任务失败了，第二天能翻日志查原因，而不是面对一个空白的终端。

**5. 为什么 `FileHandler` 要写 `encoding="utf-8"`？**

Windows 上 `FileHandler` 默认用系统编码（GBK）写文件，中文日志会乱码，甚至直接抛 `UnicodeEncodeError`。这是 Windows 上的经典坑。

---

## 已知限制 / 后续计划

- [ ] 目前只支持单文件；计划支持批量处理 CSV（每行一次摘要）→ **v1.1**
- [ ] 调用记录只落日志文件，尚未入库；计划落 MySQL 以便统计（成功率、平均耗时、token 消耗）→ **v1.3**
- [ ] 只有一套 prompt；计划做两套方案对比评测并产出数据报告 → **v2.1**
- [ ] 尚无 Web 界面

---

## 开发过程

按周推进，每周的产出都落在 git 提交里：

| 版本 | 内容 |
|---|---|
| v0.1 | 命令行调通，支持 `--file` 读取与 API 错误处理，**上线 GitHub** |
| v0.2 | 按职责拆成 `Config` / `LLMClient` / `Summarizer` 三个类，异常分级 |
| v0.3 | 新增 `@retry` 装饰器，指数退避 + 只重试可恢复错误 |
| v0.4 | `print` 改造为 `logging`，日志落盘 `logs/app.log` |
| **v1.0** | 补 pytest 单元测试 4 例，README 完整化，打 tag 发布 |

---

## License

MIT
