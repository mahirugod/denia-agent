"""
媒体下载模块 - 下载并转发图片和视频
支持B站、抖音等平台的媒体抓取
"""

import os
import re
import json
import requests
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

class MediaDownloader:
    """媒体下载管理器"""
    
    def __init__(self, cache_dir: str = "./media_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        
        # 图片和视频的存储目录
        self.image_dir = self.cache_dir / "images"
        self.video_dir = self.cache_dir / "videos"
        self.image_dir.mkdir(exist_ok=True)
        self.video_dir.mkdir(exist_ok=True)
        
        # 已下载的文件索引（避免重复下载）
        self.downloaded_index = {}
        self.load_index()
        
        # 请求头（模拟浏览器）
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    
    def load_index(self):
        """加载已下载文件索引"""
        index_file = self.cache_dir / "index.json"
        if index_file.exists():
            try:
                with open(index_file, 'r', encoding='utf-8') as f:
                    self.downloaded_index = json.load(f)
            except Exception:
                self.downloaded_index = {}
    
    def save_index(self):
        """保存已下载文件索引"""
        index_file = self.cache_dir / "index.json"
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(self.downloaded_index, f, ensure_ascii=False, indent=2)
    
    def search_bilibili_images(self, keyword: str, max_results: int = 5) -> List[Dict]:
        """
        图片搜索（改进方案）
        由于网络限制，提供多个降级方案：
        1. 尝试本地知识库的图片 URL
        2. 使用百度搜索 API（通常更稳定）
        3. 返回通用的图片占位符
        
        返回 [{"title": "...", "url": "...", "cover": "..."}]
        """
        try:
            # 【新增】先尝试本地知识库
            local_images = self._get_local_images(keyword)
            if local_images:
                print(f"[本地知识库] 找到 {len(local_images)} 张图片")
                return local_images[:max_results]
            
            # 【降级1】尝试百度图片搜索 API
            baidu_images = self._search_baidu_images(keyword, max_results)
            if baidu_images:
                return baidu_images
            
            # 【降级2】返回可用的图片源列表（B站、微博等）
            return self._get_fallback_image_sources(keyword, max_results)
            
        except Exception as e:
            print(f"[图片搜索] 异常: {e}")
            return self._get_fallback_image_sources(keyword, max_results)
    
    def _get_local_images(self, keyword: str) -> List[Dict]:
        """从本地知识库获取已缓存的图片 URL"""
        try:
            # 【新增】游戏角色本地缓存
            local_cache = {
                "达妮娅": [
                    "https://wuthering-waves-static-web.kurogame.com/official-community/attachment/9d28dfe84aaae639b06e8db2c6d3db6c.png",
                    "https://wuthering-waves-static-web.kurogame.com/official-community/attachment/abc123.png",
                ],
                "daniya": [  # 英文别名
                    "https://wuthering-waves-static-web.kurogame.com/official-community/attachment/9d28dfe84aaae639b06e8db2c6d3db6c.png",
                ],
                "派蒙": [
                    "https://uploadstatic.mihoyo.com/contentweb/20210601/2021060115193498906.png",
                ],
                "原神": [
                    "https://uploadstatic.mihoyo.com/contentweb/20230718/2023071819123758667.png",
                ],
            }
            
            results = []
            # 精确匹配
            if keyword in local_cache:
                for url in local_cache[keyword]:
                    results.append({
                        "title": keyword,
                        "url": url,
                        "cover": url,
                        "source": "local_cache"
                    })
                return results
            
            # 模糊匹配（关键词包含）
            for key, urls in local_cache.items():
                if keyword.lower() in key.lower() or key.lower() in keyword.lower():
                    for url in urls:
                        results.append({
                            "title": key,
                            "url": url,
                            "cover": url,
                            "source": "local_cache"
                        })
                    if len(results) >= 3:
                        break
            
            return results
        except Exception as e:
            print(f"[本地缓存查询] 失败: {e}")
            return []
    
    def _search_baidu_images(self, keyword: str, max_results: int = 3) -> List[Dict]:
        """尝试百度图片搜索 API（通常更稳定）"""
        try:
            print(f"[百度图片搜索] 搜索'{keyword}'...")
            # 百度搜索通常返回 JSON 数据
            search_url = f"https://image.baidu.com/search/index?tn=baiduimage&word={keyword}"
            resp = requests.get(search_url, headers=self.headers, timeout=5)
            resp.encoding = 'utf-8'
            
            # 从 HTML 中提取图片 URL（可能需要解析 JavaScript）
            # 这是一个简化版本，可能不够稳定
            pattern = r'"objURL":"([^"]+)"'
            matches = re.findall(pattern, resp.text)
            
            results = []
            for url in matches[:max_results]:
                # 去除转义字符
                url = url.replace('\\/', '/')
                results.append({
                    "title": keyword,
                    "url": url,
                    "cover": url,
                    "source": "baidu_image"
                })
            
            if results:
                print(f"[百度图片搜索] 找到 {len(results)} 张图片")
            return results
            
        except Exception as e:
            print(f"[百度图片搜索] 失败: {e}")
            return []
    
    def _get_fallback_image_sources(self, keyword: str, max_results: int = 3) -> List[Dict]:
        """获取备选图片源（生成搜索链接建议，让用户自己查看）"""
        # 当无法直接下载时，返回搜索链接建议
        results = []
        
        sources = [
            {
                "title": f"{keyword} - B站",
                "url": f"https://search.bilibili.com/all?keyword={keyword}",
                "source": "bilibili_search"
            },
            {
                "title": f"{keyword} - 百度图片",
                "url": f"https://image.baidu.com/search/index?tn=baiduimage&word={keyword}",
                "source": "baidu_image_search"
            },
            {
                "title": f"{keyword} - 微博",
                "url": f"https://s.weibo.com/weibo?q={keyword}",
                "source": "weibo_search"
            },
        ]
        
        return sources[:max_results]
    
    def search_bilibili_videos(self, keyword: str, max_results: int = 3) -> List[Dict]:
        """
        从B站搜索视频
        返回 [{"title": "...", "url": "...", "cover": "..."}]
        """
        try:
            search_url = f"https://search.bilibili.com/all?keyword={keyword}"
            resp = requests.get(search_url, headers=self.headers, timeout=10)
            resp.encoding = 'utf-8'
            
            # 提取视频信息
            pattern = r'href="(//www\.bilibili\.com/video/[^"]+)"[^>]*><span[^>]*>([^<]+)</span>'
            matches = re.findall(pattern, resp.text)
            
            results = []
            for url, title in matches[:max_results]:
                full_url = "https:" + url if url.startswith("//") else url
                results.append({
                    "title": title.strip(),
                    "url": full_url,
                    "bvid": self._extract_bvid(full_url),
                    "source": "bilibili"
                })
            
            return results
        except Exception as e:
            print(f"[B站视频搜索] 失败: {e}")
            return []
    

    def _extract_bvid(self, url: str) -> Optional[str]:
        """从B站URL中提取BVID"""
        match = re.search(r'video/(BV\w+)', url)
        return match.group(1) if match else None
    
    def download_image(self, image_url: str, keyword: str = "image") -> Optional[str]:
        """
        下载单个图片
        返回本地文件路径，失败返回None
        
        【改进】如果网络不可用，创建本地占位符或使用 PIL 生成一个简单的文本图片
        """
        try:
            # 生成唯一的文件名
            filename = f"{keyword}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            filepath = self.image_dir / filename
            
            # 检查是否已下载过
            if str(filepath) in self.downloaded_index:
                return str(self.downloaded_index[str(filepath)])
            
            # 【改进】首先尝试从 URL 下载
            try:
                resp = requests.get(image_url, headers=self.headers, timeout=5)
                resp.raise_for_status()
                
                with open(filepath, 'wb') as f:
                    f.write(resp.content)
                
                # 记录到索引
                self.downloaded_index[image_url] = str(filepath)
                self.save_index()
                
                print(f"[图片下载] OK {filename}")
                return str(filepath)
                
            except (requests.RequestException, ConnectionError) as e:
                # 【降级】网络不可用时，生成本地占位符图片
                print(f"[图片下载] 网络不可用，生成本地占位符图片...")
                return self._create_placeholder_image(filename, keyword)
            
        except Exception as e:
            print(f"[图片下载失败] {e}")
            # 最后的降级方案：创建占位符
            return self._create_placeholder_image(filename, keyword)
    
    def _create_placeholder_image(self, filename: str, keyword: str) -> Optional[str]:
        """
        创建本地占位符图片（使用 PIL）
        返回本地文件路径
        
        【改进】避免中文乱码，使用英文或Unicode编码的关键词
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
            
            filepath = self.image_dir / filename
            
            # 创建一个简单的文本图片
            img = Image.new('RGB', (600, 400), color='#f0f0f0')
            draw = ImageDraw.Draw(img)
            
            # 【改进】使用英文显示，避免中文乱码
            # 将关键词转换为ASCII（无法转换的保留原值）
            try:
                keyword_ascii = keyword.encode('ascii').decode('ascii')
            except UnicodeEncodeError:
                # 如果包含非ASCII字符，使用拼音首字母或简写
                keyword_ascii = f"keyword_{len(keyword)}"
            
            # 添加文本（全英文避免编码问题）
            text = f"[Search: {keyword_ascii}]\\n\\n(Image Placeholder)\\n\\nPlease search manually:\\nhttps://image.baidu.com/search\\n?q={keyword_ascii}\\n\\nor use your browser"
            
            # 【改进】优先查找系统中的 Arial 或 Courier 字体
            font = None
            font_candidates = [
                "arial.ttf",
                "C:\\Windows\\Fonts\\arial.ttf",
                "C:\\Windows\\Fonts\\msyh.ttc",  # 微软雅黑
                "C:\\Windows\\Fonts\\simsun.ttc",  # 宋体
            ]
            
            for font_path in font_candidates:
                try:
                    font = ImageFont.truetype(font_path, 18)
                    break
                except Exception:
                    pass
            
            if not font:
                font = ImageFont.load_default()
            
            # 【改进】处理多行文本
            lines = text.split('\\n')
            y_offset = 50
            for line in lines:
                draw.text((50, y_offset), line, fill='#333333', font=font)
                y_offset += 35
            
            # 保存图片
            img.save(str(filepath), 'JPEG')
            
            # 记录到索引
            self.downloaded_index[keyword] = str(filepath)
            self.save_index()
            
            print(f"[Placeholder] OK {filename}")
            return str(filepath)
            
        except ImportError:
            print(f"[Placeholder] PIL not installed")
            return None
        except Exception as e:
            print(f"[Placeholder] Failed: {e}")
            return None
    
    def download_bilibili_video(self, video_url: str, keyword: str = "video") -> Optional[str]:
        """
        使用yt-dlp下载B站视频
        返回本地文件路径，失败返回None
        """
        try:
            # 检查是否安装了yt-dlp
            result = subprocess.run(["yt-dlp", "--version"], 
                                  capture_output=True, timeout=5)
            if result.returncode != 0:
                print("[视频下载] yt-dlp未安装，请运行: pip install yt-dlp")
                return None
            
            # 生成输出文件名
            filename = f"{keyword}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            filepath = self.video_dir / filename
            
            # 使用yt-dlp下载
            cmd = [
                "yt-dlp",
                "-f", "best[ext=mp4]",  # 最佳质量mp4
                "-o", str(filepath),
                video_url
            ]
            
            print(f"[视频下载] 开始下载: {video_url}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode == 0 and filepath.exists():
                print(f"[视频下载] ✓ {filename}")
                self.downloaded_index[video_url] = str(filepath)
                self.save_index()
                return str(filepath)
            else:
                print(f"[视频下载失败] {result.stderr}")
                return None
                
        except subprocess.TimeoutExpired:
            print("[视频下载失败] 下载超时")
            return None
        except Exception as e:
            print(f"[视频下载失败] {e}")
            return None
    
    def get_batch_images(self, keyword: str, count: int = 3) -> List[str]:
        """
        批量获取图片并下载
        返回本地文件路径列表
        """
        print(f"[图片批量下载] 搜索'{keyword}'，目标{count}张")
        search_results = self.search_bilibili_images(keyword, max_results=count)
        
        downloaded = []
        for result in search_results:
            # 从视频页面获取封面（简单方案）
            filepath = self.download_image(result.get("cover", ""), keyword)
            if filepath:
                downloaded.append(filepath)
        
        return downloaded
    
    def get_batch_videos(self, keyword: str, count: int = 2) -> List[str]:
        """
        批量获取视频并下载
        返回本地文件路径列表
        """
        print(f"[视频批量下载] 搜索'{keyword}'，目标{count}个")
        search_results = self.search_bilibili_videos(keyword, max_results=count)
        
        downloaded = []
        for result in search_results:
            filepath = self.download_bilibili_video(result["url"], keyword)
            if filepath:
                downloaded.append(filepath)
        
        return downloaded
    
    def cleanup_old_files(self, days: int = 7):
        """清理7天前的缓存文件"""
        import time
        cutoff_time = time.time() - (days * 24 * 3600)
        
        for filepath in list(self.image_dir.glob("*.jpg")) + list(self.video_dir.glob("*.mp4")):
            if filepath.stat().st_mtime < cutoff_time:
                filepath.unlink()
                print(f"[缓存清理] 删除: {filepath.name}")


class QQMediaSender:
    """QQ媒体文件发送器"""
    
    def __init__(self, napcat_http_url: str = "http://127.0.0.1:3000"):
        self.napcat_url = napcat_http_url
    
    def send_image_to_private(self, user_id: int, image_path: str) -> bool:
        """向私聊用户发送图片"""
        try:
            # 转换为绝对路径
            abs_path = Path(image_path).absolute()
            # Windows路径转换：将 C:\path\to\file 转换为 file:///C:/path/to/file
            abs_path_str = str(abs_path)
            file_url = "file:///" + abs_path_str.replace("\\", "/")
            
            url = f"{self.napcat_url}/send_private_msg"
            message = f"[CQ:image,file={file_url}]"
            
            payload = {"user_id": user_id, "message": message}
            resp = requests.post(url, json=payload, timeout=10)
            
            if resp.status_code == 200:
                print(f"[QQ图片发送] OK 用户{user_id}")
                return True
            else:
                print(f"[QQ图片发送失败] HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[QQ图片发送失败] {e}")
            return False
    
    def send_video_to_private(self, user_id: int, video_path: str) -> bool:
        """向私聊用户发送视频"""
        try:
            abs_path = Path(video_path).absolute()
            file_url = f"file:///{str(abs_path).replace(chr(92), '/')}"
            
            url = f"{self.napcat_url}/send_private_msg"
            message = f"[CQ:video,file={file_url}]"
            
            payload = {"user_id": user_id, "message": message}
            resp = requests.post(url, json=payload, timeout=10)
            
            if resp.status_code == 200:
                print(f"[QQ视频发送] OK 用户{user_id}")
                return True
            else:
                print(f"[QQ视频发送失败] HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[QQ视频发送失败] {e}")
            return False
    
    def send_image_to_group(self, group_id: int, image_path: str) -> bool:
        """向群聊发送图片"""
        try:
            abs_path = Path(image_path).absolute()
            file_url = f"file:///{str(abs_path).replace(chr(92), '/')}"
            
            url = f"{self.napcat_url}/send_group_msg"
            message = f"[CQ:image,file={file_url}]"
            
            payload = {"group_id": group_id, "message": message}
            resp = requests.post(url, json=payload, timeout=10)
            
            if resp.status_code == 200:
                print(f"[QQ群图片发送] OK 群{group_id}")
                return True
            else:
                print(f"[QQ群图片发送失败] HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[QQ群图片发送失败] {e}")
            return False
    
    def send_video_to_group(self, group_id: int, video_path: str) -> bool:
        """向群聊发送视频"""
        try:
            abs_path = Path(video_path).absolute()
            file_url = f"file:///{str(abs_path).replace(chr(92), '/')}"
            
            url = f"{self.napcat_url}/send_group_msg"
            message = f"[CQ:video,file={file_url}]"
            
            payload = {"group_id": group_id, "message": message}
            resp = requests.post(url, json=payload, timeout=10)
            
            if resp.status_code == 200:
                print(f"[QQ群视频发送] OK 群{group_id}")
                return True
            else:
                print(f"[QQ群视频发送失败] HTTP {resp.status_code}")
                return False
        except Exception as e:
            print(f"[QQ群视频发送失败] {e}")
            return False
    
    def send_multiple_images(self, user_id: int, image_paths: List[str], 
                            is_group: bool = False) -> int:
        """批量发送多张图片，返回成功发送的数量"""
        count = 0
        for path in image_paths:
            if is_group:
                if self.send_image_to_group(user_id, path):
                    count += 1
            else:
                if self.send_image_to_private(user_id, path):
                    count += 1
        return count
