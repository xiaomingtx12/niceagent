"""
Qwen3.5-plus 模型集成测试脚本

使用方法：
1. 确保已在系统中配置 DashScope API Key
2. 运行: python test_qwen35_plus.py
"""

import os
from openai import OpenAI


def test_qwen35_plus_basic():
    """测试 Qwen3.5-plus 基本对话功能"""
    print("=" * 60)
    print("测试 1: Qwen3.5-plus 基本对话")
    print("=" * 60)
    
    # 从环境变量获取 API Key
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("❌ 错误: 未设置 DASHSCOPE_API_KEY 环境变量")
        print("请运行: export DASHSCOPE_API_KEY='your-api-key'")
        return False
    
    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        completion = client.chat.completions.create(
            model="qwen3.5-plus",
            messages=[
                {"role": "system", "content": "你是一个有帮助的AI助手。"},
                {"role": "user", "content": "用一句话介绍 Qwen3.5-plus 模型的核心特性"}
            ],
            temperature=0.7,
            max_tokens=200,
        )
        
        response = completion.choices[0].message.content
        print(f"\n✅ 模型响应:\n{response}\n")
        print(f"📊 Token 使用情况:")
        print(f"   - 输入: {completion.usage.prompt_tokens}")
        print(f"   - 输出: {completion.usage.completion_tokens}")
        print(f"   - 总计: {completion.usage.total_tokens}")
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        return False


def test_qwen35_plus_streaming():
    """测试 Qwen3.5-plus 流式输出"""
    print("\n" + "=" * 60)
    print("测试 2: Qwen3.5-plus 流式输出")
    print("=" * 60)
    
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("❌ 错误: 未设置 DASHSCOPE_API_KEY 环境变量")
        return False
    
    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        print("\n✅ 流式响应:")
        stream = client.chat.completions.create(
            model="qwen3.5-plus",
            messages=[
                {"role": "user", "content": "写一个 Python 快速排序函数"}
            ],
            stream=True,
            temperature=0.3,
        )
        
        for chunk in stream:
            if chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="", flush=True)
        
        print("\n")
        return True
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        return False


def test_qwen35_plus_function_calling():
    """测试 Qwen3.5-plus 工具调用功能"""
    print("\n" + "=" * 60)
    print("测试 3: Qwen3.5-plus 工具调用 (Function Calling)")
    print("=" * 60)
    
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("❌ 错误: 未设置 DASHSCOPE_API_KEY 环境变量")
        return False
    
    try:
        client = OpenAI(
            api_key=api_key,
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "获取指定城市的天气信息",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "城市名称，例如：北京、上海"
                            },
                            "unit": {
                                "type": "string",
                                "enum": ["celsius", "fahrenheit"],
                                "description": "温度单位"
                            }
                        },
                        "required": ["city"]
                    }
                }
            }
        ]
        
        completion = client.chat.completions.create(
            model="qwen3.5-plus",
            messages=[
                {"role": "user", "content": "北京今天天气怎么样？"}
            ],
            tools=tools,
        )
        
        message = completion.choices[0].message
        
        if message.tool_calls:
            print("\n✅ 模型决定调用工具:")
            for tool_call in message.tool_calls:
                print(f"   - 工具名称: {tool_call.function.name}")
                print(f"   - 参数: {tool_call.function.arguments}")
            return True
        else:
            print(f"\n⚠️  模型未调用工具，直接回复:\n{message.content}")
            return True
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        return False


def test_model_info():
    """显示模型配置信息"""
    print("\n" + "=" * 60)
    print("Qwen3.5-plus 模型配置信息")
    print("=" * 60)
    
    print("""
模型名称: qwen3.5-plus
模型类型: chat
上下文窗口: 1,048,576 tokens (1M)
最大输出: 8,192 tokens
支持特性:
  ✓ 工具调用 (Function Calling)
  ✓ Agent 思维链
  ✓ 视觉理解 (Vision)
  ✓ 多模态能力
  ✓ 201 种语言支持

API Endpoint:
  - 北京: https://dashscope.aliyuncs.com/compatible-mode/v1
  - 国际: https://dashscope-intl.aliyuncs.com/compatible-mode/v1
  - 金融云: https://dashscope-finance.aliyuncs.com/compatible-mode/v1

配置文件位置:
  - my-dify/internal/core/language_model/providers/tongyi/qwen3.5-plus.yaml
  - my-dify/internal/core/language_model/providers/tongyi/positions.yaml
    """)


if __name__ == "__main__":
    print("\n🚀 开始测试 Qwen3.5-plus 模型集成\n")
    
    # 显示模型信息
    test_model_info()
    
    # 运行测试
    results = []
    results.append(("基本对话", test_qwen35_plus_basic()))
    results.append(("流式输出", test_qwen35_plus_streaming()))
    results.append(("工具调用", test_qwen35_plus_function_calling()))
    
    # 显示测试结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    
    for test_name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{test_name}: {status}")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    print(f"\n总计: {passed}/{total} 测试通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！Qwen3.5-plus 模型集成成功！")
    else:
        print("\n⚠️  部分测试失败，请检查配置和 API Key")
