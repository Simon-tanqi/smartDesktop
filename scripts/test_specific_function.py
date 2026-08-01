#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_routing_function():
    """测试routing模块的_resolve_param_value函数"""
    try:
        from backend.routing import _resolve_param_value
        
        # 测试正常情况
        result = _resolve_param_value('Test ${var} test', {'var': 'value'}, {})
        print(f"✅ 正常测试通过: {result}")
        
        # 测试复杂嵌套情况
        result = _resolve_param_value('复杂 ${step.field} ${var}', {'var': 'A'}, {'step': {'field': 'B'}})
        print(f"✅ 复杂测试通过: {result}")
        
        # 测试边界情况
        result = _resolve_param_value('无变量文本', {}, {})
        print(f"✅ 边界测试通过: {result}")
        
        return True
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_routing_function()
    if success:
        print("\n🎉 Routing模块测试成功！修复生效。")
    else:
        print("\n😞 Routing模块仍有问题，需要进一步调查。")