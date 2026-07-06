
<script>
// Office文件拖放处理JavaScript

class OfficeDropZone {
    constructor() {
        this.dropZone = document.getElementById('officeDropZone');
        this.fileInput = document.getElementById('fileInput');
        this.dropZoneText = document.getElementById('dropZoneText');
        this.resultsContent = document.getElementById('resultsContent');
        this.processingResults = document.getElementById('processingResults');
        
        this.initDropZone();
        this.initFileInput();
    }
    
    initDropZone() {
        if (!this.dropZone) return;
        
        // 拖放事件
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
            this.dropZone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
            });
        });
        
        // 拖拽进入/悬停效果
        this.dropZone.addEventListener('dragenter', () => {
            this.dropZone.style.borderColor = '#2196F3';
            this.dropZone.style.backgroundColor = '#e3f2fd';
        });
        
        this.dropZone.addEventListener('dragover', () => {
            this.dropZone.style.borderColor = '#2196F3'; 
            this.dropZone.style.backgroundColor = '#e3f2fd';
        });
        
        // 拖拽离开
        this.dropZone.addEventListener('dragleave', () => {
            this.dropZone.style.borderColor = '#4CAF50';
            this.dropZone.style.backgroundColor = '';
        });
        
        // 放置文件
        this.dropZone.addEventListener('drop', (e) => {
            this.dropZone.style.borderColor = '#4CAF50';
            this.dropZone.style.backgroundColor = '';
            
            const files = e.dataTransfer.files;
            this.handleFiles(files);
        });
        
        // 点击选择文件
        this.dropZone.addEventListener('click', () => {
            this.fileInput.click();
        });
    }
    
    initFileInput() {
        if (!this.fileInput) return;
        
        this.fileInput.addEventListener('change', (e) => {
            this.handleFiles(e.target.files);
            this.fileInput.value = ''; // 清空选择
        });
    }
    
    handleFiles(files) {
        const officeFiles = Array.from(files).filter(file => {
            const ext = this.getFileExtension(file.name).toLowerCase();
            return ['.xlsx', '.xls', '.docx', '.doc', '.pptx', '.ppt', '.txt'].includes(ext);
        });
        
        if (officeFiles.length === 0) {
            this.showError('请拖放Excel、Word、PPT或文本文件');
            return;
        }
        
        officeFiles.forEach(file => {
            this.processOfficeFile(file);
        });
    }
    
    processOfficeFile(file) {
        const ext = this.getFileExtension(file.name).toLowerCase();
        let command = '';
        
        // 根据文件类型生成不同的处理命令
        switch(ext) {
            case '.xlsx':
            case '.xls':
                command = `请分析Excel文件：${file.name}，生成数据透视表和图表，总结关键数据趋势`;
                break;
                
            case '.docx':
            case '.doc': 
                command = `请提取Word文档：${file.name} 的主要内容，生成摘要和关键点`;
                break;
                
            case '.pptx':
            case '.ppt':
                command = `请总结PPT演示文稿：${file.name} 的主要内容，提取核心观点和结论`;
                break;
                
            case '.txt':
                command = `请阅读文本文件：${file.name}，总结主要内容和关键信息`;
                break;
        }
        
        // 显示处理中状态
        this.showProcessing(file.name, ext);
        
        // 调用Agent处理
        setTimeout(() => {
            this.sendOfficeCommand(command, file.name);
        }, 1000);
        
        console.log(`处理文件: ${file.name} (${ext})`);
    }
    
    showProcessing(filename, ext) {
        const icon = this.getFileIcon(ext);
        const processingHtml = `
            <div style="display: flex; align-items: center; margin: 10px 0; padding: 10px; background: #fff3cd;
                        border: 1px solid #ffeaa7; border-radius: 5px;">
                <span style="font-size: 24px; margin-right: 10px;">${icon}</span>
                <div>
                    <strong style="color: #856404;">${filename}</strong>
                    <div style="font-size: 12px; color: #856404;">处理中...请稍候</div>
                </div>
                <div style="margin-left: auto;">
                    <div class="spinner" style="width: 20px; height: 20px; border: 2px solid #f3f3f3;
                         border-top: 2px solid #4CAF50; border-radius: 50%; animation: spin 1s linear infinite;"></div>
                </div>
            </div>
        `;
        
        this.resultsContent.innerHTML += processingHtml;
        this.processingResults.style.display = 'block';
    }
    
    sendOfficeCommand(command, filename) {
        // 发送命令给Agent
        const input = document.getElementById('messageInput');
        const sendBtn = document.getElementById('sendButton');
        
        if (input && sendBtn) {
            input.value = command;
            sendBtn.click();
            
            // 添加处理结果跟踪
            this.addResultEntry(filename, command);
        }
    }
    
    addResultEntry(filename, command) {
        const entryHtml = `
            <div style="margin: 10px 0; padding: 10px; background: white; border-left: 4px solid #4CAF50;">
                <strong style="color: #4CAF50;">✓ ${filename}</strong>
                <div style="font-size: 12px; color: #666; margin-top: 5px;">
                    命令: ${command}
                </div>
                <div style="font-size: 11px; color: #999; margin-top: 3px;">
                    等待Agent处理结果...
                </div>
            </div>
        `;
        
        this.resultsContent.innerHTML += entryHtml;
    }
    
    showError(message) {
        alert(message);
    }
    
    getFileExtension(filename) {
        return filename.slice(filename.lastIndexOf('.')).toLowerCase();
    }
    
    getFileIcon(ext) {
        const icons = {
            '.xlsx': '📊', '.xls': '📊',
            '.docx': '📝', '.doc': '📝', 
            '.pptx': '📽️', '.ppt': '📽️',
            '.txt': '📄'
        };
        return icons[ext] || '📄';
    }
}

// 初始化拖放区
document.addEventListener('DOMContentLoaded', function() {
    if (document.getElementById('officeDropZone')) {
        new OfficeDropZone();
        console.log('Office拖放区初始化完成');
    }
});

// 样式动画
const style = document.createElement('style');
style.textContent = `
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    
    .drop-zone:hover {
        border-color: #2196F3 !important;
        transform: translateY(-2px);
        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
    }
`;
document.head.appendChild(style);
</script>
