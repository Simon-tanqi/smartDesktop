#!/usr/bin/env python
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

def test_routing_directly():
    """Direct test of the routing function to isolate the issue"""
    try:
        # Clean import without Flask or complex dependencies
        from backend.routing import _resolve_param_value
        
        # Test the function multiple times to see if the error occurs
        test_cases = [
            ('Simple ${var}', {'var': 'value'}, {}),
            ('Test ${step.field}', {}, {'step': {'field': 'val'}}),
            ('No variables text', {}, {}),
            ('Complex ${var1} and ${var2}', {'var1': 'A', 'var2': 'B'}, {}),
            ('Nested ${step.nested.value}', {}, {'step': {'nested': {'value': 'deep'}}}),
        ]
        
        for i, (text, variables, step_captures) in enumerate(test_cases):
            try:
                result = _resolve_param_value(text, variables, step_captures)
                print(f"Test {i+1} SUCCESS: '{text}' -> '{result}'")
            except Exception as e:
                print(f"Test {i+1} FAILED: '{text}' -> ERROR: {e}")
                import traceback
                traceback.print_exc()
                return False
        
        return True
    except Exception as e:
        print(f"Import or setup failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("Testing _resolve_param_value directly...")
    success = test_routing_directly()
    if success:
        print("\nAll tests passed! ✅")
    else:
        print("\nSome tests failed! ❌")