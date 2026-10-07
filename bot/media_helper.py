"""
媒体助手模块 - 帮助查找并发送图片和视频
"""

import json
from typing import List, Dict, Optional
from media_downloader import MediaDownloader, QQMediaSender

class MediaHelper:
    """
    媒体查找和发送助手
    """
    
    # 不同平台的URL构建规则
    PLATFORM_HANDLERS = {
        "b站": {
            "search_url": "https://search.bilibili.com/all?keyword={keyword}",
            "description": "在B站搜索：",
            "example_videos": {
                "达妮娅": [
                    {"title": "【鸣潮】达妮娅剧情CG完整合集", "url": "https://www.bilibili.com/video/BV1..."},
                    {"title": "鸣潮达妮娅角色介绍", "url": "https://www.bilibili.com/video/BV2..."},
                ],
                "迪莫": [
                    {"title": "【洛克王国世界】迪莫培养完全指南", "url": "https://www.bilibili.com/video/BV3..."},
                    {"title": "迪莫最强配队方案", "url": "https://www.bilibili.com/video/BV4..."},
                ]
            }
        },
        "抖音": {
            "search_url": "https://www.douyin.com/search/{keyword}",
            "description": "在抖音搜索：",
            "note": "抖音内容可能需要手动搜索查看"
        },
        "微博": {
            "search_url": "https://s.weibo.com/weibo?q={keyword}",
            "description": "在微博搜索：",
            "note": "微博内容可能需要手动搜索查看"
        }
    }
    
    def __init__(self, napcat_http_url: str = "http://127.0.0.1:3000"):
        self.downloader = MediaDownloader(cache_dir="./media_cache")
        self.sender = QQMediaSender(napcat_http_url=napcat_http_url)
        self.search_history = []
    
    def generate_search_response(self, 
                                subject: str, 
                                platform: str = None, 
                                media_type: str = "video") -> Dict:
        """
        生成搜索建议和链接
        
        Args:
            subject: 搜索对象（如"达妮娅"）
            platform: 平台（b站、抖音等）
            media_type: 媒体类型（video/image）
        
        Returns:
            包含建议、链接、说明的字典
        """
        response = {
            "message": "",
            "links": [],
            "disclaimer": "",
            "can_send": False
        }
        
        if not platform:
            # 默认推荐B站
            platform = "b站"
            response["message"] = f"好的呢，我帮你找{subject}的{media_type}！建议在B站搜索~\n"
        else:
            response["message"] = f"好的呢，我帮你在{platform}找{subject}的{media_type}！\n"
        
        # 如果有现成的视频建议
        if platform == "b站" and platform in self.PLATFORM_HANDLERS:
            handler = self.PLATFORM_HANDLERS[platform]
            if "example_videos" in handler and subject in handler["example_videos"]:
                videos = handler["example_videos"][subject]
                response["links"] = videos
                response["message"] += f"我找到了几个相关视频：\n"
                for i, video in enumerate(videos, 1):
                    response["message"] += f"{i}. {video['title']}\n"
            else:
                # 生成搜索链接
                search_url = handler["search_url"].format(keyword=subject)
                response["links"] = [{"title": f"{subject}相关视频", "url": search_url}]
                response["message"] += f"可以点这里搜索：{search_url}\n"
        else:
            # 其他平台也生成搜索链接
            if platform in self.PLATFORM_HANDLERS:
                handler = self.PLATFORM_HANDLERS[platform]
                search_url = handler["search_url"].format(keyword=subject)
                response["links"] = [{"title": f"{subject}相关内容", "url": search_url}]
                response["message"] += f"搜索地址：{search_url}\n"
        
        # 添加限制说明
        response["disclaimer"] = (
            "\n⚠️ 温馨提示：我作为AI助手无法直接从视频网站下载并发送视频，"
            "但我可以帮你找到视频链接或搜索建议。你可以自己点击链接去查看～"
        )
        
        response["can_send"] = False  # 当前无法直接发送视频
        
        return response
    
    def handle_media_request(self, 
                            subject: str,
                            media_type: str = "video",
                            platform: str = None,
                            need_send: bool = False) -> str:
        """
        处理媒体请求，生成回复
        
        Args:
            subject: 搜索对象
            media_type: 媒体类型
            platform: 平台
            need_send: 用户是否要求发送
        
        Returns:
            机器人的回复文本
        """
        response = self.generate_search_response(subject, platform, media_type)
        
        # 记录搜索历史
        self.search_history.append({
            "subject": subject,
            "type": media_type,
            "platform": platform
        })
        
        message = response["message"]
        
        if need_send:
            message += (
                "\n不过啊，虽然我很想直接给你发视频，"
                "但我现在还做不到呢 T_T\n"
                "我只能帮你找到链接，你自己去看可以吗？"
            )
        
        if response["links"]:
            message += "\n\n" + json.dumps(response["links"], ensure_ascii=False, indent=2)
        
        message += response["disclaimer"]
        
        return message
    
    def get_suggestion_for_media(self, subject: str, platform: str = "b站") -> str:
        """
        根据搜索对象生成更有针对性的建议
        """
        suggestions = {
            "达妮娅": {
                "b站": "建议搜索：达妮娅角色剧情、达妮娅建队攻略、鸣潮达妮娅",
                "抖音": "可以搜达妮娅相关短视频，通常有玩家分享和二创"
            },
            "迪莫": {
                "b站": "建议搜索：迪莫培养方案、迪莫配队、洛克王国世界迪莫",
                "抖音": "可以搜迪莫实战演示"
            }
        }
        
        if subject in suggestions and platform in suggestions[subject]:
            return suggestions[subject][platform]
        
        return f"在{platform}搜索'{subject}'应该能找到不少相关内容呢～"
    
    def download_and_send_images(self, user_id: int, keyword: str, 
                                  count: int = 3, is_group: bool = False) -> tuple:
        """
        下载并发送图片
        返回 (成功, 消息文本)
        """
        try:
            # 下载图片
            image_paths = self.downloader.get_batch_images(keyword, count=count)
            
            if not image_paths:
                return False, f"抱歉，没有找到'{keyword}'的图片呢..."
            
            # 发送图片
            if is_group:
                success_count = 0
                for path in image_paths:
                    if self.sender.send_image_to_group(user_id, path):
                        success_count += 1
            else:
                success_count = 0
                for path in image_paths:
                    if self.sender.send_image_to_private(user_id, path):
                        success_count += 1
            
            msg = f"成功发送 {success_count}/{len(image_paths)} 张图片！"
            return success_count > 0, msg
        
        except Exception as e:
            return False, f"发送图片时出错了：{e}"
    
    def download_and_send_videos(self, user_id: int, keyword: str, 
                                  count: int = 1, is_group: bool = False) -> tuple:
        """
        下载并发送视频
        返回 (成功, 消息文本)
        """
        try:
            # 下载视频
            video_paths = self.downloader.get_batch_videos(keyword, count=count)
            
            if not video_paths:
                return False, f"抱歉，没有找到'{keyword}'的视频呢..."
            
            # 发送视频
            if is_group:
                success_count = 0
                for path in video_paths:
                    if self.sender.send_video_to_group(user_id, path):
                        success_count += 1
            else:
                success_count = 0
                for path in video_paths:
                    if self.sender.send_video_to_private(user_id, path):
                        success_count += 1
            
            msg = f"成功发送 {success_count}/{len(video_paths)} 个视频！"
            return success_count > 0, msg
        
        except Exception as e:
            return False, f"发送视频时出错了：{e}"

# media_helper.py end
