# -*- coding: utf-8 -*-
"""NapCat WebUI（6099）API 桥接工具

达妮娅（机器人）切号/查登录状态时调用的命令行入口。
用法（在本目录 d:\\denia_terminal 下执行）：
    python webui_api.py check-ports
    python webui_api.py set-quick-login <QQ号>

设计原则：
- 端口没监听时不崩溃，而是给出"下一步怎么做"的明确引导
- 鉴权 token 从 napcat/config/webui.json 读取（顶层 token 字段），不硬编码
"""
import hashlib
import json
import os
import socket
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.abspath(__file__))
_WEBUI_CFG = os.path.join(_ROOT, "napcat", "config", "webui.json")
_DEFAULT_TOKEN = "19185259d322"


def _load_webui_cfg() -> dict:
    """读取 webui.json，返回 {host, port, token}；失败时用默认值"""
    try:
        with open(_WEBUI_CFG, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "port": int(data.get("port", 6099)),
            "token": str(data.get("token", _DEFAULT_TOKEN)),
        }
    except Exception:
        return {"port": 6099, "token": _DEFAULT_TOKEN}


def _port_open(port: int, timeout: float = 0.3) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        ok = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        return ok
    except Exception:
        return False


def _webui_port() -> int:
    return _load_webui_cfg()["port"]


def cmd_check_ports() -> int:
    """检查 3000（OneBot）/ WebUI 端口是否就绪，输出 JSON 并给引导"""
    onebot_open = _port_open(3000)
    webui_port = _webui_port()
    webui_open = _port_open(webui_port)
    result = {
        "onebot_3000": onebot_open,
        f"webui_{webui_port}": webui_open,
    }
    print(json.dumps(result, ensure_ascii=False))
    if not onebot_open and not webui_open:
        print(
            "❌ NapCat 未就绪（3000 与 WebUI 均未监听）。"
            "请先完全退出裸 QQ（所有 QQ 进程），再在终端点『启动 NapCat』"
            "由启动器拉起并注入；注入版 QQ 扫码登录成功后端口才会监听。"
        )
    elif not onebot_open:
        print(
            "⚠️ WebUI 在监听但 3000（OneBot）未监听：多半是注入版 QQ 尚未完成扫码登录。"
            "请在 QQ 窗口扫码，登录后 3000 即就绪，机器人才能收发消息。"
        )
    else:
        print("✅ 端口已就绪，可调用 set-quick-login 切号（前提：QQ 处于未登录态）。")
    return 0


def cmd_set_quick_login(uin: int) -> int:
    """通过 WebUI 切号到指定 QQ 号（走 /SetQuickLogin 快速登录）"""
    cfg = _load_webui_cfg()
    port = cfg["port"]
    token = cfg["token"]

    if not _port_open(port):
        print(
            f"❌ WebUI 端口 {port} 未监听，无法切号。"
            "请确认 NapCat 注入版已启动；若 3000/6099 都未监听，"
            "需先退出裸 QQ 再点『启动 NapCat』。"
        )
        return 1

    url = f"http://127.0.0.1:{port}/api/QQLogin/SetQuickLogin"
    hash_ = hashlib.sha256((token + ".napcat").encode()).hexdigest()
    login_req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auth/login",
        data=json.dumps({"hash": hash_}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(login_req, timeout=8) as resp:
            cred = (json.loads(resp.read().decode() or "{}").get("data") or {}).get("Credential")
    except Exception as e:
        print(f"❌ WebUI 鉴权失败：{e}")
        return 1
    if not cred:
        print("❌ WebUI 鉴权未返回 Credential（token 可能已变，请核对 napcat/config/webui.json）")
        return 1
    body = json.dumps({"uin": uin}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cred,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except Exception as e:
        print(f"❌ 调用 WebUI 失败：{e}")
        print(
            "若提示 Unauthorized：可能 token 已变，请更新 napcat/config/webui.json。"
            "若提示 QQ Is Logined：说明当前已登录，需先在 QQ 里退出到未登录态再切号。"
        )
        return 1

    print(json.dumps(data, ensure_ascii=False))
    if data.get("code") == 0:
        print(f"✅ 已切号到 {uin}（若需登录请留意 QQ 窗口的扫码/验证提示）。")
        return 0
    print(f"⚠️ WebUI 返回失败：{data}")
    return 1


def main():
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]
    if cmd == "check-ports":
        return cmd_check_ports()
    if cmd == "set-quick-login":
        if len(args) < 2 or not args[1].isdigit():
            print("用法：python webui_api.py set-quick-login <QQ号>")
            return 1
        return cmd_set_quick_login(int(args[1]))
    print(f"未知命令：{cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
