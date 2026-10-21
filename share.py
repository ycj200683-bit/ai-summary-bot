"""
share.py —— 一键把 AI 摘要服务分享给别人

【为什么需要这个脚本】
  `uvicorn app:app --port 8000` 只能本机访问：uvicorn 默认绑 127.0.0.1，
  别人的电脑/手机连不上。要给别人用，得同时满足三件事：
    ① 绑 0.0.0.0（监听所有网卡，不只是回环）
    ② 告诉对方「我的内网 IP 是多少」——这个每换一次网络就会变，必须现查
    ③ 如果对方不在同一个网络，还得开一条隧道
  这三件事每次手敲很烦，还容易记错 IP 发出去。所以做成一条命令。

【用法】
  python share.py                 # 局域网：同一个 WiFi / 校园网的人能开
  python share.py --tunnel        # 公网：生成一个谁都能开的临时链接
  python share.py --port 8080     # 换端口

【注意】
  · 隧道模式依赖 cloudflared（没装会提示安装命令），且关掉就失效、重启换新地址。
  · 电脑睡眠 / 合盖 / 断网，两种模式都会断——服务跑在你自己机器上。
"""

from __future__ import annotations

import argparse
import re
import shutil
import socket
import subprocess
import sys
import threading

# Windows 控制台默认是 GBK，直接 print 中文 + 特殊符号会抛 UnicodeEncodeError。
# ★ line_buffering=True 不是可选项：输出被重定向到文件/管道时（比如 nohup、CI），
#   Python 会用 4KB 块缓冲，结果就是"服务已经能访问了，但屏幕上半天一个字都没有"。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass


def local_ip() -> str:
    """
    取本机在内网里的 IP。

    ★ 不要用 socket.gethostbyname(socket.gethostname())：
      很多 Windows 机器上它返回 127.0.0.1 或者 VirtualBox 的虚拟网卡地址，
      发出去对方当然连不上。
      这里走 UDP「connect」——注意 UDP 不握手、不真的发包，
      只是让操作系统挑一块能出网的网卡，然后读它选的本地地址。断网时也不报错。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def start_tunnel(port: int, on_url) -> subprocess.Popen:
    """
    起一条 cloudflared 快速隧道（不需要账号、不需要域名、免费）。

    cloudflared 会把访问日志打在 stderr 上，其中有一行包含
    https://xxx.trycloudflare.com —— 把那一行抓出来就是公网地址。
    """
    exe = shutil.which("cloudflared")
    if not exe:
        print("❌ 没找到 cloudflared。装法（任选一种）：")
        print("   winget install --id Cloudflare.cloudflared")
        print("   或到 https://github.com/cloudflare/cloudflared/releases 下载 exe 放进 PATH")
        raise SystemExit(1)

    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = pattern.search(line)
            if m:
                on_url(m.group(0))

    threading.Thread(target=pump, daemon=True).start()
    return proc


def main() -> None:
    parser = argparse.ArgumentParser(description="把 AI 摘要服务分享给别人")
    parser.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    parser.add_argument("--tunnel", action="store_true",
                        help="开公网临时隧道（对方不用在同一个网络）")
    args = parser.parse_args()

    ip = local_ip()
    lan_url = f"http://{ip}:{args.port}"

    tunnel_proc = None
    public_url = ""
    if args.tunnel:
        print("正在向 Cloudflare 申请临时隧道（约 5~10 秒）…")

        url_holder: dict[str, str] = {}
        url_ready = threading.Event()

        def show(url: str) -> None:
            url_holder["url"] = url
            url_ready.set()

        tunnel_proc = start_tunnel(args.port, show)
        # 等地址出来再统一打印：否则「服务已启动」会出现在「公网链接」上面，
        # 中间隔着几段日志，用户得往上翻才知道该把哪个链接发出去。
        url_ready.wait(timeout=45)
        public_url = url_holder.get("url", "")

    try:
        try:
            import uvicorn

            from app import app
        except ModuleNotFoundError as e:
            print(f"❌ 缺依赖 {e.name}，先装：pip install -r requirements-web.txt")
            raise SystemExit(1)

        bar = "=" * 62
        print(bar)
        print("  AI 摘要服务已启动")
        print()
        print(f"  你自己打开：       http://127.0.0.1:{args.port}")
        print(f"  同一个 WiFi 的人：  {lan_url}")
        if args.tunnel:
            if public_url:
                print(f"  公网链接（谁都能开）： {public_url}")
                print("       ↳ 关掉即失效，重启会换新地址；电脑睡眠也会断")
            else:
                print("  公网链接：❌ 隧道没建立成功，本次只有局域网可用")
        print()
        if public_url:
            print("  ⚠️  用新域名分享后，「云服务」那条链路和顶部统计会失效——")
            print("      云端只认白名单里的域名。主链路「我的 Key」不受影响。")
            print()
        print("  第一次启动时 Windows 防火墙会弹窗问「是否允许 Python 访问网络」，")
        print("  必须点「允许访问」，否则别人连得进来但页面打不开。")
        print()
        print("  按 Ctrl+C 停止服务")
        print(bar)

        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    finally:
        if tunnel_proc is not None:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()
