#!/usr/bin/env python3
"""
自动为 Handler 添加限流装饰器

根据接口类型自动选择合适的限流配置
"""
import os
import re
from pathlib import Path


# 接口分类规则
RATE_LIMIT_RULES = {
    # 对话相关 - 最严格
    "chat": {
        "patterns": [r"def.*chat\(", r"def.*conversation\(", r"def.*message\("],
        "config": "chat",
        "comment": "对话接口 - 消耗 LLM 资源"
    },
    
    # 文件上传 - 严格
    "upload": {
        "patterns": [r"def.*upload.*\(", r"def.*file\("],
        "config": "upload",
        "comment": "文件上传 - 消耗 IO 和内存"
    },
    
    # 工作流执行 - 严格
    "workflow": {
        "patterns": [r"def.*workflow.*debug\(", r"def.*execute.*workflow\("],
        "config": "workflow",
        "comment": "工作流执行 - 消耗多种资源"
    },
    
    # 敏感操作 - 严格
    "sensitive": {
        "patterns": [r"def.*delete\(", r"def.*publish\(", r"def.*cancel.*publish\("],
        "config": "sensitive",
        "comment": "敏感操作 - 删除/发布"
    },
    
    # 知识库检索 - 适中
    "retrieval": {
        "patterns": [r"def.*hit\(", r"def.*search\(", r"def.*retrieval\("],
        "config": "retrieval",
        "comment": "知识库检索 - 消耗向量数据库"
    },
    
    # 数据库密集型 - 适中
    "database_intensive": {
        "patterns": [
            r"def.*get_.*_with_page\(",
            r"def.*get_.*_list\(",
            r"def.*get_.*queries\(",
        ],
        "config": "database_intensive",
        "comment": "数据库密集型 - 列表查询"
    },
    
    # 轻量级操作 - 宽松
    "lightweight": {
        "patterns": [
            r"def.*get_[a-z_]+\(self, [a-z_]+_id:",  # get_xxx(self, xxx_id:)
            r"def.*get_config\(",
            r"def.*get_detail\(",
        ],
        "config": "lightweight",
        "comment": "轻量级操作 - 获取详情"
    },
}


def analyze_function(func_def: str) -> str:
    """
    分析函数定义，返回建议的限流配置
    
    Args:
        func_def: 函数定义字符串
        
    Returns:
        str: 限流配置键
    """
    # 按优先级检查 (从严格到宽松)
    for category, rule in RATE_LIMIT_RULES.items():
        for pattern in rule["patterns"]:
            if re.search(pattern, func_def):
                return rule["config"]
    
    # 默认使用用户级别限流
    return "user"


def has_rate_limit_decorator(lines: list, func_line_idx: int) -> bool:
    """
    检查函数是否已有限流装饰器
    
    Args:
        lines: 文件所有行
        func_line_idx: 函数定义所在行索引
        
    Returns:
        bool: 是否已有限流装饰器
    """
    # 向上查找装饰器
    for i in range(func_line_idx - 1, max(0, func_line_idx - 10), -1):
        line = lines[i].strip()
        if line.startswith("@rate_limit"):
            return True
        if not line.startswith("@") and line and not line.startswith("#"):
            break
    return False


def add_rate_limit_decorator(file_path: str, dry_run: bool = True):
    """
    为文件中的函数添加限流装饰器
    
    Args:
        file_path: 文件路径
        dry_run: 是否只是预览，不实际修改
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    modified = False
    new_lines = []
    i = 0
    
    # 检查是否已导入 rate_limit
    has_import = any("from internal.middleware.rate_limit_middleware import rate_limit" in line 
                     for line in lines)
    
    while i < len(lines):
        line = lines[i]
        
        # 检查是否是函数定义
        if re.match(r'\s+def\s+\w+\(', line):
            # 检查是否已有 @login_required
            has_login_required = False
            for j in range(i - 1, max(0, i - 10), -1):
                if "@login_required" in lines[j]:
                    has_login_required = True
                    break
                if not lines[j].strip().startswith("@") and lines[j].strip() and not lines[j].strip().startswith("#"):
                    break
            
            # 只为有 @login_required 的函数添加限流
            if has_login_required and not has_rate_limit_decorator(lines, i):
                # 分析函数，确定限流配置
                config = analyze_function(line)
                
                # 获取缩进
                indent = len(line) - len(line.lstrip())
                
                # 添加限流装饰器
                decorator = f"{' ' * indent}@rate_limit(config_key=\"{config}\")\n"
                new_lines.append(decorator)
                modified = True
                
                print(f"  ✓ 添加限流: {line.strip()} -> {config}")
        
        new_lines.append(line)
        i += 1
    
    # 如果修改了文件且需要导入
    if modified and not has_import:
        # 在文件开头添加导入
        for i, line in enumerate(new_lines):
            if line.startswith("from internal."):
                new_lines.insert(i, "from internal.middleware.rate_limit_middleware import rate_limit\n")
                break
    
    # 写回文件
    if modified and not dry_run:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        print(f"  ✅ 已修改文件: {file_path}")
    elif modified:
        print(f"  📝 预览模式: {file_path} (未实际修改)")


def main():
    """主函数"""
    print("""
╔══════════════════════════════════════════════════════════╗
║         自动添加限流装饰器工具                            ║
║         (针对 4核4G 服务器优化)                          ║
╚══════════════════════════════════════════════════════════╝
    """)
    
    # 获取 handler 目录
    handler_dir = Path("internal/handler")
    
    if not handler_dir.exists():
        print("❌ 错误: 找不到 handler 目录")
        return
    
    # 获取所有 handler 文件
    handler_files = list(handler_dir.rglob("*_handler.py"))
    
    print(f"找到 {len(handler_files)} 个 Handler 文件\n")
    
    # 选择模式
    print("选择运行模式:")
    print("1. 预览模式 (只显示将要添加的装饰器，不修改文件)")
    print("2. 应用模式 (实际修改文件)")
    print("3. 退出")
    
    choice = input("\n请选择 (1-3): ").strip()
    
    if choice == "3":
        print("\n再见！")
        return
    
    dry_run = (choice == "1")
    mode_name = "预览模式" if dry_run else "应用模式"
    
    print(f"\n{'='*60}")
    print(f"运行模式: {mode_name}")
    print(f"{'='*60}\n")
    
    # 处理每个文件
    for handler_file in handler_files:
        print(f"\n处理文件: {handler_file.name}")
        print("-" * 60)
        
        try:
            add_rate_limit_decorator(str(handler_file), dry_run)
        except Exception as e:
            print(f"  ❌ 错误: {str(e)}")
    
    print(f"\n{'='*60}")
    print("处理完成！")
    print(f"{'='*60}\n")
    
    if dry_run:
        print("💡 提示: 这是预览模式，文件未被修改")
        print("   如需实际应用，请选择 '应用模式'")
    else:
        print("✅ 所有修改已应用")
        print("⚠️  建议:")
        print("   1. 检查修改的文件")
        print("   2. 运行测试验证功能")
        print("   3. 提交代码前仔细审查")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n操作已取消")
    except Exception as e:
        print(f"\n错误: {str(e)}")
