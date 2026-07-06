
/* 
 * GUI增强功能 - 用户体验优化
 * 提供流畅的交互体验和视觉反馈
 */

class EnhancedUIFeatures {
    constructor() {
        this.init();
    }
    
    init() {
        this.bindSystemControls();
        this.bindOfficeDropZone();
        this.addKeyboardShortcuts();
        this.addNotificationSystem();
        this.setupAutoHidePanel();
        console.log('GUI增强功能初始化完成');
    }
    
    bindSystemControls() {
        // 音量控制事件绑定
        this.bindCustomSlider('volumeSlider', (value) => {
            this.sendSystemCommand(`set_volume_system`, { value: value });
        });
        
        // 亮度控制事件绑定
        this.bindCustomSlider('brightnessSlider', (value) => {
            this.sendSystemCommand(`set_brightness_system`, { value: value });
        });
    }
    
    bindCustomSlider(sliderId, callback) {
        const slider = document.getElementById(sliderId);
        if (!slider) return;
        
        // 实时值显示
        slider.addEventListener('input', function() {
            const valueElement = document.getElementById(this.id.replace('Slider', 'Value'));
            if (valueElement) {
                valueElement.textContent = this.value;
                this.style.background = `linear-gradient(to right, #4CAF50 0%, #4CAF50 ${this.value}%, #ddd ${this.value}%, #ddd 100%)`;
            }
        });
        
        // 变更操作
        slider.addEventListener('change', function() {
            callback(parseInt(this.value));
        });
    }
    
    bindOfficeDropZone() {
        const dropZone = document.getElementById('officeDropZone');
        if (!dropZone) return;
        
        // 优化拖放视觉效果
        const enhanceDropEffects = () => {
            dropZone.addEventListener('dragenter', (e) => {
                e.preventDefault();
                this.showDropHint('释放文件开始处理...', 'success');
            });
            
            dropZone.addEventListener('dragleave', () => {
                this.hideDropHint();
            });
        };
        
        enhanceDropEffects();
    }
    
    addKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ctrl+Shift+快捷键
            if (e.ctrlKey && e.shiftKey) {
                switch(e.key.toLowerCase()) {
                    case 's':
                        e.preventDefault();
                        this.quickScreenshot();
                        break;
                    case 't':
                        e.preventDefault();
                        this.showSystemInfo();
                        break;
                    case 'p':
                        e.preventDefault();
                        this.toggleSystemPanel();
                        break;
                    case 'd':
                        e.preventDefault();
                        this.focusDropZone();
                        break;
                }
            }
        });
    }
    
    addNotificationSystem() {
        // 创建通知容器
        const notificationContainer = document.createElement('div');
        notificationContainer.id = 'gui-notifications';
        notificationContainer.style.cssText = `
            position: fixed;
            top: 20px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 10000;
            pointer-events: none;
        `;
        document.body.appendChild(notificationContainer);
    }
    
    setupAutoHidePanel() {
        const panel = document.getElementById('systemPanel');
        if (!panel) return;
        
        let hideTimer;
        const hideDelay = 3000; // 3秒后自动隐藏
        
        // 鼠标移出时自动隐藏
        panel.addEventListener('mouseleave', () => {
            hideTimer = setTimeout(() => {
                panel.style.opacity = '0.3';
                panel.style.transform = 'translateX(200px)';
            }, hideDelay);
        });
        
        // 鼠标移入时显示
        panel.addEventListener('mouseenter', () => {
            clearTimeout(hideTimer);
            panel.style.opacity = '1';
            panel.style.transform = 'translateX(0)';
        });
        
        // 点击时保持显示
        panel.addEventListener('click', () => {
            clearTimeout(hideTimer);
            panel.style.opacity = '1';
            panel.style.transform = 'translateX(0)';
        });
    }
    
    // 快捷操作函数
    quickScreenshot() {
        this.sendChatCommand('请帮我截图并保存到工作空间');
        this.showNotification('截图命令已发送', 'success');
    }
    
    showSystemInfo() {
        this.sendChatCommand('请帮我显示系统信息');
        this.showNotification('系统信息命令已发送', 'info');
    }
    
    toggleSystemPanel() {
        const panel = document.getElementById('systemPanel');
        if (panel.style.display === 'none') {
            panel.style.display = 'block';
            this.showNotification('系统面板已显示', 'info');
        } else {
            panel.style.display = 'none';
            this.showNotification('系统面板已隐藏', 'info');
        }
    }
    
    focusDropZone() {
        const dropZone = document.getElementById('officeDropZone');
        if (dropZone) {
            dropZone.scrollIntoView({ behavior: 'smooth' });
            dropZone.style.borderColor = '#FF9800';
            setTimeout(() => {
                dropZone.style.borderColor = '#4CAF50';
            }, 1000);
        }
    }
    
    // 通知系统
    showNotification(message, type = 'info') {
        const container = document.getElementById('gui-notifications');
        if (!container) return;
        
        const notification = document.createElement('div');
        notification.style.cssText = `
            background: ${this.getNotificationColor(type)};
            color: white;
            padding: 12px 20px;
            margin: 5px;
            border-radius: 6px;
            font-size: 14px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            animation: slideIn 0.3s ease;
            pointer-events: auto;
            cursor: pointer;
        `;
        
        notification.textContent = message;
        notification.onclick = () => notification.remove();
        container.appendChild(notification);
        
        // 自动移除
        setTimeout(() => {
            if (notification.parentNode) {
                notification.style.animation = 'slideOut 0.3s ease';
                setTimeout(() => notification.remove(), 300);
            }
        }, 3000);
    }
    
    getNotificationColor(type) {
        const colors = {
            success: '#4CAF50',
            error: '#F44336', 
            warning: '#FF9800',
            info: '#2196F3'
        };
        return colors[type] || colors.info;
    }
    
    showDropHint(message, type = 'info') {
        const dropZone = document.getElementById('officeDropZone');
        if (!dropZone) return;
        
        const hint = document.createElement('div');
        hint.style.cssText = `
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: ${this.getNotificationColor(type)};
            color: white;
            padding: 10px 20px;
            border-radius: 20px;
            font-size: 14px;
            z-index: 10;
            animation: pulse 1s ease-in-out infinite;
        `;
        hint.textContent = message;
        hint.id = 'dropHint';
        
        dropZone.style.position = 'relative';
        dropZone.appendChild(hint);
    }
    
    hideDropHint() {
        const hint = document.getElementById('dropHint');
        if (hint) hint.remove();
    }
    
    // 命令发送
    sendChatCommand(command) {
        const input = document.getElementById('messageInput');
        const sendBtn = document.getElementById('sendButton');
        
        if (input && sendBtn) {
            input.value = command;
            sendBtn.click();
        }
    }
    
    sendSystemCommand(operation, params = {}) {
        // 发送到Agent处理
        const command = `系统命令: ${operation} ${JSON.stringify(params)}`;
        this.sendChatCommand(command);
    }
}

// 初始化增强功能
document.addEventListener('DOMContentLoaded', function() {
    window.enhancedUI = new EnhancedUIFeatures();
});

// 添加动画样式
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from { transform: translateY(-100px); opacity: 0; }
        to { transform: translateY(0); opacity: 1; }
    }
    
    @keyframes slideOut {
        from { transform: translateY(0); opacity: 1; }
        to { transform: translateY(-100px); opacity: 0; }
    }
    
    @keyframes pulse {
        0%, 100% { opacity: 0.8; }
        50% { opacity: 1; }
    }
`;
document.head.appendChild(style);
