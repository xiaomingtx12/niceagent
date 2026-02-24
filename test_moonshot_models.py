"""测试月之暗面模型配置是否正确加载"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from internal.core.language_model import LanguageModelManager

def test_moonshot_models():
    """测试月之暗面模型配置"""
    print("=" * 60)
    print("测试月之暗面（Moonshot AI）模型配置")
    print("=" * 60)
    
    # 初始化语言模型管理器
    manager = LanguageModelManager()
    
    # 获取 moonshot 提供商
    provider = manager.get_provider("moonshot")
    
    if not provider:
        print("❌ 错误: 未找到 moonshot 提供商")
        return False
    
    print(f"\n✅ 提供商: {provider.name}")
    print(f"   位置: {provider.position}")
    
    # 获取所有模型
    models = provider.get_model_entities()
    
    print(f"\n📋 模型列表 (共 {len(models)} 个):")
    print("-" * 60)
    
    for model in models:
        print(f"\n模型: {model.model_name}")
        print(f"  标签: {model.label}")
        print(f"  类型: {model.model_type}")
        print(f"  上下文窗口: {model.context_window:,} tokens")
        print(f"  最大输出: {model.max_output_tokens:,} tokens")
        print(f"  特性: {', '.join(model.features)}")
    
    print("\n" + "=" * 60)
    
    # 验证新模型
    expected_models = [
        "moonshot-v1-8k",
        "moonshot-v1-32k", 
        "moonshot-v1-128k",
        "kimi-k2-instruct",
        "kimi-k2-0905",
        "kimi-k2.5"
    ]
    
    model_names = [m.model_name for m in models]
    
    print("\n验证结果:")
    all_found = True
    for expected in expected_models:
        if expected in model_names:
            print(f"  ✅ {expected}")
        else:
            print(f"  ❌ {expected} (未找到)")
            all_found = False
    
    # 特别验证 kimi-k2.5 的 video_input 特性
    print("\n" + "=" * 60)
    print("验证 Kimi K2.5 的视频输入特性:")
    print("-" * 60)
    
    k25_model = provider.get_model_entity("kimi-k2.5")
    if k25_model:
        print(f"✅ 找到 Kimi K2.5 模型")
        print(f"   特性列表: {k25_model.features}")
        if "video_input" in k25_model.features:
            print(f"   ✅ 支持视频输入 (video_input)")
        else:
            print(f"   ❌ 不支持视频输入")
    else:
        print("❌ 未找到 Kimi K2.5 模型")
        all_found = False
    
    print("\n" + "=" * 60)
    
    if all_found:
        print("\n🎉 所有测试通过！")
        return True
    else:
        print("\n❌ 部分测试失败")
        return False

if __name__ == "__main__":
    success = test_moonshot_models()
    sys.exit(0 if success else 1)
