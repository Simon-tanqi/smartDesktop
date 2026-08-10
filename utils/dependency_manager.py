"""
smartDesktop 依赖管理和功能边界说明

本文件定义了：
1. 所有第三方依赖库及其版本要求
2. 各库的功能边界和使用限制
3. 启动时的依赖检查逻辑
"""
import importlib
import sys
from typing import Dict, List, Tuple, Optional, Any


# ============================================================
# 依赖库清单
# ============================================================

REQUIRED_DEPENDENCIES = {
    # ── 核心框架 ──
    "flask": {
        "version": ">=2.0",
        "desc": "Web 框架，提供 REST API 和 SSE 流式响应",
        "boundary": "仅用于 HTTP 服务，不参与业务逻辑",
        "critical": True,
        "import_name": "flask"
    },
    "flask_cors": {
        "version": ">=3.0",
        "desc": "跨域资源共享支持",
        "boundary": "仅处理 CORS 头，不影响业务逻辑",
        "critical": False,
        "import_name": "flask_cors"
    },
    "flask_limiter": {
        "version": ">=2.0",
        "desc": "请求限流",
        "boundary": "仅用于 API 限流，不影响核心功能",
        "critical": False,
        "import_name": "flask_limiter"
    },
    
    # ── LLM 客户端 ──
    "requests": {
        "version": ">=2.28",
        "desc": "HTTP 请求库，用于调用远程 LLM API",
        "boundary": "仅用于 API 调用，超时 30 秒",
        "critical": True,
        "import_name": "requests"
    },
    
    # ── 音频控制 ──
    "pycaw": {
        "version": ">=2022.0.1",
        "desc": "Windows 音频 API 封装，控制音量/静音",
        "boundary": "仅 Windows 平台，需要 COM 初始化",
        "critical": False,
        "import_name": "pycaw",
        "version_attr": None
    },
    "comtypes": {
        "version": ">=1.1.10",
        "desc": "COM 接口封装，pycaw 的依赖",
        "boundary": "仅 Windows 平台，需正确初始化 COM",
        "critical": False,
        "import_name": "comtypes"
    },
    
    # ── 数据处理 ──
    "pandas": {
        "version": ">=1.5",
        "desc": "数据处理和分析",
        "boundary": "仅用于 Excel 数据分析，内存限制 500MB",
        "critical": False,
        "import_name": "pandas"
    },
    "openpyxl": {
        "version": ">=3.0",
        "desc": "Excel 文件读写",
        "boundary": "支持 .xlsx 格式，最大文件 100MB",
        "critical": False,
        "import_name": "openpyxl"
    },
    "xlsxwriter": {
        "version": ">=3.0",
        "desc": "Excel 写入器，支持图表",
        "boundary": "仅用于生成带图表的 Excel 文件",
        "critical": False,
        "import_name": "xlsxwriter"
    },
    "matplotlib": {
        "version": ">=3.7",
        "desc": "数据可视化",
        "boundary": "仅用于生成图表，输出 PNG 格式",
        "critical": False,
        "import_name": "matplotlib"
    },
    
    # ── Office 文档 ──
    "python-docx": {
        "version": ">=1.1",
        "desc": "Word 文档处理",
        "boundary": "支持 .docx 格式，不支持 .doc",
        "critical": False,
        "import_name": "docx"
    },
    "python-pptx": {
        "version": ">=0.6.21",
        "desc": "PowerPoint 文档处理",
        "boundary": "支持 .pptx 格式，不支持 .ppt",
        "critical": False,
        "import_name": "pptx"
    },
    
    # ── GUI 自动化 ──
    "pyautogui": {
        "version": ">=0.9.54",
        "desc": "鼠标/键盘自动化",
        "boundary": "需屏幕分辨率稳定，DPI 缩放可能影响坐标",
        "critical": False,
        "import_name": "pyautogui"
    },
    "pygetwindow": {
        "version": ">=0.0.9",
        "desc": "窗口管理",
        "boundary": "仅 Windows 平台",
        "critical": False,
        "import_name": "pygetwindow"
    },
    "pywin32": {
        "version": ">=306",
        "desc": "Windows API 封装",
        "boundary": "仅 Windows 平台，用于窗口操作",
        "critical": False,
        "import_name": "win32api",
        "version_attr": None
    },
    
    # ── 图像处理 ──
    "Pillow": {
        "version": ">=9.0",
        "desc": "图像处理，截图保存",
        "boundary": "支持 PNG/JPG 格式，最大分辨率 4K",
        "critical": False,
        "import_name": "PIL"
    },
    
    # ── 安全存储 ──
    "keyring": {
        "version": ">=23.0",
        "desc": "系统凭据管理器，安全存储 API Key",
        "boundary": "Windows 用 Credential Manager，macOS 用 Keychain",
        "critical": False,
        "import_name": "keyring",
        "version_attr": None
    },
    
    # ── 配置 ──
    "pyyaml": {
        "version": ">=6.0",
        "desc": "YAML 配置文件解析",
        "boundary": "仅用于配置加载，不执行 YAML 中的代码",
        "critical": True,
        "import_name": "yaml"
    },
    
    # ── 数据库 ──
    "sqlite3": {
        "version": ">=3.0",
        "desc": "SQLite 数据库，记忆存储",
        "boundary": "文件型数据库，最大 2GB",
        "critical": True,
        "built_in": True,
        "import_name": "sqlite3"
    },
}


# ============================================================
# 功能边界定义
# ============================================================

FEATURE_BOUNDARIES = {
    "system_audio": {
        "name": "系统音频控制",
        "operations": ["get_volume", "set_volume", "toggle_mute", "get_mic_volume", "set_mic_volume", "toggle_mic_mute"],
        "limits": {
            "volume_range": "0-100",
            "platform": "Windows only",
            "devices": "仅默认扬声器和麦克风",
            "permission": "需要管理员权限（某些操作）"
        },
        "dependencies": ["pycaw", "comtypes"]
    },
    
    "execute_command": {
        "name": "命令执行",
        "operations": ["powershell", "cmd"],
        "limits": {
            "timeout": "120秒",
            "max_output": "2000字符",
            "blocked": ["rm -rf", "format", "shutdown", "reg delete"],
            "protected_paths": ["Windows", "Program Files", "ProgramData", ".ssh", ".aws"]
        },
        "dependencies": ["subprocess"]
    },
    
    "run_python": {
        "name": "Python 代码执行",
        "operations": ["数据分析", "图表生成", "文件处理"],
        "limits": {
            "timeout": "300秒",
            "memory": "500MB",
            "blocked_modules": ["os.system", "subprocess", "sys.exit"],
            "allowed_modules": ["pandas", "numpy", "matplotlib", "openpyxl", "xlsxwriter"]
        },
        "dependencies": ["pandas", "matplotlib", "openpyxl", "xlsxwriter"]
    },
    
    "file_operations": {
        "name": "文件操作",
        "operations": ["read_file", "write_file", "edit_file", "delete_file", "scan_directory"],
        "limits": {
            "max_file_size": "10MB",
            "skip_dirs": ["Windows", "Program Files", "node_modules", ".git", "__pycache__"],
            "encoding": ["utf-8", "gbk", "gb2312", "gb18030"]
        },
        "dependencies": []
    },
    
    "web_browse": {
        "name": "网页浏览",
        "operations": ["web_search", "fetch_webpage", "get_weather", "web_download"],
        "limits": {
            "timeout": "30秒",
            "max_pages": "5",
            "risky_extensions": [".exe", ".msi", ".bat", ".ps1", ".dll"],
            "download_dir": "用户 Downloads 目录"
        },
        "dependencies": ["requests"]
    },
    
    "office_documents": {
        "name": "Office 文档",
        "operations": ["create_word_document", "add_word_content", "extract_word_text",
                      "create_presentation", "add_slide_content", "extract_presentation_info"],
        "limits": {
            "word_format": ".docx only",
            "ppt_format": ".pptx only",
            "max_filesize": "50MB"
        },
        "dependencies": ["python-docx", "python-pptx"]
    },
    
    "gui_automation": {
        "name": "GUI 自动化",
        "operations": ["gui_click", "gui_move", "gui_type", "gui_keys", 
                      "gui_screenshot", "gui_find_window", "gui_activate"],
        "limits": {
            "platform": "Windows only",
            "screen_resolution": "需稳定",
            "dpi_scaling": "可能影响坐标精度",
            "timeout": "30秒"
        },
        "dependencies": ["pyautogui", "pygetwindow", "pywin32", "Pillow"]
    },
    
    "registry": {
        "name": "注册表操作",
        "operations": ["modify_registry"],
        "limits": {
            "allowed_keys": ["HKCU"],
            "allowed_paths": ["Software\\Microsoft\\Windows\\CurrentVersion\\Explorer",
                             "Control Panel\\Desktop"],
            "blocked_operations": ["delete", "import", "export"]
        },
        "dependencies": ["subprocess"]
    },
    
    "database": {
        "name": "数据库",
        "operations": ["查询", "写入"],
        "limits": {
            "database_type": "SQLite",
            "max_size": "2GB",
            "location": "%APPDATA%/smartDesktop/"
        },
        "dependencies": ["sqlite3"]
    }
}


# ============================================================
# 依赖检查逻辑
# ============================================================

class DependencyChecker:
    """依赖检查器，启动时验证所有依赖"""
    
    def __init__(self):
        self._checked = False
        self._results: Dict[str, Dict[str, Any]] = {}
    
    def check_all(self) -> Tuple[bool, List[str]]:
        """检查所有依赖，返回 (是否全部通过, 错误信息列表)"""
        errors = []
        all_passed = True
        
        for package, config in REQUIRED_DEPENDENCIES.items():
            if config.get("built_in"):
                result = self._check_builtin(package)
            else:
                result = self._check_package(package, config)
            
            self._results[package] = result
            
            if not result["passed"]:
                all_passed = False
                errors.append(result["message"])
        
        self._checked = True
        return all_passed, errors
    
    def _check_builtin(self, package: str) -> Dict[str, Any]:
        """检查内置模块"""
        try:
            importlib.import_module(package)
            return {
                "passed": True,
                "version": sys.version.split()[0],
                "message": f"✅ {package} (内置模块)"
            }
        except ImportError:
            return {
                "passed": False,
                "version": None,
                "message": f"❌ {package} 缺失（内置模块）"
            }
    
    def _check_package(self, package: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """检查第三方包"""
        import_name = config.get("import_name", package)
        try:
            module = importlib.import_module(import_name)
            
            version_attr = config.get("version_attr", "__version__")
            if version_attr is None:
                version = "unknown"
            else:
                version = getattr(module, version_attr, 'unknown')
            
            required_version = config.get("version", "")
            if required_version.startswith(">=") and version != "unknown":
                req_major, req_minor = self._parse_version(required_version[2:])
                pkg_major, pkg_minor = self._parse_version(version)
                
                if pkg_major < req_major or (pkg_major == req_major and pkg_minor < req_minor):
                    return {
                        "passed": False,
                        "version": version,
                        "message": f"⚠️ {package} 版本 {version} 低于要求 {required_version}"
                    }
            
            return {
                "passed": True,
                "version": version,
                "message": f"✅ {package} {version}"
            }
        except ImportError:
            if config.get("critical"):
                return {
                    "passed": False,
                    "version": None,
                    "message": f"❌ {package} 缺失，核心功能无法运行。安装: pip install {package}{config.get('version', '')}"
                }
            else:
                return {
                    "passed": False,
                    "version": None,
                    "message": f"⚠️ {package} 缺失，部分功能受限。安装: pip install {package}{config.get('version', '')}"
                }
    
    def _parse_version(self, version_str: str) -> Tuple[int, int]:
        """解析版本号为 (major, minor)"""
        parts = version_str.split('.')
        try:
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            return major, minor
        except (ValueError, IndexError):
            return 0, 0
    
    def get_status(self, package: str) -> Optional[Dict[str, Any]]:
        """获取单个包的检查状态"""
        if not self._checked:
            self.check_all()
        return self._results.get(package)
    
    def get_summary(self) -> str:
        """获取检查结果汇总"""
        if not self._checked:
            self.check_all()
        
        lines = ["=" * 60]
        lines.append("smartDesktop 依赖检查报告")
        lines.append("=" * 60)
        
        passed_count = sum(1 for r in self._results.values() if r["passed"])
        total_count = len(self._results)
        
        for package, result in sorted(self._results.items()):
            lines.append(result["message"])
        
        lines.append("-" * 60)
        lines.append(f"结果: {passed_count}/{total_count} 通过")
        
        failed_packages = [p for p, r in self._results.items() if not r["passed"]]
        if failed_packages:
            lines.append(f"缺失包: {', '.join(failed_packages)}")
            install_cmd = "pip install " + " ".join(failed_packages)
            lines.append(f"安装命令: {install_cmd}")
        
        lines.append("=" * 60)
        return "\n".join(lines)


# ============================================================
# 功能可用性检查
# ============================================================

class FeatureAvailability:
    """检查各功能是否可用"""
    
    def __init__(self, checker: DependencyChecker):
        self._checker = checker
        self._availability: Dict[str, bool] = {}
    
    def check_feature(self, feature_name: str) -> bool:
        """检查单个功能是否可用"""
        if feature_name in self._availability:
            return self._availability[feature_name]
        
        feature = FEATURE_BOUNDARIES.get(feature_name)
        if not feature:
            self._availability[feature_name] = False
            return False
        
        deps = feature.get("dependencies", [])
        for dep in deps:
            status = self._checker.get_status(dep)
            if status and not status["passed"]:
                self._availability[feature_name] = False
                return False
        
        self._availability[feature_name] = True
        return True
    
    def get_available_features(self) -> List[str]:
        """获取所有可用功能列表"""
        available = []
        for feature_name in FEATURE_BOUNDARIES:
            if self.check_feature(feature_name):
                available.append(feature_name)
        return available
    
    def get_unavailable_features(self) -> List[str]:
        """获取不可用功能列表及原因"""
        unavailable = []
        for feature_name, feature in FEATURE_BOUNDARIES.items():
            if not self.check_feature(feature_name):
                unavailable.append(feature_name)
        return unavailable


# ============================================================
# 全局检查器实例
# ============================================================

dependency_checker = DependencyChecker()
feature_availability = FeatureAvailability(dependency_checker)


def check_and_report() -> str:
    """执行检查并返回报告"""
    return dependency_checker.get_summary()