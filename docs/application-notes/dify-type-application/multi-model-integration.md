---
sidebar_position: 7
---

# 平台的多模型集成实现

多模型集成不是把几个厂商 SDK 都接上就结束了。

放到这个项目里看，它更像一层独立的平台能力：前面接住 Provider、模型配置、能力标记和用户级 API Key，后面统一给 Agent、Workflow 和其他服务层一个稳定的模型运行时对象。

## 先说最关键的判断

这个项目里的多模型能力，不是靠一堆 `if/else` 切厂商，而是靠“Provider -> ModelEntity -> model_class”的配置驱动体系。

更准确地说，它做的是：

- `providers.yaml` 注册可用提供商
- `positions.yaml + <model>.yaml` 定义每个 provider 下的模型目录
- `LanguageModelManager` 在启动时把这些配置装成运行时注册表
- `LanguageModelService` 再根据 provider、model、用户 API Key 和参数，动态实例化真正的模型对象

所以这套能力本质上不是“模型列表管理”，而是“配置驱动 + 动态加载 + 统一抽象”的多模型平台。

## 为什么这一层要单独拆

多模型一旦做成平台能力，就不能只关心“能不能换模型”。

它至少还要一起解决：

- 提供商和模型怎么注册
- 模型能力怎么描述
- 通用参数和 provider 差异怎么收口
- 用户级 API Key 怎么隔离
- 模型对象怎么给 Agent 和 Workflow 复用
- 出错时怎么降级和保护主链路

如果这些问题没有一起设计，所谓“多模型集成”最后通常只是一个下拉框。

## 我会把它拆成六个部分

### 1. 统一抽象不是 SDK，而是 `ProviderEntity + ModelEntity + BaseLanguageModel`

这个项目没有直接把上层逻辑绑死在某一家模型 SDK 上。

它先定义了几层稳定抽象：

- `ProviderEntity`：提供商元数据
- `ModelEntity`：模型能力、参数、上下文、价格等信息
- `BaseLanguageModel`：真正被上层拿来调用的统一模型基类

其中 `ModelEntity` 承接的不是简单的模型名，而是：

- `model_type`
- `features`
- `context_window`
- `max_output_tokens`
- `attributes`
- `parameters`
- `metadata`

这让平台保存的不是“一个调用字符串”，而是一份真正可用于运行时装配和治理的模型描述。

### 2. Provider-Model 两级结构，让配置层和执行层都更稳定

这个项目没有把所有模型平铺成一个大列表，而是明确做了 Provider-Model 两级结构。

第一层是 provider：

- `openai`
- `moonshot`
- `deepseek`
- `tongyi`
- `ollama`

第二层才是每个 provider 下的模型目录。

这件事很关键，因为 provider 和 model 的职责本来就不同：

- provider 承担图标、描述、支持模型类型、接入方式
- model 承担能力标记、上下文长度、价格和参数规则

如果这两层不分开，后面新增 provider、展示模型目录、做参数模板和能力判断时都会变得很乱。

所以这里不是“多一层嵌套”，而是在给平台后面的注册、展示和运行时装配留边界。

### 3. YAML 配置目录，不只是配置文件，而是模型注册中心

这套多模型平台的注册中心，不在数据库，而在代码仓库里的 YAML 目录。

结构大致分三层：

- `providers.yaml`：列出所有 provider
- `positions.yaml`：列出某个 provider 下的模型顺序
- `<model>.yaml`：描述单个模型的详细配置

再加上参数模板，把通用参数抽出来复用。

这套结构的价值很直接：

- provider 和 model 的增删改可以版本化
- 通用参数不需要在每个模型文件里重复写
- 前端模型列表和后端运行时能共享同一份事实来源

所以这些 YAML 在这里不是部署时顺手写的配置，而是模型平台真正的元数据仓库。

### 4. 动态加载和参数模板，决定了这是不是插件式设计

这个项目的多模型能力不是“把所有 provider 类手工注册进一个大字典”。

它更接近插件式注册：

- `LanguageModelManager` 启动时读取 provider 列表
- `Provider` 初始化时动态导入对应模型类
- 再按模型 YAML 创建 `ModelEntity`

同时，它没有要求每个模型把所有参数都重复写一遍，而是支持参数模板补全。

这一步的价值在于：

- 新增模型时，更多是在扩展配置目录，而不是重写平台逻辑
- 同类 provider 的参数约束可以复用
- 平台能把模型“目录注册”和“运行时实例化”明确拆开

这也是为什么这套能力更像模型插件平台，而不是简单的 provider 封装。

### 5. 用户级 API Key 隔离和能力标记，才是真正接入上层的关键

多模型平台最容易被讲浅的地方，是只谈 provider 数量，不谈运行时约束。

这个项目真正值钱的，是它把下面两件事一起做了。

#### 用户级 API Key 隔离

模型不是用平台默认 key 模糊兜底，而是通过服务层按当前账户去读取和注入 API Key。

这样做的意义是：

- 不同用户的模型调用边界清楚
- 计费和权限更容易对齐
- 平台不会悄悄替用户承担不可见的 provider 调用成本

#### 模型能力标记

平台会显式标记模型是否支持：

- `TOOL_CALL`
- `AGENT_THOUGHT`
- `IMAGE_INPUT`

这些标记不是为了展示好看，而是直接参与上层执行路径：

- Agent 根据 `TOOL_CALL` 决定走 Function Calling 还是 ReAct
- 多模态输入根据 `IMAGE_INPUT` 决定是否拼图片消息
- 其他服务也可以按能力决定功能是否开放

所以模型平台对上层暴露的，不只是“模型名”，而是“能力约束下的模型对象”。

### 6. 统一运行时装配，才让 Agent 和 Workflow 真正无感切换

这套平台最后最关键的一步，是把配置对象装配成真正可执行的模型实例。

这里真正承担运行时装配职责的是 `LanguageModelService` 一类服务层。

它要做的事情包括：

- 根据 provider 和 model 找到对应 `Provider`
- 取出 `ModelEntity`
- 找到正确的 `model_class`
- 注入用户 API Key
- 合并 `attributes`、运行时参数、`features`、`metadata`
- 最终实例化成统一的 `BaseLanguageModel`

于是上层看到的就不再是 YAML 或 provider 名，而是一个真的能被调用、带能力标记、带价格信息、带多模态适配能力的模型对象。

这也是为什么：

- Agent 层可以无感切换模型
- Workflow 的 LLMNode / IntentClassifierNode 也能复用同一套模型平台

统一的不是展示层，而是运行时实例装配。

## 这套多模型平台里最值得学的工程判断

### 1. 不把模型平铺成列表，而是保留 Provider-Model 两级结构

这样 provider 级和 model 级职责才不会混在一起。

### 2. 配置目录就是注册中心

这让模型平台更像插件系统，而不是散落在代码里的硬编码。

### 3. 参数模板要复用

否则每个模型 YAML 都会变成重复劳动。

### 4. 能力标记必须进入运行时判断

只有这样，多模型平台才真的影响执行链路，而不只是 UI 展示。

### 5. API Key 要走用户级隔离

这比平台默认 key 兜底更像正式系统。

### 6. 运行时对象必须统一

否则 Agent、Workflow 和其他入口都得分别适配不同模型 SDK。

## 这一层最容易被讲浅的地方

很多人讲多模型时，只会说：

- 接了 OpenAI
- 接了 DeepSeek
- 接了 Tongyi

这只是在罗列 provider。

真正更值钱的是下面这些问题有没有答清楚：

- provider 和 model 为什么要分层
- YAML 目录为什么能承担注册中心职责
- 参数模板如何减少配置重复
- `features` 为什么会直接影响 Agent 执行模式
- 用户级 API Key 为什么要成为服务层约束
- Agent 和 Workflow 为什么能共用同一套模型装配结果

这些问题答不清，通常还只是“能换几个模型”，还没有真正沉淀成平台里的多模型系统。

## 我现在的判断

这个项目里的多模型部分，最重要的不是“接了多少家模型”，而是它做出了几个对的平台选择：

1. 用 `ProviderEntity + ModelEntity + BaseLanguageModel` 建立统一抽象
2. 用 `providers.yaml / positions.yaml / <model>.yaml` 建立注册中心
3. 用动态导入和参数模板把模型接入做成插件式扩展
4. 用 `features` 和用户级 API Key 把模型能力真正接到运行时
5. 用 `LanguageModelService` 把配置装配成统一模型对象，供 Agent 和 Workflow 复用

做到这一步，多模型在这个平台里就不只是“模型可选”，而是一个真正可扩展、可治理、可被执行层稳定消费的模型基础设施。