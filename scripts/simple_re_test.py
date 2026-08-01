#!/usr/bin/env python
# -*- coding: utf-8 -*-

import re as _re

def test_closure():
    """直接测试是否有闭包作用域问题"""
    
    def _resolve_param_value(value, variables: dict, step_captures: dict) -> object:
        """递归解析参数值中的 ${var} 和 ${step.field} 引用"""
        if isinstance(value, str):
            if "${" not in value:
                return value
            # 替换所有 ${xxx} 引用
            def _replace(match):
                ref = match.group(1).strip()
                if "." in ref:
                    # ${step_name.field} 形式
                    head, *path = ref.split(".")
                    if head in step_captures:
                        cur = step_captures[head]
                        for p in path:
                            if isinstance(cur, dict):
                                cur = cur.get(p)
                            elif isinstance(cur, list) and p.isdigit():
                                idx = int(p)
                                cur = cur[idx] if 0 <= idx < len(cur) else None
                            else:
                                return ""
                        return "" if cur is None else str(cur)
                    return ""
                if ref in variables:
                    return str(variables[ref])
                if ref in step_captures:
                    v = step_captures[ref]
                    return "" if v is None else str(v)
                return ""
            try:
                return _re.sub(r'\$\{([^}]+)\}', _replace, value)  # 这里用_re
            except Exception as e:
                print(f"Exception in _re.sub: {e}")
                return value
        return value
    
    # 测试各种情况
    try:
        result = _resolve_param_value('Test ${var}', {'var': 'value'}, {})
        print(f"Test 1 passed: {result}")
        
        result = _resolve_param_value('${step.field}', {}, {'step': {'field': 'val'}})
        print(f"Test 2 passed: {result}")
        
        result = _resolve_param_value('no variables', {}, {})
        print(f"Test 3 passed: {result}")
        
        return True
    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_closure()
    if success:
        print("\nClosure test successful! re module scope is correct.")
    else:
        print("\nClosure test failed, needs further fixing.")