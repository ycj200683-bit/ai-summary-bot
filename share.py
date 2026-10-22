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
  python share.py --named         # 固定域名：用自己的 Cloudflare 域名（推荐）
  python share.py --tunnel        # 公网临时链接：地址每次变，适合临时演示
  python share.py --port 8080     # 换端口

【--named 和 --tunnel 的区别】
  --named   用自己的域名（如 ai.example.com），地址永久不变，要先把隧道配好：
              ① cloudflared tunnel login
              ② 建隧道 + 在 ~/.cloudflared/config.yml 里写 ingress 规则
              ③ cloudflared tunnel route dns <隧道名> ai.example.com
            配好之后，这个脚本自动读 config.yml 里映射到本端口的域名并打印出来。
  --tunnel  免配置，cloudflared 临时给你一个 trycloudflare.com 地址，
            关掉就失效、重启换新的。只适合当场给人看一眼。

【注意】
  · 隧道模式依赖 cloudflared（没装会提示安装命令）。
  · 电脑睡眠 / 合盖 / 断网，所有模式都会断——服务跑在你自己机器上。
  · 想开机就在线，可以把 cloudflared 装成 Windows 服务：
      cloudflared service install
    注意装成服务后它读的也是同一份 config.yml，但**服务里不会跑 uvicorn**，
    Python 这边还得自己保证在跑（或做成计划任务）。
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

    ★ 也不能只靠「UDP connect 一下，看系统挑了哪块网卡」：
      开了 Clash / V2Ray 的 TUN 模式后，默认路由被虚拟网卡接管，
      connect 拿回来的是 198.18.0.1 这种 fake-IP 网关地址——
      **这不是别人能访问到的地址**，发出去对方必然打不开。
      （实测：2026-10-22 开着 Clash TUN 时返回 198.18.0.1）

    所以：先枚举本机所有 IPv4，只留 RFC1918 私网段里的，按常见程度排序
    （家用/校园网 192.168 > 企业 10 > 172.16-31），都拿不到再退回 connect 的结果。
    """
    import ipaddress

    private_nets = (
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
    )

    # 看着像私网、但对方根本连不上的网段（虚拟网卡）
    virtual_nets = (
        ipaddress.ip_network("198.18.0.0/15"),    # Clash / V2Ray 的 TUN fake-IP
        ipaddress.ip_network("192.168.56.0/24"),  # VirtualBox Host-Only 默认网段
        ipaddress.ip_network("172.17.0.0/16"),    # Docker 默认网桥
        ipaddress.ip_network("169.254.0.0/16"),   # link-local，没拿到 DHCP 时的兜底地址
    )

    def rank(ip: str) -> int:
        """越小越优先；99 表示不可用（虚拟网卡或非法地址）。"""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return 99
        if any(addr in net for net in virtual_nets):
            return 99
        for idx, net in enumerate(private_nets):
            if addr in net:
                return idx
        return 99

    # 1) 枚举本机所有 IPv4
    candidates: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.add(info[4][0])
    except OSError:
        pass

    usable = sorted((ip for ip in candidates if rank(ip) < 99), key=rank)
    if usable:
        return usable[0]

    # 2) 兜底：UDP connect（不发包，只让系统挑一块出网网卡）
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def cloudflared_exe() -> str:
    exe = shutil.which("cloudflared")
    if not exe:
        print("❌ 没找到 cloudflared。装法（任选一种）：")
        print("   winget install --id Cloudflare.cloudflared")
        print("   或到 https://github.com/cloudflare/cloudflared/releases 下载 exe 放进 PATH")
        raise SystemExit(1)
    return exe


def cloudflared_config() -> str:
    """cloudflared 的配置文件路径（login 之后默认就落在这里）。"""
    import os

    return os.path.join(os.path.expanduser("~"), ".cloudflared", "config.yml")


def named_tunnel_hosts(port: int) -> list[str]:
    """
    从 config.yml 里找出「映射到本端口」的域名，只用于打印提示。

    ★ 为什么不引 yaml 库：这个脚本要在部署机上跑，多一个依赖就多一次装不上；
      而 config.yml 的结构是固定的两行一组（hostname 下一行是 service），
      按行扫足够，也不会因为多一个字段就崩。
      —— 只在文件读不了或结构不认识时返回空，绝不因为解析失败而影响启动。
    """
    path = cloudflared_config()
    try:
        text = open(path, "r", encoding="utf-8").read()
    except OSError:
        return []

    hosts: list[str] = []
    current = ""
    for line in text.splitlines():
        m = re.match(r"\s*-\s*hostname:\s*(\S+)", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"\s*service:\s*http://(?:127\.0\.0\.1|localhost):(\d+)", line)
        if m and current and int(m.group(1)) == port:
            hosts.append(current)
            current = ""
    return hosts


def start_named_tunnel(on_line) -> subprocess.Popen:
    """
    用已有的 cloudflared 配置起隧道（固定域名，地址不会变）。

    和 --tunnel 的区别只在于：这里不申请临时域名，直接读 config.yml，
    所以不需要抓 URL，只需要把日志转出去方便排错。
    """
    exe = cloudflared_exe()
    cfg = cloudflared_config()
    import os

    if not os.path.exists(cfg):
        print(f"❌ 没找到隧道配置 {cfg}")
        print("   先做一次登录：cloudflared tunnel login")
        raise SystemExit(1)

    proc = subprocess.Popen(
        [exe, "tunnel", "--config", cfg, "run"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            # cloudflared 的日志很吵（每次重连都刷屏），只留有用的几类
            if any(k in line for k in ("ERR", "WRN", "Registered tunnel connection", "error")):
                on_line(line.rstrip())

    threading.Thread(target=pump, daemon=True).start()
    return proc


def start_tunnel(port: int, on_url) -> subprocess.Popen:
    """
    起一条 cloudflared 快速隧道（不需要账号、不需要域名、免费）。

    cloudflared 会把访问日志打在 stderr 上，其中有一行包含
    https://xxx.trycloudflare.com —— 把那一行抓出来就是公网地址。
    """
    exe = cloudflared_exe()

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
    parser.add_argument("--port", type=int, default=None,
                        help="端口，默认 8000（--named 模式下 8001）")
    parser.add_argument("--tunnel", action="store_true",
                        help="开公网临时隧道（地址每次变，适合临时演示）")
    parser.add_argument("--named", action="store_true",
                        help="用 Cloudflare 固定域名（读 ~/.cloudflared/config.yml）")
    args = parser.parse_args()

    if args.tunnel and args.named:
        print("❌ --tunnel 和 --named 只能选一个：前者地址每次变，后者地址固定。")
        raise SystemExit(1)

    # --named 的默认端口是 8001：很多人机器上 8000 已经被别的项目占了，
    # 撞端口会让服务直接起不来（[Errno 10048]）。
    port = args.port or (8001 if args.named else 8000)

    ip = local_ip()
    lan_url = f"http://{ip}:{port}"

    tunnel_proc = None
    public_url = ""
    hosts: list[str] = []

    if args.named:
        hosts = named_tunnel_hosts(port)
        tunnel_proc = start_named_tunnel(lambda line: print("   [tunnel] " + line))
    elif args.tunnel:
        print("正在向 Cloudflare 申请临时隧道（约 5~10 秒）…")

        url_holder: dict[str, str] = {}
        url_ready = threading.Event()

        def show(url: str) -> None:
            url_holder["url"] = url
            url_ready.set()

        tunnel_proc = start_tunnel(port, show)
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
        print(f"  AI 摘要服务已启动（端口 {port}）")
        print()
        print(f"  你自己打开：       http://127.0.0.1:{port}")
        print(f"  同一个 WiFi 的人：  {lan_url}")
        if args.named:
            if hosts:
                for h in hosts:
                    print(f"  固定域名：         https://{h}")
                print("       ↳ 地址永久不变；电脑睡眠 / 断网会断，醒来重跑这条命令即可")
            else:
                print(f"  ⚠️  config.yml 里没有映射到 {port} 端口的域名，公网访问不了。")
                print("      去 ~/.cloudflared/config.yml 的 ingress 里加一条：")
                print("        - hostname: ai.你的域名.com")
                print(f"          service: http://127.0.0.1:{port}")
        elif args.tunnel:
            if public_url:
                print(f"  公网链接（谁都能开）： {public_url}")
                print("       ↳ 关掉即失效，重启会换新地址；电脑睡眠也会断")
            else:
                print("  公网链接：❌ 隧道没建立成功，本次只有局域网可用")
        print()
        if public_url or hosts:
            print("  ⚠️  用非 localhost 的域名打开时，「云服务」那条链路和顶部统计会失效——")
            print("      云端只认白名单里的域名。主链路「我的 Key」不受影响。")
            print()
        print("  第一次启动时 Windows 防火墙会弹窗问「是否允许 Python 访问网络」，")
        print("  必须点「允许访问」，否则别人连得进来但页面打不开。")
        print()
        print("  按 Ctrl+C 停止服务")
        print(bar)

        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    finally:
        if tunnel_proc is not None:
            tunnel_proc.terminate()


if __name__ == "__main__":
    main()
