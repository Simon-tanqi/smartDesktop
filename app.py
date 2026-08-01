import os
import sys
import signal
import time
from pathlib import Path
from datetime import datetime
import tkinter


def _tk_safe_del(original_del):
    """包装 __del__，静默捕获跨线程 GC 错误。"""
    def wrapper(self):
        try:
            original_del(self)
        except RuntimeError:
            pass
    return wrapper


_tk_patched = {"Image", "Variable"}
_tk_mod = tkinter
for _name in _tk_patched:
    _cls = getattr(_tk_mod, _name, None)
    if _cls is not None and hasattr(_cls, "__del__"):
        _cls.__del__ = _tk_safe_del(_cls.__del__)

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

from backend.chat import chat_bp
from backend.upload_api import upload_bp
from backend.rate_limiter import limiter
from memory.session import ensure_session_titles
from utils.dependency_manager import check_and_report
from logger import get_logger

logger = get_logger()

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

_PORT = int(os.environ.get('AGENT_PORT', '8765'))
_HOST = os.environ.get('AGENT_HOST', '127.0.0.1')


class Config:
    PORT = _PORT
    HOST = _HOST
    CORS_ORIGINS = os.environ.get('AGENT_CORS_ORIGINS', 'http://localhost:8765,http://127.0.0.1:8765').split(',')
    RATELIMIT_STORAGE_URI = os.environ.get('AGENT_RATELIMIT_STORAGE', 'memory://')
    RATELIMIT_ENABLED = os.environ.get('AGENT_RATELIMIT_ENABLED', 'true').lower() == 'true'
    DEBUG = os.environ.get('AGENT_DEBUG', 'false').lower() == 'true'


def _register_tools():
    """延迟注册工具，避免模块导入时的副作用"""
    from tools import _ensure_tool_registration
    _ensure_tool_registration()


def _setup_graceful_shutdown(app):
    """注册优雅停机信号处理"""
    def graceful_shutdown(signum, frame):
        logger.info("收到停机信号，正在优雅关闭服务...")
        try:
            app.do_teardown_appcontext()
        except Exception as e:
            logger.warning(f"do_teardown_appcontext 异常（无活动上下文，可忽略）: {e}")
        time.sleep(1)
        logger.info("服务已关闭")
        os._exit(0)
    
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)


def _validate_model_configs():
    """验证所有模型配置的有效性"""
    from llm_client import get_available_models, get_api_key
    
    try:
        models = get_available_models()
        logger.info(f"发现 {len(models)} 个可用模型")
        
        for model in models:
            model_id = model.get("id", "")
            provider = model.get("provider", "")
            base_url = model.get("base_url", "")
            temperature = model.get("temperature", 0.3)
            
            logger.debug(f"验证模型配置: id={model_id}, provider={provider}, base_url={base_url}, temperature={temperature}")
            
            if not base_url:
                logger.warning(f"模型 {model_id} 缺少 base_url 配置")
            
            if provider != "ollama":
                api_key = get_api_key(model_id)
                if not api_key:
                    logger.warning(f"模型 {model_id} 缺少 api_key 配置")
        
        logger.info("模型配置验证完成")
    except Exception as e:
        logger.error(f"模型配置验证失败: {e}", exc_info=True)


def _init_app(app, config):
    """应用初始化逻辑 - 生产环境部署时也会执行"""
    logger.info("="*60)
    logger.info("Agent 服务启动中...")
    logger.info(f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"运行模式: {'调试模式' if config.DEBUG else '生产模式'}")
    logger.info(f"主机地址: {config.HOST}")
    logger.info(f"监听端口: {config.PORT}")
    logger.info(f"CORS允许来源: {config.CORS_ORIGINS}")
    logger.info(f"限速: {'启用' if config.RATELIMIT_ENABLED else '禁用'}")
    logger.info("-"*60)
    
    dep_report = check_and_report()
    if dep_report:
        logger.info(f"依赖检查结果: {dep_report}")
    
    logger.info("开始验证模型配置...")
    _validate_model_configs()
    
    logger.info("初始化会话标题...")
    ensure_session_titles()
    
    logger.info("注册工具...")
    _register_tools()
    
    logger.info("-"*60)
    logger.info(f"Agent 服务初始化完成")
    logger.info(f"访问地址: http://{config.HOST}:{config.PORT}")
    logger.info("="*60)
    
    if hasattr(logger, 'flush'):
        logger.flush()


def create_app(config=None):
    """Flask 应用工厂函数"""
    if config is None:
        config = Config
    
    app = Flask(__name__, 
                template_folder=str(BASE_DIR / 'web'), 
                static_folder=str(BASE_DIR / 'web/static'))
    
    app.config.from_object(config)
    
    app.register_blueprint(chat_bp)
    app.register_blueprint(upload_bp)
    
    CORS(app, resources={r"/api/*": {"origins": config.CORS_ORIGINS}}, supports_credentials=True)
    
    limiter.init_app(app)
    
    @app.route('/')
    def index():
        return render_template('index.html')
    
    @app.route('/api/health')
    def health():
        return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})
    
    @app.before_request
    def log_request_start():
        request.start_time = time.time()
    
    @app.after_request
    def log_request_end(response):
        if hasattr(request, 'start_time'):
            duration = (time.time() - request.start_time) * 1000
            logger.info(
                f"REQUEST {request.method} {request.path} {response.status_code} "
                f"from {request.remote_addr} in {duration:.2f}ms"
            )
        if request.path == '/' or request.path.endswith('.html') or request.path.endswith('.js'):
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response
    
    @app.errorhandler(Exception)
    def handle_exception(e):
        logger.error(f"未捕获异常: {str(e)}", exc_info=True)
        return jsonify({"error": "Internal Server Error", "message": str(e)}), 500
    
    _setup_graceful_shutdown(app)
    _init_app(app, config)
    
    return app


app = None


# 启动时打印可用模型列表，便于诊断 provider/model 错路由问题
try:
    from llm_client import get_available_models
    _models = get_available_models()
    logger.info(f"[STARTUP] 可用模型列表 (共 {len(_models)} 个):")
    for _m in _models:
        logger.info(f"  - id={_m.get('id')!r} display={_m.get('display')!r} provider={_m.get('provider')!r} model={_m.get('model')!r}")
    import os
    _env_key = os.environ.get("API_KEY", "") or os.environ.get("MYAGENT_API_KEY", "")
    _env_model = os.environ.get("API_MODEL", "")
    logger.info(f"[STARTUP] 环境变量 API_KEY={'已设置' if _env_key else '未设置'} API_MODEL={_env_model!r}")
except Exception as e:
    logger.warning(f"[STARTUP] 列出模型失败: {e}")


def _cleanup_pycache(root_dir=None):
    """启动前清除所有 __pycache__，防止修改后的 .py 被旧 .pyc 遮蔽。"""
    import shutil
    if root_dir is None:
        root_dir = os.path.dirname(os.path.abspath(__file__))
    removed = 0
    for dirpath, dirnames, _ in os.walk(root_dir):
        # 跳过虚拟环境、node_modules 等
        if any(skip in dirpath for skip in ('site-packages', 'node_modules', '.venv', 'venv')):
            continue
        for d in dirnames:
            if d == '__pycache__':
                pyc_dir = os.path.join(dirpath, d)
                try:
                    shutil.rmtree(pyc_dir, ignore_errors=True)
                    removed += 1
                except Exception:
                    pass
    return removed


if __name__ == '__main__':
    # 启动前清理所有 pyc 缓存，确保加载最新源码
    _cleaned = _cleanup_pycache()
    logger.info(f"[STARTUP] 已清理 {_cleaned} 个 __pycache__ 目录")
    app = create_app()
    logger.info(f"Agent 前端已启动，请访问: http://{Config.HOST}:{Config.PORT}")
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, use_reloader=False, threaded=True)
