"""memory_facts 表 Marshmallow schema (R4 end-user "我的记忆" API)

复用 master wtforms / marshmallow 模式（详见 internal/schema/app_schema.py）。

API 端点（plan §3.1, 5 端点 WebApp + 1 端点 creator）：
- WebApp token 鉴权 (token + 可选 end_user_id):
  - GET  /web-apps/<token>/memory/facts            列表
  - POST /web-apps/<token>/memory/facts            手动写入
  - GET  /web-apps/<token>/memory/facts/<fact_id>  单个
  - DELETE /web-apps/<token>/memory/facts/<fact_id> 软删除 (仅本人)
- Creator @login_required:
  - GET    /apps/<app_id>/memory/facts            列表 (含 end-user filter)
  - DELETE /apps/<app_id>/memory/facts/<fact_id>  软删除 (creator 全权)

约束 (硬约束 #5): 复用 master wtforms 校验模式 (FlaskForm + DataRequired 等);
不自建参数解析层.
"""
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import (
    DataRequired,
    Length,
    Optional,
    NumberRange,
)
from wtforms import FloatField


# ---------- WebApp token 鉴权端点 ----------


class CreateMemoryFactReq(FlaskForm):
    """end-user 手动写入 fact (POST /web-apps/<token>/memory/facts).

    end_user_id 由 service 层从会话 / token 推断, 不接受外部传 (避免越权).
    """

    fact_type = StringField(
        "fact_type",
        validators=[
            DataRequired("fact_type 不能为空"),
            Length(max=64, message="fact_type 长度最大 64"),
        ],
    )
    content = StringField(
        "content",
        validators=[
            DataRequired("content 不能为空"),
            Length(max=2000, message="content 长度最大 2000"),
        ],
    )
    confidence = FloatField(
        "confidence",
        validators=[
            Optional(),
            NumberRange(min=0.0, max=1.0, message="confidence 必须在 [0.0, 1.0]"),
        ],
        default=1.0,
    )


# ---------- Creator 面板端点 ----------


class GetMemoryFactsReq(FlaskForm):
    """creator 面板 (GET /apps/<app_id>/memory/facts) 查询参数.

    end_user_id 留空字符串 = '只看 account 级'; 留 None = 全 app.
    """

    end_user_id = StringField(
        "end_user_id",
        validators=[Optional(), Length(max=64)],
        default=None,
    )
    fact_type = StringField(
        "fact_type",
        validators=[Optional(), Length(max=64)],
        default=None,
    )


__all__ = ["CreateMemoryFactReq", "GetMemoryFactsReq"]