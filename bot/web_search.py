#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联网搜索模块 - Playwright Edge Bing
支持普通搜索（摘要）和深度爬取（进入页面抓正文）
比 Selenium 更快更稳更轻量

重要：所有 Playwright 操作通过专用浏览器线程执行，
      解决 Playwright sync API 不能跨线程调用的问题（greenlet.error）
"""

import re
import os
import time
import threading
import queue
from typing import Optional, List

from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PWTimeout

# 系统 Edge 路径
_EDGE_PATH = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def _clean_text(text: str) -> str:
    """清理文本中的特殊 Unicode 字符，避免 GBK 编码错误"""
    if not text:
        return ""
    # 替换特殊空格字符为普通空格
    text = text.replace('\u2002', ' ')  # EN SPACE
    text = text.replace('\u2003', ' ')  # EM SPACE
    text = text.replace('\u2009', ' ')  # THIN SPACE
    text = text.replace('\u00a0', ' ')  # NO-BREAK SPACE
    text = text.replace('\u3000', ' ')  # IDEOGRAPHIC SPACE
    # 移除其他不可见字符
    text = re.sub(r'[\u200b-\u200f\u2028-\u202f\u205f-\u206f]', '', text)
    return text.strip()

# ────────────────────────────────────────
# 专用浏览器线程（解决 Playwright 跨线程问题）
# ────────────────────────────────────────
_search_queue = queue.Queue(maxsize=20)  # 搜索请求队列
_browser_thread = None
_browser_thread_running = False


def _browser_worker():
    """专用浏览器线程：所有 Playwright 操作都在这个线程执行"""
    global _browser, _playwright
    _browser = None
    _playwright = None

    print("[搜索] 浏览器专用线程已启动")

    while _browser_thread_running:
        try:
            # 等待任务，超时1秒后检查是否需要退出
            try:
                task = _search_queue.get(timeout=1)
            except queue.Empty:
                continue

            if task is None:
                # 关闭信号
                break

            cmd = task.get("cmd")
            event = task.get("event")
            result_key = task.get("result_key", "result")

            if cmd == "search":
                # 执行搜索
                try:
                    result = _search_impl(task["query"], task.get("deep"))
                    task[result_key] = result
                except Exception as e:
                    print(f"[搜索线程错误] {e}")
                    task[result_key] = None
            elif cmd == "cleanup":
                # 关闭浏览器
                try:
                    if _browser:
                        _browser.close()
                except Exception:
                    pass
                try:
                    if _playwright:
                        _playwright.stop()
                except Exception:
                    pass
                _browser = None
                _playwright = None
                task[result_key] = True

            event.set()

        except Exception as e:
            print(f"[搜索线程] 未知错误: {e}")

    # 线程退出时清理
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    print("[搜索] 浏览器专用线程已退出")


def _ensure_browser_thread():
    """确保浏览器专用线程已启动"""
    global _browser_thread, _browser_thread_running
    if _browser_thread is None or not _browser_thread.is_alive():
        _browser_thread_running = True
        _browser_thread = threading.Thread(target=_browser_worker, daemon=True, name="browser-worker")
        _browser_thread.start()


def _submit_task(cmd, **kwargs) -> dict:
    """提交任务到浏览器线程并等待结果"""
    _ensure_browser_thread()
    event = threading.Event()
    task = {"cmd": cmd, "event": event, **kwargs}
    _search_queue.put(task)
    event.wait(timeout=30)  # 最多等30秒
    return task


def _get_browser():
    """获取或复用 Playwright Edge 浏览器实例（必须在浏览器线程内调用）"""
    global _browser, _playwright
    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
            else:
                print("[搜索] 浏览器已断开，重新启动")
        except Exception as e:
            print(f"[搜索] 浏览器检查异常: {e}，重新启动")
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None

    try:
        if _playwright is None:
            _playwright = sync_playwright().start()

        launch_args = [
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--window-size=1280,800',
        ]

        if os.path.exists(_EDGE_PATH):
            print(f"[搜索] 使用系统 Edge: {_EDGE_PATH}")
            _browser = _playwright.chromium.launch(
                executable_path=_EDGE_PATH,
                headless=False,
                args=launch_args,
            )
        else:
            print("[搜索] 未找到系统 Edge，使用 Playwright Chromium")
            _browser = _playwright.chromium.launch(
                headless=False,
                args=launch_args,
            )
        print("[搜索] Edge 浏览器启动成功（有界面模式）")
        return _browser
    except Exception as e:
        print(f"[搜索] Playwright 启动失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def _new_page():
    """创建一个新的浏览器页面（带反检测配置）"""
    browser = _get_browser()
    if not browser:
        return None
    ctx = browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0',
        viewport={'width': 1280, 'height': 800},
        locale='zh-CN',
        timezone_id='Asia/Shanghai',
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    # 阻止图片加载加速搜索
    ctx.route('**/*.{png,jpg,jpeg,gif,svg,webp,ico}', lambda route: route.abort())
    page = ctx.new_page()
    # 反检测：覆盖 navigator.webdriver 等自动化标志
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });"
        "window.chrome = { runtime: {} };"
    )
    page.set_default_timeout(8000)
    return page


# ────────────────────────────────────────
# 搜索判断相关常量
# ────────────────────────────────────────

# 不适合深度爬取的域名（视频/社交平台，正文没有文字攻略）
_SKIP_DOMAINS = [
    "bilibili.com", "youtube.com", "douyin.com", "weibo.com",
    "twitter.com", "tiktok.com", "zhihu.com/video",
]

# 优先深度爬取的域名
_PREFERRED_DOMAINS = [
    "wiki.biligame.com", "game8.co.jp", "prydwen.gg",
    "nga.178.com", "tieba.baidu.com", "bbs.mihoyo.com",
    "kurogames.com", "wuthering-waves.fandom.com",
]


def _should_deep_crawl(query: str) -> bool:
    """根据查询词判断是否需要深度爬取正文（默认都深度爬取）"""
    if len(query) < 3:
        return False
    if re.match(r'^[\d\s\W]+$', query):
        return False
    return True


def _is_crawlable_url(url: str) -> bool:
    """判断URL是否值得深度爬取"""
    if not url:
        return False
    for domain in _SKIP_DOMAINS:
        if domain in url:
            return False
    return True


def _score_url(url: str) -> int:
    """优先级打分，优先域名得分高"""
    for i, domain in enumerate(_PREFERRED_DOMAINS):
        if domain in url:
            return 10 - i
    return 0


def crawl_page(url: str, timeout: int = 8000) -> Optional[str]:
    """进入指定 URL，提取正文内容（去除导航、广告等噪声）"""
    page = _new_page()
    if not page:
        return None

    try:
        print(f"[深度爬取] 进入: {url[:60]}...")
        page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        page.wait_for_timeout(800)

        content_parts = []

        # 常见正文容器选择器，按优先级尝试
        content_selectors = [
            "article", ".article-content", ".post-content",
            ".wiki-content", ".entry-content", ".content-body",
            "#content", ".main-content", "main",
        ]

        for sel in content_selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    text = _clean_text(el.inner_text())
                    if len(text) > 100:
                        tags = el.query_selector_all("p, li, td, h2, h3")
                        if tags:
                            for tag in tags:
                                t = _clean_text(tag.inner_text())
                                if t and len(t) >= 8:
                                    content_parts.append(t)
                                if sum(len(p) for p in content_parts) > 600:
                                    break
                        break
            except Exception:
                continue

        # 降级：全页提取
        if not content_parts:
            tags = page.query_selector_all("p, li")
            for tag in tags:
                t = _clean_text(tag.inner_text())
                if t and len(t) >= 8:
                    content_parts.append(t)
                if sum(len(p) for p in content_parts) > 600:
                    break

        noise_words = ["广告", "copyright", "©", "隐私政策", "用户协议",
                       "登录", "注册", "下载", "APP", "关注我们"]

        filtered = []
        for p in content_parts:
            if not any(n in p.lower() for n in noise_words):
                filtered.append(p)

        if filtered:
            result = "\n".join(filtered)
            result = re.sub(r'\n{3,}', '\n\n', result).strip()
            print(f"[深度爬取] 提取 {len(result)} 字")
            return result[:600]
        else:
            print("[深度爬取] 未找到有效正文")
            return None

    except Exception as e:
        print(f"[深度爬取] 失败: {e}")
        return None
    finally:
        try:
            page.close()
        except Exception:
            pass


def should_search(text: str) -> bool:
    """是否应该搜索"""
    return True


def _search_impl(query: str, deep: bool = None, retry_count: int = 0) -> Optional[str]:
    """
    搜索实现（在浏览器线程内执行）
    包含搜索结果提取、深度爬取、重试逻辑
    """
    page = _new_page()
    if not page:
        return None

    try:
        if deep is None:
            deep = _should_deep_crawl(query)

        url = f"https://cn.bing.com/search?q={query}"
        print(f"[搜索] {'深度' if deep else '摘要'}模式: {query[:40]}...")

        # 使用 networkidle 等待完整JS渲染（Bing结果由JS动态生成）
        try:
            page.goto(url, wait_until='networkidle', timeout=15000)
        except PWTimeout:
            print(f"[搜索] networkidle超时，尝试继续提取结果")
        except Exception as e:
            if retry_count < 2:
                print(f"[搜索] 页面加载失败: {e}，重试 ({retry_count + 1}/2)")
                page.close()
                return _search_impl(query, deep, retry_count + 1)
            return None

        # Bing结果由JS渲染，需要额外等待
        page.wait_for_timeout(2000)

        # 智能等待：等搜索结果出现（最多10秒）
        try:
            page.wait_for_selector('li.b_algo, #b_results', timeout=10000)
            print("[搜索] 搜索结果已加载")
        except PWTimeout:
            items_check = page.query_selector_all('li.b_algo')
            if items_check:
                print(f"[搜索] wait_for_selector超时但实际已找到{len(items_check)}条结果，继续")
            else:
                body_snippet = _clean_text(page.inner_text('body'))[:200]
                print(f"[搜索] 页面内容预览: {body_snippet}")
                if retry_count < 2:
                    print(f"[搜索] 页面无结果，重试 ({retry_count + 1}/2)")
                    page.close()
                    time.sleep(1)
                    return _search_impl(query, deep, retry_count + 1)
                print(f"[搜索] 页面无结果，已重试2次，放弃")
                return None
        except Exception as e:
            print(f"[搜索] 等待异常: {e}")

        # ── 提取 Bing 搜索结果（多重选择器备选）──
        results_meta = []

        for selector in ['li.b_algo', '#b_results > li', '.b_algo']:
            items = page.query_selector_all(selector)
            if items:
                break
        else:
            items = []

        if not items:
            print("[搜索] 未找到任何搜索结果条目")
            if retry_count < 2:
                print(f"[搜索] 尝试重试 ({retry_count + 1}/2)")
                time.sleep(1)
                page.close()
                return _search_impl(query, deep, retry_count + 1)
            return None

        clean = lambda s: re.sub(r'\s+', ' ', s).strip()

        for item in items[:8]:
            try:
                title = ""
                snippet = ""
                link = ""

                h2 = item.query_selector('h2')
                if h2:
                    title = _clean_text(h2.inner_text())
                    a = h2.query_selector('a')
                    if a:
                        link = a.get_attribute('href') or ""
                else:
                    a = item.query_selector('a')
                    if a:
                        title = _clean_text(a.inner_text())
                        link = a.get_attribute('href') or ""

                for sel in ['p', '.b_caption p', '.b_algo_slug p', 'div[class*="caption"]']:
                    el = item.query_selector(sel)
                    if el:
                        snippet = _clean_text(el.inner_text())
                        if snippet:
                            break

                if title and any(x in title for x in ['查看次数', '播放']):
                    continue
                if not title and not snippet:
                    continue

                results_meta.append((clean(title), clean(snippet), link))
            except Exception:
                continue

        if not results_meta:
            print("[搜索] 无结果")
            # 尝试从页面直接提取文本作为降级
            try:
                body_text = _clean_text(page.inner_text('body'))
                # 提取前500字作为降级结果
                if len(body_text) > 100:
                    # 过滤掉导航、广告等噪声
                    lines = body_text.split('\n')
                    useful_lines = []
                    for line in lines:
                        line = line.strip()
                        if len(line) < 10:
                            continue
                        if any(x in line for x in ['登录', '注册', '广告', '隐私', '版权', '©', 'Microsoft', 'Bing']):
                            continue
                        useful_lines.append(line)
                        if len(useful_lines) >= 5:
                            break
                    if useful_lines:
                        result = "\n".join(useful_lines)
                        print(f"[搜索] 降级提取页面文本 ({len(result)}字)")
                        return result
            except Exception as e:
                print(f"[搜索] 降级提取失败: {e}")
            return None

        # ── 仅摘要模式 ──
        if not deep:
            lines = []
            for title, snippet, _ in results_meta[:3]:
                if title and snippet:
                    lines.append(f"{title}：{snippet}"[:200])
                elif title:
                    lines.append(title[:200])
                elif snippet:
                    lines.append(snippet[:200])
            print(f"[搜索] 返回 {len(lines)} 条摘要")
            return "\n".join(lines) if lines else None

        # ── 深度爬取模式 ──
        fallback_lines = []
        for title, snippet, _ in results_meta[:3]:
            text = f"{title}：{snippet}" if title and snippet else (title or snippet)
            if text:
                fallback_lines.append(text[:200])

        candidates = [(score, title, link)
                      for title, snippet, link in results_meta
                      if _is_crawlable_url(link)
                      for score in [_score_url(link)]]
        candidates.sort(key=lambda x: x[0], reverse=True)

        # 关闭搜索页，释放资源（深度爬取会开新页面）
        page.close()
        page = None

        deep_result = None
        for score, title, link in candidates[:3]:
            if not link:
                continue
            deep_result = crawl_page(link)
            if deep_result and len(deep_result) > 50:
                print(f"[深度爬取] 成功获取正文，来自: {link[:50]}")
                break

        if deep_result:
            header = fallback_lines[0] if fallback_lines else ""
            if header:
                return f"{header}\n\n【详细内容】\n{deep_result}"
            else:
                return deep_result
        else:
            print("[深度爬取] 未获取到正文，降级为摘要")
            return "\n".join(fallback_lines) if fallback_lines else None

    except Exception as e:
        print(f"[搜索错误] {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def search_and_summarize(query: str, deep: bool = None, retry_count: int = 0) -> Optional[str]:
    """
    搜索并返回格式化的结果（线程安全，从任意线程调用）

    Args:
        query: 搜索词
        deep: 是否深度爬取正文。None=自动判断（根据关键词）
        retry_count: 当前重试次数（内部使用，外部忽略）

    Returns:
        格式化的搜索结果文本
    """
    task = _submit_task("search", query=query, deep=deep)
    return task.get("result")


def cleanup():
    """关闭浏览器，释放资源"""
    task = _submit_task("cleanup")
    _browser_thread_running = False


if __name__ == "__main__":
    print("=== 摘要模式测试 ===")
    result = search_and_summarize("鸣潮 更新 最新", deep=False)
    if result:
        print(f"结果 ({len(result)}字):\n{result}")
    else:
        print("无结果")

    print("\n=== 深度爬取测试 ===")
    result2 = search_and_summarize("鸣潮 达妮娅 攻略 配队", deep=True)
    if result2:
        print(f"结果 ({len(result2)}字):\n{result2}")
    else:
        print("无结果")

    cleanup()
    print("\n[完成] 浏览器已关闭")
