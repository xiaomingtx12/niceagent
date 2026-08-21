import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from injector import inject
from redis import Redis

from internal.model import Account, App, Message
from pkg.sqlalchemy import SQLAlchemy
from .app_service import AppService
from .base_service import BaseService


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
        # 升级缓存版本，避免部署后继续返回不含新 Token 字段的旧结果。
        cache_key = f"analysis_v2:{today.strftime('%Y_%m_%d')}:{str(app.id)}"

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

        # 11.定义5个指标字段名称
        fields = [
            "total_messages",
            "active_accounts",
            "avg_of_conversation_messages",
            "token_output_rate",
            "cost_consumption",
            "total_token_count",
            "input_token_count",
            "output_token_count",
            "cached_token_count",
            "reasoning_token_count",
            "model_call_count",
            "tool_call_count",
        ]

        # 12.构建应用分析字典
        app_analysis = {
            **trend,
            **{
                field: {
                    "data": seven_overview_indicators.get(field),
                    "pop": pop.get(field),
                }
                for field in fields
            },
            "model_usage": {
                "data": model_usage,
                "pop": self.calculate_model_usage_pop(
                    model_usage,
                    previous_model_usage,
                ),
            },
        }

        # 13.将数据存储到redis缓存中，并设置过期时间为1天
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
        """根据传递的消息列表计算概览指标，涵盖全部会话数、激活用户数、平均会话互动数、Token输出速度、费用消耗"""
        # 1.计算全部会话数，使用消息总数来计算
        total_messages = len(messages)

        # 2.计算激活用户数，使用唯一用户数判断
        active_accounts = len({message.created_by for message in messages})

        # 3.平均会话互动数，使用消息总数/会话总数，涉及除法要做/0判断
        avg_of_conversation_messages = 0
        conversation_count = len({message.conversation_id for message in messages})
        if conversation_count != 0:
            avg_of_conversation_messages = total_messages / conversation_count

        # 4.Token输出速度，使用总token数/总耗时，涉及除法要做/0判断
        token_output_rate = 0
        latency_sum = sum(cls._metric(message, "latency") for message in messages)
        if latency_sum != 0:
            token_output_rate = (
                sum(
                    cls._metric(message, "total_token_count")
                    for message in messages
                )
                / latency_sum
            )

        # 5.计算费用消耗，使用总花费进行求和
        cost_consumption = sum(
            cls._metric(message, "total_price") for message in messages
        )

        # 6.返回数据，并且对于小数型数据，如果数值过小，需要转换成float，避免Python使用科学计数法进行展示
        return {
            "total_messages": total_messages,
            "active_accounts": active_accounts,
            "avg_of_conversation_messages": float(avg_of_conversation_messages),
            "token_output_rate": float(token_output_rate),
            "cost_consumption": float(cost_consumption),
            "total_token_count": sum(
                cls._metric(message, "total_token_count") for message in messages
            ),
            "input_token_count": sum(
                cls._metric(message, "input_token_count") for message in messages
            ),
            "output_token_count": sum(
                cls._metric(message, "output_token_count") for message in messages
            ),
            "cached_token_count": sum(
                cls._metric(message, "cached_token_count") for message in messages
            ),
            "reasoning_token_count": sum(
                cls._metric(message, "reasoning_token_count")
                for message in messages
            ),
            "model_call_count": sum(
                cls._metric(message, "model_call_count") for message in messages
            ),
            "tool_call_count": sum(
                cls._metric(message, "tool_call_count") for message in messages
            ),
        }

    @classmethod
    def calculate_pop_by_overview_indicators(
        cls, current_data: dict[str, Any], previous_data: dict[str, Any]
    ) -> dict[str, Any]:
        """根据传递的当前数据+相邻时期的数据计算环比增长"""
        # 1.初始化环比字典
        pop = {}

        # 2.定义需要计算环比的字段
        fields = [
            "total_messages",
            "active_accounts",
            "avg_of_conversation_messages",
            "token_output_rate",
            "cost_consumption",
            "total_token_count",
            "input_token_count",
            "output_token_count",
            "cached_token_count",
            "reasoning_token_count",
            "model_call_count",
            "tool_call_count",
        ]

        # 3.循环遍历字段计算环比增长
        for field in fields:
            current_value = current_data.get(field)
            previous_value = previous_data.get(field)

            # 4.确保上一时期的数据不为0，避免/0错误
            if previous_value != 0:
                pop[field] = float((current_value - previous_value) / previous_value)
            else:
                # 5.如果前一期数据为0，则直接设置环比为0
                pop[field] = 0

        return pop

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
        """根据传递的结束时间、回退天数、消息列表计算对应指标的趋势数据"""
        # 1.重新计算end_at为午夜时间
        end_at = datetime.combine(end_at, datetime.min.time())

        # 2.定义初始数据
        total_messages_trend = {"x_axis": [], "y_axis": []}
        active_accounts_trend = {"x_axis": [], "y_axis": []}
        avg_of_conversation_messages_trend = {"x_axis": [], "y_axis": []}
        cost_consumption_trend = {"x_axis": [], "y_axis": []}
        token_trends = {
            field: {"x_axis": [], "y_axis": []}
            for field in (
                "total_token_count",
                "input_token_count",
                "output_token_count",
                "cached_token_count",
                "reasoning_token_count",
                "model_call_count",
                "tool_call_count",
            )
        }

        # 3.循环遍历计算并提取数据
        for day in range(days_ago):
            # 4.计算起点和终点时间
            trend_start_at = end_at - timedelta(days_ago - day)
            trend_end_at = end_at - timedelta(days_ago - day - 1)

            # 5.计算全部会话趋势
            total_messages_trend_y_axis = len(
                [
                    message
                    for message in messages
                    if trend_start_at <= message.created_at < trend_end_at
                ]
            )
            total_messages_trend["x_axis"].append(int(trend_start_at.timestamp()))
            total_messages_trend["y_axis"].append(total_messages_trend_y_axis)

            # 6.计算激活用户趋势数据
            active_accounts_trend_y_axis = len(
                {
                    message.created_by
                    for message in messages
                    if trend_start_at <= message.created_at < trend_end_at
                }
            )
            active_accounts_trend["x_axis"].append(int(trend_start_at.timestamp()))
            active_accounts_trend["y_axis"].append(active_accounts_trend_y_axis)

            # 7.计算平均会话互动趋势
            avg_of_conversation_messages_trend_y_axis = 0
            conversation_count = len(
                {
                    message.conversation_id
                    for message in messages
                    if trend_start_at <= message.created_at < trend_end_at
                }
            )
            if conversation_count != 0:
                avg_of_conversation_messages_trend_y_axis = (
                    total_messages_trend_y_axis / conversation_count
                )
            avg_of_conversation_messages_trend["x_axis"].append(
                int(trend_start_at.timestamp())
            )
            avg_of_conversation_messages_trend["y_axis"].append(
                float(avg_of_conversation_messages_trend_y_axis)
            )

            # 8.计算费用消耗趋势
            cost_consumption_trend_y_axis = sum(
                cls._metric(message, "total_price")
                for message in messages
                if trend_start_at <= message.created_at < trend_end_at
            )
            cost_consumption_trend["x_axis"].append(int(trend_start_at.timestamp()))
            cost_consumption_trend["y_axis"].append(
                float(cost_consumption_trend_y_axis)
            )

            # 9.计算 Run 级 Token 和调用次数趋势，兼容历史消息。
            day_messages = [
                message
                for message in messages
                if trend_start_at <= message.created_at < trend_end_at
            ]
            for field, trend_data in token_trends.items():
                trend_data["x_axis"].append(int(trend_start_at.timestamp()))
                trend_data["y_axis"].append(
                    sum(cls._metric(message, field) for message in day_messages)
                )

        # 10.返回数据
        return {
            "total_messages_trend": total_messages_trend,
            "active_accounts_trend": active_accounts_trend,
            "avg_of_conversation_messages_trend": avg_of_conversation_messages_trend,
            "cost_consumption_trend": cost_consumption_trend,
            **{
                f"{field}_trend": trend_data
                for field, trend_data in token_trends.items()
            },
        }

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
