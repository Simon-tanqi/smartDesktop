"""共享速率限制器模块，供 app.py 和 chat.py 共同使用"""
import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    get_remote_address,
    storage_uri=os.environ.get('AGENT_RATELIMIT_STORAGE', 'memory://'),
    default_limits=["30 per minute;200 per hour"],
)


def exempt(func):
    """豁免装饰器：跳过限流检查"""
    limiter.exempt(func)
    return func
