# 允许的语音音色（简称，用于前端展示和配置存储）
ALLOWED_AUDIO_VOICES = [
    "xiaoxiao",    # 晓晓 - 女声，温暖亲切
    "xiaoyi",      # 晓艺 - 女声，活泼
    "yunjian",     # 云健 - 男声，沉稳
    "yunxi",       # 云希 - 男声，阳光
    "yunxia",      # 云夏 - 男声，少年
    "yunyang",     # 云扬 - 男声，新闻播报
    "xiaobei",     # 晓北 - 女声，东北口音
    "xiaoni",      # 晓妮 - 女声，温柔
]

# 音色简称到Edge-TTS完整voice名称的映射
EDGE_TTS_VOICE_MAP = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunxia": "zh-CN-YunxiaNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "xiaobei": "zh-CN-liaoning-XiaobeiNeural",
    "xiaoni": "zh-CN-shaanxi-XiaoniNeural",
}
