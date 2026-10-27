"""发现快照参数契约：标准名称、显式覆盖与 OpenAI-compatible 调用映射。"""

from __future__ import annotations

import math
from typing import Any

from .entities.model_entity import ModelEntity, ModelParameter, ModelType, DEFAULT_MAX_OUTPUT_TOKENS

# 这些是适配器支持的请求字段，不代表任何模型默认支持它们。
PARAMETER_TYPES = {
    "temperature": ("float", "温度"), "top_p": ("float", "Top P"),
    "top_k": ("int", "Top K（采样）"),
    "presence_penalty": ("float", "存在惩罚"), "frequency_penalty": ("float", "频率惩罚"),
    "max_output_tokens": ("int", "最大输出 token"), "reasoning_effort": ("string", "思考级别"),
    "seed": ("int", "随机种子"), "stop": ("string", "停止序列"),
}
OUTPUT_NAMES = {"max_tokens", "max_completion_tokens", "max_output_tokens"}
THINKING_NAMES = {"reasoning_effort", "thinking_level", "thinking"}
COMMON_REASONING_LEVELS = ("minimal", "low", "medium", "high")


def canonical_parameter_name(name: str) -> str:
    """统一输出上限与字符串思考级别的配置名，实际字段由契约保留。"""
    if name in OUTPUT_NAMES:
        return "max_output_tokens"
    # Different OpenAI-compatible providers use different names for the same
    # low/medium/high reasoning control.
    return "reasoning_effort" if name in THINKING_NAMES else name


def canonicalize_parameters(values: dict[str, Any]) -> dict[str, Any]:
    """标准名与旧名冲突时拒绝，未设置值不发送给供应商。"""
    result = {}
    for name, value in values.items():
        if value is None:
            continue
        key = canonical_parameter_name(name)
        if key in result and result[key] != value:
            raise ValueError(f"{PARAMETER_TYPES.get(key, ('', key))[1]}参数存在冲突，请只保留一个值")
        result[key] = value
    return result


def parse_parameter_definitions(raw: Any, *, output_limit: int = 0) -> list[ModelParameter]:
    """只解析供应商或用户明确声明的支持列表，缺失列表不填入旧通用模板。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("模型参数定义必须是数组")
    parameters = []
    seen = set()
    for item in raw:
        if not isinstance(item, (str, dict)):
            raise ValueError("模型参数定义必须是名称或对象")
        item = {"name": item} if isinstance(item, str) else dict(item)
        raw_name = str(item.get("name", ""))
        name = canonical_parameter_name(raw_name)
        if name not in PARAMETER_TYPES:
            continue
        if name in seen:
            raise ValueError(f"模型参数重复：{name}")
        kind, label = PARAMETER_TYPES[name]
        item["type"] = {"number": "float", "integer": "int"}.get(item.get("type"), item.get("type", kind))
        if "minimum" in item and "min" not in item:
            item["min"] = item["minimum"]
        if "maximum" in item and "max" not in item:
            item["max"] = item["maximum"]
        if "enum" in item and "options" not in item:
            item["options"] = [{"label": str(value), "value": value} for value in item["enum"]]
        api_name = item.get("api_name") or raw_name
        valid_api_names = OUTPUT_NAMES if name == "max_output_tokens" else THINKING_NAMES if name == "reasoning_effort" else {name}
        if api_name not in valid_api_names:
            raise ValueError(f"模型参数 API 字段不合法：{name}")
        if item.get("type", kind) != kind:
            raise ValueError(f"模型参数类型不符合调用协议：{name}")
        definition = {**item, "name": name, "api_name": api_name, "label": item.get("label") or label, "type": kind}
        if name == "max_output_tokens":
            definition["type"] = "int"
            definition["min"] = max(1, definition.get("min") or 1)
            if output_limit:
                definition["max"] = min(definition.get("max") or output_limit, output_limit)
        parameter = ModelParameter.model_validate(definition)
        if parameter.min is not None and parameter.max is not None and parameter.min > parameter.max:
            raise ValueError(f"模型参数最小值大于最大值：{name}")
        for option in parameter.options:
            validate_parameter_value(parameter.model_copy(update={"options": []}), option.value)
        if parameter.default is not None:
            validate_parameter_value(parameter, parameter.default)
        seen.add(name)
        parameters.append(parameter)
    return parameters


def normalize_model_parameter_contract(model: ModelEntity) -> ModelEntity:
    """校验发现参数；目录缺省时采用用户要求的 GPT-5 初始模板。"""
    model.parameters = parse_parameter_definitions(
        [parameter.model_dump() for parameter in model.parameters],
        output_limit=model.max_output_tokens,
    )
    model.metadata = {**model.metadata, "parameter_contract_version": 2}
    if not model.parameters and model.metadata.get("parameters_source", "unknown") == "unknown" and model.model_type in {ModelType.CHAT, ModelType.COMPLETION}:
        model.parameters = effective_model_parameters(model)
        model.metadata["parameters_source"] = "gpt5_preset"
    return model


def effective_model_parameters(model: ModelEntity) -> list[ModelParameter]:
    """缺少参数列表时提供 GPT-5 初始配置；服务声明和人工定义优先。"""
    source = model.metadata.get("parameters_source", "unknown")
    if model.parameters or model.model_type not in {ModelType.CHAT, ModelType.COMPLETION}:
        return model.parameters
    if source != "unknown":
        return model.parameters
    output_default = min(limit for limit in (DEFAULT_MAX_OUTPUT_TOKENS, model.max_output_tokens, model.context_window) if limit > 0)
    return parse_parameter_definitions([
        {"name": "max_output_tokens", "api_name": "max_output_tokens" if model.attributes.get("api_format") == "responses" else "max_completion_tokens",
         "default": output_default, "min": 1},
        {"name": "reasoning_effort", "default": "medium",
         "options": [{"label": value, "value": value} for value in COMMON_REASONING_LEVELS]},
    ], output_limit=model.max_output_tokens)


def validate_parameter_value(parameter: ModelParameter, value: Any) -> None:
    """零值边界、枚举值与 bool/int 分离，避免静默重置用户输入。"""
    kind = parameter.type.value
    valid = {
        "int": type(value) is int,
        "float": type(value) in {int, float} and math.isfinite(value),
        "boolean": type(value) is bool,
        "string": isinstance(value, str),
    }[kind]
    if not valid:
        raise ValueError(f"{parameter.label} 类型应为 {kind}")
    if parameter.options and not any(value == option.value and (type(value) is type(option.value) or kind == "float" and type(option.value) in {float, int}) for option in parameter.options):
        raise ValueError(f"{parameter.label} 必须是支持的选项")
    if kind in {"int", "float"}:
        if parameter.min is not None and value < parameter.min:
            raise ValueError(f"{parameter.label} 不能小于 {parameter.min:g}")
        if parameter.max is not None and value > parameter.max:
            raise ValueError(f"{parameter.label} 不能大于 {parameter.max:g}")


def validate_model_parameters(model: ModelEntity, values: dict[str, Any]) -> dict[str, Any]:
    """保存时仅保留显式覆盖，避免把连接默认值固化到 Agent 配置。"""
    if not isinstance(values, dict):
        raise ValueError("模型参数必须是对象")
    result = canonicalize_parameters(values)
    definitions = {parameter.name: parameter for parameter in effective_model_parameters(model)}
    unsupported = set(result) - definitions.keys()
    if unsupported:
        raise ValueError("当前模型参数配置不包含这些字段，请调整配置：" + ", ".join(sorted(unsupported)))
    for name, parameter in definitions.items():
        if name not in result:
            if not parameter.required:
                continue
            if parameter.default is None:
                raise ValueError(f"{parameter.label} 为必填参数")
            result[name] = parameter.default
        validate_parameter_value(parameter, result[name])
    limit = model.max_output_tokens
    if limit and result.get("max_output_tokens", 0) > limit:
        raise ValueError(f"最大输出 token 不能超过模型上限 {limit}")
    if model.context_window and result.get("max_output_tokens", 0) > model.context_window:
        raise ValueError("最大输出 token 不能超过上下文容量")
    return result


def resolve_model_parameters(model: ModelEntity, values: dict[str, Any]) -> dict[str, Any]:
    """执行时合并连接默认值与调用覆盖，随后统一校验范围。"""
    defaults = {parameter.name: parameter.default for parameter in effective_model_parameters(model) if parameter.default is not None}
    return validate_model_parameters(model, {**defaults, **canonicalize_parameters(values)})


def runtime_model_parameters(model: ModelEntity, values: dict[str, Any]) -> dict[str, Any]:
    """供应商采样、非标准思考与精确输出字段使用 extra_body，避免 SDK 拒绝或重命名。"""
    normalized = resolve_model_parameters(model, values)
    definitions = {parameter.name: parameter for parameter in effective_model_parameters(model)}
    result = {}
    for name, value in normalized.items():
        if name == "max_output_tokens" and model.attributes.get("api_format") == "responses":
            result["max_tokens"] = value
        elif name in {"max_output_tokens", "top_k"}:
            result.setdefault("extra_body", {})[definitions[name].api_name or name] = value
        elif name == "reasoning_effort":
            api_name = definitions[name].api_name or name
            if model.attributes.get("api_format") == "responses":
                result["reasoning"] = {"effort": value}
            elif api_name == "reasoning_effort":
                result[name] = value
            else:
                result.setdefault("extra_body", {})[api_name] = value
        else:
            result[name] = value
    return result
