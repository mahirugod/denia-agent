import requests
import os
import subprocess
import re
import time

from engine_adapters import TTSFactory, normalize_for_tts


class VoiceSynthesizer:
    def __init__(self, api_url: str = "http://127.0.0.1:9880"):
        """
        初始化语音合成器（仅 GPT-SoVITS，失败时不发声）

        Args:
            api_url: GPT-SoVITS API服务地址，默认 http://127.0.0.1:9880
        """
        self.api_url = api_url
        self.default_ref_audio = "dania_ref.wav"
        self.default_prompt_text = "呼呼……你好呀……"
        self.default_language = "zh"
        self.available = False
        self.ffmpeg_path = self._find_ffmpeg()
        # 引擎降级链缓存（5分钟刷新，避免每句都探测）
        self._engine_cache = None
        self._engine_cache_time = 0.0
        self._last_engine_name = None

    @property
    def last_engine_name(self):
        """最近一次成功合成所用的引擎名"""
        return self._last_engine_name

    @staticmethod
    def _clean_text_for_tts(text: str) -> str:
        """
        清理文本，使其适合语音合成

        去除 CQ 码、emoji、表情标签、特殊符号等 GPT-SoVITS 无法正确处理的字符，
        防止语音内容重复或错乱。
        """
        if not text:
            return ""

        # 1. 去除 CQ 码（如 [CQ:image,...], [CQ:record,...]）
        text = re.sub(r'\[CQ:[^\]]*\]', '', text)

        # 1.5 去除情绪标签 [情绪:happy] 等
        text = re.sub(r'\[情绪:\w+\]', '', text)

        # 2. 去除表情标签（如 [@笑哈哈], [心形眼], [微笑], [开心] 等）
        text = re.sub(r'\[[@\u4e00-\u9fff][^\]]*\]', '', text)

        # 3. 去除 markdown 链接语法，保留文字
        text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)

        # 4. 去除 URL
        text = re.sub(r'https?://\S+', '', text)

        # 5. 去除括号内的描述性文本（如"（打哈欠）"、"（脸红）"等）
        text = re.sub(r'[（(][^）)]*[）)]', '', text)

        # 6. 去除【参考资料：xxx】等方括号标签
        text = re.sub(r'【[^【】]+】', '', text)

        # 7. 去除 emoji 和特殊 unicode 字符
        text = re.sub(
            r'[^\u0020-\u007E\u3000-\u303F\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uFF00-\uFFEF\u2026\u2014\u201C\u201D]',
            '', text
        )

        # 8. 去除多余换行和连续空白
        text = re.sub(r'[\r\n]+', '，', text)
        text = re.sub(r' {2,}', ' ', text)

        # 9. 去除只包含标点或空白的片段产生的多余逗号
        text = re.sub(r'，{2,}', '，', text)

        # 10. 去除首尾空白和标点
        text = text.strip().rstrip('，。、！？；：')

        # 11. 如果清理后为空，返回默认文本
        if not text or len(text.strip()) < 1:
            return ""

        return text

    @staticmethod
    def _split_text_for_tts(text: str, max_len: int = 80) -> list:
        """
        将长文本按语义合理切分为短句，避免 GPT-SoVITS 的 cut5 切分导致的重复错乱。
        """
        if len(text) <= max_len:
            return [text]

        # 按中文标点切分
        segments = re.split(r'(?<=[。！？；…])', text)
        chunks = []
        current = ""

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if len(current) + len(seg) <= max_len:
                current += seg
            else:
                if current:
                    chunks.append(current)
                # 如果单句超长，按逗号再切
                if len(seg) > max_len:
                    sub_parts = re.split(r'(?<=[，、])', seg)
                    for part in sub_parts:
                        if len(current) + len(part) <= max_len:
                            current += part
                        else:
                            if current:
                                chunks.append(current)
                            current = part
                else:
                    current = seg

        if current:
            chunks.append(current)

        return chunks

    def _find_ffmpeg(self):
        """自动查找ffmpeg路径（不再硬编码目录）"""
        import shutil

        # 1. 优先用系统PATH里的ffmpeg
        result = shutil.which("ffmpeg")
        if result:
            return result

        # 2. 检查已知的GPT-SoVITS目录里的ffmpeg
        known_dirs = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "GPT-SoVITS"),
            r"D:\GPT-SoVITS-v2pro-20250604",
            r"C:\Users\mahiru\Downloads\GPT-SoVITS-v2pro-20250604",
        ]
        for base in known_dirs:
            for sub in ["runtime", "runtime/Scripts"]:
                cand = os.path.join(base, sub, "ffmpeg.exe")
                if os.path.exists(cand):
                    return cand

        # 3. 搜索Downloads和D盘前两层目录
        search_roots = [
            os.path.expanduser("~/Downloads"),
            "D:\\",
        ]
        for search_root in search_roots:
            if not os.path.isdir(search_root):
                continue
            for root, dirs, files in os.walk(search_root):
                depth = root.replace(search_root, "").count(os.sep)
                if depth > 3:
                    dirs.clear()
                    continue
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")

        # 3. 兜底：就当系统里有
        return "ffmpeg"

    def synthesize(self, text: str, output_path: str = "output.wav") -> bool:
        """
        调用语音合成API生成语音

        Args:
            text: 要合成的文本
            output_path: 输出音频文件路径

        Returns:
            bool: 是否成功
        """
        # 清理文本
        cleaned = self._clean_text_for_tts(text)
        print(f"[TTS] 原文: {text[:60]}{'...' if len(text)>60 else ''}")
        print(f"[TTS] 清理后: {cleaned}")
        if not cleaned:
            print(f"[TTS] 文本清理后为空，跳过合成")
            return False

        # 数字读法规范化（时间/百分比/小数/整数 → 中文读法）
        cleaned = normalize_for_tts(cleaned)

        # 将长文本切分为短句，避免 GPT-SoVITS 对长文本处理不稳定
        segments = self._split_text_for_tts(cleaned, max_len=80)
        print(f"[TTS] 分段数: {len(segments)}")

        if len(segments) == 1:
            # 单段直接合成
            return self._synth_one_segment(segments[0], output_path)
        else:
            # 多段分段合成后拼接
            wav_paths = []
            try:
                for i, seg in enumerate(segments):
                    seg_path = f"{output_path}.part{i}.wav"
                    if not self._synth_one_segment(seg, seg_path):
                        print(f"[TTS] 第{i+1}段合成失败: {seg[:20]}...")
                        # 失败的段跳过，继续合成后续段
                        continue
                    wav_paths.append(seg_path)

                if not wav_paths:
                    return False

                if len(wav_paths) == 1:
                    # 只有一段成功，直接重命名
                    if wav_paths[0] != output_path:
                        os.replace(wav_paths[0], output_path)
                    return True

                # 用 ffmpeg 拼接多段 WAV
                list_file = f"{output_path}.list.txt"
                with open(list_file, 'w', encoding='utf-8') as f:
                    for wp in wav_paths:
                        # ffmpeg concat demuxer 需要转义单引号
                        escaped = wp.replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

                result = subprocess.run(
                    [self.ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                     "-i", list_file, "-c", "copy", output_path],
                    capture_output=True, timeout=30
                )

                return result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0
            finally:
                # 清理临时分段文件
                for wp in wav_paths:
                    try:
                        os.remove(wp)
                    except OSError:
                        pass
                try:
                    os.remove(f"{output_path}.list.txt")
                except OSError:
                    pass

    def _get_engine_chain(self, force_refresh: bool = False) -> list:
        """按优先级返回可用引擎列表（缓存5分钟）"""
        now = time.time()
        if not force_refresh and self._engine_cache and now - self._engine_cache_time < 300:
            return self._engine_cache
        chain = TTSFactory.priority_chain(probe=True)
        self._engine_cache = chain
        self._engine_cache_time = now
        names = [a.name for a in chain]
        print(f"[TTS] 可用引擎: {names if names else '无'}")
        return chain

    def _synth_one_segment(self, text: str, output_path: str) -> bool:
        """合成单个文本段（仅 GPT-SoVITS；失败返回 False，不发声）"""
        print(f"[TTS] 合成文本: {text[:50]}{'...' if len(text)>50 else ''} (共{len(text)}字)")

        # 第一轮：用缓存引擎
        for adapter in self._get_engine_chain():
            if adapter.synthesize(text, output_path):
                self._last_engine_name = adapter.name
                return True

        # 第二轮：引擎可能刚启动，强制重新探测重试一次
        for adapter in self._get_engine_chain(force_refresh=True):
            if adapter.synthesize(text, output_path):
                self._last_engine_name = adapter.name
                return True

        print("[TTS] GPT-SoVITS 合成失败，本段不发声")
        return False

    def convert_to_silk(self, wav_path: str, silk_path: str) -> bool:
        """
        将WAV转换为silk格式，使用ffmpeg先转PCM再手动添加silk头

        Args:
            wav_path: WAV文件路径
            silk_path: 输出silk文件路径

        Returns:
            bool: 是否成功
        """
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-y", "-i", wav_path,
                 "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le",
                 "-f", "wav", "-"],
                capture_output=True, timeout=30
            )

            if result.returncode != 0:
                print(f"ffmpeg转换失败: {result.stderr.decode('utf-8', errors='ignore')}")
                return False

            pcm_data = result.stdout

            import io
            input_io = io.BytesIO(pcm_data)
            output_io = io.BytesIO()

            try:
                import pysilk
                pysilk.encode(input_io, output_io, 24000, 24000, tencent=True)
                silk_data = output_io.getvalue()
            except Exception as e:
                print(f"pysilk编码失败: {e}")
                with open(silk_path.replace('.silk', '_pcm.wav'), 'wb') as f:
                    f.write(pcm_data)
                return False

            with open(silk_path, 'wb') as f:
                f.write(silk_data)

            return os.path.exists(silk_path) and os.path.getsize(silk_path) > 100
        except Exception as e:
            print(f"音频转换失败: {e}")
            return False

    def speak(self, text: str) -> str:
        """
        合成语音并返回CQ码用于发送

        Args:
            text: 要合成的文本

        Returns:
            str: CQ码（语音附件），失败返回None
        """
        base_dir = os.path.dirname(__file__)
        wav_path = os.path.join(base_dir, "temp_audio.wav")
        silk_path = os.path.join(base_dir, "temp_audio.silk")

        wav_path = os.path.abspath(wav_path)
        silk_path = os.path.abspath(silk_path)

        if self.synthesize(text, wav_path):
            try:
                if self.convert_to_silk(wav_path, silk_path):
                    file_url = "file:///" + silk_path.replace("\\", "/")
                    return f"[CQ:record,file={file_url}]"
            finally:
                if os.path.exists(wav_path):
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass
        return None

    def is_available(self) -> bool:
        """
        检查语音服务是否可用（任一引擎可用即 True）

        Returns:
            bool: 服务是否可用
        """
        self.available = len(self._get_engine_chain()) > 0
        return self.available

if __name__ == "__main__":
    synthesizer = VoiceSynthesizer()

    if synthesizer.is_available():
        print("[OK] Voice service available")
        cq_code = synthesizer.speak("呼呼……你好呀……我是达妮娅……")
        if cq_code:
            print(f"CQ code: {cq_code}")
        else:
            print("[ERROR] Voice conversion failed")
    else:
        print("[ERROR] Voice service not available")
