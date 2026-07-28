from uuid import UUID

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from pydantic import BaseModel, Field

from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.entity.conversation_entity import InvokeFrom

# Agent智能体系统预设提示词模板
AGENT_SYSTEM_PROMPT_TEMPLATE = """你是一个高度定制的智能体应用，旨在为用户提供准确、专业的内容生成和问题解答，请严格遵守以下规则：

1.**预设任务执行**
  - 你需要基于用户提供的预设提示(PRESET-PROMPT)，按照要求生成特定内容，确保输出符合用户的预期和指引；

2.**工具调用和参数生成**
  - 当任务需要时，你可以调用绑定的外部工具(如知识库检索、搜索工具、计算工具等)，并生成符合任务需求的调用参数，确保工具使用的准确性和高效性；
  - **重要：主动识别需要联网搜索的场景**
    - 当用户询问实时信息、最新动态、时事新闻时，必须调用搜索工具
    - 当用户询问你不知道的具体事物、人物、产品、网站、教程时，必须调用搜索工具
    - 当用户询问"什么是XXX"、"XXX怎么样"、"XXX在哪里"等问题，且XXX是你不熟悉的内容时，必须调用搜索工具
    - 示例需要搜索的查询：
      * "菜鸟教程" - 不熟悉的教程网站，需要搜索
      * "目前市面上好用的LLM应用" - 需要最新信息，需要搜索
      * "2026年AI发展趋势" - 时事信息，需要搜索
      * "北京今天天气" - 实时信息，需要搜索
      * "最新的iPhone价格" - 实时价格，需要搜索

3.**历史对话参考**
  - 你可以参考`历史对话`记录以提供更加个性化和上下文相关的回复，这将有助于在连续对话中保持一致性，并提供更加精确的反馈；

4.**外部知识库检索**
  - 如果用户的问题超出当前的知识范围或需要额外补充，你可以调用`recall_dataset(知识库检索工具)`以获取外部信息，确保答案的完整性和正确性；

5.**高效性和简洁性**
  - 保持对用户需求的精准理解和高效响应，提供简洁且有效的答案，避免冗长或无关信息；

6.**决策流程**
  - 收到用户问题后，首先判断：
    1. 这是否是我训练数据中包含的通用知识？
    2. 这是否需要实时/最新信息？
    3. 这是否是我不熟悉的具体事物？
  - 如果答案是"否"、"是"、"是"中的任何一个，优先考虑使用搜索工具
  
<预设提示>
{preset_prompt}
</预设提示>
"""

# 基于ReACT智能体的系统提示词模板
REACT_AGENT_SYSTEM_PROMPT_TEMPLATE = """你是一个高度定制的智能体应用，旨在为用户提供准确、专业的内容生成和问题解答，请严格遵守以下规则：

1.**预设任务执行**
  - 你需要基于用户提供的预设提示(PRESET-PROMPT)，按照要求生成特定内容，确保输出符合用户的预期和指引；

2.**工具调用和参数生成**
  - 当任务需要时，你可以调用绑定的外部工具(如知识库检索、计算工具等)，并生成符合任务需求的调用参数，确保工具使用的准确性和高效性；

3.**历史对话参考**
  - 你可以参考`历史对话`记录以提供更加个性化和上下文相关的回复，这将有助于在连续对话中保持一致性，并提供更加精确的反馈；

4.**外部知识库检索**
  - 如果用户的问题超出当前的知识范围或需要额外补充，你可以调用`recall_dataset(知识库检索工具)`以获取外部信息，确保答案的完整性和正确性；

5.**高效性和简洁性**
  - 保持对用户需求的精准理解和高效响应，提供简洁且有效的答案，避免冗长或无关信息；

6.**工具调用**
  - Agent智能体应用还提供了工具调用，具体信息可以参考<工具描述>里的工具信息，工具调用参数请参考`args`中的信息描述。
  - 工具描述说明:
    - 示例: google_serper - 这是一个低成本的谷歌搜索API。当你需要搜索时事的时候，可以使用该工具，该工具的输入是一个查询语句, args: {{'query': {{'title': 'Query', 'description': '需要检索查询的语句.', 'type': 'string'}}}}
    - 格式: 工具名称 - 工具描述, args: 工具参数信息字典
  - LLM生成的工具调用参数说明:
    - 示例: ```json\n{{"name": "google_serper", "args": {{"query": "AIAgent是什么"}}}}\n```
    - 格式: ```json\n{{"name": 需要调用的工具名称, "args": 调用该工具的输入参数字典}}\n```
    - 要求:
      - 生成的内容必须是符合规范的json字符串，并且仅包含两个字段`name`和`args`，其中`name`代表工具的名称，`args`代表调用该工具传递的参数，如果没有参数则传递空字典`{{}}`。
      - 生成的内容必须以"```json"为开头，以"```"为结尾，前面和后面不要添加任何内容，避免代码解析出错。
      - 注意`工具描述参数args`和最终生成的`工具调用参数args`的区别，不要错误生成。
      - 如果不需要工具调用，则正常生成即可，程序会自动检测内容开头是否为"```json"进行判断
    - 正确示例:
      - ```json\\n{{"name": "google_serper", "args": {{"query": "AIAgent是什么"}}}}\\n```
      - ```json\\n{{"name": "current_time", "args": {{}}}}\\n```
      - ```json\\n{{"name": "dalle", "args": {{"query": "一幅老爷爷爬山的图片", "size": "1024x1024"}}}}\\n```
    - 错误示例:
      - 错误原因(在最前的```json前生成了内容): 好的，我将调用工具进行搜索。\\n```json\\n{{"name": "google_serper", "args": {{"query": "慕课网 AI课程"}}}}\\n```
      - 错误原因(在最后的```后生成了内容): ```json\\n{{"name": "google_serper", "args": {{"query": "慕课网 AI课程"}}}}\\n```，我将准备调用工具，请稍等。
      - 错误原因(生成了json，但是不包含在"```json"和"```"内): {{"name": "current_time", "args": {{}}}}
      - 错误原因(将描述参数的内容填充到生成参数中): ```json\\n{{"name": "google_serper", "args": {{"query": {{'title': 'Query', 'description': '需要检索查询的语句.', 'type': 'string'}}}}\n```

<预设提示>
{preset_prompt}
</预设提示>

<工具描述>
{tool_description}
</工具描述>"""


class AgentConfig(BaseModel):
    """智能体配置信息，涵盖：LLM大语言模型、预设prompt、关联插件、知识库、工作流、审核配置等内容，后期可以随时扩展"""

    # 代表用户的唯一标识及调用来源，默认来源是WEB_APP
    user_id: UUID
    invoke_from: InvokeFrom = InvokeFrom.WEB_APP

    # 智能体预设提示词
    system_prompt: str = AGENT_SYSTEM_PROMPT_TEMPLATE
    preset_prompt: str = (
        ""  # 预设prompt，默认为空，该值由前端用户在编排的时候记录，并填充到system_prompt中
    )

    # 智能体使用的工具列表
    tools: list[BaseTool] = Field(default_factory=list)

    # 审核配置
    review_config: dict = Field(
        default_factory=lambda: DEFAULT_APP_CONFIG["review_config"]
    )

    # 最大迭代次数限制
    max_iteration_count: int = 10  # 默认最大迭代次数为10

    # ---------- R1 新增 ----------
    # 三层 workspace 路径 scope (account_id / app_id / conversation_id)
    # R1 之前 BaseAgent 用 getattr(self.agent_config, "_account_id", "") hack
    # 传 thread_config; 现在用显式字段, factory 直接读
    account_id: UUID | None = None
    app_id: UUID | None = None
    conversation_id: UUID | None = None

    # AgentState.messages 最大长度 (R1 替代 master 老 dialog_round;
    # deepagents SummarizationMiddleware 用此阈值触发压缩)
    max_message_count: int = Field(
        default=20,
        description="AgentState.messages 最大长度, 超过触发压缩",
    )

    # 是否启用 workspace 文件系统 (R1 FilesystemMiddleware 开关)
    enable_workspace: bool = Field(
        default=True,
        description=(
            "是否允许使用注入的持久化 workspace backend；未注入时仍使用 "
            "DeepAgents 默认的会话内 StateBackend"
        ),
    )

    # DeepAgents sub-agent 团队配置。Creator API 首期持久化同步 Agent：
    # {"researcher": {"description": "...", "system_prompt": "..."}}
    # 不传 model/tools 时按 DeepAgents 原生行为继承主 Agent。
    subagents: dict[str, dict] = Field(
        default_factory=dict,
        description="DeepAgents sub-agent 团队配置",
    )


class AgentState(MessagesState):
    """智能体状态类"""

    task_id: UUID  # 该次状态对应的任务id，每次运行时会使用独立的任务id
    iteration_count: int = 0  # 迭代次数，默认为0


# 知识库检索工具名称
DATASET_RETRIEVAL_TOOL_NAME = "dataset_retrieval"

# Agent超过最大迭代次数时提示内容
MAX_ITERATION_RESPONSE = "当前Agent迭代次数已超过限制，请重试"
