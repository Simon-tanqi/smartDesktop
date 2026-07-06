from flask import Flask, render_template, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from backend.chat import chat_bp
from backend.upload_api import upload_bp
from memory.session import ensure_session_titles

app = Flask(__name__, template_folder='web', static_folder='web/static')
app.register_blueprint(chat_bp)
app.register_blueprint(upload_bp)

CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["30 per minute", "200 per hour"],
    storage_uri="memory://",
)


@app.route('/')
def index():
    return render_template('index.html')


@app.after_request
def add_no_cache(response):
    if request.path == '/' or request.path.endswith('.html') or request.path.endswith('.js'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


if __name__ == '__main__':
    ensure_session_titles()
    port = 8765
    print(f"Agent 前端已启动，请访问: http://127.0.0.1:{port}")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False, threaded=True)
