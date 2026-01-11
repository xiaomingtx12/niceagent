from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired, AnyOf, Optional

from .schema import ListField, DictField


class CreateDocumentsReq(FlaskForm):
    """创建文档请求"""

    upload_file_ids = ListField(
        "upload_file_ids",
        validators=[
            DataRequired("文件id列表不能为空"),
        ],
    )
    process_type = StringField(
        "process_type",
        validators=[
            DataRequired("文件处理类型不能为空"),
            AnyOf(values=["automic", "custom"], message="处理类型格式错误"),
        ],
    )
    rule = DictField(
        "process_rule",
        validators=[
            Optional(),
        ],
    )
