"""
Migration script to integrate optimized routing into the existing chat system.
This provides a smooth transition path from the old routing system to the new one.
"""

import json
import time
from typing import Dict, List, Optional, Any
from dataclasses import asdict

from backend.routing import check_chat_layer, check_frequent_cache, check_complexity, record_success
from backend.routing_optimized import route_message_optimized, analyze_routing_path, RoutingResult
from tools import match_domains


class HybridRoutingManager:
    """Manages the transition between old and new routing systems"""
    
    def __init__(self, enable_optimized: bool = True, comparison_mode: bool = False):
        self.enable_optimized = enable_optimized
        self.comparison_mode = comparison_mode
        self.migration_log = []
        
        # Performance tracking
        self.old_routing_times = []
        self.new_routing_times = []
        
    def _log_migration(self, message: str, data: Dict = None):
        """Log migration events"""
        entry = {
            'timestamp': time.time(),
            'message': message,
            'data': data or {}
        }
        self.migration_log.append(entry)
        
    def _time_function(self, func, *args, **kwargs) -> tuple:
        """Time a function call and return result with timing"""
        start_time = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            success = True
        except Exception as e:
            result = None
            success = False
            error = str(e)
        finally:
            elapsed = time.perf_counter() - start_time
            
        timing_data = {
            'elapsed': elapsed,
            'success': success,
            'error': error if not success else None
        }
        
        return result, timing_data

    def route_message_hybrid(self, 
                           user_message: str,
                           model_choice: Dict,
                           session_id: str = "",
                           user_id: str = "",
                           message_history: List[Dict] = None,
                           context: Dict = None) -> Dict:
        """
        Hybrid routing that can use either old or new system, or compare both
        """
        
        if self.comparison_mode:
            return self._compare_routing_systems(
                user_message, model_choice, session_id, user_id, message_history, context
            )
        elif self.enable_optimized:
            return self._use_optimized_routing(
                user_message, model_choice, session_id, user_id, message_history, context
            )
        else:
            return self._use_legacy_routing(
                user_message, model_choice, session_id, user_id, context
            )

    def _use_optimized_routing(self, user_message: str, model_choice: Dict,
                             session_id: str, user_id: str, 
                             message_history: List[Dict], context: Dict) -> Dict:
        """Use the new optimized routing system"""
        
        start_time = time.perf_counter()
        
        try:
            # Get optimized routing result
            routing_result = route_message_optimized(
                user_message=user_message,
                model_choice=model_choice,
                session_id=session_id,
                user_id=user_id,
                message_history=message_history
            )
            
            # Analyze routing path for debugging
            analysis = analyze_routing_path(
                user_message=user_message,
                model_choice=model_choice,
                context=context
            )
            
            elapsed = time.perf_counter() - start_time
            self.new_routing_times.append(elapsed)
            
            self._log_migration("Optimized routing completed", {
                'route_type': routing_result.route_type,
                'confidence': routing_result.confidence,
                'domains': routing_result.domains,
                'elapsed': elapsed
            })
            
            return {
                'success': True,
                'routing_result': asdict(routing_result),
                'analysis': analysis,
                'system': 'optimized',
                'performance': {'routing_time': elapsed}
            }
            
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            self._log_migration("Optimized routing failed", {
                'error': str(e),
                'elapsed': elapsed
            })
            
            # Fall back to legacy routing
            return self._use_legacy_routing(
                user_message, model_choice, session_id, user_id, context
            )

    def _use_legacy_routing(self, user_message: str, model_choice: Dict,
                           session_id: str, user_id: str, context: Dict) -> Dict:
        """Use the original routing system"""
        
        start_time = time.perf_counter()
        
        try:
            # Legacy routing logic
            chat_reply = check_chat_layer(user_message)
            if chat_reply:
                result = {
                    'route_type': 'chat',
                    'response': chat_reply,
                    'confidence': 0.9
                }
            else:
                cache_hit, cache_reply = check_frequent_cache(user_message)
                if cache_hit and cache_reply:
                    result = {
                        'route_type': 'cached',
                        'response': cache_reply,
                        'confidence': 0.8
                    }
                else:
                    complexity = check_complexity(user_message, model_choice)
                    domains = match_domains(user_message)
                    
                    result = {
                        'route_type': 'agent',
                        'complexity': complexity,
                        'domains': domains,
                        'confidence': 0.7
                    }
            
            elapsed = time.perf_counter() - start_time
            self.old_routing_times.append(elapsed)
            
            self._log_migration("Legacy routing completed", {
                'route_type': result.get('route_type', 'unknown'),
                'elapsed': elapsed
            })
            
            return {
                'success': True,
                'routing_result': result,
                'system': 'legacy',
                'performance': {'routing_time': elapsed}
            }
            
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            self._log_migration("Legacy routing failed", {
                'error': str(e),
                'elapsed': elapsed
            })
            
            return {
                'success': False,
                'error': str(e),
                'system': 'legacy',
                'performance': {'routing_time': elapsed}
            }

    def _compare_routing_systems(self, user_message: str, model_choice: Dict,
                               session_id: str, user_id: str,
                               message_history: List[Dict], context: Dict) -> Dict:
        """Run both routing systems and compare results"""
        
        # Run legacy routing
        legacy_result, legacy_timing = self._time_function(
            self._use_legacy_routing,
            user_message, model_choice, session_id, user_id, context
        )
        
        # Run optimized routing
        optimized_result, optimized_timing = self._time_function(
            self._use_optimized_routing,
            user_message, model_choice, session_id, user_id, message_history, context
        )
        
        # Store timing data
        if legacy_timing['success']:
            self.old_routing_times.append(legacy_timing['elapsed'])
        if optimized_timing['success']:
            self.new_routing_times.append(optimized_timing['elapsed'])
            
        # Analyze differences
        comparison = self._analyze_routing_differences(legacy_result, optimized_result)
        
        self._log_migration("Routing comparison completed", {
            'legacy_time': legacy_timing['elapsed'],
            'optimized_time': optimized_timing['elapsed'],
            'legacy_success': legacy_timing['success'],
            'optimized_success': optimized_timing['success'],
            'comparison': comparison
        })
        
        return {
            'success': True,
            'legacy_result': legacy_result,
            'optimized_result': optimized_result,
            'comparison': comparison,
            'performance': {
                'legacy_time': legacy_timing['elapsed'],
                'optimized_time': optimized_timing['elapsed']
            },
            'system': 'comparison'
        }

    def _analyze_routing_differences(self, legacy_result: Dict, optimized_result: Dict) -> Dict:
        """Analyze differences between routing results"""
        
        if not legacy_result.get('success') or not optimized_result.get('success'):
            return {'status': 'comparison_failed', 'reason': 'one_or_both_systems_failed'}
            
        legacy_routing = legacy_result.get('routing_result', {})
        optimized_routing = optimized_result.get('routing_result', {})
        
        differences = {
            'route_type_match': legacy_routing.get('route_type') == optimized_routing.get('route_type'),
            'confidence_diff': abs(legacy_routing.get('confidence', 0) - optimized_routing.get('confidence', 0)),
            'domains_match': set(legacy_routing.get('domains', [])) == set(optimized_routing.get('domains', [])),
            'complexity_match': legacy_routing.get('complexity') == optimized_routing.get('complexity')
        }
        
        # Calculate overall similarity
        matches = sum(1 for match in differences.values() if match is True)
        total_checks = len([check for check in differences.values() if isinstance(check, bool)])
        similarity = matches / total_checks if total_checks > 0 else 0
        
        return {
            'status': 'analyzed',
            'similarity_score': similarity,
            'differences': differences,
            'legacy_domains': legacy_routing.get('domains', []),
            'optimized_domains': optimized_routing.get('domains', []),
            'legacy_complexity': legacy_routing.get('complexity'),
            'optimized_complexity': optimized_routing.get('complexity')
        }

    def get_performance_stats(self) -> Dict:
        """Get performance statistics for both routing systems"""
        
        def calc_stats(times: List[float]) -> Dict:
            if not times:
                return {'count': 0, 'avg': 0, 'min': 0, 'max': 0}
                
            return {
                'count': len(times),
                'avg': sum(times) / len(times),
                'min': min(times),
                'max': max(times)
            }
            
        return {
            'legacy_routing': calc_stats(self.old_routing_times),
            'optimized_routing': calc_stats(self.new_routing_times),
            'migration_log_entries': len(self.migration_log)
        }

    def get_migration_recommendations(self) -> Dict:
        """Get recommendations based on migration data"""
        stats = self.get_performance_stats()
        
        recommendations = {
            'should_enable_optimized': True,
            'confidence': 'high',
            'reasons': [],
            'warnings': []
        }
        
        # Performance comparison
        legacy_avg = stats['legacy_routing']['avg']
        optimized_avg = stats['optimized_routing']['avg']
        
        if legacy_avg > 0 and optimized_avg > 0:
            speedup = legacy_avg / optimized_avg
            if speedup > 1.1:
                recommendations['reasons'].append(f"Optimized routing is {speedup:.1f}x faster")
            elif speedup < 0.9:
                recommendations['warnings'].append("Optimized routing is slower than legacy")
                recommendations['confidence'] = 'medium'
                
        # Success rate comparison
        legacy_count = stats['legacy_routing']['count']
        optimized_count = stats['optimized_routing']['count']
        
        if legacy_count > 0 and optimized_count > 0:
            if optimized_count >= legacy_count:
                recommendations['reasons'].append("Optimized routing handles similar or more requests")
            else:
                recommendations['warnings'].append("Optimized routing has handled fewer requests")
                
        # Check for any critical failures in migration log
        critical_failures = [entry for entry in self.migration_log 
                           if 'failed' in entry.get('message', '').lower()]
        
        if critical_failures:
            recommendations['warnings'].append(f"Found {len(critical_failures)} critical failures")
            if len(critical_failures) > 5:
                recommendations['confidence'] = 'low'
                recommendations['should_enable_optimized'] = False
                
        if not recommendations['reasons']:
            recommendations['reasons'].append("Conservative recommendation based on available data")
            
        return recommendations


# Global migration manager
_migration_manager = None


def get_migration_manager(enable_optimized: bool = True, 
                         comparison_mode: bool = False) -> HybridRoutingManager:
    """Get or create the global migration manager"""
    global _migration_manager
    if _migration_manager is None:
        _migration_manager = HybridRoutingManager(enable_optimized, comparison_mode)
    return _migration_manager


# Convenience functions for easy integration

def route_message_with_migration(user_message: str, model_choice: Dict,
                                session_id: str = "", user_id: str = "", 
                                message_history: List[Dict] = None,
                                context: Dict = None,
                                enable_optimized: bool = True,
                                comparison_mode: bool = False) -> Dict:
    """Route message using the migration manager"""
    
    manager = get_migration_manager(enable_optimized, comparison_mode)
    return manager.route_message_hybrid(
        user_message, model_choice, session_id, user_id, message_history, context
    )


def get_routing_performance_stats() -> Dict:
    """Get performance statistics from migration manager"""
    manager = get_migration_manager()
    return manager.get_performance_stats()


def get_migration_recommendations() -> Dict:
    """Get migration recommendations"""
    manager = get_migration_manager()
    return manager.get_migration_recommendations()


def analyze_routing_migration(user_message: str, model_choice: Dict) -> Dict:
    """Analyze how a message would be routed by both systems"""
    manager = get_migration_manager(comparison_mode=True)
    result = manager.route_message_hybrid(user_message, model_choice)
    
    if result.get('system') == 'comparison' and result.get('comparison'):
        return result['comparison']
    else:
        return {'error': 'Comparison failed', 'result': result}