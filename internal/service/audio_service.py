import asyncio
import base64
import json
import logging
import threading
from dataclasses import dataclass
from typing import Generator, Union
from uuid import UUID

import edge_tts
from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
from injector import inject
from werkzeug.datastructures import FileStorage

from internal.exception import ForbiddenException, NotFoundException, FailException
from internal.lib.api_key_helper import get_api_key_for_provider
from internal.model import Account, Message, App, AppConfig, AppConfigVersion
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from .web_app_service import WebAppService
from ..entity.app_entity import AppStatus
from ..entity.audio_entity import EDGE_TTS_VOICE_MAP
from ..entity.conversation_entity import InvokeFrom


class _RecognitionCallback(RecognitionCallback):
    """DashScope实时语音识别回调，用于收集识别结果"""

    def __init__(self):
        self.texts = []
        self.error = None
        self.done = threading.Event()

    def on_complete(self) -> None:
        self.done.set()

    def on_error(self, result: RecognitionResult) -> None:
        self.error = result
        self.done.set()

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        if (
            sentence
            and sentence.get("text")
            and RecognitionResult.is_sentence_end(sentence)
        ):
            self.texts.append(sentence["text"])

    def on_close(self) -> None:
        self.done.set()


@inject
@dataclass
class AudioService(BaseService):
    """语音服务，涵盖语音转文本（DashScope Paraformer）、消息流式输出语音（Edge-TTS）"""

    db: SQLAlchemy
    web_app_service: WebAppService

    def audio_to_text(self, audio: FileStorage, account: Account) -> str:
        """使用DashScope Paraformer将语音转换成文本"""
        # 1.获取用户配置的通义API Key（复用已有的tongyi key）
        try:
            api_key = get_api_key_for_provider("tongyi", account.id)
        except Exception as e:
            raise FailException(f"语音转文本失败: {str(e)}")

        # 2.提取音频文件内容
        file_content = audio.stream.read()

        # 3.使用Recognition实时识别（通过callback收集结果）
        try:
            callback = _RecognitionCallback()
            recognition = Recognition(
                model="paraformer-realtime-v2",
                format="wav",
                sample_rate=16000,
                callback=callback,
                api_key=api_key,
            )
            recognition.start()

            # 4.分块发送音频数据
            chunk_size = 3200  # 每次发送3200字节（100ms的16kHz 16bit音频）
            for i in range(0, len(file_content), chunk_size):
                chunk = file_content[i : i + chunk_size]
                recognition.send_audio_frame(chunk)

            recognition.stop()

            # 5.等待识别完成
            callback.done.wait(timeout=30)

            if callback.error:
                raise FailException(f"语音识别失败: {callback.error}")

            # 6.返回识别结果
            text = "".join(callback.texts)
            return text if text else ""
        except FailException:
            raise
        except Exception as e:
            logging.error("语音转文本失败: %(error)s", {"error": e}, exc_info=True)
            raise FailException(f"语音转文本失败: {str(e)}")

    def message_to_audio(self, message_id: UUID | str, account: Account) -> Generator:
        """使用Edge-TTS将消息转换成流式输出语音"""
        # 1.根据传递的消息id获取有效消息
        message = self.get(Message, self._normalize_message_id(message_id))
        if (
            not message
            or message.is_deleted
            or message.answer.strip() == ""
        ):
            raise NotFoundException("该消息不存在，请核实后重试")

        # 2.校验消息归属的会话状态是否正常。不同入口的 created_by
        # 含义不同：调试页保存 Account.id，WebApp 保存 EndUser.id，
        # 因此不能在这里统一与当前 Account.id 比较。
        conversation = message.conversation
        if conversation is None or conversation.is_deleted:
            raise NotFoundException("该消息会话不存在，请核实后重试")

        # 3.定义文本转语音启动配置、音色，默认为开启+xiaoxiao音色
        enable = True
        voice = "xiaobei"

        # 4.根据调用入口校验消息权限并获取文字转语音配置
        if message.invoke_from in [InvokeFrom.WEB_APP, InvokeFrom.DEBUGGER]:
            app = self.get(App, conversation.app_id)
            if app is None or message.app_id != app.id:
                raise NotFoundException(
                    "该消息会话归属的应用不存在或校验失败，请核实后重试"
                )

            if message.invoke_from == InvokeFrom.DEBUGGER:
                if (
                    app.account_id != account.id
                    or message.created_by != account.id
                    or conversation.created_by != account.id
                ):
                    raise NotFoundException(
                        "该消息会话归属的应用不存在或校验失败，请核实后重试"
                    )
            else:
                if app.status != AppStatus.PUBLISHED:
                    raise NotFoundException("该消息会话归属的应用未发布，请核实后重试")
                try:
                    # WebApp 消息和会话保存的是 EndUser.id；复用 WebApp
                    # 的账号映射鉴权，兼容旧数据中直接保存 Account.id 的情况。
                    self.web_app_service.get_owned_web_conversation_by_app_id(
                        app_id=app.id,
                        conversation_id=conversation.id,
                        account=account,
                    )
                except (ForbiddenException, NotFoundException) as error:
                    raise NotFoundException(
                        "该消息会话不存在，请核实后重试"
                    ) from error

            app_config: Union[AppConfig, AppConfigVersion] = (
                app.draft_app_config
                if message.invoke_from == InvokeFrom.DEBUGGER
                else app.app_config
            )
            text_to_speech = app_config.text_to_speech
            enable = text_to_speech.get("enable", False)
            voice = text_to_speech.get("voice", "xiaoxiao")
        elif message.invoke_from == InvokeFrom.CREATOR_TASK:
            # Creator 任务消息由当前账号创建，并使用已发布 Agent 的配置。
            # 任务中心还会展示 WebApp 历史消息，但这条分支只处理
            # invoke_from=creator_task 的业务消息。
            app = self.get(App, conversation.app_id)
            if (
                app is None
                or app.id != message.app_id
                or app.account_id != account.id
                or app.status != AppStatus.PUBLISHED
                or message.created_by != account.id
                or not (
                    conversation.owner_account_id == account.id
                    or conversation.created_by == account.id
                )
            ):
                raise NotFoundException("该消息不存在，请核实后重试")

            app_config: Union[AppConfig, AppConfigVersion] = app.app_config
            text_to_speech = app_config.text_to_speech
            enable = text_to_speech.get("enable", False)
            voice = text_to_speech.get("voice", "xiaoxiao")
        elif message.invoke_from == InvokeFrom.ASSISTANT_AGENT:
            if (
                message.created_by != account.id
                or conversation.created_by != account.id
            ):
                raise NotFoundException("该消息不存在，请核实后重试")
        elif message.invoke_from == InvokeFrom.SERVICE_API:
            raise NotFoundException("开放API消息不支持文本转语音服务")
        else:
            raise NotFoundException("该消息不存在，请核实后重试")

        # 5.根据状态判断是否开启文字转语音
        if enable is False:
            raise FailException("该应用未开启文字转语音功能，请核实后重试")

        # 6.将音色名映射为Edge-TTS的完整voice名称
        edge_voice = EDGE_TTS_VOICE_MAP.get(voice, "zh-CN-XiaoxiaoNeural")

        # 7.使用Edge-TTS生成语音数据
        try:
            audio_data = self._edge_tts_synthesize(
                text=message.answer.strip(),
                voice=edge_voice,
            )
        except Exception as error:
            logging.error("文字转语音失败: %(error)s", {"error": error}, exc_info=True)
            raise FailException("文字转语音失败，请稍后重试")

        # 8.定义内部函数实现流式事件输出
        def tts() -> Generator:
            """从音频数据中分块输出流式事件"""
            common_data = {
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "audio": "",
            }
            # 按1024字节分块输出
            for i in range(0, len(audio_data), 1024):
                chunk = audio_data[i : i + 1024]
                data = {
                    **common_data,
                    "audio": base64.b64encode(chunk).decode("utf-8"),
                }
                yield f"event: tts_message\ndata: {json.dumps(data)}\n\n"
            yield f"event: tts_end\ndata: {json.dumps(common_data)}\n\n"

        # 9.调用tts函数流式事件输出语音数据
        return tts()

    @staticmethod
    def _normalize_message_id(message_id: UUID | str) -> UUID:
        """把业务 UUID 和工作台 ``UUID:assistant`` ID 统一成业务主键。"""
        value = str(message_id).strip()
        if value.endswith(":assistant"):
            value = value[: -len(":assistant")]
        try:
            return UUID(value)
        except (TypeError, ValueError) as exc:
            raise NotFoundException("该消息不存在，请核实后重试") from exc

    @staticmethod
    def _edge_tts_synthesize(text: str, voice: str) -> bytes:
        """调用Edge-TTS合成语音，返回mp3字节数据"""

        async def _synthesize() -> bytes:
            communicate = edge_tts.Communicate(text, voice)
            audio_chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_chunks.append(chunk["data"])
            return b"".join(audio_chunks)

        # 在同步上下文中运行异步函数
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _synthesize())
                    return future.result(timeout=30)
            else:
                return loop.run_until_complete(_synthesize())
        except RuntimeError:
            return asyncio.run(_synthesize())
