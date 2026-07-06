#!/usr/bin/env python3
"""安装Office文档处理所需依赖库"""

import subprocess
import sys

def install_package(package_name):
    """安装指定的Python包"""
    try:
        print(f"正在安装 {package_name}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", package_name])
        print(f"✅ {package_name} 安装成功")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {package_name} 安装失败: {e}")
        return False

def check_and_install_dependencies():
    """检查并安装所有必需的依赖"""
    dependencies = [
        "openpyxl",      # Excel处理
        "python-docx",   # Word处理  
        "python-pptx",   # PPT处理
    ]
    
    print("=== Office文档处理库依赖安装 ===")
    
    failed_installs = []
    for dep in dependencies:
        if not install_package(dep):
            failed_installs.append(dep)
    
    if failed_installs:
        print("\n❌ 以下依赖安装失败:")
        for dep in failed_installs:
            print(f"  - {dep}")
        print("\n请手动安装这些依赖:")
        print("pip install python-docx python-pptx")
        return False
    else:
        print("\n✅ 所有Office处理依赖安装成功!")
        return True

def test_imports():
    """测试新工具模块是否能正确导入"""
    print("\n=== 测试工具模块导入 ===")
    
    modules_to_test = [
        ('openpyxl', 'openpyxl'),
        ('docx', 'python-docx'),
        ('pptx', 'python-pptx'),
    ]
    
    for module_name, package_name in modules_to_test:
        try:
            if module_name == 'openpyxl':
                import openpyxl
                print(f"✅ {module_name} 导入成功")
            elif module_name == 'docx':
                from docx import Document
                print(f"✅ {module_name} 导入成功")
            elif module_name == 'pptx':
                from pptx import Presentation
                print(f"✅ {module_name} 导入成功")
        except ImportError as e:
            print(f"❌ {module_name} 导入失败: {e}")
            return False
    
    # 测试自定义工具模块
    try:
        print("\n=== 测试自定义工具模块 ===")
        
        print("测试 file_tools...")
        from tools import file_tools
        print("✅ file_tools 导入成功")
        
        print("测试 word_tools...")
        from tools import word_tools
        print("✅ word_tools 导入成功")
        
        print("测试 ppt_tools...")  
        from tools import ppt_tools
        print("✅ ppt_tools 导入成功")
        
        return True
        
    except ImportError as e:
        print(f"❌ 工具模块导入失败: {e}")
        return False

if __name__ == "__main__":
    print("安装MyAgent Office文档处理功能依赖...")
    
    # 安装依赖
    deps_ok = check_and_install_dependencies()
    
    # 测试导入
    imports_ok = test_imports()
    
    if deps_ok and imports_ok:
        print("\n🎉 所有Office处理功能准备就绪！")
        print("\n您现在可以使用以下新功能:")
        print("📊 Excel高级处理: 透视表、格式化、图表生成、批量处理")
        print("📝 Word文档: 创建、编辑、格式化、内容提取、批量处理")
        print("🎭 PowerPoint: 创建演示文稿、添加幻灯片、提取内容、批量处理")
    else:
        print("\n❌ 安装过程中出现问题，请检查错误信息。")
        sys.exit(1)