from concurrent.futures import ThreadPoolExecutor

import pytest
from edge_tts.exceptions import NoAudioReceived

from internal.service import audio_service
from internal.service.audio_service import AudioService


def _synthesize_in_worker() -> bytes:
    """模拟 Flask 请求线程调用同步语音合成边界。"""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            AudioService._edge_tts_synthesize,
            "测试语音",
            "zh-CN-XiaoxiaoNeural",
        ).result(timeout=5)


class _IntermittentCommunicate:
    """第一次返回空音频，第二次恢复的 Edge-TTS 替身。"""

    attempts = 0

    def __init__(self, text: str, voice: str) -> None:
        """记录本次合成尝试序号。"""
        del text, voice
        type(self).attempts += 1
        self.attempt = type(self).attempts

    async def stream(self):
        """首轮复现上游空响应，后续返回有效音频。"""
        if self.attempt == 1:
            if False:
                yield {}
            raise NoAudioReceived("No audio was received")
        yield {"type": "audio", "data": b"audio"}


class _EmptyCommunicate:
    """每次都不返回音频的 Edge-TTS 替身。"""

    attempts = 0

    def __init__(self, text: str, voice: str) -> None:
        """累计实际执行的合成次数。"""
        del text, voice
        type(self).attempts += 1

    async def stream(self):
        """稳定抛出生产日志中的空音频异常。"""
        if False:
            yield {}
        raise NoAudioReceived("No audio was received")


def test_edge_tts_retries_transient_empty_audio(monkeypatch) -> None:
    """瞬时空音频应在有限重试后恢复。"""
    _IntermittentCommunicate.attempts = 0
    monkeypatch.setattr(audio_service.edge_tts, "Communicate", _IntermittentCommunicate)

    assert _synthesize_in_worker() == b"audio"
    assert _IntermittentCommunicate.attempts == 2


def test_edge_tts_exhausts_retries_without_event_loop_noise(monkeypatch) -> None:
    """持续失败应保留原始异常，且不串入工作线程事件循环异常。"""
    _EmptyCommunicate.attempts = 0
    monkeypatch.setattr(audio_service.edge_tts, "Communicate", _EmptyCommunicate)

    with pytest.raises(NoAudioReceived) as error:
        _synthesize_in_worker()

    assert _EmptyCommunicate.attempts == 3
    assert not isinstance(error.value.__context__, RuntimeError)
