"""DashScope 音频 SDK 适配。"""

import threading

from dashscope.audio.asr import RecognitionCallback, RecognitionResult


class DashScopeRecognitionCallback(RecognitionCallback):
    """收集 DashScope 实时语音识别结果和完成状态。"""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.error: RecognitionResult | None = None
        self.done = threading.Event()

    def on_complete(self) -> None:
        """标记识别流正常完成。"""
        self.done.set()

    def on_error(self, result: RecognitionResult) -> None:
        """保存识别错误并结束等待。"""
        self.error = result
        self.done.set()

    def on_event(self, result: RecognitionResult) -> None:
        """收集已经结束的识别句子。"""
        sentence = result.get_sentence()
        if (
            sentence
            and sentence.get("text")
            and RecognitionResult.is_sentence_end(sentence)
        ):
            self.texts.append(sentence["text"])

    def on_close(self) -> None:
        """在 SDK 关闭回调时结束等待。"""
        self.done.set()


__all__ = ["DashScopeRecognitionCallback"]
