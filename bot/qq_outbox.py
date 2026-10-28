# -*- coding: utf-8 -*-
"""
QQ 图片外发队列

工具（如 page_image）只负责把图片挂到「当前会话」的队列上，
真正发送由 napcat_adapter_ws 在回复之后统一完成。
这样工具不必知道私聊/群聊的区别，也不会重复组装 OneBot 报文。

桌面端没有 QQ 会话上下文：注册了本地出口（local_sink）时图片直接推给
桌面控制台，否则 queue_images() 返回 False，工具据此改成「已保存到本地」的说法。
"""
import threading

_ctx = threading.local()
_local_sink = None


def set_local_sink(fn):
    """注册桌面控制台出口：没有 QQ 会话时把图片交给它（fn(paths)）"""
    global _local_sink
    _local_sink = fn


def set_current_chat(chat_type: str, chat_id):
    """标记当前消息线程对应的 QQ 会话，并清空上一轮的图片队列"""
    _ctx.chat = (chat_type, chat_id)
    _ctx.images = []


def clear_current_chat():
    _ctx.chat = None
    _ctx.images = []


def queue_images(paths) -> bool:
    """把图片挂到当前会话；没有 QQ 会话时交给桌面控制台（已注册时）"""
    if not paths:
        return False
    chat = getattr(_ctx, "chat", None)
    if not chat:
        if _local_sink is None:
            return False
        try:
            _local_sink(list(paths))
            return True
        except Exception:
            return False
    if not hasattr(_ctx, "images"):
        _ctx.images = []
    _ctx.images.extend(paths)
    return True


def take_images() -> list:
    """取走并清空当前会话的待发图片"""
    imgs = list(getattr(_ctx, "images", []) or [])
    _ctx.images = []
    return imgs


def pending_count() -> int:
    """当前会话已排队但还没发出的图片数量（>0 说明本轮只发图、不发文字）"""
    return len(getattr(_ctx, "images", []) or [])
