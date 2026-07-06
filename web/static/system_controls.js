<script>
// 系统控制面板JavaScript函数

// 音量控制
function setVolume(operation, value = 50) {
    const volumeSlider = document.getElementById('volumeSlider');
    const volumeValue = document.getElementById('volumeValue');
    
    if (operation === 'set_volume') {
        volumeSlider.value = value;
        volumeValue.textContent = value;
    }
    
    // 调用Agent的音量控制工具
    sendQuickCommand(`帮我调整音量为${value}%`);
    
    // 也可以直接调用API（如果实现了后端接口）
    // fetch('/api/system/volume', {
    //     method: 'POST',
    //     headers: {'Content-Type': 'application/json'},
    //     body: JSON.stringify({operation, value})
    // });
}

// 亮度控制
function setBrightness(value) {
    const brightnessSlider = document.getElementById('brightnessSlider');
    const brightnessValue = document.getElementById('brightnessValue');
    
    brightnessSlider.value = value;
    brightnessValue.textContent = value;
    
    // 调用Agent的亮度控制工具
    sendQuickCommand(`帮我调整屏幕亮度为${value}%`);
    
    console.log(`设置亮度: ${value}%`);
}

// 截图功能  
function takeScreenshot() {
    // 调用Agent的截图工具
    sendQuickCommand("请帮我截图并保存到工作空间");
    
    console.log('执行截图操作');
    alert('截图命令已发送，请查看Agent执行结果');
}

// 任务管理器
function openTaskManager() {
    sendQuickCommand("请帮我查看当前运行的进程和CPU使用情况");
}

// 系统信息
function showSystemInfo() {
    sendQuickCommand("请帮我显示系统信息：CPU使用率、内存使用、磁盘空间");
}

// 快捷命令发送函数
function sendQuickCommand(command) {
    // 获取输入框和发送按钮
    const input = document.getElementById('messageInput');
    const sendBtn = document.getElementById('sendButton');
    
    // 填充命令并发送
    if (input && sendBtn) {
        input.value = command;
        sendBtn.click();
    }
}

// 面板显示/隐藏切换
function toggleSystemPanel() {
    const panel = document.getElementById('systemPanel');
    if (panel.style.display === 'none') {
        panel.style.display = 'block';
    } else {
        panel.style.display = 'none';
    }
}

// 绑定滑块事件
document.addEventListener('DOMContentLoaded', function() {
    const volumeSlider = document.getElementById('volumeSlider');
    const brightnessSlider = document.getElementById('brightnessSlider');
    
    if (volumeSlider) {
        volumeSlider.addEventListener('input', function() {
            document.getElementById('volumeValue').textContent = this.value;
        });
        
        volumeSlider.addEventListener('change', function() {
            setVolume('set_volume', parseInt(this.value));
        });
    }
    
    if (brightnessSlider) {
        brightnessSlider.addEventListener('input', function() {
            document.getElementById('brightnessValue').textContent = this.value;
        });
        
        brightnessSlider.addEventListener('change', function() {
            setBrightness(parseInt(this.value));
        });
    }
    
    console.log('系统控制面板加载完成');
});

// 快捷键支持
document.addEventListener('keydown', function(e) {
    // Ctrl+Shift+S: 截图
    if (e.ctrlKey && e.shiftKey && e.key === 'S') {
        e.preventDefault();
        takeScreenshot();
    }
    
    // Ctrl+Shift+T: 系统信息
    if (e.ctrlKey && e.shiftKey && e.key === 'T') {
        e.preventDefault();  
        showSystemInfo();
    }
    
    // Ctrl+Shift+P: 切换面板显示
    if (e.ctrlKey && e.shiftKey && e.key === 'P') {
        e.preventDefault();
        toggleSystemPanel();
    }
});
</script>