#!/usr/bin/env python

import sys
import os
sys.path.append(os.path.dirname(__file__))

try:
    from backend.routing import _resolve_param_value
    result = _resolve_param_value('Test ${var}', {'var': 'value'}, {})
    print('Test passed:', result)
except Exception as e:
    print('Error:', e)
    import traceback
    traceback.print_exc()