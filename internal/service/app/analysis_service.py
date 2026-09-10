import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from injector import inject
from redis import Redis

from internal.model import Account, App, Message
from pkg.sqlalchemy import SQLAlchemy
from .app_service import AppService
from internal.service.common.base_service import BaseService


# 汇总与趋势共用同一组字段，避免新增指标时漏改另一条计算路径。
_SUM_FIELDS = (
    "total_token_count",
    "input_token_count",
    "output_token_count",
    "cached_token_count",
    "reasoning_token_count",
    "model_call_count",
    "tool_call_count",
)
_OVERVIEW_FIELDS = (
    "total_messages",
    "active_accounts",
    "avg_of_conversation_messages",
    "average_latency",
    "token_output_rate",
    "cost_consumption",
    *_SUM_FIELDS,
)
_TREND_FIELDS = tuple(
    field for field in _OVERVIEW_FIELDS if field != "token_output_rate"
)


@inject
@dataclass
class AnalysisService(BaseService):
    """统计分析服务"""

    db: SQLAlchemy
    redis_client: Redis
    app_service: AppService

    def get_app_analysis(self, app_id: UUID, account: Account) -> dict[str, Any]:
        """根据传递的应用id+账号获取指定应用的分析信息"""
        # 1.根据传递的应用id获取应用信息并校验权限
        app = self.app_service.get_app(app_id, account)

        # 2.获取当前时间、午夜时间、7天前时间、14天前的时间
        today = datetime.now()
        today_midnight = datetime.combine(today, datetime.min.time())
        seven_days_ago = today_midnight - timedelta(days=7)
        fourteen_days_ago = today_midnight - timedelta(days=14)

        # 3.计算统计分析数据的缓存键
        # 升级缓存版本，避免继续返回旧吞吐口径及缺少平均耗时的缓存。
        cache_key = f"analysis_v3:{today.strftime('%Y_%m_%d')}:{str(app.id)}"

        # 4.从缓存中获取指定的结果，如果存在则直接返回
        try:
            if self.redis_client.exists(cache_key):
                # 5.解析数据并将数据返回
                app_analysis = self.redis_client.get(cache_key)
                return json.loads(app_analysis)
        except Exception:
            # 6.如果出错则什么都不处理，重新计算数据并更新缓存
            pass

        # 7.查询消息表最近7天，以及最近14-7天的数据（数据要求答案非空、并且只要需要计算的数据）
        seven_days_messages = self.get_messages_by_time_range(
            app, seven_days_ago, today_midnight
        )
        fourteen_days_messages = self.get_messages_by_time_range(
            app, fourteen_days_ago, seven_days_ago
        )

        # 8.计算概览指标，涵盖会话、用户、耗时、费用、Token和调用次数
        seven_overview_indicators = self.calculate_overview_indicators_by_messages(
            seven_days_messages
        )
        fourteen_overview_indicators = self.calculate_overview_indicators_by_messages(
            fourteen_days_messages
        )

        # 9.统计环比数据
        pop = self.calculate_pop_by_overview_indicators(
            seven_overview_indicators, fourteen_overview_indicators
        )

        # 10.计算基础指标和 Run 级 Token 趋势
        trend = self.calculate_trend_by_messages(today_midnight, 7, seven_days_messages)
        model_usage = self.calculate_model_usage_by_messages(seven_days_messages)
        previous_model_usage = self.calculate_model_usage_by_messages(
            fourteen_days_messages
        )

        # 11.构建应用分析字典
        app_analysis = {
            **trend,
            **{
                field: {
                    "data": seven_overview_indicators.get(field),
                    "pop": pop.get(field),
                }
                for field in _OVERVIEW_FIELDS
            },
            "model_usage": {
                "data": model_usage,
                "pop": self.calculate_model_usage_pop(
                    model_usage,
                    previous_model_usage,
                ),
            },
        }

        # 12.将数据存储到redis缓存中，并设置过期时间为1天
        self.redis_client.setex(cache_key, 24 * 60 * 60, json.dumps(app_analysis))

        return app_analysis

    def get_messages_by_time_range(
        self, app: App, start_at: datetime, end_at: datetime
    ) -> list[Message]:
        """根据传递的时间段获取指定应用的消息会话数据"""
        return (
            self.db.session.query(Message)
            .with_entities(
                Message.id,
                Message.conversation_id,
                Message.created_by,
                Message.latency,
                Message.total_token_count,
                Message.input_token_count,
                Message.output_token_count,
                Message.cached_token_count,
                Message.reasoning_token_count,
                Message.model_call_count,
                Message.tool_call_count,
                Message.token_usage,
                Message.total_price,
                Message.created_at,
            )
            .filter(
                Message.app_id == app.id,
                Message.created_at >= start_at,
                Message.created_at < end_at,
                Message.answer != "",
            )
            .all()
        )

    @classmethod
    def calculate_overview_indicators_by_messages(
        cls, messages: list[Message]
    ) -> dict[str, Any]:
        """仅统计有回复的消息；耗时包含工具等待，吞吐不代表模型生成速度。"""
        message_count = len(messages)
        conversation_count = len({message.conversation_id for message in messages})
        latency_sum = sum(cls._metric(message, "latency") for message in messages)
        totals = {
            field: sum(cls._metric(message, field) for message in messages)
            for field in _SUM_FIELDS
        }
        return {
            "total_messages": message_count,
            "active_accounts": len({message.created_by for message in messages}),
            "avg_of_conversation_messages": (
                message_count / conversation_count if conversation_count else 0.0
            ),
            "average_latency": (
                float(latency_sum / message_count) if message_count else 0.0
            ),
            "token_output_rate": (
                float(totals["output_token_count"] / latency_sum)
                if latency_sum
                else 0.0
            ),
            "cost_consumption": float(
                sum(cls._metric(message, "total_price") for message in messages)
            ),
            **totals,
        }

    @classmethod
    def calculate_pop_by_overview_indicators(
        cls, current_data: dict[str, Any], previous_data: dict[str, Any]
    ) -> dict[str, Any]:
        """上一周期为零时沿用零环比约定，不产生无穷大。"""
        return {
            field: (
                float(
                    (current_data.get(field, 0) - previous_data.get(field, 0))
                    / previous_data[field]
                )
                if previous_data.get(field)
                else 0.0
            )
            for field in _OVERVIEW_FIELDS
        }

    @classmethod
    def calculate_model_usage_by_messages(
        cls,
        messages: list[Message],
    ) -> dict[str, dict[str, int]]:
        """按模型汇总输入、输出、缓存和推理 Token。"""
        usage_by_model: dict[str, dict[str, int]] = {}
        for message in messages:
            raw_usage = getattr(message, "token_usage", {}) or {}
            if not isinstance(raw_usage, dict):
                continue
            for model_name, raw_model_usage in raw_usage.items():
                if not isinstance(raw_model_usage, dict):
                    continue
                target = usage_by_model.setdefault(
                    str(model_name),
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                    },
                )
                target["input_tokens"] += cls._int_value(
                    raw_model_usage.get("input_tokens")
                )
                target["output_tokens"] += cls._int_value(
                    raw_model_usage.get("output_tokens")
                )
                target["total_tokens"] += cls._int_value(
                    raw_model_usage.get("total_tokens")
                )
                input_details = raw_model_usage.get("input_details", {})
                output_details = raw_model_usage.get("output_details", {})
                if isinstance(input_details, dict):
                    target["cached_tokens"] += cls._detail_value(
                        input_details,
                        "cached",
                    )
                    target["reasoning_tokens"] += cls._detail_value(
                        input_details,
                        "reasoning",
                    )
                if isinstance(output_details, dict):
                    target["reasoning_tokens"] += cls._detail_value(
                        output_details,
                        "reasoning",
                    )
        return usage_by_model

    @classmethod
    def calculate_model_usage_pop(
        cls,
        current_data: dict[str, dict[str, int]],
        previous_data: dict[str, dict[str, int]],
    ) -> dict[str, float]:
        """计算各模型总 Token 的周期环比。"""
        pop: dict[str, float] = {}
        for model_name in set(current_data) | set(previous_data):
            current_total = current_data.get(model_name, {}).get("total_tokens", 0)
            previous_total = previous_data.get(model_name, {}).get(
                "total_tokens",
                0,
            )
            pop[model_name] = (
                float((current_total - previous_total) / previous_total)
                if previous_total
                else 0.0
            )
        return pop

    @classmethod
    def calculate_trend_by_messages(
        cls,
        end_at: datetime,
        days_ago: int,
        messages: list[Message],
    ) -> dict[str, Any]:
        """按日分组一次，复用概览口径，日期区间为左闭右开。"""
        end_at = datetime.combine(end_at, datetime.min.time())
        start_at = end_at - timedelta(days=days_ago)
        daily_messages: dict[date, list[Message]] = {}
        for message in messages:
            if start_at <= message.created_at < end_at:
                daily_messages.setdefault(message.created_at.date(), []).append(message)
        trends = {
            f"{field}_trend": {"x_axis": [], "y_axis": []} for field in _TREND_FIELDS
        }
        for day in range(days_ago):
            day_start = start_at + timedelta(days=day)
            indicators = cls.calculate_overview_indicators_by_messages(
                daily_messages.get(day_start.date(), [])
            )
            for field in _TREND_FIELDS:
                trends[f"{field}_trend"]["x_axis"].append(int(day_start.timestamp()))
                trends[f"{field}_trend"]["y_axis"].append(indicators[field])
        return trends

    @staticmethod
    def _metric(message: Message, field: str) -> int | float:
        """读取消息统计字段，并兼容历史对象或空值。"""
        value = getattr(message, field, 0)
        return value or 0

    @staticmethod
    def _int_value(value: Any) -> int:
        """把模型用量中的数值安全转换为整数。"""
        return int(value) if isinstance(value, (int, float)) else 0

    @classmethod
    def _detail_value(cls, details: dict[str, Any], name: str) -> int:
        """读取模型明细中的兼容字段名。"""
        aliases = {
            "cached": ("cached", "cached_tokens", "cache_read", "cache_read_tokens"),
            "reasoning": ("reasoning", "reasoning_tokens"),
        }
        for alias in aliases.get(name, (name,)):
            value = details.get(alias)
            if isinstance(value, (int, float)):
                return int(value)
        return 0
