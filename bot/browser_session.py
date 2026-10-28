# -*- coding: utf-8 -*-
"""
持久化浏览器会话
用 Playwright 的 launch_persistent_context 把登录态存到本地 profile 目录，
首次由用户在弹窗里手动登录一次，之后所有抓取复用同一份 cookie。

注意：
  - Playwright 同步 API 不能跨线程，所有操作都在一个常驻浏览器线程里串行执行
  - 抓取复用同一个 headless 上下文，空闲 3 分钟自动关闭
  - profile 目录等价于账号钥匙，不要外传
"""
import json
import os
import queue
import re
import shutil
import socket
import tempfile
import threading
import time

PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_profile")
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "settings.json")

# 代理配置缓存（改 settings.json 无需重启）
_PROXY_CACHE = {"mtime": 0, "proxy": ""}
# 代理失败冷却：失败后 3 分钟内不再尝试，直接走直连
_PROXY_DOWN = {"proxy": "", "until": 0.0}
# 未配置代理时自动探测的常见本地端口（Clash / v2rayN / SSR 等）
_COMMON_PROXY_PORTS = [7897, 7890, 10809, 10808, 1080, 8080]
_PROXY_DETECT = {"at": 0.0, "proxy": ""}


def _probe_proxy(server: str) -> bool:
    """用一条 204 探测请求确认这个端口真的是可用代理（而不只是端口开着）"""
    try:
        import requests
        r = requests.get(
            "https://www.gstatic.com/generate_204",
            proxies={"http": server, "https": server},
            timeout=1.5,
        )
        return r.status_code in (204, 200)
    except Exception:
        return False


def _autodetect_proxy() -> str:
    """browser_proxy 留空时自动找本机代理，命中结果缓存 2 分钟"""
    if time.time() - _PROXY_DETECT["at"] < 120:
        return _PROXY_DETECT["proxy"]
    hit = ""
    for port in _COMMON_PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            continue
        server = f"http://127.0.0.1:{port}"
        if _probe_proxy(server):
            hit = server
            break
    _PROXY_DETECT["at"] = time.time()
    _PROXY_DETECT["proxy"] = hit
    if hit:
        print(f"[持久化浏览器] 自动探测到本机代理 {hit}")
    return hit


def _proxy_server() -> str:
    """读 settings.json 的 browser_proxy，留空表示直连；按 mtime 缓存"""
    try:
        mt = os.path.getmtime(_SETTINGS_PATH)
        if _PROXY_CACHE["mtime"] == mt:
            cached = _PROXY_CACHE["proxy"]
        else:
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cached = str(cfg.get("browser_proxy") or "").strip()
            _PROXY_CACHE["mtime"] = mt
            _PROXY_CACHE["proxy"] = cached
        # 显式配置优先；留空则自动探测常见本地端口
        if cached:
            # 代理没开时直接走直连，别交给 Playwright 白等几秒再失败
            if _proxy_alive(cached):
                return cached
            return ""
        return _autodetect_proxy()
    except Exception as e:
        print(f"[持久化浏览器] 读取代理配置失败: {e}")
        return ""


# 配置代理的端口探测（只做 TCP 连接，结果缓存 60 秒，状态变化才打日志）
_PROXY_CHECK = {"server": "", "at": 0.0, "ok": False, "logged": None}


def _proxy_alive(server: str) -> bool:
    """判断代理端口是否在监听；只连 TCP，不发请求，避免拖慢抓取"""
    m = re.match(r"^(?:https?|socks5h?)://([^:/@]+):(\d+)/?$", server or "")
    if not m:
        # 解析不了（远程域名/带认证等）就交给 Playwright 自己试
        return True
    host, port = m.group(1), int(m.group(2))
    cached = (
        _PROXY_CHECK["server"] == server
        and time.time() - _PROXY_CHECK["at"] < 60
    )
    if cached:
        return _PROXY_CHECK["ok"]
    timeout = 0.3 if host in ("127.0.0.1", "localhost") else 0.8
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ok = True
    except OSError:
        ok = False
    if ok != _PROXY_CHECK["logged"]:
        print(f"[持久化浏览器] 代理 {server} {'可用' if ok else '端口没开，改用直连'}")
        _PROXY_CHECK["logged"] = ok
    _PROXY_CHECK.update({"server": server, "at": time.time(), "ok": ok})
    return ok


# 正文容器候选（含 X/Twitter 等 SPA 的专用容器）

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]

# 登录墙特征（只认真正的登录页路径，避免 /author 这类误撞 "/auth"）
_LOGIN_URL_RE = re.compile(
    r"(?:/i/flow/login|/login(?:[/?#]|$)|/signin(?:[/?#]|$)|passport\.|/auth/login)",
    re.IGNORECASE)
_LOGIN_TEXT_RE = re.compile(r"(请先登录|请登录|登录后查看|登录后可见|扫码登录|立即登录|账号登录|"
                            r"未登录|登录以|sign in|log in)", re.IGNORECASE)

_STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });"
    "window.chrome = { runtime: {} };"
)

# Edge 自己产生的缓存目录（删了会自动重建，不影响 Cookies / 登录态）
_CACHE_SUBDIRS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
)


def cleanup_profile_cache() -> int:
    """清掉 profile 里的 Edge 缓存目录，返回释放的字节数（登录态不受影响）"""
    freed = 0
    for sub in _CACHE_SUBDIRS:
        target = os.path.join(PROFILE_DIR, *sub.split("/"))
        if not os.path.isdir(target):
            continue
        try:
            for dirpath, _dirs, files in os.walk(target):
                for fn in files:
                    try:
                        freed += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
            shutil.rmtree(target)
        except OSError:
            print(f"[持久化浏览器] 缓存目录正被占用，跳过: {sub}")
    if freed > 1048576:
        print(f"[持久化浏览器] 已清理 Edge 缓存 {freed / 1048576:.1f} MB")
    return freed


# 正文容器（与 web_search 保持一致，另加社区类/SPA 站点常见容器）
_TEXT_SELECTORS = [
    "div[data-testid='tweetText']",          # X / Twitter 推文正文
    "article[data-testid='tweet']",          # X / Twitter 整条推文
    "article", ".article-content", ".post-content", ".topic-content",
    ".wiki-content", ".entry-content", ".content-body", ".detail-content",
    "#content", "#article", ".main-content", "main",
]


def _launch(p, headless: bool, proxy: str = "", profile: str = ""):
    """优先用系统 Edge（无需下载 Playwright 浏览器），不可用时回落自带 chromium

    profile 为空时用共享登录态目录；传临时目录则得到一个匿名干净上下文。
    """
    kwargs = dict(
        user_data_dir=profile or PROFILE_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 860},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=_UA,
        ignore_https_errors=True,
        args=_LAUNCH_ARGS,
        # Playwright 默认带 --enable-automation，会把 navigator.webdriver 置为 true，
        # X 这类站点会据此判定"自动化登录"直接拦截，登录窗口必须去掉它
        ignore_default_args=["--enable-automation"],
    )
    if proxy:
        kwargs["proxy"] = {"server": proxy}
        print(f"[持久化浏览器] 使用代理 {proxy}")
    try:
        ctx = p.chromium.launch_persistent_context(channel="msedge", **kwargs)
    except Exception as e:
        print(f"[持久化浏览器] Edge 通道不可用（{str(e)[:80]}），改用自带 chromium")
        ctx = p.chromium.launch_persistent_context(**kwargs)
    try:
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    except Exception:
        pass
    return ctx


def _is_login_wall(final_url: str, text: str) -> bool:
    """判断当前页面是不是登录墙（正文很短才算，长页面里的「登录」按钮是误报）"""
    if _LOGIN_URL_RE.search(final_url or ""):
        return True
    head = (text or "")[:800]
    if not _LOGIN_TEXT_RE.search(head):
        return False
    return len((text or "").strip()) < 1200


def _has_login_cookie(ctx, domain: str) -> bool:
    """profile 里有没有该站点的登录 cookie（比猜页面文字可靠）"""
    try:
        for c in ctx.cookies():
            d = str(c.get("domain") or "").lstrip(".").lower()
            if d.endswith(domain) and c.get("value") and c.get("name") in ("auth_token", "ct0"):
                return True
    except Exception:
        pass
    return False


_NAV_WORDS = (
    "首页", "登录", "注册", "下载", "客户端", "关于我们", "联系我们", "隐私", "协议",
    "广告", "更多", "上一页", "下一页", "返回", "分享", "评论", "收藏", "点赞", "关注",
    "粉丝", "推荐", "热门", "排行榜", "分类", "标签", "专题", "帮助", "客服", "导航",
    "版权所有", "免责声明", "友情链接", "论坛", "板块", "版块",
)


def _is_nav_line(text: str) -> bool:
    """判断一行是不是导航/按钮/分类链接这类噪声"""
    t = text.strip()
    if not t:
        return True
    if len(t) <= 12 and any(w in t for w in _NAV_WORDS):
        return True
    return any(t.startswith(w) or t == w for w in _NAV_WORDS)


def _score_parts(parts) -> float:
    """按内容密度给候选容器打分：长段落占比越高分越高，导航行越多扣分越多"""
    total = sum(len(p) for p in parts)
    if total <= 0:
        return 0.0
    long_chars = sum(len(p) for p in parts if len(p) >= 25)
    density = long_chars / max(total, 1)
    nav_hits = sum(1 for p in parts if _is_nav_line(p))
    return long_chars * density - nav_hits * 30


def extract_text(page) -> str:
    """按内容密度挑选最佳正文容器（支持一个容器多条，如 X 的一串推文），抽不到就全页 p/li"""
    best_text, best_score, best_total = "", 0.0, 0
    for sel in _TEXT_SELECTORS:
        parts = []
        try:
            nodes = page.query_selector_all(sel)
            if not nodes:
                continue
            for node in nodes[:60]:
                txt = (node.inner_text() or "").strip()
                if len(txt) < 10:
                    continue
                parts.append(txt)
                if sum(len(x) for x in parts) > 3000:
                    break
        except Exception:
            continue
        if sum(len(x) for x in parts) < 60:
            continue
        score = _score_parts(parts)
        total = sum(len(x) for x in parts)
        # 同为高密度时选更长的，避免窄容器先命中
        if score > best_score or (score == best_score and total > best_total):
            best_text, best_score, best_total = "\n".join(parts).strip(), score, total

    if best_text and best_score > 0:
        lines = [ln for ln in best_text.splitlines() if not _is_nav_line(ln)]
        if sum(len(x) for x in lines) >= 60:
            return "\n".join(lines).strip()

    # 兜底：全页 p/li（同样剔除导航行）
    parts = []
    try:
        for tag in page.query_selector_all("p, li"):
            t = (tag.inner_text() or "").strip()
            if t and len(t) >= 6 and not _is_nav_line(t):
                parts.append(t)
            if sum(len(x) for x in parts) > 3000:
                break
    except Exception:
        pass
    return "\n".join(parts).strip()


# ── 常驻浏览器线程 ────────────────────────────────────────────────
# Playwright 同步 API 不能跨线程使用，所以所有浏览器操作都投递到同一个专用线程里执行。
# 页面抓取复用同一个 headless 上下文，省掉每次 1.5~2 秒的启动开销；
# 空闲超过 _IDLE_CLOSE_SECONDS 自动关闭释放内存。
_IDLE_CLOSE_SECONDS = 180
_JOBS = queue.Queue()
_WORKER = {"thread": None, "lock": threading.Lock()}


def _ensure_worker():
    with _WORKER["lock"]:
        t = _WORKER["thread"]
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=_worker_loop, name="denia-browser", daemon=True)
        _WORKER["thread"] = t
        t.start()


def _submit(job: dict, wait_seconds: float):
    """投递任务到浏览器线程并等结果"""
    _ensure_worker()
    ev = threading.Event()
    job["ev"] = ev
    _JOBS.put(job)
    if not ev.wait(wait_seconds):
        raise TimeoutError("浏览器任务超时")
    return job.get("result")


def _close_ctx(state: dict):
    try:
        if state.get("ctx") is not None:
            state["ctx"].close()
    except Exception:
        pass
    state["ctx"] = None
    state["proxy"] = ""
    # 匿名临时 profile 用完即删
    tmp = state.pop("anon_dir", "")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    state["anon"] = False


def _ctx_alive(ctx) -> bool:
    try:
        _ = ctx.pages
        return True
    except Exception:
        return False


def _get_ctx(state: dict, proxy: str):
    """复用健康的上下文；代理变了或上下文坏了就重建

    Chromium 的 persistent profile 是单进程独占的，桌面端和 QQ 机器人
    同时用同一个 profile 时后启动的一方会失败。这里自动降级成匿名临时
    profile：公开页面照常读，只是拿不到登录态。
    """
    from playwright.sync_api import sync_playwright

    if state["ctx"] is not None:
        if state["proxy"] == proxy and _ctx_alive(state["ctx"]):
            return state["ctx"]
        _close_ctx(state)
    if state["pw"] is None:
        state["pw"] = sync_playwright().start()
    try:
        ctx = _launch(state["pw"], headless=True, proxy=proxy)
    except Exception as e:
        tmp = tempfile.mkdtemp(prefix="denia_anon_")
        print(f"[持久化浏览器] 登录态 profile 打不开（{str(e)[:70]}），"
              "改用匿名临时 profile（只能读公开内容）")
        ctx = _launch(state["pw"], headless=True, proxy=proxy, profile=tmp)
        state["anon_dir"] = tmp
        state["anon"] = True
    ctx.add_init_script(_STEALTH_JS)
    state["ctx"] = ctx
    state["proxy"] = proxy
    return ctx


def _run_fetch(job: dict, state: dict) -> dict:
    url = job["url"]
    timeout = job.get("timeout", 20000)
    proxy = job.get("proxy", "")
    result = {"ok": False, "text": "", "login_wall": False, "final_url": url,
              "error": "", "proxy": "", "anonymous": False}
    # 配了代理就先用代理；代理不通（没开/端口错）自动回落直连
    plans = [proxy, ""] if proxy else [""]
    for px in plans:
        result["error"] = ""
        try:
            ctx = _get_ctx(state, px)
            result["anonymous"] = bool(state.get("anon"))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(800)
            result["final_url"] = page.url
            try:
                result["title"] = page.title()
            except Exception:
                result["title"] = ""
            result["text"] = extract_text(page)
            result["login_wall"] = _is_login_wall(page.url, result["text"])
            result["ok"] = True
            result["proxy"] = px
            print(f"[持久化浏览器] {page.url[:70]} 正文 {len(result['text'])} 字"
                  f"{'（登录墙）' if result['login_wall'] else ''}")
            return result
        except Exception as e:
            result["error"] = str(e)
            print(f"[持久化浏览器] 抓取失败(proxy={px or '直连'}): {str(e)[:160]}")
            if px and "ERR_PROXY" in str(e):
                _PROXY_DOWN["proxy"] = px
                _PROXY_DOWN["until"] = time.time() + 180
            # 上下文可能已经坏了，下次重建
            _close_ctx(state)
    return result


# ── 页面配图提取 ──────────────────────────────────────────────────
_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_cache", "page_images")
_IMG_BAD = ("logo", "icon", "avatar", "sprite", "loading", "placeholder",
            "blank.gif", "1x1", "spacer", "emoji", "badge", "qrcode",
            "profile_images", "profile_banners")


def _pick_images(page, limit: int) -> list:
    """挑页面里的图片：X 推文图优先，其次 og:image，最后正文大图"""
    urls, seen = [], set()

    def add(u: str, force: bool = False):
        u = (u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith(("http://", "https://")) or u.startswith("data:"):
            return
        # 站点模板里的占位文本（如 "<link or path of image for open>"）不是网址
        low = u.lower()
        if re.search(r"[\s<>\"']", u) or "%3c" in low or "%3e" in low or "link%20or%20path" in low:
            return
        if not force and any(b in low for b in _IMG_BAD):
            return
        base = low.split("?")[0]
        if base in seen:
            return
        seen.add(base)
        urls.append(u)

    # 1) X / Twitter 推文图片（缩略图换成大图）；放在最前，避免头像/横幅挤占名额
    try:
        for node in page.query_selector_all("img[src*='pbs.twimg.com/media']"):
            src = node.get_attribute("src") or ""
            if "name=" in src:
                src = re.sub(r"name=[A-Za-z0-9_]+", "name=large", src)
            elif "?" not in src:
                src += "?format=jpg&name=large"
            add(src, force=True)
            if len(urls) >= limit:
                return urls[:limit]
    except Exception:
        pass

    # 2) 页面声明的封面图
    try:
        for meta in page.query_selector_all(
                "meta[property='og:image'], meta[name='og:image'], "
                "meta[property='twitter:image'], meta[name='twitter:image']"):
            add(meta.get_attribute("content") or "", force=True)
    except Exception:
        pass

    # 3) 正文里尺寸够大的图（懒加载站点补 data-src / data-original）
    try:
        for node in page.query_selector_all("img"):
            src = (node.get_attribute("src")
                   or node.get_attribute("data-src")
                   or node.get_attribute("data-original") or "")
            w_attr = (node.get_attribute("width") or "").strip()
            if w_attr.isdigit() and int(w_attr) < 200:
                continue
            try:
                box = node.bounding_box() or {}
                if box.get("width", 0) and box["width"] < 180:
                    continue
            except Exception:
                pass
            add(src)
            if len(urls) >= limit:
                break
    except Exception:
        pass
    return urls[:limit]


def _pick_x_search_images(page, limit: int, keyword: str = "") -> list:
    """X 搜索结果页专用：只在推文正文里取图，文本命中关键词的推文优先"""
    kw = (keyword or "").lower().strip()
    cands, order = [], 0
    try:
        arts = page.query_selector_all("article[data-testid='tweet']")
    except Exception:
        return []
    for art in arts:
        try:
            txt = (art.inner_text() or "").lower()
        except Exception:
            txt = ""
        score = 2 if kw and kw in txt else 0
        try:
            nodes = art.query_selector_all("img[src*='pbs.twimg.com/media']")
        except Exception:
            nodes = []
        for node in nodes[:2]:          # 同一条推文最多取 2 张，避免刷屏
            src = node.get_attribute("src") or ""
            if "name=" in src:
                src = re.sub(r"name=[A-Za-z0-9_]+", "name=large", src)
            elif "?" not in src:
                src += "?format=jpg&name=large"
            if src:
                cands.append((score, order, src))
            order += 1
    cands.sort(key=lambda x: (-x[0], x[1]))
    seen, out = set(), []
    for _, _, src in cands:
        base = src.lower().split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        out.append(src)
        if len(out) >= limit:
            break
    return out


def _run_images(job: dict, state: dict) -> dict:
    """打开页面，挑图并用页面上下文下载（自带 cookie / Referer，绕防盗链）"""
    url = job["url"]
    limit = max(1, min(int(job.get("count") or 3), 6))
    proxy = job.get("proxy", "")
    out = {"ok": False, "paths": [], "title": "", "urls": [],
           "error": "", "anonymous": False}
    plans = [proxy, ""] if proxy else [""]
    for px in plans:
        out["error"] = ""
        try:
            ctx = _get_ctx(state, px)
            out["anonymous"] = bool(state.get("anon"))
            _is_x = "x.com" in url or "twitter.com" in url
            if _is_x:
                # 先看 cookie 判断登录态，别拿「页面没出结果」当成没登录
                out["logged_in"] = _has_login_cookie(ctx, "x.com")
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=job.get("timeout", 25000))
                page.wait_for_timeout(int(job.get("settle") or 1200))
                if "/i/flow/login" in (page.url or ""):
                    out["error"] = "这页把我拦在门外了，看着像是得先登一下才行"
                    return out
                # 有些站点（如 X 搜索页）结果区是懒加载的，要轮询等一会儿才出内容
                need = job.get("require")
                if need:
                    ok = False
                    tries = 8 if _is_x else 5      # X 结果区更慢，最多等约 16 秒
                    for _ in range(tries):
                        try:
                            if page.query_selector(need):
                                ok = True
                                break
                        except Exception:
                            pass
                        try:
                            page.mouse.wheel(0, 1200)   # 触发懒加载
                        except Exception:
                            pass
                        page.wait_for_timeout(2000)
                    if not ok:
                        if _is_x and out.get("logged_in"):
                            # 登录 cookie 在，只是页面没渲染出结果
                            out["error"] = "结果一直没加载出来——不是登录的问题，多半是网络或页面抽风"
                        else:
                            out["login_wall"] = True
                            out["error"] = ("没登上，X 不给陌生人看搜索结果" if _is_x
                                            else "页面迟迟没给我结果，可能只是慢，也可能它不太欢迎我这个陌生访客")
                        return out
                for _ in range(max(1, int(job.get("scrolls") or 1))):
                    try:
                        page.mouse.wheel(0, 1500)      # 触发懒加载
                        page.wait_for_timeout(700)
                    except Exception:
                        break
                try:
                    out["title"] = page.title()
                except Exception:
                    pass
                if "/search" in url and ("x.com" in url or "twitter.com" in url):
                    srcs = _pick_x_search_images(page, limit, job.get("keyword") or "")
                else:
                    srcs = _pick_images(page, limit)
                out["urls"] = srcs
                if not srcs:
                    out["error"] = "页面上没找到图片"
                    return out
                os.makedirs(_IMAGE_DIR, exist_ok=True)
                for i, src in enumerate(srcs):
                    try:
                        resp = page.request.get(src, headers={"Referer": url}, timeout=20000)
                        if not resp.ok:
                            print(f"[抓图] HTTP {resp.status} {src[:60]}")
                            continue
                        body = resp.body()
                        if not body or len(body) < 2048:
                            continue
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if "png" in ctype:
                            ext = ".png"
                        elif "webp" in ctype:
                            ext = ".webp"
                        elif "gif" in ctype:
                            ext = ".gif"
                        else:
                            ext = ".jpg"
                        path = os.path.join(_IMAGE_DIR, f"page_{int(time.time())}_{i}{ext}")
                        with open(path, "wb") as f:
                            f.write(body)
                        out["paths"].append(path)
                    except Exception as e:
                        print(f"[抓图] 下载失败 {src[:60]}: {e}")
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            out["ok"] = bool(out["paths"])
            if not out["ok"] and not out["error"]:
                out["error"] = "图片下载失败"
            print(f"[抓图] {url[:60]} → 候选 {len(out['urls'])} 张，下载成功 {len(out['paths'])} 张")
            return out
        except Exception as e:
            out["error"] = str(e)
            print(f"[抓图] 失败(proxy={px or '直连'}): {str(e)[:140]}")
            _close_ctx(state)
    return out


def _run_login(job: dict, state: dict) -> str:
    """弹出可见窗口让用户登录；窗口关闭或超时后保存登录态"""
    from playwright.sync_api import sync_playwright

    url = job["url"]
    wait_seconds = job.get("wait_seconds", 150)
    proxy = job.get("proxy", "")
    _close_ctx(state)  # 登录独占 profile，先放掉常驻上下文
    if state["pw"] is None:
        state["pw"] = sync_playwright().start()

    ctx = None
    try:
        ctx = _launch(state["pw"], headless=False, proxy=proxy)
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            # 代理配错/没开时，关掉重开一个直连窗口
            if proxy:
                print(f"[登录] 代理 {proxy} 打开失败（{str(e)[:80]}），改用直连重试")
                try:
                    ctx.close()
                except Exception:
                    pass
                ctx = _launch(state["pw"], headless=False, proxy="")
                ctx.add_init_script(_STEALTH_JS)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            else:
                raise
        print(f"[登录] 已打开登录窗口: {url}，等待手动登录…")
        start = time.time()
        _timed_out = False
        # 用户关掉浏览器窗口后 ctx.pages 会变空（或抛异常）
        while time.time() - start < wait_seconds:
            try:
                if not ctx.pages:
                    break
            except Exception:
                break
            time.sleep(1)
        else:
            _timed_out = True
        elapsed = int(time.time() - start)
        try:
            ctx.close()
        except Exception:
            pass
        print(f"[登录] 结束，用时 {elapsed}s，登录态已保存到 {PROFILE_DIR}")
        if _timed_out:
            return (f"（等了 {elapsed} 秒你还没关窗口，我先把它关掉了。"
                    "登录态已尽力保存；要是没登上，再跟我说一次「登录 <网址>」）")
        return f"（登录窗口已关闭，用时 {elapsed} 秒，登录状态已保存）"
    except Exception as e:
        print(f"[登录] 失败: {e}")
        _low = str(e).lower()
        hint = ""
        if "closed" in _low or "profile" in _low or "singleton" in _low:
            hint = ("（像是登录态被别的进程占着——桌面端或者 QQ 机器人正开着浏览器，"
                   "等它空闲一会儿再喊我登录）")
        elif "timeout" in _low or "err_" in _low:
            hint = ("（这个网址打不开——直连不通的话，先把顶栏的 WARP 连上，"
                   "或者配置好 browser_proxy 再试一次）")
        return f"（打开登录窗口失败：{e}）{hint}"
    finally:
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass


def _worker_loop():
    state = {"pw": None, "ctx": None, "proxy": "", "last": 0.0,
             "anon": False, "anon_dir": ""}
    while True:
        try:
            job = _JOBS.get(timeout=60)
        except queue.Empty:
            # 空闲太久就关掉常驻浏览器，避免长期占内存
            if state["ctx"] is not None and time.time() - state["last"] > _IDLE_CLOSE_SECONDS:
                print("[持久化浏览器] 空闲超时，关闭常驻浏览器")
                _close_ctx(state)
            continue
        try:
            if job.get("type") == "login":
                job["result"] = _run_login(job, state)
            elif job.get("type") == "images":
                job["result"] = _run_images(job, state)
            else:
                job["result"] = _run_fetch(job, state)
        except Exception as e:
            job["result"] = {"ok": False, "text": "", "login_wall": False,
                             "final_url": job.get("url", ""), "error": str(e)}
            print(f"[持久化浏览器] 任务异常: {e}")
        finally:
            state["last"] = time.time()
            job["ev"].set()


def fetch(url: str, timeout: int = 20000) -> dict:
    """
    用持久化会话抓页面（复用常驻 headless 上下文）。
    返回 {"ok": bool, "text": str, "login_wall": bool, "final_url": str, "error": str}
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)
    proxy = _proxy_server()
    # 代理刚失败过就在 3 分钟内直接走直连，避免每次都白等一次尝试
    if proxy and _PROXY_DOWN["proxy"] == proxy and time.time() < _PROXY_DOWN["until"]:
        proxy = ""
    try:
        return _submit(
            {"type": "fetch", "url": url, "timeout": timeout, "proxy": proxy},
            wait_seconds=(timeout / 1000.0) * 2 + 30,
        )
    except Exception as e:
        print(f"[持久化浏览器] 抓取任务失败: {e}")
        return {"ok": False, "text": "", "login_wall": False, "final_url": url, "error": str(e)}


def fetch_images(url: str, count: int = 3) -> dict:
    """提取并下载页面配图（复用常驻会话，带登录态与 Referer）

    返回 {"ok": bool, "paths": [本地文件], "urls": [原图地址], "title": str,
          "error": str, "anonymous": bool}
    """
    if not (url or "").strip():
        return {"ok": False, "paths": [], "urls": [], "title": "", "error": "网址为空"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    os.makedirs(PROFILE_DIR, exist_ok=True)
    proxy = _proxy_server()
    if proxy and _PROXY_DOWN["proxy"] == proxy and time.time() < _PROXY_DOWN["until"]:
        proxy = ""
    job = {"type": "images", "url": url, "count": count,
           "timeout": 25000, "proxy": proxy}
    try:
        res = _submit(job, wait_seconds=120)
    except Exception as e:
        print(f"[持久化浏览器] 抓图任务失败: {e}")
        return {"ok": False, "paths": [], "urls": [], "title": "", "error": str(e)}
    return res or {"ok": False, "paths": [], "urls": [], "title": "", "error": "无结果"}


def search_x_images(keyword: str, count: int = 3) -> dict:
    """在 X 上按关键词搜索（只看带图的推文），抓图并下载到本地

    返回结构与 fetch_images 相同，额外带 query 字段。
    X 对未登录搜索有登录墙，必须先用 login() 登录一次。
    """
    from urllib.parse import quote

    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "paths": [], "urls": [], "title": "", "error": "关键词是空的"}
    # 默认按「热门/相关」排序，避免 f=live 把时间序的无关内容排前面
    url = ("https://x.com/search?q=" + quote(f"{kw} filter:images")
           + "&src=typed_query")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    proxy = _proxy_server()
    if proxy and _PROXY_DOWN["proxy"] == proxy and time.time() < _PROXY_DOWN["until"]:
        proxy = ""
    job = {"type": "images", "url": url, "count": count, "timeout": 30000,
           "settle": 2500, "scrolls": 2, "proxy": proxy, "keyword": kw,
           "require": "article[data-testid='tweet']"}
    try:
        res = _submit(job, wait_seconds=140)
    except Exception as e:
        print(f"[持久化浏览器] X 搜图任务失败: {e}")
        res = {"ok": False, "paths": [], "urls": [], "title": "", "error": str(e)}
    res = res or {"ok": False, "paths": [], "urls": [], "title": "", "error": "无结果"}
    res["query"] = kw
    return res


def login(url: str, wait_seconds: int = 600) -> str:
    """
    打开可见浏览器窗口让用户手动登录，窗口关闭后登录态写入 profile。

    注意：profile 是独立目录，用户自己浏览器的登录态不会共享过来，
    必须在这个窗口里登录（默认给 10 分钟，扫码/验证码都够用）。
    """
    os.makedirs(PROFILE_DIR, exist_ok=True)
    try:
        return _submit(
            {"type": "login", "url": url, "wait_seconds": wait_seconds, "proxy": _proxy_server()},
            wait_seconds=wait_seconds + 60,
        )
    except Exception as e:
        print(f"[登录] 任务失败: {e}")
        return f"（打开登录窗口失败：{e}）"
