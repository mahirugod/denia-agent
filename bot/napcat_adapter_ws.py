#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NapCat WebSocket 适配层
通过 WebSocket 接收消息，HTTP API 发送消息
"""

import json
import asyncio
import random
import time
from concurrent.futures import ThreadPoolExecutor
import websockets
import requests as _requests

# 消息处理线程池（限制最大并发线程数，防止消息洪泛）
_MSG_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg_handler")


from denia_chatbot import DeniaChatbot
from voice_synthesizer import VoiceSynthesizer
from config_napcat import *

try:
    from image_recognizer import parse_cq_images, process_image_message
    HAS_IMAGE_RECOGNITION = True
except ImportError:
    HAS_IMAGE_RECOGNITION = False

try:
    from voice_recognizer import process_voice_message
    HAS_VOICE_RECOGNITION = True
except ImportError:
    HAS_VOICE_RECOGNITION = False

try:
    from personality import get_personality_system
    HAS_PERSONALITY = True
except ImportError:
    HAS_PERSONALITY = False

# 全局存储
bots: dict[str, DeniaChatbot] = {}
_bot_last_used: dict[str, float] = {}  # 记录每个 bot 最后使用时间
_MAX_BOTS = 50  # 最大 bot 实例数量
_BOT_TTL = 3600  # bot 空闲超过1小时清理

# 全局语音合成器
voice_synthesizer = VoiceSynthesizer()

# 回复频率控制
_last_reply_time = {"private": {}, "group": {}}  # 记录每个用户/群最后回复时间

# 主动互动间隔（秒）
_AUTO_CHAT_INTERVAL = 300  # 5分钟


def get_bot(session_id: str, user_id: int = 0, is_group: bool = False) -> DeniaChatbot:
    """获取或创建指定会话的 DeniaChatbot 实例"""
    if session_id not in bots:
        _cleanup_idle_bots()
        bot = DeniaChatbot(
            use_llm=USE_LLM,
            llm_model=LLM_MODEL,
            llm_api_key=LLM_API_KEY,
            llm_base_url=LLM_BASE_URL if LLM_BASE_URL else None,
            user_id=user_id
        )
        bot.is_group_chat = is_group
        bots[session_id] = bot
    _bot_last_used[session_id] = time.time()
    return bots[session_id]


def _cleanup_idle_bots():
    """清理空闲过期的 bot 实例，防止内存泄漏"""
    now = time.time()
    # 清理超过 TTL 的
    expired = [sid for sid, t in _bot_last_used.items() if now - t > _BOT_TTL]
    for sid in expired:
        bots.pop(sid, None)
        _bot_last_used.pop(sid, None)
    # 如果仍然超量，清理最久未使用的
    if len(bots) > _MAX_BOTS:
        sorted_sids = sorted(_bot_last_used, key=_bot_last_used.get)
        for sid in sorted_sids[: len(bots) - _MAX_BOTS]:
            bots.pop(sid, None)
            _bot_last_used.pop(sid, None)


def send_private_msg(user_id: int, message: str, voice_cq: str = None):
    """向私聊用户发送消息和语音

    有语音时只发语音条，避免文字和语音重复；
    语音合成失败时退化为发文字，保证至少有回复。
    """
    url = f"{NAPCAT_HTTP_URL}/send_private_msg"

    if voice_cq:
        full_message = voice_cq        # 只发语音，不再附带文字
    else:
        full_message = message         # 没有语音时发文字兜底

    payload = {"user_id": user_id, "message": full_message}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        if voice_cq:
            print(f"[私聊] → {user_id}: [语音消息]")
        else:
            print(f"[私聊] → {user_id}: {message[:50]}...")
    except Exception as e:
        print(f"[私聊发送失败] {e}")


def send_group_msg(group_id: int, message: str, voice_cq: str = None):
    """向群聊发送消息和语音

    有语音时只发语音条，避免文字和语音重复；
    语音合成失败时退化为发文字。
    """
    url = f"{NAPCAT_HTTP_URL}/send_group_msg"

    if voice_cq:
        full_message = voice_cq        # 只发语音，不再附带文字
    else:
        full_message = message         # 没有语音时发文字兜底

    payload = {"group_id": group_id, "message": full_message}
    try:
        resp = _requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        if voice_cq:
            print(f"[群聊] → 群{group_id}: [语音消息]")
        else:
            print(f"[群聊] → 群{group_id}: {message[:50]}...")
    except Exception as e:
        print(f"[群聊发送失败] {e}")


def strip_cq_at(text: str, self_id: int) -> str:
    """去除消息中的 CQ:at 标签，返回纯文本"""
    import re
    pattern = rf"\[CQ:at,qq={self_id}\]"
    return re.sub(pattern, "", text).strip()


def _is_voice_only(raw_msg: str) -> bool:
    """检测是否是纯语音消息（不包含任何文字）"""
    import re
    # 去掉所有 CQ 码
    stripped = re.sub(r'\[CQ:[^\]]*\]', '', raw_msg).strip()
    # 去掉空白
    return not stripped


def _handle_voice_only_message(chat_type: str, chat_id: int, user_id: int):
    """处理纯语音消息（无文字）"""
    if HAS_VOICE_RECOGNITION:
        # 用 Whisper 转写语音
        send_text = f"[语音识别] 正在听你说话...（首次加载模型可能需要1-2分钟）"
        if chat_type == "private":
            send_private_msg(user_id, send_text)
        else:
            send_group_msg(chat_id, send_text)
        return None  # 返回 None 表示由上层处理转写结果
    else:
        # 无 Whisper，发提示
        voice_replies = [
            "嗯……你发了一条语音呢……但我现在还听不懂语音……",
            "啊……语音吗……抱歉我现在只能看懂文字和图片呢……",
            "唔……你发的语音我听不到啦……能不能打字告诉我呀……",
            "对不起……我还不会听语音呢……打字和我说话吧……",
        ]
        reply = random.choice(voice_replies)
        print(f"[语音] 收到语音消息（无识别模块），回复提示")
        voice_cq = voice_synthesizer.speak(reply)
        if chat_type == "private":
            send_private_msg(user_id, reply, voice_cq)
            _last_reply_time["private"][user_id] = time.time()
        else:
            send_group_msg(chat_id, reply, voice_cq)
            _last_reply_time["group"][chat_id] = time.time()
        return "SKIP"  # 告诉上层跳过后续处理


def should_reply(chat_type: str, chat_id: int, is_mentioned: bool, has_image: bool) -> bool:
    """
    判断是否应该回复，综合概率 + 冷却时间 + 特殊情况

    Args:
        chat_type: "private" 或 "group"
        chat_id: 用户ID（私聊）或群ID（群聊）
        is_mentioned: 是否被@了（群聊）
        has_image: 是否包含图片

    Returns:
        True=应该回复，False=跳过
    """
    # 私聊几乎总是回，除非在冷却期
    if chat_type == "private":
        if has_image:
            return True  # 有图片必回
        # 检查冷却
        last = _last_reply_time["private"].get(chat_id, 0)
        now = time.time()
        cooldown = REPLY_COOLDOWN_PRIVATE
        if now - last < cooldown:
            print(f"[限流] 私聊冷却中，剩余 {cooldown - (now - last):.0f}s")
            return False
        # 概率判断
        if random.random() < REPLY_PROBABILITY_PRIVATE:
            return True
        else:
            print(f"[限流] 私聊概率跳过 ({REPLY_PROBABILITY_PRIVATE*100:.0f}%)")
            return False

    # 群聊：被@或有图片必回
    if chat_type == "group":
        if is_mentioned or has_image:
            return True

        # 检查冷却
        last = _last_reply_time["group"].get(chat_id, 0)
        now = time.time()
        cooldown = REPLY_COOLDOWN_GROUP
        if now - last < cooldown:
            print(f"[限流] 群聊冷却中，剩余 {cooldown - (now - last):.0f}s")
            return False

        # 概率判断
        if random.random() < REPLY_PROBABILITY_GROUP:
            return True
        else:
            print(f"[限流] 群聊概率跳过 ({REPLY_PROBABILITY_GROUP*100:.0f}%)")
            return False

    return True


def process_message(data: dict):
    """处理收到的消息"""
    post_type = data.get('post_type')
    if post_type != 'message':
        return

    msg_type = data.get('message_type')
    raw_msg = data.get('raw_message', '') or data.get('message', '')
    sender = data.get('sender', {})
    user_id = sender.get('user_id') or data.get('user_id')
    self_id = data.get('self_id', 0)

    if user_id == self_id:
        return

    print(f"[收到消息] {msg_type} from {user_id}: {raw_msg}")

    if msg_type == 'private':
        text = strip_cq_at(raw_msg, self_id)
        has_image = '[CQ:image' in raw_msg
        has_voice = '[CQ:record' in raw_msg or '[CQ:voice' in raw_msg

        # 语音消息处理
        voice_transcribed = False
        if has_voice and _is_voice_only(raw_msg):
            result = _handle_voice_only_message("private", chat_id=user_id, user_id=user_id)
            if result == "SKIP":
                return
            # Whisper 转写
            voice_text = None
            if HAS_VOICE_RECOGNITION:
                try:
                    voice_text = process_voice_message(raw_msg)
                except Exception as e:
                    print(f"[语音识别] 处理失败: {e}")
                    import traceback
                    traceback.print_exc()
            if voice_text:
                text = f"[用户发送了一条语音，内容: {voice_text}]"
                voice_transcribed = True
                print(f"[语音识别] 转写成功，继续处理")
            else:
                fail_reply = "唔……你的语音我没太听清呢……能不能再说一次或者打字告诉我呀……"
                voice_cq = voice_synthesizer.speak(fail_reply)
                send_private_msg(user_id, fail_reply, voice_cq)
                _last_reply_time["private"][user_id] = time.time()
                return

        # 频率限制检查
        if not should_reply("private", user_id, is_mentioned=False, has_image=has_image):
            return

        # 图片识别：提取图片 + 文字分别处理
        image_desc = None
        if HAS_IMAGE_RECOGNITION and has_image:
            text, image_desc = process_image_message(raw_msg)

        if not text and not image_desc:
            return

        # 图片识别结果不再包装成标记文本，而是通过参数传递给LLM
        display_text = text or ""
        if image_desc:
            print(f"[识图] 识别结果: {image_desc[:80]}...")
        elif voice_transcribed:
            display_text = text  # 语音转写结果已在 text 中

        print(f"[私聊] ← {user_id}: {display_text[:80]}...")
        try:
            bot = get_bot(f"private_{user_id}", user_id=user_id, is_group=False)
            if bot.llm:
                bot.llm.is_group_chat = False
                bot.llm.user_id = user_id
            reply = bot.get_response(display_text, play_voice=False, image_desc=image_desc)
            _last_reply_time["private"][user_id] = time.time()
            voice_cq = voice_synthesizer.speak(reply)
            send_private_msg(user_id, reply, voice_cq)
        except Exception as e:
            print(f"[错误] 处理私聊消息失败: {e}")
            import traceback
            traceback.print_exc()

    elif msg_type == 'group':
        # 群聊模式已关闭，私聊测试中
        return
        text = strip_cq_at(raw_msg, self_id)
        group_id = data.get('group_id')
        has_image = '[CQ:image' in raw_msg
        has_voice = '[CQ:record' in raw_msg or '[CQ:voice' in raw_msg

        # 检测是否被@
        is_mentioned = f"[CQ:at,qq={self_id}]" in raw_msg

        # 语音消息处理（群聊中仅处理被@时的纯语音）
        voice_transcribed = False
        if has_voice and _is_voice_only(raw_msg) and is_mentioned:
            result = _handle_voice_only_message("group", chat_id=group_id, user_id=user_id)
            if result == "SKIP":
                return
            voice_text = None
            if HAS_VOICE_RECOGNITION:
                try:
                    voice_text = process_voice_message(raw_msg)
                except Exception as e:
                    print(f"[语音识别] 处理失败: {e}")
            if voice_text:
                text = f"[用户发送了一条语音，内容: {voice_text}]"
                voice_transcribed = True
                print(f"[语音识别] 群聊转写成功，继续处理")
            else:
                fail_reply = "唔……语音没太听清呢……打字告诉我吧……"
                voice_cq = voice_synthesizer.speak(fail_reply)
                send_group_msg(group_id, fail_reply, voice_cq)
                _last_reply_time["group"][group_id] = time.time()
                return

        # 频率限制检查（被@或有图片时跳过限流）
        if not should_reply("group", group_id, is_mentioned=is_mentioned, has_image=has_image):
            return

        # 图片识别：群聊也支持
        image_desc = None
        if HAS_IMAGE_RECOGNITION and has_image:
            text, image_desc = process_image_message(raw_msg)

        if not text and not image_desc:
            return

        # 图片识别结果不再包装成标记文本，而是通过参数传递给LLM
        display_text = text or ""
        if image_desc:
            print(f"[识图] 识别结果: {image_desc[:80]}...")
        elif voice_transcribed:
            display_text = text  # 语音转写结果已在 text 中

        group_id = data.get('group_id')
        print(f"[群聊] ← 群{group_id} 用户{user_id}: {display_text[:80]}...")
        try:
            bot = get_bot(f"group_{group_id}_{user_id}", user_id=group_id, is_group=True)
            if bot.llm:
                bot.llm.is_group_chat = True
                bot.llm.user_id = group_id
            reply = bot.get_response(display_text, play_voice=False, image_desc=image_desc)
            _last_reply_time["group"][group_id] = time.time()
            voice_cq = voice_synthesizer.speak(reply)
            send_group_msg(group_id, reply, voice_cq)
        except Exception as e:
            print(f"[错误] 处理群聊消息失败: {e}")
            import traceback
            traceback.print_exc()


async def ws_client():
    """WebSocket 客户端，连接 NapCat 接收消息"""
    ws_url = NAPCAT_WS_URL  # NapCat WS Server 在根路径推送消息，不加 /event
    print(f"[WebSocket] 正在连接 {ws_url}...")

    retry_delay = 5  # 初始重试间隔（秒）

    while True:
        try:
            async with websockets.connect(ws_url) as websocket:
                print("[WebSocket] 连接成功！")
                retry_delay = 5  # 连接成功，重置退避
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        _MSG_POOL.submit(process_message, data)
                    except json.JSONDecodeError:
                        print(f"[WebSocket] 收到非 JSON 消息: {message[:100]}")
                    except Exception as e:
                        print(f"[WebSocket] 处理消息出错: {e}")
        except websockets.exceptions.ConnectionClosed:
            print(f"[WebSocket] 断开，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except websockets.exceptions.InvalidStatusCode as e:
            if e.status_code == 403:
                print("[WebSocket] 请先启动NapCat")
            else:
                print(f"[WebSocket] 错误{e.status_code}，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except OSError as e:
            if "拒绝" in str(e) or "refused" in str(e).lower():
                print("[WebSocket] 请先启动NapCat")
            else:
                print(f"[WebSocket] 网络错误，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)
        except Exception as e:
            print(f"[WebSocket] 连接失败，{retry_delay}s 后重连...")
            await asyncio.sleep(retry_delay)

        # 指数退避：5 → 10 → 20 → 40 → 60（最大60秒）
        retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    print("=" * 50)
    print("  达妮娅 NapCat WebSocket 适配层")
    print(f"  NapCat WebSocket: {NAPCAT_WS_URL}")
    print(f"  NapCat HTTP API: {NAPCAT_HTTP_URL}")
    print("  群聊模式: 见话就接")
    print(f"  回复限流: 群聊{REPLY_PROBABILITY_GROUP*100:.0f}%/冷{REPLY_COOLDOWN_GROUP}s, 私聊{REPLY_PROBABILITY_PRIVATE*100:.0f}%/冷{REPLY_COOLDOWN_PRIVATE}s")
    print("  语音模式: 发送语音条")
    print(f"  语音识别: {'Whisper ' + ('base' if HAS_VOICE_RECOGNITION else '') + ' (已启用)' if HAS_VOICE_RECOGNITION else '未启用'}")
    print(f"  图片识别: {'已启用' if HAS_IMAGE_RECOGNITION else '未启用'}")
    print(f"  主动互动: {'已启用' if HAS_PERSONALITY else '未启用'}")
    print("=" * 50)

    # 启动主动互动定时任务（仅私聊）
    if HAS_PERSONALITY:
        personality = get_personality_system()
        
        async def auto_chat_task():
            """定时检查是否需要主动发起对话"""
            while True:
                await asyncio.sleep(_AUTO_CHAT_INTERVAL)
                try:
                    if personality.should_initiate_chat(_AUTO_CHAT_INTERVAL):
                        # 获取最近活跃的私聊用户
                        now = time.time()
                        active_users = [
                            uid for uid, last_time in _last_reply_time["private"].items()
                            if now - last_time < 3600  # 1小时内活跃过
                        ]
                        if active_users:
                            user_id = random.choice(active_users)
                            message = personality.get_initiation_line()
                            voice_cq = voice_synthesizer.speak(message)
                            send_private_msg(user_id, message, voice_cq)
                            print(f"[主动互动] → {user_id}: {message}")
                except Exception as e:
                    print(f"[主动互动错误] {e}")
        
        # 启动后台任务（在ws_client内启动，确保有running event loop）
        async def ws_client_with_auto_chat():
            asyncio.create_task(auto_chat_task())
            await ws_client()

        main_task = ws_client_with_auto_chat
    else:
        main_task = ws_client

    try:
        asyncio.run(main_task())
    except KeyboardInterrupt:
        print("\n[退出] 收到中断信号，正在关闭...")
