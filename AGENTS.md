# AI 代码注释规范

- 注释、模块说明、类和方法 docstring 默认使用简短中文。
- 普通英文注释统一翻译；`DeepAgents`、`LangChain`、`LangGraph`、`Agent`、`Backend`、`Workspace`、`Skill`、`Store`、`Checkpoint`、`Sandbox`、`SSE` 等技术名词保留。
- 类 docstring 说明职责；方法 docstring 说明职责、关键输入输出或约束，控制在一至三行。
- 复杂流程使用 `# 1.获取...`、`# 2.校验...` 的连续步骤注释；简单代码不添加重复注释。
- 注释只解释非显然的设计原因、权限边界、资源生命周期和异常处理，不复述代码或记录过程。
- 修改代码时同步清理附近的英文、中英混杂和不规范注释；保持业务逻辑不变。
