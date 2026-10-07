#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片识别模块 - 使用 Ollama 视觉模型识别图片内容
支持解析 QQ CQ:image 码，下载图片，调用 qwen2.5-vl 识别
"""

import os
import re
import base64
import html
import requests
from typing import Optional, Tuple
from pathlib import Path
from datetime import datetime
import io
from PIL import Image


# 配置
VISION_MODEL = "minicpm-v:8b"  # Ollama 视觉模型 (qwen3-vl:4b 返回空内容)
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
IMAGE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_cache", "received")
REQUEST_TIMEOUT = 300  # 视觉模型在CPU上运行较慢，需要较长超时

# 确保缓存目录存在
os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)


def parse_cq_images(raw_msg: str) -> Tuple[str, list]:
    """
    从QQ消息中提取图片CQ码

    Returns:
        (clean_text, image_list)
        clean_text: 去除图片CQ码后的纯文本
        image_list: [{"file": "xxx.jpg", "url": "http://..."}, ...]
    """
    # 先解码 HTML 实体（&amp; → & 等），NapCat 有时会编码 CQ 码
    raw_msg = html.unescape(raw_msg)

    # 匹配 [CQ:image,file=xxx.jpg,url=http://...] 或 [CQ:image,file=xxx.jpg]
    pattern = r'\[CQ:image[^\]]*\]'
    images = []
    clean_text = raw_msg

    for match in re.finditer(pattern, raw_msg):
        cq_str = match.group()

        # 提取 file 字段
        file_match = re.search(r'file=([^,\]]+)', cq_str)
        # 提取 url 字段
        url_match = re.search(r'url=([^\],]+)', cq_str)

        img_info = {}
        if file_match:
            img_info["file"] = file_match.group(1)
        if url_match:
            img_info["url"] = url_match.group(1)

        if img_info:
            images.append(img_info)

        # 从文本中移除
        clean_text = clean_text.replace(cq_str, "").strip()

    return clean_text, images


def download_image(url: str, filename: str = None) -> Optional[str]:
    """下载图片到本地缓存，返回本地路径"""
    try:
        if not filename:
            filename = f"recv_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"

        filepath = os.path.join(IMAGE_CACHE_DIR, filename)

        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://multimedia.nt.qq.com.cn/",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        }, timeout=15)

        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            # 检查返回的是否真的是图片，不是 HTML 错误页
            if "image" not in content_type and not resp.content[:4] in (b'\xff\xd8\xff\xe0', b'\xff\xd8\xff\xe1', b'\x89PNG', b'GIF8'):
                print(f"[识图] 下载返回非图片内容: Content-Type={content_type}")
                return None
            with open(filepath, 'wb') as f:
                f.write(resp.content)
            print(f"[识图] 下载图片成功: {filepath} ({len(resp.content)}bytes)")
            return filepath
        else:
            print(f"[识图] 下载失败: HTTP {resp.status_code}")
            return None
    except Exception as e:
        print(f"[识图] 下载异常: {e}")
        return None


def resolve_image_path(file_field: str) -> Optional[str]:
    """
    将 CQ:image 的 file 字段解析为可读取的本地路径
    NapCat 的 file 字段可能是: UUID.jpg / 路径 / 网络URL
    """
    # 如果已经是绝对路径
    if os.path.isabs(file_field) and os.path.exists(file_field):
        return file_field

    # 尝试 NapCat 常见的缓存目录
    napcat_dirs = [
        os.path.expanduser("~/.config/QQ/NapCat"),
        os.path.expanduser("~/AppData/Roaming/QQ/NapCat"),
        os.path.expanduser("~/.napcat"),
        os.path.expanduser("~/AppData/Local/QQ/NapCat"),
    ]
    # 搜索 cache 和 data/cache 等子目录
    cache_subdirs = ["cache", "data/cache", "data/images", "images", "data"]

    for base in napcat_dirs:
        # 先直接拼
        full_path = os.path.join(base, file_field)
        if os.path.exists(full_path):
            return full_path
        # 再搜索子目录
        for subdir in cache_subdirs:
            candidate = os.path.join(base, subdir, file_field)
            if os.path.exists(candidate):
                return candidate

    # 尝试当前目录
    if os.path.exists(file_field):
        return file_field

    return None


def compress_image(image_path: str, max_size: int = 1024, quality: int = 85) -> Optional[bytes]:
    """
    压缩图片后再转 base64，避免大图片导致模型返回空
    - max_size: 长边最大像素（默认1024）
    - quality: JPEG压缩质量（默认85）
    返回压缩后的图片字节，失败返回 None
    """
    try:
        img = Image.open(image_path)
        # 转换为 RGB（处理 RGBA/CMYK 等格式）
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        # 等比例缩放
        w, h = img.size
        if max(w, h) > max_size:
            scale = max_size / max(w, h)
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            print(f"[识图] 图片已缩放: {w}x{h} → {new_w}x{new_h}")
        # 存到内存字节流
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"[识图] 压缩图片失败: {e}")
        return None


def encode_image_base64(image_path: str) -> Optional[str]:
    """将图片编码为 base64（自动先压缩）"""
    try:
        compressed = compress_image(image_path)
        if compressed:
            return base64.b64encode(compressed).decode('utf-8')
        # 压缩失败，回退到原始文件
        with open(image_path, 'rb') as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except Exception as e:
        print(f"[识图] base64编码失败: {e}")
        return None


def recognize_image(image_path: str, prompt: str = None) -> Optional[str]:
    """
    调用 Ollama 视觉模型识别图片

    Args:
        image_path: 图片本地路径
        prompt: 识别提示词（可选）

    Returns:
        识别结果文本，失败返回 None
    """
    if prompt is None:
        prompt = "请用中文生动描述这张图片的内容。描述人物的发色、瞳色、服装、表情、姿势、背景环境、画面氛围。如果是动漫/游戏角色，尽可能说出名字和出处。描述要详细，让没看到图片的人也能想象出画面。100-200字。"

    try:
        # 编码图片为 base64
        b64 = encode_image_base64(image_path)
        if not b64:
            return None

        # 构建请求
        data = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [b64]
                }
            ],
            "stream": False,
            "options": {
                "temperature": 0.5,  # 适度温度，描述既准确又有细节
                "num_predict": 512
            }
        }

        print(f"[识图] 调用 {VISION_MODEL} 识别图片...（首次加载可能需要1-3分钟）")
        resp = requests.post(OLLAMA_URL, json=data, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        result = resp.json()
        text = result.get("message", {}).get("content", "").strip()

        if text:
            print(f"[识图] 识别结果: {text[:100]}...")
            return text
        else:
            # 打印完整返回，方便排查模型为何返回空
            print(f"[识图] 模型返回空结果，完整响应: {result}")
            return None

    except requests.exceptions.ConnectionError:
        print(f"[识图] Ollama连接失败，请确认 {VISION_MODEL} 已安装且Ollama在运行")
        return None
    except Exception as e:
        print(f"[识图] 识别失败: {e}")
        return None


def recognize_image_url(url: str, prompt: str = None) -> Optional[str]:
    """通过URL下载后识别图片"""
    # 先尝试直接用URL（某些Ollama版本支持URL）
    filepath = download_image(url)
    if not filepath:
        return None
    return recognize_image(filepath, prompt)


def process_image_message(raw_msg: str, prompt_prefix: str = None) -> Tuple[str, Optional[str]]:
    """
    处理包含图片的QQ消息

    Args:
        raw_msg: 原始QQ消息（可能含CQ:image码）
        prompt_prefix: 附加到识别提示词的上下文（如角色设定）

    Returns:
        (clean_text, recognition_result)
        clean_text: 去除图片CQ码后的纯文字
        recognition_result: 图片识别结果（无图片时为None）
    """
    clean_text, images = parse_cq_images(raw_msg)

    if not images:
        return clean_text, None

    print(f"[识图] 消息中包含 {len(images)} 张图片")

    # 构建识别提示词
    base_prompt = "请用中文生动描述这张图片的所有内容。描述人物的发色、瞳色、服装、表情、姿势、动作，描述背景环境、光线、氛围、色彩风格。如果是动漫或游戏角色，说出名字和作品名。描述要足够详细生动，让没看到图片的人也能在脑海中想象出画面。100-200字。"
    if prompt_prefix:
        base_prompt = prompt_prefix + "\n" + base_prompt

    results = []
    for img in images:
        result = None
        local_path = None

        # 策略1：有URL，先下载到本地缓存
        if img.get("url"):
            # 用固定文件名（基于URL哈希），避免重复下载
            import hashlib
            url_hash = hashlib.md5(img["url"].encode()).hexdigest()[:16]
            ext = ".jpg"
            if img.get("file"):
                orig_ext = os.path.splitext(img["file"])[1]
                if orig_ext:
                    ext = orig_ext
            filename = f"recv_{url_hash}{ext}"
            local_path = download_image(img["url"], filename=filename)
            if local_path:
                print(f"[识图] 图片已下载到: {local_path}")
                result = recognize_image(local_path, base_prompt)

        # 策略2：URL失败或无URL，尝试本地文件
        if not result and img.get("file"):
            local_path = resolve_image_path(img["file"])
            if local_path:
                print(f"[识图] 使用本地文件: {local_path}")
                result = recognize_image(local_path, base_prompt)
            else:
                print(f"[识图] 无法定位图片文件: {img['file']}")

        if result:
            results.append(result)

    combined_result = None
    if results:
        if len(results) == 1:
            combined_result = results[0]
        else:
            combined_result = "\n".join(f"【图片{i+1}】{r}" for i, r in enumerate(results))

    return clean_text, combined_result


if __name__ == "__main__":
    print("=== 图片识别模块测试 ===")

    # 测试CQ码解析
    test_msg = "[CQ:at,qq=123]看看这个[CQ:image,file=abc.jpg,url=https://example.com/img.jpg]认识吗"
    text, imgs = parse_cq_images(test_msg)
    print(f"纯文本: {text}")
    print(f"图片: {imgs}")

    # 测试本地图片识别（如果有测试图片）
    test_images = [
        os.path.join(os.path.dirname(__file__), "media_cache", "images", "达妮娅_20260331_183137.jpg"),
    ]

    for img_path in test_images:
        if os.path.exists(img_path):
            print(f"\n识别测试图片: {img_path}")
            result = recognize_image(img_path)
            if result:
                print(f"结果: {result}")
            else:
                print("识别失败")
        else:
            print(f"测试图片不存在: {img_path}")
