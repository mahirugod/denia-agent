#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技能包：browse_url
达妮娅自己访问网页/图片/视频链接，读取内容后用人设回答。
底层依赖 web_search.crawl_page（Playwright）+ media_downloader.MediaDownloader。

链接识别：
  - 图片直链（.jpg/.png/.webp/.gif/.bmp）→ 下载 → 喂视觉模型
  - B站视频链接 → B站 API 拿封面+标题+简介 → 下载封面 → 视觉模型（不下载视频）
  - 其他网页 → crawl_page 抓正文
"""
from skills import skill
from tool_registry import tool
import re
import os
import sys
import threading

# 让 skills/ 包内可 import 兄弟模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

skill("browse_url", "达妮娅自己访问网页/图片/视频链接并理解内容", tools=["browse_url", "login_site"])

# 暴露给 skills 包
SKILLS = ["browse_url"]
SKILL_DESCRIPTION = "达妮娅自己访问网页/图片/视频链接并理解内容"


_IMAGE_EXT_RE = re.compile(
    r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|#|$)", re.IGNORECASE
)
_BILIBILI_VIDEO_RE = re.compile(
    r"(bilibili\.com|b23\.tv)/(video/BV\w+|\w+)", re.IGNORECASE
)
_BILIBILI_BV_RE = re.compile(r"BV[0-9A-Za-z]+")
_VIDEO_EXT_RE = re.compile(
    r"\.(mp4|avi|mov|mkv|webm|flv|wmv|mpg)(\?|#|$)", re.IGNORECASE
)


def _extract_url(query: str) -> str:
    """从 query 中抓取 URL（兼容中文标点）"""
    m = re.search(r"https?://[^\s，。！？\u4e00-\u9fa5<>\"']+|b23\.tv/\w+", query, re.IGNORECASE)
    url = m.group(0) if m else query.strip()
    return url


def _normalize_bilibili(url: str) -> str:
    """b23.tv 短链 / B站播放页 → 标准 BV 链接"""
    if not _BILIBILI_VIDEO_RE.search(url):
        return url
    bv = _BILIBILI_BV_RE.search(url)
    if bv:
        return f"https://www.bilibili.com/video/{bv.group(0)}"
    return url


def _get_downloader():
    """模块级 MediaDownloader 单例（带缓存）"""
    try:
        from media_downloader import MediaDownloader
    except Exception as _e:
        print(f"[browse_url] MediaDownloader 不可用: {_e}")
        return None
    if not hasattr(_get_downloader, "_instance"):
        import threading
        _get_downloader._instance = MediaDownloader(
            cache_dir=os.path.join(os.path.dirname(__file__), "..", "media_cache", "browse")
        )
        _get_downloader._lock = threading.Lock()
    return _get_downloader._instance


def _get_llm():
    """拿到可用 DeniaLLM 实例（用于视觉理解）

    优先级：
      1. 模块级全局变量（桌面路径，chat_controller 里挂的）
      2. 遍历 DeniaChatbot 实例的 llm 属性（QQ 路径，napcat_adapter_ws 创建的）
    """
    # 1. 模块级
    for _mod in sys.modules.values():
        for _attr in ("llm", "_llm", "denia_llm"):
            _v = getattr(_mod, _attr, None)
            if _v and hasattr(_v, "get_vision_response"):
                return _v

    # 2. QQ 路径：找 DeniaChatbot 实例（napcat_adapter_ws 的 bots dict）
    try:
        import napcat_adapter_ws
        for _bot in getattr(napcat_adapter_ws, "bots", {}).values():
            _llm = getattr(_bot, "llm", None)
            if _llm and hasattr(_llm, "get_vision_response"):
                return _llm
    except Exception:
        pass

    return None


def _vision_understand(local_path: str, prompt: str) -> str:
    """本地图片/视频 → 视觉模型理解，返回达妮娅风格描述"""
    llm = _get_llm()
    if not llm:
        return f"（本地文件 {os.path.basename(local_path)}，视觉模型不可用）"
    try:
        reply = llm.get_vision_response(prompt, local_path)
        if reply:
            return f"（已理解 {os.path.basename(local_path)}：{reply}）"
    except Exception as _e:
        print(f"[browse_url] 视觉理解失败: {_e}")
    return f"（文件 {os.path.basename(local_path)} 暂时没理解出来呢）"


def _handle_image_link(url: str, prompt: str) -> str:
    """图片直链：下载 → 视觉理解"""
    dl = _get_downloader()
    if not dl:
        return "（MediaDownloader 初始化失败，没法下载图片呢）"
    try:
        local_path = dl.download_image(url, keyword="browse")
        if not local_path or not os.path.exists(local_path):
            return "（图片下载失败呢）"
        print(f"[browse_url] 图片已下载: {os.path.basename(local_path)}")
        return _vision_understand(local_path, prompt)
    except Exception as _e:
        print(f"[browse_url] 图片链接处理异常: {_e}")
        return f"（图片下载出错：{_e}）"


def _bilibili_video_to_text(url: str, prompt: str) -> str:
    """B站视频：API 拿封面+标题+简介 → 下载封面 → 视觉理解，不下载视频本身"""
    import requests as _req
    dl = _get_downloader()
    if not dl:
        return "（MediaDownloader 初始化失败，没法读取 B站视频呢）"
    try:
        canonical = _normalize_bilibili(url)
        print(f"[browse_url] B站视频（API 路径）: {canonical}")
        info = dl.get_bilibili_video_info(canonical)
        if not info:
            return "（B站 API 没返回信息，可能是视频不存在或反爬了，稍后再试？）"
        print(f"[browse_url] B站视频信息: {info.get('title','?')} / 时长 {info.get('duration', 0)}s")

        # 拼文本描述（标题+简介）给 LLM 作为文字上下文
        parts = []
        if info.get("title"):
            parts.append(f"视频标题：{info['title']}")
        if info.get("desc"):
            parts.append(f"视频简介：{info['desc']}")
        if info.get("owner_name"):
            parts.append(f"UP主：{info['owner_name']}")
        dur = info.get("duration", 0)
        if dur:
            parts.append(f"时长：{dur // 60}分{dur % 60}秒")
        text_ctx = "\n".join(parts)

        # 下载封面图 → 视觉理解（把封面+文字一起喂）
        cover = info.get("cover", "")
        cover_local = None
        if cover:
            cover_local = dl.download_image(cover, keyword="bilibili_cover")
            if not cover_local or not os.path.exists(cover_local):
                cover_local = None

        llm = _get_llm()
        if not llm:
            # 没有视觉模型，直接返回文字信息
            return f"（B站视频信息：{text_ctx}。视觉模型不可用，没法描述画面。）"

        # 有封面图：下载后交给视觉模型（封面+文字一起）
        if cover_local:
            vision_prompt = f"{prompt}\n\n【视频文字信息】\n{text_ctx}"
            try:
                reply = llm.get_vision_response(vision_prompt, cover_local)
                if reply:
                    return f"（已理解 B站视频封面+信息：{reply}\n{text_ctx}）"
            except Exception as _e:
                print(f"[browse_url] B站封面视觉理解失败: {_e}")

        # 无封面或视觉理解失败：直接返回文字信息，LLM 也能根据标题/简介回答
        return f"（B站视频信息：{text_ctx}。视频画面暂时没有获取到，根据标题和简介回答。）"
    except Exception as _e:
        print(f"[browse_url] B站视频处理异常: {_e}")
        return f"（B站视频处理出错：{_e}）"


def _handle_video_link(url: str, prompt: str) -> str:
    """通用视频直链（非 B站）：yt-dlp 下载（与 B站视频共用方法）"""
    dl = _get_downloader()
    if not dl:
        return "（MediaDownloader 初始化失败，没法下载视频呢）"
    try:
        local_path = dl.download_bilibili_video(url, keyword="browse")
        if not local_path or not os.path.exists(local_path):
            return "（视频下载失败呢，需要安装 yt-dlp：pip install yt-dlp）"
        print(f"[browse_url] 视频已下载: {os.path.basename(local_path)}")
        return _vision_understand(local_path, prompt)
    except Exception as _e:
        print(f"[browse_url] 视频直链处理异常: {_e}")
        return f"（视频下载出错：{_e}）"


def _handle_web_page(url: str, prompt: str) -> str:
    """普通网页：先无痕抓取，遇到登录墙/空壳再用持久化会话重试"""
    # 登录窗口还开着时浏览器线程被独占，此时抓取会白等到超时，直接说清楚
    if _LOGIN_STATE["running"]:
        return "（登录窗口还开着呢，等你登完把窗口关掉，我再去抓这个页面）"
    text = ""
    try:
        from web_search import crawl_page
        text = (crawl_page(url) or "").strip()
    except Exception as e:
        print(f"[browse_url] 无痕抓取失败: {e}")

    # 空壳或登录墙 → 用带登录态的持久化会话重试（访客无权借用主人的登录态）
    need_session = (not text) or len(text) < 120
    _net_error = ""
    _anon = False
    if (need_session or _looks_like_login_page(url, text)) and _session_allowed():
        try:
            import browser_session
            hit = browser_session.fetch(url)
            _anon = bool(hit.get("anonymous"))
            if hit.get("text"):
                text = hit["text"].strip()
            if hit.get("login_wall") and len(text) < 200:
                if _anon:
                    return ("（这页要登录，但登录态正被另一个进程占着，我这次只能匿名看。"
                            "等那边空闲一会儿再叫我，或者现在跟我说「登录 <网址>」）")
                return ("（这个页面要登录才能看，我还进不去呢。"
                        "跟我说一句「登录 <网址>」，我会弹个窗口让你登一次，之后就记住了）")
            if not hit.get("ok"):
                _net_error = hit.get("error") or ""
        except Exception as e:
            print(f"[browse_url] 持久化会话失败: {e}")
            _net_error = str(e)

    if not text or len(text.strip()) < 10:
        if _net_error and ("net::" in _net_error or "Timeout" in _net_error or "ERR_" in _net_error):
            return ("（这个网站我连不上呢，像是网络被挡住了。"
                    "检查一下代理有没有开（settings.json 里的 browser_proxy）再叫我？）")
        return "（这个页面好像没什么内容呢…可能是动态加载的页面，我读不到正文）"
    limit = 2000
    if len(text) > limit:
        text = text[:limit] + "\n（…正文较长已截断）"
    return "（网页内容：%s）\n请用达妮娅的语气，根据以上内容自然回答，不要说'根据网页内容'。" % text


def _session_allowed() -> bool:
    """访客（非主人）不能借用持久化登录态，避免泄漏主人账号里的内容"""
    try:
        from tool_registry import is_remote_restricted
        return not is_remote_restricted()
    except Exception:
        return True


def _looks_like_login_page(url: str, text: str) -> bool:
    """粗判登录墙（持久化会话内部还有更精确的判断）"""
    if re.search(r"(login|signin|sign-in|passport|sso|/auth)", url or "", re.IGNORECASE):
        return True
    head = (text or "")[:500]
    return bool(re.search(r"(请先登录|请登录|登录后查看|扫码登录|立即登录|账号登录|未登录)",
                          head, re.IGNORECASE))


def _fetch_url_text(url: str, prompt: str = None) -> str:
    """路由：根据 URL 类型分发到不同处理器"""
    default_prompt = prompt or "请描述这个内容"
    try:
        if _IMAGE_EXT_RE.search(url):
            return _handle_image_link(url, default_prompt)
        if _BILIBILI_VIDEO_RE.search(url) or _BILIBILI_BV_RE.search(url):
            return _bilibili_video_to_text(url, default_prompt)
        if _VIDEO_EXT_RE.search(url):
            return _handle_video_link(url, default_prompt)
        return _handle_web_page(url, default_prompt)
    except Exception as _e:
        print(f"[browse_url] 路由异常: {_e}")
        return f"（链接处理出错：{_e}）"


# 登录任务状态：同一时间只允许一个登录窗口，避免连点开出一堆 Edge
_LOGIN_STATE = {"running": False, "url": ""}
_LOGIN_LOCK = threading.Lock()


def _login_worker(url: str):
    """后台等用户登录完关窗，避免把消息线程占住几分钟"""
    try:
        import browser_session
        msg = browser_session.login(url)
        print(f"[login_site] 登录流程结束: {msg}")
    except Exception as e:
        print(f"[login_site] 登录失败: {e}")
    finally:
        with _LOGIN_LOCK:
            _LOGIN_STATE["running"] = False
            _LOGIN_STATE["url"] = ""


@tool(
    name="login_site",
    description=("打开一个可见浏览器窗口，让用户手动登录指定网站（扫码/账号密码），登录态保存后长期复用。"
                 "登录态只存在达妮娅自己的浏览器 profile 里，用户在自己浏览器里登录不会共享过来，"
                 "所以必须在这个弹出的窗口里登录"),
    keywords=["登录网站", "登陆网站", "帮我登录", "帮我登陆", "登录一下", "登陆一下",
              "扫码登录", "登录这个", "登陆这个"],
)
def login_site(query: str) -> str:
    """从 query 抓 URL，弹出浏览器让用户登录；等待在后台进行，不卡住对话。"""
    url = _extract_url(query)
    if not url.startswith("http"):
        return "（告诉我具体网址吧，比如「登录 https://www.kurobbs.com」）"
    with _LOGIN_LOCK:
        if _LOGIN_STATE["running"]:
            return (f"登录窗口已经开着啦（{_LOGIN_STATE['url'][:40]}），"
                    "在那里面登完把窗口关掉就行。")
        _LOGIN_STATE["running"] = True
        _LOGIN_STATE["url"] = url
    print(f"[login_site] 打开登录窗口: {url}")
    threading.Thread(target=_login_worker, args=(url,), name="login-site", daemon=True).start()
    return ("登录窗口打开啦，得在这个窗口里扫码或者输账号哦——"
            "你自己平时那个浏览器登的不算数，我读不到那边的登录态。"
            "登完把窗口关掉，登录态就存下来了。登完跟我说一声，我再去。")



@tool(
    name="browse_url",
    description="达妮娅访问指定 URL 并理解内容：图片直链、B站视频（封面+标题+简介，不下载）、普通网页正文",
    keywords=["url", "链接", "网页", "网站", "页面", "文章",
              "这个网站", "这个页面", "https", "http"],
)
def browse_url(query: str) -> str:
    """
    从 query 中抓取 URL，按类型路由：
      - 图片直链 → 下载 + 视觉理解
      - B站视频 → yt-dlp + 抽帧 + 视觉理解
      - 其他视频 → 下载 + 视觉理解
      - 普通网页 → 抓正文
    支持格式：
      - 纯 URL：https://...
      - "看看 https://..." / "访问 https://..." 等含 URL 的自然语言
    """
    url = _extract_url(query)
    if not url.startswith(("http", "b23")):
        return "（请给我一个完整的网址（以 http/https 或 b23.tv 开头），我才打开哦）"

    # prompt：用户原始文本（含链接意图）
    user_prompt = query
    return _fetch_url_text(url, user_prompt)
