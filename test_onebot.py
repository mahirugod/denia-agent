# -*- coding: utf-8 -*-
"""快速验证 NapCat 3000 端口的 OneBot HTTP API 是否就绪"""
import json
import urllib.request

# 调用标准 OneBot 端点探测登录态
url = "http://127.0.0.1:3000/get_login_info"
try:
    req = urllib.request.Request(url, method="GET", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode() or "{}")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if data.get("status"):
        uin = (data.get("data") or {}).get("user_id")
        print(f"\n✅ OneBot API 就绪，当前登录号: {uin}")
    else:
        print(f"\n⚠️ API 返回 status=False: {data.get('retcode')} {data.get('msg')}")
except Exception as e:
    print(f"❌ 调用 3000 失败：{e}")
    print("提示：若为连接失败，说明 OneBot 实例尚未完全就绪，等 NapCat 日志出现『实例已创建』再试。")
