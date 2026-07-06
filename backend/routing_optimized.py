"""
Optimized Hierarchical Routing Mechanism

This module implements an enhanced routing system that addresses the limitations
of the current three-layer approach and provides better domain classification,
intelligent tool selection, and improved caching.
"""

import json
import hashlib
import difflib
import tempfile
import os
import time
import re
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from pathlib import Path

from tools import TOOL_REGISTRY, _load_tools_config, _load_agent_tools_config
from llm_client import ai_concat
from config.settings import DATA_DIR


@dataclass
class RoutingContext:
    """Context information for routing decisions"""
    user_message: str
    session_id: str = ""
    user_id: str = ""
    message_history: List[Dict] = field(default_factory=list)
    complexity: str = "unknown"
    matched_domains: List[str] = field(default_factory=list)
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    

@dataclass 
class RoutingResult:
    """Result of routing decision"""
    route_type: str  # "chat", "cached", "simple", "complex", "domain_specific"
    response: Optional[str] = None
    domains: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    planning_needed: bool = False
    confidence: float = 0.0
    metadata: Dict = field(default_factory=dict)


class IntelligentRouter:
    """Enhanced routing system with ML-inspired confidence scoring"""
    
    def __init__(self):
        self._chat_cache = self._load_chat_cache()
        self._domain_cache = {}
        self._complexity_cache = {}
        self._last_cache_cleanup = time.time()
        
        # Enhanced keyword mappings with weights
        self.domain_keywords = self._build_enhanced_keywords()
        
        # Conversation patterns for better context understanding
        self.conversation_patterns = {
            'follow_up': r'^(那|那|接下来|然后|还有|另外|再)\b',
            'clarification': r'(什么|怎么|如何|为什么|吗|呢|？|\?)',
            'confirmation': r'(对吗|是不是|确认|check)',
            'elaboration': r'(详细|具体|展开|说说|解释)',
        }
        
    def _build_enhanced_keywords(self) -> Dict[str, Dict[str, float]]:
        """Build enhanced keyword mapping with confidence weights"""
        config = _load_tools_config()
        agent_config = _load_agent_tools_config()
        
        all_domains = {}
        all_domains.update(config.get('domains', {}))
        all_domains.update(agent_config.get('domains', {}))
        
        enhanced = {}
        
        for domain, info in all_domains.items():
            keywords = info.get('keywords', [])
            enhanced[domain] = {}
            
            # Assign weights based on keyword specificity
            for kw in keywords:
                if len(kw) >= 4:  # Longer keywords are more specific
                    weight = 0.9
                elif len(kw) >= 2:
                    weight = 0.7
                else:
                    weight = 0.5
                    
                # Boost weights for action-oriented keywords
                action_boost = any(word in kw for word in ['执行', '运行', '创建', '删除', '修改', '安装', '卸载'])
                if action_boost:
                    weight = min(weight + 0.2, 1.0)
                    
                enhanced[domain][kw] = weight
                
        return enhanced
        
    def _load_chat_cache(self) -> Dict:
        """Load cached chat responses"""
        cache_path = Path(DATA_DIR) / "chat_cache.json"
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}
        
    def _save_chat_cache(self):
        """Save chat cache to disk"""
        cache_path = Path(DATA_DIR) / "chat_cache.json"
        cache_path.parent.mkdir(exist_ok=True, parents=True)
        
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', 
                                       dir=DATA_DIR, delete=False, suffix='.tmp') as tf:
            json.dump(self._chat_cache, tf, ensure_ascii=False, indent=2)
            tmpname = tf.name
        os.replace(tmpname, str(cache_path))
        
    def _cleanup_cache(self, max_age_hours: int = 24):
        """Clean up old cache entries"""
        current_time = time.time()
        if current_time - self._last_cache_cleanup < 3600:  # Clean up max once per hour
            return
            
        cutoff_time = current_time - (max_age_hours * 3600)
        
        # Clean chat cache
        expired_keys = []
        for key, data in self._chat_cache.items():
            if data.get('timestamp', 0) < cutoff_time:
                expired_keys.append(key)
        for key in expired_keys:
            del self._chat_cache[key]
            
        # Clean complexity cache
        expired_keys = []
        for key, data in self._complexity_cache.items():
            if data.get('timestamp', 0) < cutoff_time:
                expired_keys.append(key)
        for key in expired_keys:
            del self._complexity_cache[key]
            
        self._last_cache_cleanup = current_time
        
        # Save cleaned cache
        if expired_keys:
            self._save_chat_cache()

    def _calculate_domain_confidence(self, message: str, domain: str) -> float:
        """Calculate confidence score for domain match"""
        message_lower = message.lower()
        keywords = self.domain_keywords.get(domain, {})
        
        if not keywords:
            return 0.0
            
        max_confidence = 0.0
        matched_keywords = []
        
        for keyword, weight in keywords.items():
            if keyword.lower() in message_lower:
                matched_keywords.append((keyword, weight))
                max_confidence = max(max_confidence, weight)
                
        # Bonus for multiple keyword matches
        if len(matched_keywords) > 1:
            bonus = min(0.1 * (len(matched_keywords) - 1), 0.3)
            max_confidence = min(max_confidence + bonus, 1.0)
            
        # Penalty for very short messages unless they're exact matches
        if len(message.strip()) < 5:
            exact_match = any(kw.lower() == message_lower for kw, _ in matched_keywords)
            if not exact_match:
                max_confidence *= 0.7
                
        return max_confidence

    def _detect_conversation_context(self, context: RoutingContext) -> str:
        """Detect conversation context for better routing"""
        if not context.message_history:
            return "initial"
            
        recent_messages = context.message_history[-3:]  # Last 3 messages
        
        # Check for conversation patterns
        for msg in reversed(recent_messages):
            content = msg.get('content', '').lower()
            
            for pattern_name, pattern in self.conversation_patterns.items():
                if re.search(pattern, content):
                    return pattern_name
                    
        return "continuation"

    def _classify_complexity_enhanced(self, context: RoutingContext, 
                                   model_choice: Dict) -> Tuple[str, float]:
        """Enhanced complexity classification with caching"""
        message = context.user_message
        
        # Check cache first
        cache_key = hashlib.md5(f"{message}_{model_choice.get('model', '')}".encode()).hexdigest()
        if cache_key in self._complexity_cache:
            cached = self._complexity_cache[cache_key]
            return cached['complexity'], cached['confidence']

        # Quick rule-based classification
        msg = message.strip()
        simple_confidence = 0.0
        
        # Length-based heuristics
        if len(msg) <= 20:
            COMPLEX_INDICATORS = [
                "分析", "统计", "对比", "总结", "报告", "生成", "批量", "替换", "重构", 
                "安装", "卸载", "配置", "修复", "诊断", "排查", "优化", "迁移", "转换", "提取", "同步"
            ]
            has_complex_indicator = any(indicator in msg for indicator in COMPLEX_INDICATORS)
            if not has_complex_indicator:
                simple_confidence = 0.8
                
        # Question pattern analysis
        if msg.endswith(("?", "？")):
            ACTION_VERBS = ["帮我", "请", "搜索", "下载", "创建", "删除", "修改", "执行", "运行"]
            has_action_verb = any(verb in msg for verb in ACTION_VERBS)
            if not has_action_verb:
                simple_confidence = max(simple_confidence, 0.6)

        # Domain-based complexity hints
        high_complexity_domains = ['system_high_risk', 'excel_analyze', 'code_dev']
        if context.matched_domains:
            has_high_complexity = any(domain in high_complexity_domains for domain in context.matched_domains)
            if has_high_complexity:
                simple_confidence *= 0.3  # Reduce confidence for simple classification

        # Use LLM for ambiguous cases
        if 0.3 < simple_confidence < 0.8:
            try:
                judge_prompt = (
                    f'请判断以下用户请求的复杂度，回复格式："复杂"或"简单"，后跟0-1的置信度：\n'
                    f'用户请求：{message}\n\n'
                    f'判断标准：\n'
                    f'- 复杂：涉及多步骤操作、需要搜索资料、需要修改系统配置、需要跨工具协作\n'
                    f'- 简单：单步操作、已知知识的直接回答、文件读写等直接可执行的任务'
                )
                
                llm_result = ai_concat(
                    messages=[{"role": "user", "content": judge_prompt}],
                    model_name=model_choice.get("model"),
                    provider=model_choice.get("provider"),
                    trace_id=model_choice.get("model", "complexity"),
                )
                
                if llm_result and isinstance(llm_result, dict):
                    content = llm_result.get("message", {}).get("content", "")
                    content = content.strip().lower()
                    
                    if "复杂" in content:
                        # Extract confidence if provided
                        confidence_match = re.search(r'(\d+\.?\d*)', content)
                        confidence = float(confidence_match.group(1)) if confidence_match else 0.7
                        result = ("复杂", confidence)
                    else:
                        confidence_match = re.search(r'(\d+\.?\d*)', content)
                        confidence = float(confidence_match.group(1)) if confidence_match else 0.7
                        result = ("简单", confidence)
                else:
                    result = ("复杂", 0.5)  # Default to complex on error
            except Exception:
                result = ("复杂", 0.5)
        else:
            complexity = "简单" if simple_confidence >= 0.5 else "复杂"
            confidence = abs(simple_confidence - 0.5) * 2  # Convert to 0-1 range
            result = (complexity, confidence)

        # Cache result
        self._complexity_cache[cache_key] = {
            'complexity': result[0],
            'confidence': result[1],
            'timestamp': time.time()
        }
        
        return result

    def route_message(self, context: RoutingContext, model_choice: Dict) -> RoutingResult:
        """Main routing method with enhanced intelligence"""
        
        # Clean cache periodically
        self._cleanup_cache()
        
        message = context.user_message
        
        # 1. Enhanced chat layer with context awareness
        chat_result = self._route_chat_layer(context)
        if chat_result:
            return chat_result
            
        # 2. Domain matching with confidence scoring
        domain_scores = {}
        for domain in self.domain_keywords.keys():
            confidence = self._calculate_domain_confidence(message, domain)
            if confidence > 0.5:  # Threshold for domain consideration
                domain_scores[domain] = confidence
                
        # Sort domains by confidence
        sorted_domains = sorted(domain_scores.items(), key=lambda x: x[1], reverse=True)
        matched_domains = [domain for domain, score in sorted_domains]
        
        context.matched_domains = matched_domains
        context.confidence_scores = domain_scores
        
        # 3. Enhanced complexity classification
        complexity, complexity_confidence = self._classify_complexity_enhanced(context, model_choice)
        context.complexity = complexity
        
        # 4. Determine routing strategy
        if matched_domains:
            if len(matched_domains) == 1:
                primary_domain = matched_domains[0]
                domain_confidence = domain_scores[primary_domain]
                
                if domain_confidence > 0.8:  # High confidence domain match
                    return RoutingResult(
                        route_type="domain_specific",
                        domains=matched_domains,
                        tools=self._get_domain_tools(primary_domain),
                        planning_needed=complexity == "复杂",
                        confidence=domain_confidence,
                        metadata={"complexity": complexity, "complexity_confidence": complexity_confidence}
                    )
                elif complexity == "简单":
                    return RoutingResult(
                        route_type="simple",
                        domains=matched_domains,
                        tools=self._get_domain_tools(primary_domain)[:3],  # Limit tools for simple tasks
                        confidence=domain_confidence,
                        metadata={"complexity": complexity}
                    )
            else:
                # Multiple domain matches - use complexity to decide
                if complexity == "复杂":
                    return RoutingResult(
                        route_type="complex",
                        domains=matched_domains,
                        tools=self._merge_domain_tools(matched_domains),
                        planning_needed=True,
                        confidence=max(domain_scores.values()) if domain_scores else 0.5,
                        metadata={"complexity": complexity, "multi_domain": True}
                    )
                    
        # 5. Fallback strategies
        if complexity == "简单":
            return RoutingResult(
                route_type="simple",
                domains=["general_chat"],
                tools=self._get_basic_tools(),
                confidence=0.6,
                metadata={"complexity": complexity, "fallback": True}
            )
        else:
            return RoutingResult(
                route_type="complex",
                domains=["general_chat"],
                tools=self._get_all_tools(),
                planning_needed=True,
                confidence=0.5,
                metadata={"complexity": complexity, "fallback": True}
            )

    def _route_chat_layer(self, context: RoutingContext) -> Optional[RoutingResult]:
        """Enhanced chat layer with context awareness and caching"""
        message = context.user_message.strip().lower()
        
        # Check cache first
        if message in self._chat_cache:
            cached = self._chat_cache[message]
            return RoutingResult(
                route_type="chat",
                response=cached['response'],
                confidence=cached['confidence'],
                metadata={"cached": True, "timestamp": cached['timestamp']}
            )
            
        # Enhanced chat patterns with dynamic responses
        chat_patterns = {
            # Greetings
            r'^(你好|hello|hi|hey)': {
                'response': self._generate_personalized_greeting(context),
                'confidence': 0.95
            },
            # Thanks
            r'(谢谢|感谢|thx|thanks)': {
                'response': "不客气，随时为您服务。",
                'confidence': 0.9
            },
            # Time-based greetings
            r'(早上好|早安)': {
                'response': f"早上好！{self._get_time_based_greeting()}有什么需要处理的吗？",
                'confidence': 0.9
            },
            r'(晚安)': {
                'response': "晚安，早点休息。",
                'confidence': 0.9
            },
            # Identity questions
            r'(你是谁|你叫什么)': {
                'response': "我是 MyAgent，您的本地智能助手。我可以帮您处理文件、执行命令、上网搜索等各种任务。",
                'confidence': 0.9
            },
            # Availability
            r'(在吗|在线吗)': {
                'response': "您好，我在。请问有什么可以帮您？",
                'confidence': 0.9
            }
        }
        
        # Match patterns
        for pattern, response_data in chat_patterns.items():
            if re.search(pattern, message):
                response = response_data['response']
                confidence = response_data['confidence']
                
                # Cache the response
                self._chat_cache[message] = {
                    'response': response,
                    'confidence': confidence,
                    'timestamp': time.time()
                }
                
                return RoutingResult(
                    route_type="chat",
                    response=response,
                    confidence=confidence,
                    metadata={"pattern": pattern}
                )
                
        return None

    def _generate_personalized_greeting(self, context: RoutingContext) -> str:
        """Generate personalized greeting based on context"""
        hour = time.localtime().tm_hour
        
        if 5 <= hour < 12:
            time_greeting = "早上好"
        elif 12 <= hour < 18:
            time_greeting = "下午好"
        else:
            time_greeting = "晚上好"
            
        # Check for user name in session history
        user_name = None
        for msg in reversed(context.message_history[-10:]):  # Check last 10 messages
            content = msg.get('content', '').lower()
            if '我叫' in content or '我是' in content:
                # Extract potential name
                name_match = re.search(r'(?:我叫|我是)(.+?)(?：|，|。|！|\s|$)', content)
                if name_match:
                    user_name = name_match.group(1).strip()
                    break
                    
        if user_name:
            return f"{time_greeting}，{user_name}！我在。请问有什么可以帮您？"
        else:
            return f"{time_greeting}！我在。请问有什么可以帮您？"

    def _get_time_based_greeting(self) -> str:
        """Get time-based additional greeting"""
        hour = time.localtime().tm_hour
        
        if 5 <= hour < 9:
            return "新的一天开始了，"
        elif 9 <= hour < 12:
            return "今天上午"
        else:
            return ""

    def _get_domain_tools(self, domain: str) -> List[str]:
        """Get tools for a specific domain"""
        config = _load_tools_config()
        agent_config = _load_agent_tools_config()
        
        all_domains = {}
        all_domains.update(config.get('domains', {}))
        all_domains.update(agent_config.get('domains', {}))
        
        domain_info = all_domains.get(domain, {})
        tools = domain_info.get('tools', [])
        
        # Add always_inject tools
        always_inject = set(config.get('always_inject', []))
        tools = list(always_inject) + [t for t in tools if t not in always_inject]
        
        return tools

    def _merge_domain_tools(self, domains: List[str]) -> List[str]:
        """Merge tools from multiple domains"""
        all_tools = set()
        
        for domain in domains:
            domain_tools = self._get_domain_tools(domain)
            all_tools.update(domain_tools)
            
        return list(all_tools)

    def _get_basic_tools(self) -> List[str]:
        """Get basic set of tools for simple tasks"""
        config = _load_tools_config()
        basic_tools = [
            'read_file', 'scan_directory', 'search_files', 'get_workspace_path',
            'ask_user_clarification'
        ]
        
        # Ensure always_inject tools are included
        always_inject = set(config.get('always_inject', []))
        for tool in always_inject:
            if tool not in basic_tools:
                basic_tools.append(tool)
                
        return basic_tools[:5]  # Limit to prevent tool overload

    def _get_all_tools(self) -> List[str]:
        """Get all available tools"""
        config = _load_tools_config()
        always_inject = set(config.get('always_inject', []))
        
        all_tools = list(always_inject)
        
        # Add tools from all domains
        domains = config.get('domains', {})
        for domain_info in domains.values():
            domain_tools = domain_info.get('tools', [])
            for tool in domain_tools:
                if tool not in all_tools:
                    all_tools.append(tool)
                    
        return all_tools


# Global router instance
_intelligent_router = None


def get_intelligent_router() -> IntelligentRouter:
    """Get or create the global intelligent router instance"""
    global _intelligent_router
    if _intelligent_router is None:
        _intelligent_router = IntelligentRouter()
    return _intelligent_router


def route_message_optimized(user_message: str, model_choice: Dict, 
                          session_id: str = "", user_id: str = "", 
                          message_history: List[Dict] = None) -> RoutingResult:
    """Optimized message routing with enhanced intelligence"""
    
    router = get_intelligent_router()
    
    context = RoutingContext(
        user_message=user_message,
        session_id=session_id,
        user_id=user_id,
        message_history=message_history or [],
    )
    
    return router.route_message(context, model_choice)


# Backward compatibility functions

def check_chat_layer_optimized(user_message: str, context: Dict = None) -> Optional[str]:
    """Optimized chat layer check with context awareness"""
    router = get_intelligent_router()
    
    routing_context = RoutingContext(
        user_message=user_message,
        session_id=context.get('session_id', '') if context else '',
        user_id=context.get('user_id', '') if context else '',
        message_history=context.get('message_history', []) if context else [],
    )
    
    result = router._route_chat_layer(routing_context)
    return result.response if result else None


def analyze_routing_path(user_message: str, model_choice: Dict, 
                        context: Dict = None) -> Dict:
    """Analyze the routing path for debugging and optimization"""
    router = get_intelligent_router()
    
    routing_context = RoutingContext(
        user_message=user_message,
        session_id=context.get('session_id', '') if context else '',
        user_id=context.get('user_id', '') if context else '',
        message_history=context.get('message_history', []) if context else [],
    )
    
    # Get domain confidence scores
    domain_scores = {}
    for domain in router.domain_keywords.keys():
        confidence = router._calculate_domain_confidence(user_message, domain)
        if confidence > 0.3:  # Include all non-trivial matches
            domain_scores[domain] = confidence
    
    # Get complexity analysis
    complexity, complexity_confidence = router._classify_complexity_enhanced(
        routing_context, model_choice
    )
    
    # Get conversation context
    conversation_context = router._detect_conversation_context(routing_context)
    
    return {
        'domain_scores': domain_scores,
        'complexity': {
            'classification': complexity,
            'confidence': complexity_confidence
        },
        'conversation_context': conversation_context,
        'message_length': len(user_message),
        'has_special_characters': bool(re.search(r'[^\w\s\u4e00-\u9fff]', user_message)),
        'language_mix': bool(re.search(r'[a-zA-Z]', user_message)) and bool(re.search(r'[\u4e00-\u9fff]', user_message))
    }