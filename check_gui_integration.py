#!/usr/bin/env python3

"""
简单的GUI集成检查脚本
"""

import os

def check_web_gui():
    """检查Web界面GUI集成"""
    print("开始检查GUI集成...")
    
    web_file = "web/index.html"
    if not os.path.exists(web_file):
        print("Web界面文件不存在")
        return False
    
    with open(web_file, 'r', encoding='utf-8') as file:
        content = file.read()
    
    # 检查关键组件
    checks = {
        'systemPanel': '系统控制面板',
        'volumeSlider': '音量控制',
        'brightnessSlider': '亮度控制',
        'officeDropZone': 'Office拖放区',
        'processingResults': '处理结果区',
        'setVolume': '音量控制函数',
        'OfficeDropZone': '文件拖放类'
    }
    
    print("\n组件检查:")
    for key, name in checks.items():
        status = "OK" if key in content else "MISSING"
        print(f"  {status}: {name}")
    
    return True

def check_tools():
    """检查工具状态"""
    print("\n工具状态:")
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        
        from tools import TOOL_REGISTRY
        office_tools = ['excel_pivot', 'create_word_document', 'create_presentation', 'system_audio']
        
        for tool in office_tools:
            status = "OK" if tool in TOOL_REGISTRY else "MISSING"
            print(f"  {status}: {tool}")
            
    except Exception as e:
        print(f"  工具检查失败: {e}")

if __name__ == "__main__":
    check_web_gui()
    check_tools()
    print("\n检查完成！")