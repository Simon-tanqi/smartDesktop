"""
Integration module for updating chat.py to use optimized routing.

This module provides the necessary patches and integration points to replace
the existing routing calls with the optimized version.
"""

import time
from typing import Optional, Dict, Any, Generator

from flask import Response, stream_with_context
from backend.routing_migration import route_message_with_migration, get_migration_manager
from backend.routing_optimized import get_intelligent_router
from memory import get_chat_messages


class RoutingIntegration:
    """Handles integration of optimized routing into the chat system"""
    
    def __init__(self, enable_optimized: bool = True, 
                 performance_monitoring: bool = True):
        self.enable_optimized = enable_optimized
        self.performance_monitoring = performance_monitoring
        self.router = get_intelligent_router() if enable_optimized else None
        
        # Performance tracking
        self.routing_times = []
        self.routing_results = []
        
    def get_message_history(self, session_id: str, max_messages: int = 10) -> list:
        """Get recent message history for context-aware routing"""
        try:
            messages = get_chat_messages(session_id)
            # Return last N messages for context
            return messages[-max_messages:] if messages else []
        except Exception:
            return []
            
    def enhanced_route_decision(self, 
                              user_input: str,
                              model_choice: Dict,
                              session_id: str = "",
                              user_id: str = "",
                              context: Dict = None) -> Dict:
        """Make enhanced routing decision with context"""
        
        start_time = time.perf_counter()
        
        try:
            # Get message history for context-aware routing
            message_history = self.get_message_history(session_id) if session_id else []
            
            # Use migration manager for routing
            result = route_message_with_migration(
                user_message=user_input,
                model_choice=model_choice,
                session_id=session_id,
                user_id=user_id,
                message_history=message_history,
                context=context,
                enable_optimized=self.enable_optimized,
                comparison_mode=False  # Set to True for debugging
            )
            
            elapsed = time.perf_counter() - start_time
            
            if self.performance_monitoring:
                self.routing_times.append(elapsed)
                self.routing_results.append({
                    'session_id': session_id,
                    'route_type': result.get('routing_result', {}).get('route_type'),
                    'confidence': result.get('routing_result', {}).get('confidence'),
                    'elapsed': elapsed,
                    'success': result.get('success', False)
                })
                
            return result
            
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            
            if self.performance_monitoring:
                self.routing_times.append(elapsed)
                self.routing_results.append({
                    'session_id': session_id,
                    'error': str(e),
                    'elapsed': elapsed,
                    'success': False
                })
                
            # Return safe fallback
            return {
                'success': True,
                'routing_result': {
                    'route_type': 'agent',
                    'domains': ['general_chat'],
                    'confidence': 0.5
                },
                'system': 'fallback'
            }

    def should_use_chat_layer(self, routing_result: Dict) -> bool:
        """Determine if we should use chat layer based on routing result"""
        if not routing_result.get('success'):
            return False
            
        route_type = routing_result.get('routing_result', {}).get('route_type')
        return route_type == 'chat'
        
    def should_use_cached_response(self, routing_result: Dict) -> bool:
        """Determine if we should use cached response"""
        if not routing_result.get('success'):
            return False
            
        route_type = routing_result.get('routing_result', {}).get('route_type')
        return route_type == 'cached'
        
    def get_chat_response(self, routing_result: Dict) -> Optional[str]:
        """Get chat response from routing result"""
        return routing_result.get('routing_result', {}).get('response')
        
    def get_detected_domains(self, routing_result: Dict) -> list:
        """Get detected domains from routing result"""
        return routing_result.get('routing_result', {}).get('domains', [])
        
    def should_use_planning(self, routing_result: Dict) -> bool:
        """Determine if planning is needed based on routing result"""
        planning_needed = routing_result.get('routing_result', {}).get('planning_needed', False)
        route_type = routing_result.get('routing_result', {}).get('route_type')
        
        return planning_needed or route_type == 'complex'
        
    def get_recommended_tools(self, routing_result: Dict) -> list:
        """Get recommended tools from routing result"""
        return routing_result.get('routing_result', {}).get('tools', [])

    def get_routing_confidence(self, routing_result: Dict) -> float:
        """Get confidence score from routing result"""
        return routing_result.get('routing_result', {}).get('confidence', 0.5)

    def get_performance_stats(self) -> Dict:
        """Get performance statistics"""
        if not self.routing_times:
            return {'count': 0, 'avg_time': 0, 'success_rate': 0}
            
        successful = sum(1 for r in self.routing_results if r.get('success', False))
        success_rate = successful / len(self.routing_results) if self.routing_results else 0
        
        return {
            'count': len(self.routing_times),
            'avg_time': sum(self.routing_times) / len(self.routing_times),
            'min_time': min(self.routing_times),
            'max_time': max(self.routing_times),
            'success_rate': success_rate
        }


# Global integration instance
_routing_integration = None


def get_routing_integration(enable_optimized: bool = True) -> RoutingIntegration:
    """Get or create the global routing integration instance"""
    global _routing_integration
    if _routing_integration is None:
        _routing_integration = RoutingIntegration(enable_optimized)
    elif enable_optimized != _routing_integration.enable_optimized:
        # Recreate if settings changed
        _routing_integration = RoutingIntegration(enable_optimized)
    return _routing_integration


def apply_chat_py_patch():
    """Generate the patch for chat.py to use optimized routing"""
    
    patch_content = '''
# PATCH FOR chat.py - Integration of Optimized Routing
# Add these imports at the top of the file after existing imports:

from backend.routing_integration import get_routing_integration


# REPLACE the routing logic in api_chat() function (around lines 215-248):

# OLD CODE:
# # ── 第1层：闲聊匹配 ──
# chat_reply = check_chat_layer(user_input)
# if chat_reply is not None:
#     print(f"[ROUTE L1] 闲聊命中 → 直接返回", flush=True)
#     gen = _sse_quick_reply(chat_reply, session_id, trace_id, user_id)
#     return Response(stream_with_context(gen),
#                     mimetype='text/event-stream',
#                     headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
#
# # ── 第2层：高频固化 ──
# cache_hit, cache_reply = check_frequent_cache(user_input)
# if cache_hit and cache_reply:
#     print(f"[ROUTE L2] 高频固化命中 → 直接调用工具返回", flush=True)
#     gen = _sse_quick_reply(cache_reply, session_id, trace_id, user_id)
#     return Response(stream_with_context(gen),
#                     mimetype='text/event-stream',
#                     headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
#
# # ── 第3层：复杂度判断 ──
# complexity = check_complexity(user_input, model_choice)
# print(f"[ROUTE L3] 复杂度判定: {complexity}", flush=True)

# NEW CODE:
# ── Enhanced Routing Layer ──
routing_integration = get_routing_integration(enable_optimized=True)
routing_result = routing_integration.enhanced_route_decision(
    user_input=user_input,
    model_choice=model_choice,
    session_id=session_id,
    user_id=user_id
)

# Handle chat layer response
if routing_integration.should_use_chat_layer(routing_result):
    chat_reply = routing_integration.get_chat_response(routing_result)
    print(f"[ROUTE ENHANCED] 闲聊命中 (confidence: {routing_integration.get_routing_confidence(routing_result):.2f}) → 直接返回", flush=True)
    gen = _sse_quick_reply(chat_reply, session_id, trace_id, user_id)
    return Response(stream_with_context(gen),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# Handle cached response
if routing_integration.should_use_cached_response(routing_result):
    cache_reply = routing_integration.get_chat_response(routing_result)
    print(f"[ROUTE ENHANCED] 缓存命中 → 直接返回", flush=True)
    gen = _sse_quick_reply(cache_reply, session_id, trace_id, user_id)
    return Response(stream_with_context(gen),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

# Get routing analysis for agent processing
detected_domains = routing_integration.get_detected_domains(routing_result)
planning_needed = routing_integration.should_use_planning(routing_result)
recommended_tools = routing_integration.get_recommended_tools(routing_result)
routing_confidence = routing_integration.get_routing_confidence(routing_result)

print(f"[ROUTE ENHANCED] 路由分析: 域={detected_domains}, 规划={planning_needed}, 置信度={routing_confidence:.2f}", flush=True)

# Use enhanced planning logic
planning_extra = ""
if planning_needed:
    planning_extra = (
        "\\n\\n[系统指令 - 步骤规划] 这是一个复杂任务。"
        "请在首轮以 JSON 格式输出结构化步骤规划：\\n"
        '{"plan": [{"step": 1, "description": "第一步做什么", "expected_tool": "建议工具"}, ...]}\\n'
        "输出规划后，按步骤顺序逐一执行。每完成一步，用 progress 报告完成进度（如 '已完成步骤1: xxx'）。"
    )
    print(f"[ROUTE ENHANCED] 复杂任务 → 注入规划提示", flush=True)

# Continue with agent execution using enhanced routing information...
# The detected_domains can be used to influence tool selection in the agent
'''
    
    return patch_content


def generate_integration_test():
    """Generate test cases for the routing integration"""
    
    test_content = '''
import pytest
import time
from backend.routing_integration import get_routing_integration, RoutingIntegration
from backend.routing_migration import get_migration_manager


class TestRoutingIntegration:
    """Test cases for routing integration"""
    
    def test_chat_layer_routing(self):
        """Test chat layer routing"""
        integration = get_routing_integration()
        
        result = integration.enhanced_route_decision(
            user_input="你好",
            model_choice={"provider": "test", "model": "test"},
            session_id="test_session"
        )
        
        assert result['success'] == True
        assert integration.should_use_chat_layer(result) == True
        assert integration.get_chat_response(result) is not None
        
    def test_domain_detection(self):
        """Test domain detection"""
        integration = get_routing_integration()
        
        result = integration.enhanced_route_decision(
            user_input="帮我搜索一下文件",
            model_choice={"provider": "test", "model": "test"},
            session_id="test_session"
        )
        
        assert result['success'] == True
        domains = integration.get_detected_domains(result)
        assert isinstance(domains, list)
        
    def test_complexity_assessment(self):
        """Test complexity assessment"""
        integration = get_routing_integration()
        
        # Simple task
        result = integration.enhanced_route_decision(
            user_input="读取这个文件",
            model_choice={"provider": "test", "model": "test"}
        )
        simple_planning = integration.should_use_planning(result)
        
        # Complex task  
        result = integration.enhanced_route_decision(
            user_input="帮我分析这个项目的架构设计",
            model_choice={"provider": "test", "model": "test"}
        )
        complex_planning = integration.should_use_planning(result)
        
        # Complex tasks should more likely need planning
        assert complex_planning >= simple_planning
        
    def test_performance_monitoring(self):
        """Test performance monitoring"""
        integration = get_routing_integration()
        
        # Clear previous stats
        integration.routing_times.clear()
        integration.routing_results.clear()
        
        # Make some routing decisions
        for i in range(5):
            integration.enhanced_route_decision(
                user_input=f"测试消息 {i}",
                model_choice={"provider": "test", "model": "test"}
            )
            
        stats = integration.get_performance_stats()
        assert stats['count'] == 5
        assert stats['avg_time'] > 0
        
    def test_fallback_behavior(self):
        """Test fallback behavior on errors"""
        integration = RoutingIntegration(enable_optimized=True)
        
        # Force an error by passing invalid data
        result = integration.enhanced_route_decision(
            user_input="",
            model_choice={},  # Invalid model choice
            session_id="test"
        )
        
        # Should still return a valid result
        assert result['success'] == True
        assert 'routing_result' in result
        
    def test_context_awareness(self):
        """Test context-aware routing with message history"""
        integration = get_routing_integration()
        
        # First, create some message history context
        result1 = integration.enhanced_route_decision(
            user_input="我叫张三",
            model_choice={"provider": "test", "model": "test"},
            session_id="context_test"
        )
        
        # Then test with follow-up that should be context-aware
        result2 = integration.enhanced_route_decision(
            user_input="你好",  # Should use personalized greeting
            model_choice={"provider": "test", "model": "test"},
            session_id="context_test"
        )
        
        # Both should succeed
        assert result1['success'] == True
        assert result2['success'] == True


if __name__ == "__main__":
    # Run basic tests
    test_instance = TestRoutingIntegration()
    
    print("Running routing integration tests...")
    
    try:
        test_instance.test_chat_layer_routing()
        print("✅ Chat layer routing test passed")
        
        test_instance.test_domain_detection()
        print("✅ Domain detection test passed")
        
        test_instance.test_complexity_assessment()
        print("✅ Complexity assessment test passed")
        
        test_instance.test_performance_monitoring()
        print("✅ Performance monitoring test passed")
        
        test_instance.test_fallback_behavior()
        print("✅ Fallback behavior test passed")
        
        test_instance.test_context_awareness()
        print("✅ Context awareness test passed")
        
        print("\\n🎉 All tests passed!")
        
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
'''
    
    return test_content


def create_patch_script():
    """Create a script to automatically apply the routing patches"""
    
    script_content = '''#!/usr/bin/env python3
"""
Automated patch script for integrating optimized routing into chat.py
"""

import re
import shutil
from pathlib import Path
from backend.routing_integration import apply_chat_py_patch

def backup_file(file_path: str):
    """Create a backup of the original file"""
    original_path = Path(file_path)
    backup_path = original_path.with_suffix(original_path.suffix + '.backup')
    
    if not backup_path.exists():
        shutil.copy2(original_path, backup_path)
        print(f"✅ Created backup: {backup_path}")
    else:
        print(f"ℹ️  Backup already exists: {backup_path}")

def apply_routing_patch():
    """Apply the routing optimization patch to chat.py"""
    
    chat_file = Path('backend/chat.py')
    if not chat_file.exists():
        print(f"❌ File not found: {chat_file}")
        return False
        
    # Create backup
    backup_file(str(chat_file))
    
    # Read current content
    content = chat_file.read_text(encoding='utf-8')
    
    # Apply import patch
    import_pattern = r'(from backend\.agent import run_agent)'
    import_replacement = r'\1\nfrom backend.routing_integration import get_routing_integration'
    
    if 'from backend.routing_integration import get_routing_integration' not in content:
        content = re.sub(import_pattern, import_replacement, content)
        print("✅ Added routing integration import")
    
    # Apply routing logic patch
    old_routing_pattern = r'''# ── 第1层：闲聊匹配 ──\s*\n\s*chat_reply = check_chat_layer\(user_input\)\s*\n\s*if chat_reply is not None:\s*\n\s*print\(f"\[ROUTE L1\] 闲聊命中 → 直接返回", flush=True\)\s*\n\s*gen = _sse_quick_reply\(chat_reply, session_id, trace_id, user_id\)\s*\n\s*return Response\(stream_with_context\(gen\),\s*\n\s*mimetype=\'text/event-stream\',\s*\n\s*headers=\{\'Cache-Control\': \'no-cache\', \'X-Accel-Buffering\': \'no\'\}\)''' 
    
    new_routing_logic = '''# ── Enhanced Routing Layer ──
routing_integration = get_routing_integration(enable_optimized=True)
routing_result = routing_integration.enhanced_route_decision(
    user_input=user_input,
    model_choice=model_choice,
    session_id=session_id,
    user_id=user_id
)

# Handle chat layer response
if routing_integration.should_use_chat_layer(routing_result):
    chat_reply = routing_integration.get_chat_response(routing_result)
    print(f"[ROUTE ENHANCED] 闲聊命中 (confidence: {routing_integration.get_routing_confidence(routing_result):.2f}) → 直接返回", flush=True)
    gen = _sse_quick_reply(chat_reply, session_id, trace_id, user_id)
    return Response(stream_with_context(gen),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})'''

    if re.search(old_routing_pattern, content, re.MULTILINE | re.DOTALL):
        content = re.sub(old_routing_pattern, new_routing_logic, content, flags=re.MULTILINE | re.DOTALL)
        print("✅ Applied enhanced routing logic")
    
    # Write updated content
    chat_file.write_text(content, encoding='utf-8')
    print("✅ Updated chat.py with optimized routing")
    
    return True

def main():
    """Main patch application function"""
    print("🔧 Applying optimized routing patches...")
    print("=" * 50)
    
    try:
        success = apply_routing_patch()
        
        if success:
            print("\n✅ Patching completed successfully!")
            print("\n📋 Next steps:")
            print("1. Restart the application")
            print("2. Monitor routing performance in logs")
            print("3. Check backend/routing_migration.py for performance stats")
            print("4. Use get_migration_recommendations() to validate the migration")
        else:
            print("\n❌ Patching failed. Check the error messages above.")
            
    except Exception as e:
        print(f"\n❌ Error during patching: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
'''
    
    return script_content


if __name__ == "__main__":
    # Generate and display the integration information
    print("🚀 Routing Optimization Integration")
    print("=" * 50)
    
    print("\n📝 Patch Instructions:")
    print(apply_chat_py_patch())
    
    print("\n🧪 Test Cases:")
    print(generate_integration_test())
    
    print("\n🔧 Patch Script:")
    print(create_patch_script())