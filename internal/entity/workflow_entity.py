from enum import Enum


class WorkflowStatus(str, Enum):
    """工作流状态类型枚举"""

    DRAFT = "draft"
    PUBLISHED = "published"


class WorkflowResultStatus(str, Enum):
    """工作流运行结果状态"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# 工作流默认配置信息，默认添加一个空的工作流
DEFAULT_WORKFLOW_CONFIG = {
    "graph": {},
    "draft_graph": {"nodes": [], "edges": []},
}


# 工作流生成提示词模板
WORKFLOW_GENERATION_PROMPT_TEMPLATE = """你是一个工作流配置生成专家。根据用户需求生成完整的工作流配置。

## 工作流信息
- 名称：{name}
- 描述：{description}

## 用户需求
{requirements}

{node_types_filter}

{available_node_types}

## 重要规则

### 1. 变量引用格式
- 在prompt和template中使用Jinja2格式：`{{{{ variable_name }}}}`
- 在inputs数组中定义所有变量引用
- 引用格式：
```json
{{
  "name": "variable_name",
  "type": "string",
  "required": true,
  "value": {{
    "type": "ref",
    "content": {{
      "ref_node_id": "前置节点ID",
      "ref_var_name": "前置节点输出变量名"
    }}
  }}
}}
```

### 2. 节点ID规则
- 格式：`节点类型-数字`，例如：`start-001`, `llm-001`, `end-001`
- 必须唯一
- 使用连续编号

### 3. 边ID规则
- 格式：`edge-数字`，例如：`edge-001`, `edge-002`
- 必须唯一
- 使用连续编号

### 4. 必须包含的节点
- 必须有且只有一个start节点
- 必须有且只有一个end节点

### 5. 模型配置
- 默认使用通义千问：provider="tongyi", model="qwen-plus"
- temperature范围：0-1
- max_tokens范围：1-4000

## 示例配置
{example_config}

## 输出要求
1. 只输出JSON格式的配置，不要包含任何解释文字
2. 确保JSON格式正确，可以被解析
3. 所有字符串使用双引号
4. 确保节点之间的连接关系正确
5. 确保变量引用的ref_node_id和ref_var_name正确

请生成符合要求的工作流配置（只输出JSON，不要其他内容）：
"""
