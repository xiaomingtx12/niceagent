from langchain_core.messages import HumanMessage

from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel,
    ModelFeature,
    ModelModality,
)


class _TestLanguageModel(BaseLanguageModel):
    """仅用于调用消息转换逻辑的最小抽象实现。"""

    @property
    def _llm_type(self) -> str:
        return "test"

    def invoke(self, input, config=None, **kwargs):
        return input

    def generate_prompt(self, prompts, stop=None, callbacks=None, **kwargs):
        return None

    async def agenerate_prompt(
        self, prompts, stop=None, callbacks=None, **kwargs
    ):
        return None


def test_convert_to_human_message_uses_model_capabilities_for_attachments():
    model = _TestLanguageModel.model_construct(
        features=[],
        input_modalities=[
            ModelModality.TEXT,
            ModelModality.IMAGE,
            ModelModality.VIDEO,
        ],
    )

    message = model.convert_to_human_message(
        "请检查这些文件",
        attachments=[
            {
                "name": "photo.png",
                "mime_type": "image/png",
                "url": "https://files.example/photo.png",
                "path": "/workspace/attachments/photo.png",
            },
            {
                "name": "clip.mp4",
                "mime_type": "video/mp4",
                "url": "https://files.example/clip.mp4",
                "path": "/workspace/attachments/clip.mp4",
            },
        ],
    )

    assert isinstance(message, HumanMessage)
    assert message.content == [
        {"type": "text", "text": "请检查这些文件"},
        {"type": "image", "url": "https://files.example/photo.png"},
        {"type": "video", "url": "https://files.example/clip.mp4"},
    ]


def test_convert_to_human_message_falls_back_to_workspace_read_for_documents():
    model = _TestLanguageModel.model_construct(features=[])

    message = model.convert_to_human_message(
        "总结附件",
        attachments=[
            {
                "name": "report.pdf",
                "mime_type": "application/pdf",
                "path": "/workspace/attachments/report.pdf",
            }
        ],
    )

    assert isinstance(message, HumanMessage)
    assert message.content[0] == {"type": "text", "text": "总结附件"}
    assert "report.pdf" in message.content[1]["text"]
    assert "/workspace/attachments/report.pdf" in message.content[1]["text"]


def test_convert_to_human_message_uses_audio_and_file_blocks_when_supported():
    model = _TestLanguageModel.model_construct(
        features=[],
        input_modalities=[
            ModelModality.TEXT,
            ModelModality.AUDIO,
        ],
        native_file_input=True,
    )

    message = model.convert_to_human_message(
        "分析附件",
        attachments=[
            {
                "name": "meeting.mp3",
                "mime_type": "audio/mpeg",
                "url": "https://files.example/meeting.mp3",
            },
            {
                "name": "report.pdf",
                "mime_type": "application/pdf",
                "url": "https://files.example/report.pdf",
            },
        ],
    )

    assert message.content == [
        {"type": "text", "text": "分析附件"},
        {"type": "audio", "url": "https://files.example/meeting.mp3"},
        {
            "type": "file",
            "url": "https://files.example/report.pdf",
            "mime_type": "application/pdf",
        },
    ]
