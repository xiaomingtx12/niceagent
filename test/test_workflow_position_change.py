"""
工作流 position 变化检测测试
测试 _is_only_position_changed 方法是否正确判断
"""
import json
import copy


def is_only_position_changed(old_graph: dict, new_graph: dict) -> bool:
    """
    判断两个 graph 之间是否只有 position 发生了变化
    这是从 workflow_service.py 中提取的测试版本
    """
    # 1. 深拷贝，避免修改原数据
    old_graph_copy = copy.deepcopy(old_graph)
    new_graph_copy = copy.deepcopy(new_graph)
    
    # 2. 移除所有节点的 position 字段
    for node in old_graph_copy.get("nodes", []):
        node.pop("position", None)
    
    for node in new_graph_copy.get("nodes", []):
        node.pop("position", None)
    
    # 3. 序列化为 JSON 字符串后对比（sort_keys 确保顺序一致）
    old_json = json.dumps(old_graph_copy, sort_keys=True)
    new_json = json.dumps(new_graph_copy, sort_keys=True)
    
    return old_json == new_json


def test_only_position_changed():
    """测试用例1：仅修改 position"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            },
            {
                "id": "2",
                "title": "结束",
                "node_type": "end",
                "position": {"x": 200, "y": 200},
                "data": {"key": "value"}
            }
        ],
        "edges": [
            {"id": "e1", "source": "1", "target": "2"}
        ]
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 150, "y": 150},  # 只改了 position
                "data": {"key": "value"}
            },
            {
                "id": "2",
                "title": "结束",
                "node_type": "end",
                "position": {"x": 250, "y": 250},  # 只改了 position
                "data": {"key": "value"}
            }
        ],
        "edges": [
            {"id": "e1", "source": "1", "target": "2"}
        ]
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is True, "仅修改 position 应该返回 True"
    print("✅ 测试用例1通过：仅修改 position")


def test_title_changed():
    """测试用例2：修改了 title"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始节点",  # 改了 title
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is False, "修改 title 应该返回 False"
    print("✅ 测试用例2通过：修改了 title")


def test_position_and_title_changed():
    """测试用例3：同时修改 position 和 title"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始节点",  # 改了 title
                "node_type": "start",
                "position": {"x": 150, "y": 150},  # 改了 position
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is False, "同时修改 position 和 title 应该返回 False"
    print("✅ 测试用例3通过：同时修改 position 和 title")


def test_add_new_node():
    """测试用例4：添加新节点"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            },
            {
                "id": "2",
                "title": "结束",
                "node_type": "end",
                "position": {"x": 200, "y": 200},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is False, "添加新节点应该返回 False"
    print("✅ 测试用例4通过：添加新节点")


def test_edge_changed():
    """测试用例5：修改边配置"""
    old_graph = {
        "nodes": [
            {"id": "1", "title": "开始", "position": {"x": 100, "y": 100}},
            {"id": "2", "title": "结束", "position": {"x": 200, "y": 200}},
            {"id": "3", "title": "其他", "position": {"x": 300, "y": 300}}
        ],
        "edges": [
            {"id": "e1", "source": "1", "target": "2"}
        ]
    }

    new_graph = {
        "nodes": [
            {"id": "1", "title": "开始", "position": {"x": 100, "y": 100}},
            {"id": "2", "title": "结束", "position": {"x": 200, "y": 200}},
            {"id": "3", "title": "其他", "position": {"x": 300, "y": 300}}
        ],
        "edges": [
            {"id": "e1", "source": "1", "target": "3"}  # 改了 target
        ]
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is False, "修改边配置应该返回 False"
    print("✅ 测试用例5通过：修改边配置")


def test_no_change():
    """测试用例6：完全没有变化"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "node_type": "start",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value"}
            }
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is True, "完全没有变化应该返回 True"
    print("✅ 测试用例6通过：完全没有变化")


def test_data_changed():
    """测试用例7：修改节点 data"""
    old_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value1"}
            }
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {
                "id": "1",
                "title": "开始",
                "position": {"x": 100, "y": 100},
                "data": {"key": "value2"}  # 改了 data
            }
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is False, "修改节点 data 应该返回 False"
    print("✅ 测试用例7通过：修改节点 data")


def test_empty_graph():
    """测试用例8：空 graph"""
    old_graph = {"nodes": [], "edges": []}
    new_graph = {"nodes": [], "edges": []}

    result = is_only_position_changed(old_graph, new_graph)
    assert result is True, "空 graph 应该返回 True"
    print("✅ 测试用例8通过：空 graph")


def test_node_without_position():
    """测试用例9：节点没有 position 字段"""
    old_graph = {
        "nodes": [
            {"id": "1", "title": "开始", "data": {"key": "value"}}
        ],
        "edges": []
    }

    new_graph = {
        "nodes": [
            {"id": "1", "title": "开始", "data": {"key": "value"}}
        ],
        "edges": []
    }

    result = is_only_position_changed(old_graph, new_graph)
    assert result is True, "节点没有 position 字段应该返回 True"
    print("✅ 测试用例9通过：节点没有 position 字段")


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("开始运行工作流 position 变化检测测试")
    print("=" * 60)
    
    try:
        test_only_position_changed()
        test_title_changed()
        test_position_and_title_changed()
        test_add_new_node()
        test_edge_changed()
        test_no_change()
        test_data_changed()
        test_empty_graph()
        test_node_without_position()
        
        print("=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        return True
    except AssertionError as e:
        print("=" * 60)
        print(f"❌ 测试失败: {e}")
        print("=" * 60)
        return False


if __name__ == "__main__":
    run_all_tests()
