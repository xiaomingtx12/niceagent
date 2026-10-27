"""将配置容量或平台默认窗口转换为框架输入预算，保留来源供界面解释。"""

from internal.core.language_model.entities.model_entity import DEFAULT_CONTEXT_WINDOW


def positive_int(value):
    """未知配置保持未知，不把布尔值和零当作容量。"""
    return value if type(value) is int and value > 0 else None


def with_context_budget(model):
    """只复制模型配置；自动与主动压缩都使用此 profile，不修改共享模型。"""
    metadata = dict(model.metadata or {})
    if positive_int(metadata.get("context_input_budget")):
        return model
    profile = dict(model.profile or {})
    window = positive_int(metadata.get("context_window"))
    if window is None:
        window = DEFAULT_CONTEXT_WINDOW
        metadata["context_window_source"] = "default"
    input_limit = positive_int(metadata.get("max_input_tokens")) if "max_input_tokens" in metadata else positive_int(profile.get("max_input_tokens"))
    metadata.update(context_window=window, max_input_tokens=input_limit)
    reserve = positive_int(metadata.get("configured_output_tokens")) or positive_int(getattr(model, "max_tokens", None))
    limits = [value for value in (input_limit, window - reserve if window and reserve else None) if value is not None]
    if not limits:
        profile.pop("max_input_tokens", None)
        return model.model_copy(update={"metadata": metadata, "profile": profile})
    ceiling = min(limits)
    safety = min(4096, max(256, int(ceiling * 0.02)))
    budget = ceiling - safety
    if budget <= 0:
        raise ValueError("模型输出预留和安全余量已用尽输入预算")
    metadata.update(max_input_tokens=input_limit, context_input_budget=budget, context_safety_tokens=safety)
    profile["max_input_tokens"] = budget
    return model.model_copy(update={"metadata": metadata, "profile": profile})


def request_input_budget(model):
    """展示构图时确定的预算；当前平台不会在循环中动态改变输出上限。"""
    return positive_int((model.metadata or {}).get("context_input_budget"))
