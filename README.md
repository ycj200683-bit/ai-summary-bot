# 项目①：大模型文本摘要器（B 方向练手项目）

目标：用 Python 调用大模型 API，做一个"粘贴文本 → 返回摘要"的小工具。  
这是为「大模型应用 / AI 产品」方向准备的第一个真实项目，做完能写进简历。

## 第一步：准备环境

1. 确认装了 Python 3.10+（终端输入 `python --version` 看版本）。
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 第二步：申请 API Key

推荐用 **DeepSeek**（国内、便宜、OpenAI 兼容）：

1. 打开 <https://platform.deepseek.com/> 注册。
2. 在「API keys」页面创建一个 key，复制保存好。

> 其他可选：通义千问、文心一言（需改 main.py 里的 API_URL 和模型名）。

## 第三步：设置环境变量

**Windows（PowerShell）：**

```powershell
$env:DEEPSEEK_API_KEY = "你的key"
```

**macOS / Linux：**

```bash
export DEEPSEEK_API_KEY="你的key"
```

> 每次新开终端都要重新设置；想永久生效可搜"系统环境变量"添加。

## 第四步：跑起来

```bash
python main.py
```

粘贴一段文字回车，就能看到摘要。

## 下一步可以加的功能（让项目更有料）

- [ ] 支持读 `.txt` / `.md` 文件而不是手贴
- [ ] 加一个简单命令行参数（如 `python main.py 文章.txt`）
- [ ] 把摘要结果保存到文件
- [ ] 改成可调长度的摘要（短/中/长）
- [ ] 推到 GitHub，写清楚 README（招聘方会看）

做完上面任意两三个，这个项目就足够写进简历「项目经历」了。
