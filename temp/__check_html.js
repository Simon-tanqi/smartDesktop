
		// ============================
		// 1. 配置与状态
		// ============================
		const CURRENT_TIME = new Date().toISOString().slice(0, 10);
		
		let currentSessionId = null;
		const sessions = {};
		// 问题4：每个会话独立的 AbortController，支持多会话并行
		const sessionAbortControllers = {};  // sessionId → AbortController
		let contextMenuTargetId = null;
		// 问题6：自动滚动意图跟踪
		let userScrolledUp = false;
		// 文件上传：待发送附件列表
		let pendingFiles = [];  // [{name, path}, ...]
		// 全局状态：是否有任何会话正在运行
		let hasActiveSession = false;
		let activeSessionId = null;

		// ============================
		// 2. DOM 元素
		// ============================
		const newChatBtn = document.getElementById('newChatBtn');
		const sessionListEl = document.getElementById('sessionList');
		const chatContainer = document.getElementById('chatArea');
		const messageInput = document.getElementById('messageInput');
		const sendButton = document.getElementById('sendButton');
		const typingIndicator = document.getElementById('typingIndicator');
		const pulseSpinner = document.getElementById('pulseSpinner');
		const slowHint = document.getElementById('slowHint');
		const toolProgress = document.getElementById('toolProgress');
		const tpName = document.getElementById('tpName');
		const modelSelect = document.getElementById('modelSelect');
		const sessionContextMenu = document.getElementById('sessionContextMenu');

		// 全局点击：隐藏右键菜单
		document.addEventListener('click', () => {
			sessionContextMenu.style.display = 'none';
		});
		// 右键菜单动作处理
		sessionContextMenu.addEventListener('click', (e) => {
			const menuItem = e.target.closest('.menu-item');
			if (!menuItem || !contextMenuTargetId) return;
			const action = menuItem.dataset.action;
			if (action === 'rename') {
				// 触发该会话项的编辑模式
				const li = document.querySelector(`.session-item[data-id="${contextMenuTargetId}"]`);
				if (li) {
					const titleSpan = li.querySelector('.session-title');
					const editInput = li.querySelector('.session-title-edit');
					if (titleSpan && editInput) {
						editInput.style.display = 'inline-block';
						titleSpan.style.display = 'none';
						editInput.value = sessions[contextMenuTargetId].title;
						editInput.focus();
						editInput.select();
					}
				}
			}
			sessionContextMenu.style.display = 'none';
		});

		// 问题6：滚动意图检测 — 用户手动上滚后不再自动追底
		chatContainer.addEventListener('scroll', () => {
			const atBottom = chatContainer.scrollHeight - chatContainer.scrollTop <= 
			                 chatContainer.clientHeight + 50;
			userScrolledUp = !atBottom;
		});
		function autoScrollToBottom() {
			if (!userScrolledUp) {
				chatContainer.scrollTop = chatContainer.scrollHeight;
			}
		}

		// 问题3+4：取消当前请求
	async function cancelCurrentRequest() {
		const ctrl = sessionAbortControllers[currentSessionId];
		if (ctrl) {
			ctrl.abort();
			delete sessionAbortControllers[currentSessionId];
		}
		// 发送明确的取消请求到后端
		if (currentSessionId) {
			try {
				await fetch('/api/cancel', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ session_id: currentSessionId })
				});
			} catch (e) {
				console.log('取消请求发送失败:', e);
			}
		}
		pulseSpinner.style.display = 'none';
		slowHint.style.display = 'none';
		typingIndicator.style.display = 'none';
	}

		// ============================
		// 3. 辅助函数
		// ============================
		function formatTime(date) {
			if (!(date instanceof Date)) date = new Date(date);
			if (isNaN(date.getTime())) date = new Date();
			const now = new Date();
			const hh = date.getHours().toString().padStart(2, '0');
			const mm = date.getMinutes().toString().padStart(2, '0');
			const ss = date.getSeconds().toString().padStart(2, '0');
			// 跨天显示年月日前缀
			if (date.toDateString() !== now.toDateString()) {
				const y = date.getFullYear();
				const mo = (date.getMonth() + 1).toString().padStart(2, '0');
				const d = date.getDate().toString().padStart(2, '0');
				return `${y}-${mo}-${d} ${hh}:${mm}:${ss}`;
			}
			return `${hh}:${mm}:${ss}`;
		}

		function cleanAIResponse(text) {
			let cleaned = text || '';
			cleaned = cleaned.replace(/```reasoning\s*[\s\S]*?\s*```/g, '').trim();
			cleaned = cleaned.replace(/```action\s*[\s\S]*?\s*```/g, '').trim();
			cleaned = cleaned.replace(/```json\s*[\s\S]*?\s*```/g, '').trim();
			return cleaned;
		}

		function appendMessage(sender, text, msgType = 'chat', timestamp = null, unsaved = false) {
			if (msgType === 'think') {
				const block = document.createElement('div');
				block.className = 'think-block';
				const cleanedText = cleanAIResponse(text);
				const escaped = escapeHtml(cleanedText).replace(/\n/g, '<br>');
				block.innerHTML = `
					<div class="think-header" onclick="this.parentElement.classList.toggle('open')">
						<span class="think-arrow">&#9654;</span>
						<span>AI 思考过程</span>
					</div>
					<div class="think-body">${escaped}</div>
				`;
				chatContainer.insertBefore(block, typingIndicator);
				chatContainer.scrollTop = chatContainer.scrollHeight;
				return;
			}
			const msgDiv = document.createElement('div');
			msgDiv.className = `message ${sender}-message`;
			const bubble = document.createElement('div');
			bubble.className = 'bubble';
			let displayText = text;
			if (sender === 'ai') {
				displayText = cleanAIResponse(text);
			}
			bubble.innerHTML = renderMarkdown(displayText);
			const timeSpan = document.createElement('div');
			timeSpan.className = 'message-time';
			timeSpan.textContent = formatTime(timestamp || new Date());
			bubble.appendChild(timeSpan);
			const avatar = document.createElement('div');
			avatar.className = sender === 'user' ? 'avatar user-avatar' : 'avatar ai-avatar';
			avatar.textContent = sender === 'user' ? '我' : 'AI';
			if (sender === 'user') {
				msgDiv.appendChild(bubble);
				msgDiv.appendChild(avatar);
			} else {
				msgDiv.appendChild(avatar);
				msgDiv.appendChild(bubble);
				// 未持久化标记：仅实时 SSE 消息标记，历史加载消息不标记
				if (unsaved) {
					msgDiv.dataset.unsaved = 'true';
					const badge = document.createElement('span');
					badge.className = 'unsaved-badge';
					badge.textContent = '数据异常';
					bubble.appendChild(badge);
				}
			}
			chatContainer.insertBefore(msgDiv, typingIndicator);
			chatContainer.scrollTop = chatContainer.scrollHeight;
		}

		function escapeHtml(str) {
			const div = document.createElement('div');
			div.textContent = str;
			return div.innerHTML;
		}

		function renderMarkdown(text) {
			// 先提取图片语法，用占位符保护，避免被 escapeHtml 破坏
			const images = [];
			let processed = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, url) => {
				images.push(`<img src="${url}" alt="${alt}" style="max-width:100%;border-radius:8px;margin:8px 0">`);
				return `\uE000IMG_${images.length - 1}\uE000`;
			});
			let html = escapeHtml(processed);
			html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
			// 还原图片（U+E000 是 Unicode 私用区，escapeHtml 不处理）
			html = html.replace(/\uE000IMG_(\d+)\uE000/g, (match, idx) => images[idx]);
			return html;
		}

		function generateUUID() {
			// crypto.randomUUID polyfill for older browsers
			if (typeof crypto !== 'undefined' && crypto.randomUUID) {
				return crypto.randomUUID();
			}
			return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
				const r = Math.random() * 16 | 0;
				return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
			});
		}

		function clearChatArea() {
			chatContainer.innerHTML = '';
			chatContainer.appendChild(typingIndicator);
			chatContainer.appendChild(pulseSpinner);
			chatContainer.appendChild(slowHint);
			chatContainer.appendChild(toolProgress);
			typingIndicator.style.display = 'none';
			pulseSpinner.style.display = 'none';
			slowHint.style.display = 'none';
			toolProgress.style.display = 'none';
		}

		function renderChatHistory(messages) {
			clearChatArea();
			// 去重：同内容的 AI 回复（如旧版因 msg_type 不一致导致的 text/chat 双记录）只保留一条
			const seenKeys = new Set();
			const unique = [];
			for (let i = 0; i < messages.length; i++) {
				const m = messages[i];
				// 仅对 AI 消息做内容级去重，防止同内容重复渲染
				if (m.sender === 'ai' && m.text) {
					const key = m.text.trim();
					if (seenKeys.has(key)) {
						continue;  // 跳过重复的 AI 回复
					}
					seenKeys.add(key);
				}
				unique.push(m);
			}
			
			const groups = [];
			let currentGroup = { user: null, thinks: [], replies: [] };
			
			for (let i = 0; i < unique.length; i++) {
				const msg = unique[i];
				if ((msg.msgType === 'chat' || msg.msgType === 'text') && msg.sender === 'user') {
					if (currentGroup.user || currentGroup.thinks.length > 0 || currentGroup.replies.length > 0) {
						groups.push(currentGroup);
					}
					currentGroup = { user: msg, thinks: [], replies: [] };
				} else if (msg.msgType === 'think') {
					currentGroup.thinks.push(msg);
				} else if ((msg.msgType === 'chat' || msg.msgType === 'text' || msg.msgType === 'final_answer') && msg.sender === 'ai') {
					currentGroup.replies.push(msg);
				}
			}
			if (currentGroup.user || currentGroup.thinks.length > 0 || currentGroup.replies.length > 0) {
				groups.push(currentGroup);
			}
			
			for (let g = 0; g < groups.length; g++) {
				const group = groups[g];
				if (group.user) {
					appendMessage(group.user.sender, group.user.text, group.user.msgType, group.user.timestamp);
				}
				if (group.thinks.length > 0) {
					let combinedThink = '';
					for (let t = 0; t < group.thinks.length; t++) {
						const thinkText = group.thinks[t].text.trim();
						if (thinkText && !combinedThink.includes(thinkText)) {
							if (combinedThink) combinedThink += '\n';
							combinedThink += thinkText;
						}
					}
					if (combinedThink) {
						const firstThinkTs = group.thinks[0].timestamp;
						appendMessage('ai', combinedThink, 'think', firstThinkTs);
					}
				}
				for (let r = 0; r < group.replies.length; r++) {
					const reply = group.replies[r];
					appendMessage(reply.sender, reply.text, reply.msgType, reply.timestamp);
				}
			}
		}

		function renderSessionList() {
			sessionListEl.innerHTML = '';
			// 排序显示会话
			Object.values(sessions)
				.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt))
				.forEach(session => {
					const li = document.createElement('li');
					li.className = session.id === currentSessionId ? 'session-item active' : 'session-item';
					li.dataset.id = session.id;
					li.innerHTML = `
						<span class="session-title" title="${session.title}" data-id="${session.id}">${session.title}</span>
						<input class="session-title-edit" data-id="${session.id}" maxlength="30">
						<span class="session-time">${formatTime(session.createdAt)}</span>
						<span class="session-delete" title="删除会话" style="margin-left:auto;cursor:pointer;color:#e74c3c;font-size:14px;">&#10005;</span>
					`;

					// 双击标题 → 进入编辑模式
					const titleSpan = li.querySelector('.session-title');
					const editInput = li.querySelector('.session-title-edit');
					titleSpan.addEventListener('dblclick', () => {
						editInput.style.display = 'inline-block';
						titleSpan.style.display = 'none';
						editInput.value = sessions[session.id].title;
						editInput.focus();
						editInput.select();
					});
					editInput.addEventListener('blur', () => finishEdit(session.id, editInput, titleSpan));
					editInput.addEventListener('keydown', (e) => {
						if (e.key === 'Enter') finishEdit(session.id, editInput, titleSpan);
						if (e.key === 'Escape') cancelEdit(editInput, titleSpan);
					});

					li.addEventListener('click', (e) => {
						if (e.target.classList.contains('session-delete')) return;
						if (e.target.classList.contains('session-title-edit')) return;
						switchSession(session.id);
					});
					li.querySelector('.session-delete').addEventListener('click', (e) => {
						e.stopPropagation();
						deleteSessionById(session.id);
					});
					li.addEventListener('contextmenu', (e) => {
						e.preventDefault();
						contextMenuTargetId = session.id;
						sessionContextMenu.style.display = 'block';
						sessionContextMenu.style.left = e.clientX + 'px';
						sessionContextMenu.style.top = e.clientY + 'px';
					});
					sessionListEl.appendChild(li);
				});
		}

		async function finishEdit(sessionId, input, titleSpan) {
			const newTitle = input.value.trim();
			input.style.display = 'none';
			titleSpan.style.display = '';
			if (!newTitle || newTitle === sessions[sessionId].title) return;
			try {
				const res = await fetch(`/api/sessions/${sessionId}/rename`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ title: newTitle })
				});
				const data = await res.json();
				if (data.success) {
					sessions[sessionId].title = newTitle;
					titleSpan.textContent = newTitle;
					titleSpan.title = newTitle;
				}
			} catch (e) {}
		}

		function cancelEdit(input, titleSpan) {
			input.style.display = 'none';
			titleSpan.style.display = '';
		}

		function generateSessionId() {
			const now = new Date();
			const dateStr = now.getFullYear().toString() + 
						   (now.getMonth() + 1).toString().padStart(2, '0') + 
						   now.getDate().toString().padStart(2, '0');
			const timeStr = now.getHours().toString().padStart(2, '0') + 
						   now.getMinutes().toString().padStart(2, '0');
			return `${dateStr}_${timeStr}`;
		}

		// ============================
		// 4. 会话操作
		// ============================
		async function loadSessions() {
			try {
				const [activeRes, listRes] = await Promise.all([
					fetch('/api/sessions/active'),
					fetch('/api/sessions')
				]);
				const activeData = await activeRes.json();
				const activeId = activeData.session_id;
				const sessionList = await listRes.json();

				// 清空本地 sessions 并重新填充
				for (let key in sessions) delete sessions[key];
				for (let sess of sessionList) {
					sessions[sess.id] = {
						id: sess.id,
						title: sess.title || '未命名',
						messages: [],
						createdAt: sess.created_at ? new Date(sess.created_at) : new Date()
					};
				}

				renderSessionList();

				if (activeId && sessions[activeId]) {
					currentSessionId = activeId;
					await switchSession(activeId);
				} else if (Object.keys(sessions).length > 0) {
					const firstId = Object.keys(sessions)[0];
					currentSessionId = firstId;
					await switchSession(firstId);
				} else {
					currentSessionId = null;
				}
			} catch (error) {
				console.error("加载会话失败:", error);
				// 如果加载失败，创建一个新会话
				if (!currentSessionId) {
					await createNewSession();
				}
			}
		}

		async function initSession() {
			const res = await fetch('/api/sessions/active');
			const data = await res.json();
			currentSessionId = data.session_id;
		}
		async function switchSession(sessionId) {
			// 问题4：如果当前会话有正在运行的请求，弹窗确认后再切换
			if (sessionAbortControllers[currentSessionId]) {
				const confirmed = confirm(
					'当前会话正在生成回复中。\n\n切换后该回复将在后台继续完成，你可以随时切回来查看。\n\n是否切换到新会话？'
				);
				if (!confirmed) return;
				// 不中止旧请求，保留 sessionAbortControllers[currentSessionId]
			}
			try {
				const res = await fetch(`/api/sessions/${sessionId}`);
				const data = await res.json();
				if (!data.success) throw new Error(data.message);
				currentSessionId = sessionId;
				if (!sessions[sessionId]) {
					sessions[sessionId] = { id: sessionId, title: '会话', messages: [], createdAt: new Date() };
				}
				sessions[sessionId].messages = data.messages || [];
				renderSessionList();
				renderChatHistory(sessions[sessionId].messages);
				// 切换后重置 UI 状态
				userScrolledUp = false;
				pulseSpinner.style.display = 'none';
				slowHint.style.display = 'none';
				typingIndicator.style.display = 'none';
			} catch (error) {
				console.error("切换会话失败:", error);
				if (error.message && !error.message.includes('429') && !error.message.includes('Too Many Requests')) {
					appendMessage('ai', `加载会话失败：${error.message}`);
				} else {
					console.warn("会话加载被限流，自动重试中...");
					setTimeout(() => switchSession(sessionId), 2000);
				}
			}
		}

		async function saveSessionToBackend(sessionId) {
			try {
				const sessionData = sessions[sessionId];
				const allMessages = sessionData.messages.filter(m => m.text && m.text.trim());
				await fetch('/api/sessions', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						id: sessionData.id,
						title: sessionData.title,
						messages: allMessages,
						created_at: sessionData.createdAt.toISOString()
					})
				});
				// 持久化成功，清除当前会话所有"数据异常"标记
				document.querySelectorAll('[data-unsaved="true"]').forEach(el => {
					delete el.dataset.unsaved;
					const badge = el.querySelector('.unsaved-badge');
					if (badge) badge.remove();
				});
			} catch (error) {
				console.error('保存会话失败:', error);
			}
		}

		async function createNewSession() {
			// 问题4：如果当前会话有正在运行的请求，弹窗确认后再创建新会话
			if (sessionAbortControllers[currentSessionId]) {
				const confirmed = confirm(
					'当前会话正在生成回复中。\n\n创建新会话后，原会话回复将在后台继续完成。\n\n是否创建新会话？'
				);
				if (!confirmed) return;
			}
			try {
				const res = await fetch('/api/sessions/new', { method: 'POST' });
				const data = await res.json();
				if (!data.success || !data.session) throw new Error(data.message || '创建失败');
				const sess = data.session;
				sessions[sess.id] = {
					id: sess.id,
					title: sess.title || sess.id,
					messages: [],
					createdAt: sess.created_at ? new Date(sess.created_at) : new Date()
				};
				currentSessionId = sess.id;
				renderSessionList();
				clearChatArea();
				appendMessage('system', `会话 [${sess.title || sess.id}] 已启动`);
				return sess.id;
			} catch (e) {
				// 兜底：本地创建
				const sessionId = generateSessionId();
				const sessionTitle = `新对话`;
				sessions[sessionId] = {
					id: sessionId,
					title: sessionTitle,
					messages: [],
					createdAt: new Date()
				};
				currentSessionId = sessionId;
				renderSessionList();
				clearChatArea();
				appendMessage('system', `会话 [${sessionTitle}] 已启动`);
				await saveSessionToBackend(sessionId);
				return sessionId;
			}
		}

		async function deleteSessionById(sessionId) {
			if (!confirm('确定要删除此会话吗？')) return;
			try {
				const res = await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
				const data = await res.json();
				if (!data.success) throw new Error(data.message);
				delete sessions[sessionId];
				if (currentSessionId === sessionId) {
					const keys = Object.keys(sessions);
					if (keys.length > 0) {
						currentSessionId = keys[0];
						await switchSession(keys[0]);
					} else {
						currentSessionId = null;
						clearChatArea();
					}
				}
				renderSessionList();
			} catch (error) {
				console.error('删除会话失败:', error);
				appendMessage('ai', `删除会话失败：${error.message}`);
			}
		}

		// ============================
		// 5. 发送消息
		// ============================
		let isSending = false;

		// 工具执行进度管理（CSS 动画驱动，无需 JS 计时器）
		function resetToolProgress() {
			toolProgress.style.display = 'none';
		}

		// ============================
		// 5.5 文件上传
		// ============================
		function renderFileChips() {
			const container = document.getElementById('fileChips');
			container.innerHTML = '';
			pendingFiles.forEach((f, i) => {
				const chip = document.createElement('span');
				chip.className = 'file-chip';
				chip.innerHTML = `<span title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
					<span class="chip-remove" data-index="${i}">&times;</span>`;
				container.appendChild(chip);
			});
			container.querySelectorAll('.chip-remove').forEach(el => {
				el.addEventListener('click', (e) => {
					const idx = parseInt(el.dataset.index);
					pendingFiles.splice(idx, 1);
					renderFileChips();
				});
			});
		}

		function escapeHtml(str) {
			const d = document.createElement('div');
			d.textContent = str;
			return d.innerHTML;
		}

		async function uploadFiles(files) {
			if (!currentSessionId) {
				alert('请先选择或创建会话');
				return;
			}
			for (const file of files) {
				// 去重
				if (pendingFiles.some(f => f.name === file.name && f.size === file.size)) continue;
				const formData = new FormData();
				formData.append('file', file);
				formData.append('session_id', currentSessionId);
				try {
					const resp = await fetch('/api/upload', { method: 'POST', body: formData });
					const data = await resp.json();
					if (data.success) {
						pendingFiles.push({ name: data.file_name, path: data.file_path });
					} else {
						appendMessage('ai', `上传失败: ${data.message}`);
					}
				} catch (e) {
					appendMessage('ai', `上传错误: ${e.message}`);
				}
			}
			renderFileChips();
		}

		function removeAllFiles() {
			pendingFiles = [];
			renderFileChips();
		}

		async function sendMessage() {
			const text = messageInput.value.trim();
			console.log('[DEBUG] sendMessage called, text:', text, 'currentSessionId:', currentSessionId, 'isSending:', isSending);
			if ((!text && pendingFiles.length === 0) || !currentSessionId || isSending) {
				console.log('[DEBUG] sendMessage returning early');
				return;
			}

			if (hasActiveSession && activeSessionId !== currentSessionId) {
				alert(`会话 "${activeSessionId}" 正在运行中，请等待它完成后再发送新消息。`);
				return;
			}

			// 立即清空输入框，按钮切换为"停止"状态（允许点击中断）
			isSending = true;
			sendButton.textContent = '停止';
			sendButton.classList.add('sending');
			sendButton.disabled = false;
			
			hasActiveSession = true;
			activeSessionId = currentSessionId;
			messageInput.value = '';
			// 注入附件块
			let finalMessage = text;
			if (pendingFiles.length > 0) {
				let attachBlock = '\n<attachments>';
				for (const f of pendingFiles) {
					attachBlock += `\n- [${f.name}](<${f.path}>)`;
				}
				attachBlock += '\n</attachments>';
				finalMessage = text + attachBlock;
			}
			const sentFiles = [...pendingFiles];
			pendingFiles = [];
			renderFileChips();
			// 问题4：捕获请求时的会话 ID，SSE 事件处理器全部用此值
			const requestSessionId = currentSessionId;

			try {
			const userTimestamp = new Date().toISOString();
			const msgId = generateUUID();
			appendMessage('user', finalMessage, 'chat', userTimestamp);
			if (sessions[requestSessionId]) {
				sessions[requestSessionId].messages.push({
					sender: 'user',
					text: finalMessage,
					msgType: 'chat',
					timestamp: userTimestamp,
					msgId: msgId
				});
			}
			// 用户消息已由 api_chat 后端持久化，删除冗余调用

			// 问题3：显示脉冲加载动画（替代纯文字 typing indicator）
			typingIndicator.style.display = 'none';
			pulseSpinner.style.display = 'flex';
			slowHint.style.display = 'none';
			userScrolledUp = false;  // 新消息重置滚动意图

			// 问题3：3 秒后首条 SSE 未到达时显示超时提示
			const slowTimer = setTimeout(() => {
				if (pulseSpinner.style.display !== 'none') {
					slowHint.style.display = 'flex';
				}
			}, 3000);
			
			let timeoutTimer = null;
			// 防重复渲染：收到首个 final 后忽略后续 final/兜底事件
			let finalReceived = false;

			let hasThinking = false;
			let thinkContent = '';
			let currentThinkBlockId = 'think-' + generateUUID();

			try {
				const modelValue = modelSelect.value;
				let modelChoice = null;
				let modelId = null;
				if (modelValue) {
					try {
						const parsed = JSON.parse(modelValue);
						modelChoice = { display: parsed.display, provider: parsed.provider, model: parsed.model, id: parsed.id, base_url: parsed.base_url };
						modelId = parsed.id || null;
						// 防御：如果选项中缺 provider/model（Stale page），记警告并由后端补全
						if (!modelChoice.provider || !modelChoice.model) {
							console.warn('[MODEL] 下拉选项缺 provider/model，将由后端补全：', modelChoice);
						}
					} catch(e) {
						console.warn('[MODEL] modelSelect.value 不是合法 JSON：', modelValue, e);
						modelChoice = { display: modelValue };
					}
				}

				// 问题4：每会话独立的 AbortController
				sessionAbortControllers[requestSessionId] = new AbortController();
				const abortCtrl = sessionAbortControllers[requestSessionId];
				
				// 全局请求超时：600秒没有任何响应则自动取消（包含工具执行和确认等待时间）
				timeoutTimer = setTimeout(() => {
					if (abortCtrl.signal.aborted) return;
					abortCtrl.abort();
					console.warn('[TIMEOUT] Request timed out after 600s');
					pulseSpinner.style.display = 'none';
					slowHint.style.display = 'none';
					typingIndicator.style.display = 'none';
					appendMessage('ai', '错误: 请求超时（600s 无响应），请检查 API Key 配置或网络连接');
				}, 600000);
				
				console.log('[DEBUG] About to fetch /api/chat with data:', {
					message: finalMessage,
					session_id: requestSessionId,
					model_choice: modelChoice,
					model_id: modelId
				});
				console.log('[DEBUG] fetch /api/chat called');
				const response = await fetch('/api/chat', {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						message: finalMessage,
						session_id: requestSessionId,
						model_choice: modelChoice,
						model_id: modelId,
						msg_id: msgId
					}),
					signal: abortCtrl.signal,
					keepalive: true
				});

				console.log('[DEBUG] fetch /api/chat response received, status:', response.status, 'content-type:', response.headers.get('Content-Type'));

				// 非 SSE 响应（如 JSON 错误），直接展示错误信息
				const contentType = response.headers.get('Content-Type') || '';
				if (!contentType.includes('text/event-stream')) {
					clearTimeout(slowTimer);
					clearTimeout(timeoutTimer);
					pulseSpinner.style.display = 'none';
					typingIndicator.style.display = 'none';
					let errMsg = '未知错误';
					try {
						const errData = await response.json();
						errMsg = errData.message || JSON.stringify(errData);
					} catch(e) {}
					appendMessage('ai', '错误: ' + errMsg);
					return;
				}

				const reader = response.body.getReader();
				const decoder = new TextDecoder();
				let buffer = '';

				while (true) {
					const { done, value } = await reader.read();
					if (done) break;

					buffer += decoder.decode(value, { stream: true });
					const lines = buffer.split('\n');
					buffer = lines.pop() || '';

					for (const line of lines) {
						if (!line.startsWith('data: ')) continue;
						try {
							const data = JSON.parse(line.slice(6));
							console.log('[SSE] received:', data.type, data);
							switch (data.type) {
								case 'heartbeat':
									console.log('[SSE] heartbeat received, hasThinking:', hasThinking, 'status:', data.status);
									clearTimeout(slowTimer);
									clearTimeout(timeoutTimer);
									break;
								case 'reasoning':
									clearTimeout(slowTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									hasThinking = true;
									thinkContent += data.text;
									let existingThinkBlock = document.getElementById(currentThinkBlockId);
									if (existingThinkBlock) {
										const thinkBody = existingThinkBlock.querySelector('.think-body');
										if (thinkBody) {
											thinkBody.innerHTML = escapeHtml(thinkContent).replace(/\n/g, '<br>');
											existingThinkBlock.classList.add('open');
										}
									} else {
										const block = document.createElement('div');
										block.id = currentThinkBlockId;
										block.className = 'think-block open';
										const escaped = escapeHtml(thinkContent).replace(/\n/g, '<br>');
										block.innerHTML = `
											<div class="think-header" onclick="this.parentElement.classList.toggle('open')">
												<span class="think-arrow">&#9654;</span>
												<span>AI 思考过程</span>
											</div>
											<div class="think-body">${escaped}</div>
										`;
										chatContainer.insertBefore(block, typingIndicator);
									}
									autoScrollToBottom();
									break;
								case 'step':
									clearTimeout(slowTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									hasThinking = true;
									const stepText = '\n【' + data.status + '】' + data.text;
									thinkContent += stepText;
									
									let stepThinkBlock = document.getElementById(currentThinkBlockId);
									if (stepThinkBlock) {
										const stepThinkBody = stepThinkBlock.querySelector('.think-body');
										if (stepThinkBody) {
											stepThinkBody.innerHTML = escapeHtml(thinkContent).replace(/\n/g, '<br>');
											stepThinkBlock.classList.add('open');
										}
									} else {
										const block = document.createElement('div');
										block.id = currentThinkBlockId;
										block.className = 'think-block open';
										const escaped = escapeHtml(thinkContent).replace(/\n/g, '<br>');
										block.innerHTML = `
											<div class="think-header" onclick="this.parentElement.classList.toggle('open')">
												<span class="think-arrow">&#9654;</span>
												<span>AI 思考过程</span>
											</div>
											<div class="think-body">${escaped}</div>
										`;
										chatContainer.insertBefore(block, typingIndicator);
									}
									
									// 工具执行进度（CSS 不定长动画，不依赖超时预测）
									if (data.tool_name) {
										tpName.textContent = data.tool_name;
										toolProgress.style.display = 'block';
									}
									autoScrollToBottom();
									break;
								case 'confirm':
									clearTimeout(slowTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									resetToolProgress();
									hasThinking = true;
									thinkContent += '\n⚠️ 等待用户确认操作...';
									{
										const token = data.confirmation_token;
										const challengePhrase = data.challenge_phrase;
										const isHighRisk = !!challengePhrase;
										
										const card = document.createElement('div');
										card.className = 'confirm-card';
										card.id = 'confirm-' + token;
										const title = data.title || (isHighRisk ? '🔴 高危操作需要授权' : '⚠️ 操作需要确认');
										const desc = data.message || (isHighRisk ? '此操作存在较高风险，请完整输入授权文字以确认执行。' : '请确认是否执行此操作。');
										const isDownload = data.tool_name === 'web_download';
										const btnText = isDownload ? '确认下载' : (isHighRisk ? '确认授权' : '确认执行');
										let bodyHtml = '';
										if (data.details && Object.keys(data.details).length > 0) {
											bodyHtml = '<div class="confirm-details">';
											for (const [key, val] of Object.entries(data.details)) {
												bodyHtml += `<div class="confirm-detail-row"><span class="confirm-detail-key">${escapeHtml(key)}</span><span class="confirm-detail-val">${escapeHtml(String(val))}</span></div>`;
											}
											bodyHtml += '</div>';
										} else if (data.command) {
											bodyHtml = `<div class="confirm-cmd">${escapeHtml(data.command)}</div>`;
										}
										const challengeHtml = isHighRisk ? `
											<div class="confirm-challenge-box">
												<div class="challenge-label">🔴 请输入授权文字确认此高危操作</div>
												<div class="challenge-phrase">${escapeHtml(challengePhrase)}</div>
												<input type="text" id="challenge-${token}" class="challenge-input" placeholder="在此输入授权文字..." autocomplete="off">
											</div>` : '';
										card.innerHTML = `
											<div class="confirm-header">${escapeHtml(title)}</div>
											<div class="confirm-desc">${escapeHtml(desc)}</div>
											${bodyHtml}
											${challengeHtml}
											<div class="confirm-actions">
												<button class="btn-confirm-ok" data-action="confirm">${btnText}</button>
												<button class="btn-confirm-cancel" data-action="cancel">取消</button>
											</div>
										`;
										
										const msgDiv = document.createElement('div');
										msgDiv.className = 'message ai-message';
										const bubble = document.createElement('div');
										bubble.className = 'bubble';
										bubble.appendChild(card);
										const avatar = document.createElement('div');
										avatar.className = 'avatar ai-avatar';
										avatar.textContent = 'AI';
										msgDiv.appendChild(avatar);
										msgDiv.appendChild(bubble);
										chatContainer.insertBefore(msgDiv, typingIndicator);
										autoScrollToBottom();
										
										card.querySelector('.btn-confirm-ok').addEventListener('click', async () => {
											if (isHighRisk) {
												const challengeInput = document.getElementById('challenge-' + token);
												const challengeResp = challengeInput ? challengeInput.value.trim() : '';
												if (!challengeResp) {
													challengeInput.classList.add('error');
													challengeInput.placeholder = '请输入授权文字';
													return;
												}
												if (challengeResp !== challengePhrase) {
													challengeInput.classList.add('error');
													challengeInput.value = '';
													challengeInput.placeholder = '授权文字不匹配，请重新输入';
													return;
												}
											}
											const okBtn = card.querySelector('.btn-confirm-ok');
											okBtn.disabled = true;
											okBtn.textContent = '正在执行...';
											okBtn.style.background = '#ccc';
											const challengeInput = document.getElementById('challenge-' + token);
											const challengeResp = challengeInput ? challengeInput.value.trim() : '';
											const cancelBtn = card.querySelector('.btn-confirm-cancel');
											cancelBtn.disabled = true;
											await fetch('/api/confirm', {
												method: 'POST',
												headers: { 'Content-Type': 'application/json' },
												body: JSON.stringify({ confirmation_token: token, confirmed: true, challenge_response: challengeResp })
											});
											thinkContent += `\n✅ 用户已确认${isHighRisk ? '授权' : ''}`;
											msgDiv.remove();
										});
										card.querySelector('.btn-confirm-cancel').addEventListener('click', async () => {
											const cancelBtn = card.querySelector('.btn-confirm-cancel');
											cancelBtn.disabled = true;
											cancelBtn.textContent = '已取消';
											cancelBtn.style.background = '#e57373';
											cancelBtn.style.color = '#fff';
											await fetch('/api/confirm', {
												method: 'POST',
												headers: { 'Content-Type': 'application/json' },
												body: JSON.stringify({ confirmation_token: token, confirmed: false })
											});
											msgDiv.remove();
										});
									}
									break;
								case 'tool_call':
									clearTimeout(slowTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									hasThinking = true;
									thinkContent += `\n🔧 调用工具: ${data.tool_name}`;
									tpName.textContent = data.tool_name;
									toolProgress.style.display = 'block';
									autoScrollToBottom();
									break;
							case 'tool_result':
									clearTimeout(slowTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									hasThinking = true;
									toolProgress.style.display = 'none';
									const toolCallPrefix = `\n🔧 调用工具: ${data.tool_name}`;
									const toolResultPrefix = data.status === 'success' ? '✅' : '❌';
									const toolResultMsg = data.error ? ` (${data.error})` : '';
									if (thinkContent.includes(toolCallPrefix)) {
										thinkContent = thinkContent.replace(toolCallPrefix, `\n${toolResultPrefix} 调用工具: ${data.tool_name} ${data.status}${toolResultMsg}`);
									} else {
										thinkContent += `\n${toolResultPrefix} ${data.tool_name}: ${data.status}${toolResultMsg}`;
									}
									autoScrollToBottom();
									break;
							case 'final':
									// 首个 final 到达后忽略后续 final（后端偶发双 final 时避免重复渲染/重复兜底）
									if (finalReceived) break;
									finalReceived = true;
									clearTimeout(slowTimer);
									clearTimeout(timeoutTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									typingIndicator.style.display = 'none';
									resetToolProgress();
									if (hasThinking && thinkContent.trim()) {
										const existingThink = sessions[requestSessionId].messages.find(m => m.msgType === 'think' && m.text === thinkContent);
										if (!existingThink) {
											const thinkMsg = { sender: 'ai', text: thinkContent, msgType: 'think', timestamp: data.timestamp, msgId: generateUUID() };
											const lastUserMsgIndex = sessions[requestSessionId].messages.findLastIndex(m => m.sender === 'user');
											if (lastUserMsgIndex !== -1) {
												sessions[requestSessionId].messages.splice(lastUserMsgIndex + 1, 0, thinkMsg);
											} else {
												sessions[requestSessionId].messages.push(thinkMsg);
											}
											const existingThinkBlock = document.getElementById(currentThinkBlockId);
											if (!existingThinkBlock) {
												appendMessage('ai', thinkContent, 'think', data.timestamp);
											} else {
												const thinkBody = existingThinkBlock.querySelector('.think-body');
												if (thinkBody) {
													thinkBody.innerHTML = escapeHtml(thinkContent).replace(/\n/g, '<br>');
												}
											}
										}
									}
									if (data.text && data.text.trim()) {
										appendMessage('ai', data.text, 'chat', data.timestamp, true);
										// 推入 sessions.messages，确保 refresh 后 AI 回复不丢失
										sessions[requestSessionId].messages.push({
											sender: 'ai', text: data.text, msgType: 'chat',
											timestamp: data.timestamp, msgId: generateUUID()
										});
									}
									autoScrollToBottom();
									break;
								case 'error':
									// 任务已完成（final 已收到）后忽略后续 error，避免"已完成后仍弹错误兜底"
									if (finalReceived) break;
									clearTimeout(slowTimer);
									clearTimeout(timeoutTimer);
									pulseSpinner.style.display = 'none';
									slowHint.style.display = 'none';
									typingIndicator.style.display = 'none';
									resetToolProgress();
									const errorText = '错误: ' + (data.text || '未知错误');
									const existingError = sessions[requestSessionId].messages.find(m => m.msgType === 'chat' && m.text === errorText);
									if (!existingError) {
										if (hasThinking && thinkContent.trim()) {
											const existingThink = sessions[requestSessionId].messages.find(m => m.msgType === 'think' && m.text === thinkContent);
											if (!existingThink) {
												const thinkMsg = { sender: 'ai', text: thinkContent, msgType: 'think', timestamp: new Date().toISOString(), msgId: generateUUID() };
												const lastUserMsgIndex = sessions[requestSessionId].messages.findLastIndex(m => m.sender === 'user');
												if (lastUserMsgIndex !== -1) {
													sessions[requestSessionId].messages.splice(lastUserMsgIndex + 1, 0, thinkMsg);
												} else {
													sessions[requestSessionId].messages.push(thinkMsg);
												}
												const existingThinkBlock = document.getElementById(currentThinkBlockId);
												if (!existingThinkBlock) {
													appendMessage('ai', thinkContent, 'think', new Date().toISOString());
												}
											}
										}
										appendMessage('ai', errorText, 'chat', null, true);
									}
									break;
							}
						} catch(e) {
							console.error('SSE 解析错误:', e, line);
						}
					}
				}

				// 异常结束处理
				clearTimeout(timeoutTimer);
				if (pulseSpinner.style.display !== 'none') {
					pulseSpinner.style.display = 'none';
				}
				if (slowHint.style.display !== 'none') {
					slowHint.style.display = 'none';
				}
				if (typingIndicator.style.display !== 'none') {
					typingIndicator.style.display = 'none';
				}
				if (hasThinking && thinkContent.trim()) {
					const existingThink = sessions[requestSessionId].messages.find(m => m.msgType === 'think' && m.text === thinkContent);
					if (!existingThink) {
						const thinkMsg = { sender: 'ai', text: thinkContent, msgType: 'think', timestamp: new Date().toISOString(), msgId: generateUUID() };
						const lastUserMsgIndex = sessions[requestSessionId].messages.findLastIndex(m => m.sender === 'user');
						if (lastUserMsgIndex !== -1) {
							sessions[requestSessionId].messages.splice(lastUserMsgIndex + 1, 0, thinkMsg);
						} else {
							sessions[requestSessionId].messages.push(thinkMsg);
						}
					}
				}
				saveSessionToBackend(requestSessionId);
			} catch (error) {
				clearTimeout(slowTimer);
				clearTimeout(timeoutTimer);
				pulseSpinner.style.display = 'none';
				slowHint.style.display = 'none';
				typingIndicator.style.display = 'none';
				resetToolProgress();
				if (error.name !== 'AbortError') {
					appendMessage('ai', '网络错误: ' + error.message, 'chat', null, true);
				}
				saveSessionToBackend(requestSessionId);
			}
		} catch (e) {
			pulseSpinner.style.display = 'none';
			slowHint.style.display = 'none';
			typingIndicator.style.display = 'none';
			resetToolProgress();
			appendMessage('ai', '发送失败: ' + (e.message || '未知错误'));
			console.error('sendMessage error:', e);
		} finally {
			// 问题4：清理该会话的 AbortController
			delete sessionAbortControllers[requestSessionId];
			isSending = false;
			sendButton.textContent = '发送';
			sendButton.classList.remove('sending');
			sendButton.disabled = false;
			
			hasActiveSession = false;
			activeSessionId = null;
		}
	}

		async function checkActiveSessions() {
			try {
				const response = await fetch('/api/session/status');
				const data = await response.json();
				if (data.success && data.has_active_sessions) {
					hasActiveSession = true;
					if (data.active_sessions && data.active_sessions.length > 0) {
						activeSessionId = data.active_sessions[0];
					}
				} else {
					hasActiveSession = false;
					activeSessionId = null;
				}
			} catch (e) {
				console.error('checkActiveSessions error:', e);
			}
		}

		// ============================
		// 6. 初始化（唯一监听器）
		// ============================
		window.addEventListener('DOMContentLoaded', async () => {
			console.log('[VERSION] 2026-07-11-12:25 — Ollama路由防御 + model_choice完整日志');
			// 加载模型列表
			try {
				const response = await fetch('/api/models');
				if (!response.ok) throw new Error(`HTTP ${response.status}`);
				const models = await response.json();
				modelSelect.innerHTML = '';
				models.forEach(item => {
					const option = document.createElement('option');
					option.value = JSON.stringify({ display: item.display, id: item.id || '', provider: item.provider, model: item.model, base_url: item.base_url });
					option.textContent = item.display || `${item.provider} - ${item.model}`;
					modelSelect.appendChild(option);
				});
				// 问题5：优先读取 localStorage 中的模型选择，没有则回退到 DeepSeek
				const savedModel = localStorage.getItem('selectedModel');
				if (savedModel) {
					const opt = Array.from(modelSelect.options).find(o => {
						try { return JSON.parse(o.value).display === savedModel; } catch(e) { return o.value === savedModel; }
					});
					if (opt) opt.selected = true;
				}
				if (!savedModel || !modelSelect.value) {
					const deepseekOpt = Array.from(modelSelect.options).find(opt => {
						try { return JSON.parse(opt.value).display?.includes('DeepSeek'); } catch(e) { return false; }
					});
					if (deepseekOpt) deepseekOpt.selected = true;
				}
				// 问题5：选中变更时写入 localStorage（后台服务不重启就一直用此模型）
				modelSelect.addEventListener('change', () => {
					try {
						const parsed = JSON.parse(modelSelect.value);
						localStorage.setItem('selectedModel', parsed.display);
					} catch(e) {
						localStorage.setItem('selectedModel', modelSelect.value);
					}
				});
			} catch (e) {
				modelSelect.innerHTML = '<option value="">无法加载模型</option>';
				console.error("加载模型失败:", e);
			}

			// 加载会话列表
			await loadSessions();
			console.log('[DEBUG] Sessions loaded, currentSessionId:', currentSessionId, 'sessions count:', Object.keys(sessions).length);
			if (!currentSessionId) {
				console.log('[DEBUG] No currentSessionId, creating new session');
				await createNewSession();
			}
			
			// 定期检查活跃会话状态
			checkActiveSessions();
			setInterval(checkActiveSessions, 5000);
			
			// 事件绑定
			newChatBtn.addEventListener('click', () => createNewSession());
			sendButton.addEventListener('click', () => {
				console.log('[DEBUG] sendButton clicked, isSending:', isSending, 'currentSessionId:', currentSessionId);
				if (isSending) {
					// 停止当前请求
					const ctrl = sessionAbortControllers[currentSessionId];
					if (ctrl) ctrl.abort();
				} else {
					sendMessage();
				}
			});
			messageInput.addEventListener('keypress', (e) => e.key === 'Enter' && sendMessage());

			// ── 文件上传事件 ──
			const fileInput = document.getElementById('fileInput');
			const uploadBtn = document.getElementById('uploadBtn');
			const inputArea = document.querySelector('.input-area');

			uploadBtn.addEventListener('click', () => fileInput.click());
			fileInput.addEventListener('change', () => {
				if (fileInput.files.length > 0) {
					uploadFiles(fileInput.files);
					fileInput.value = '';
				}
			});

			// 拖拽上传
			inputArea.addEventListener('dragover', (e) => {
				e.preventDefault();
				e.stopPropagation();
				inputArea.classList.add('drag-over');
			});
			inputArea.addEventListener('dragleave', (e) => {
				e.preventDefault();
				e.stopPropagation();
				inputArea.classList.remove('drag-over');
			});
			inputArea.addEventListener('drop', (e) => {
				e.preventDefault();
				e.stopPropagation();
				inputArea.classList.remove('drag-over');
				if (e.dataTransfer.files.length > 0) {
					uploadFiles(e.dataTransfer.files);
				}
			});

			// 剪贴板粘贴上传
			document.addEventListener('paste', (e) => {
				const items = e.clipboardData?.items;
				if (!items) return;
				const files = [];
				for (const item of items) {
					if (item.kind === 'file') {
						files.push(item.getAsFile());
					}
				}
				if (files.length > 0) {
					e.preventDefault();
					uploadFiles(files);
				}
			});

			// ═══════════════════════════════════════════
			// 设置面板 - 模型配置
			// ═══════════════════════════════════════════
			const settingsOverlay = document.getElementById('settingsOverlay');
			const settingsBtn = document.getElementById('settingsBtn');
			const settingsClose = document.getElementById('settingsClose');
			const modelList = document.getElementById('modelList');
			const modelForm = document.getElementById('modelForm');
			const addModelBtn = document.getElementById('addModelBtn');
			const modelFormCancel = document.getElementById('modelFormCancel');
			const modelFormSave = document.getElementById('modelFormSave');
			const modelFormTitle = document.getElementById('modelFormTitle');

			let modelsConfig = { models: [], active_model_id: '' };
			let editingModelIndex = -1;

			function openSettings() {
				settingsOverlay.classList.add('open');
				loadModelsConfig();
				loadUserProfile();
			}
			function closeSettings() {
				settingsOverlay.classList.remove('open');
				modelForm.style.display = 'none';
			}

			settingsBtn.addEventListener('click', openSettings);
			settingsClose.addEventListener('click', closeSettings);
			settingsOverlay.addEventListener('click', (e) => {
				if (e.target === settingsOverlay) closeSettings();
			});

			// Tab 切换
			document.querySelectorAll('.settings-tab').forEach(tab => {
				tab.addEventListener('click', () => {
					if (tab.classList.contains('disabled')) return;
					document.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
					document.querySelectorAll('.settings-section').forEach(s => s.classList.remove('active'));
					tab.classList.add('active');
					const sec = document.getElementById('tab-' + tab.dataset.tab);
					if (sec) sec.classList.add('active');
				});
			});

			async function loadModelsConfig() {
				try {
					const res = await fetch('/api/config/models');
					const data = await res.json();
					if (data.success) {
						modelsConfig = data;
						renderModelList();
					}
				} catch (e) {
					console.error('加载模型配置失败:', e);
				}
			}

			function renderModelList() {
				modelList.innerHTML = '';
				if (!modelsConfig.models || modelsConfig.models.length === 0) {
					modelList.innerHTML = '<div class="placeholder-text">暂无模型，点击下方添加</div>';
					return;
				}
				modelsConfig.models.forEach((m, i) => {
					const isActive = m.id === modelsConfig.active_model_id;
					const card = document.createElement('div');
					card.className = 'model-card' + (isActive ? ' active' : '');
					card.innerHTML = '\
						<div class="model-card-info">\
							<div class="model-card-name">' + (m.name || m.model) + (isActive ? ' &#10003;' : '') + '</div>\
							<div class="model-card-detail">' + m.provider + ' &middot; ' + m.model + ' &middot; temp=' + (m.temperature || 0.3) + ' &middot; ' + (m.base_url || '') + '</div>\
						</div>\
						<div class="model-card-actions">\
							' + (!isActive ? '<button class="set-active" onclick="setActiveModel(\'' + m.id + '\')">启用</button>' : '<span style="color:#22c55e;font-size:12px">当前使用</span>') + '\
							<button onclick="editModel(' + i + ')">编辑</button>\
							<button class="danger" onclick="deleteModel(' + i + ')">删除</button>\
						</div>\
					';
					modelList.appendChild(card);
				});
			}

			function showModelForm(model = null, index = -1) {
				editingModelIndex = index;
				modelFormTitle.textContent = model ? '编辑模型' : '添加模型';
				document.getElementById('mf_name').value = model?.name || '';
				document.getElementById('mf_model').value = model?.model || '';
				document.getElementById('mf_provider').value = model?.provider || 'deepseek';
				document.getElementById('mf_temperature').value = model?.temperature ?? 0.3;
				document.getElementById('mf_base_url').value = model?.base_url || 'https://api.deepseek.com';
				document.getElementById('mf_api_key').value = model?.api_key || '';
				document.getElementById('mf_stream').checked = model?.stream !== false;
				modelForm.style.display = 'block';
			}

			addModelBtn.addEventListener('click', () => showModelForm());
			modelFormCancel.addEventListener('click', () => { modelForm.style.display = 'none'; editingModelIndex = -1; });

			modelFormSave.addEventListener('click', async () => {
				const name = document.getElementById('mf_name').value.trim();
				const modelId = document.getElementById('mf_model').value.trim();
				if (!name || !modelId) { alert('名称和模型 ID 不能为空'); return; }
				const m = {
					id: modelId,
					name: name,
					provider: document.getElementById('mf_provider').value,
					model: modelId,
					temperature: parseFloat(document.getElementById('mf_temperature').value) || 0.3,
					base_url: document.getElementById('mf_base_url').value.trim(),
					api_key: document.getElementById('mf_api_key').value.trim(),
					stream: document.getElementById('mf_stream').checked
				};
				if (editingModelIndex >= 0) {
					m.id = modelsConfig.models[editingModelIndex].id;
					modelsConfig.models[editingModelIndex] = m;
				} else {
					modelsConfig.models.push(m);
				}
				await saveModelsConfig();
				modelForm.style.display = 'none';
				editingModelIndex = -1;
			});

			window.editModel = function(index) {
				showModelForm(modelsConfig.models[index], index);
			};

			window.deleteModel = async function(index) {
				if (!confirm('确定删除该模型？')) return;
				modelsConfig.models.splice(index, 1);
				if (modelsConfig.active_model_id === modelsConfig.models[index]?.id) {
					modelsConfig.active_model_id = modelsConfig.models[0]?.id || '';
				}
				await saveModelsConfig();
			};

			window.setActiveModel = async function(id) {
				modelsConfig.active_model_id = id;
				await saveModelsConfig();
			};

			async function saveModelsConfig() {
				try {
					const res = await fetch('/api/config/models', {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify(modelsConfig)
					});
					const data = await res.json();
					if (data.success) {
						renderModelList();
						await refreshModelSelect();
					} else {
						alert('保存失败: ' + (data.message || ''));
					}
				} catch (e) {
					alert('保存失败: ' + e.message);
				}
			}

			async function refreshModelSelect() {
				try {
					const res = await fetch('/api/models');
					const models = await res.json();
					const savedModel = localStorage.getItem('selectedModel');
					modelSelect.innerHTML = '';
					models.forEach(m => {
						const opt = document.createElement('option');
						opt.value = JSON.stringify({ display: m.display, id: m.id || '', provider: m.provider, model: m.model, base_url: m.base_url });
						opt.textContent = m.display;
						modelSelect.appendChild(opt);
					});
					if (savedModel) {
						const opt = Array.from(modelSelect.options).find(o => {
							try { return JSON.parse(o.value).display === savedModel; } catch(e) { return false; }
						});
						if (opt) opt.selected = true;
					}
				} catch (e) {
					console.error('刷新模型列表失败:', e);
				}
			}

			// ═══════════════════════════════════════════
			// 个人信息
			// ═══════════════════════════════════════════
			async function loadUserProfile() {
				try {
					const res = await fetch('/api/config/user_profile');
					const data = await res.json();
					if (data.success && data.profile) {
						document.getElementById('pf_nickname').value = data.profile.nickname || '';
						document.getElementById('pf_name').value = data.profile.name || '';
						document.getElementById('pf_hobbies').value = data.profile.hobbies || '';
						document.getElementById('pf_background').value = data.profile.background || '';
					}
				} catch (e) { console.error('加载个人信息失败:', e); }
			}

			document.getElementById('profileSaveBtn').addEventListener('click', async () => {
				const profile = {
					nickname: document.getElementById('pf_nickname').value.trim(),
					name: document.getElementById('pf_name').value.trim(),
					hobbies: document.getElementById('pf_hobbies').value.trim(),
					background: document.getElementById('pf_background').value.trim()
				};
				try {
					const res = await fetch('/api/config/user_profile', {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						body: JSON.stringify({ profile })
					});
					const data = await res.json();
					if (data.success) {
						alert('个人信息已保存');
					} else {
						alert('保存失败: ' + (data.message || ''));
					}
				} catch (e) { alert('保存失败: ' + e.message); }
			});

			// 首次启动检测
			try {
				const res = await fetch('/api/config/first_run');
				const data = await res.json();
				if (data.first_run) {
					setTimeout(() => {
						document.querySelector('.settings-tab[data-tab="profile"]').click();
						openSettings();
					}, 500);
				}
			} catch (e) {}

			// ═══════════════════════════════════════════
			// 模型快速预设
			// ═══════════════════════════════════════════
			let presetsData = {};
			async function loadPresets() {
				try {
					const res = await fetch('/api/config/model_presets');
					const data = await res.json();
					if (data.success) presetsData = data.presets;
				} catch (e) { console.error('加载预设失败:', e); }
			}
			loadPresets();

            document.getElementById('mf_preset').addEventListener('change', function() {
			const key = this.value;
			const preset = presetsData[key];
			const warningEl = document.getElementById('presetWarning');
			if (!preset) { warningEl.style.display = 'none'; return; }
			document.getElementById('mf_provider').value = key;
			document.getElementById('mf_base_url').value = preset.base_url || '';
			document.getElementById('mf_temperature').value = preset.temperature || 0.7;
			if (preset.warning) {
				warningEl.textContent = preset.warning;
				warningEl.style.display = 'block';
			} else {
				warningEl.style.display = 'none';
			}
			// 预设模型名
			if (preset.models && preset.models.length > 0) {
				document.getElementById('mf_model').value = preset.models[0];
				document.getElementById('mf_name').value = preset.name + ' - ' + preset.models[0];
			} else if (key === 'ollama') {
				document.getElementById('mf_model').placeholder = '填写本地模型名，如 llama3';
			}
		});
		});





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


	// 绑定滑块事件
	document.addEventListener('DOMContentLoaded', function() {
	    const volumeSlider = document.getElementById('volumeSlider');
	    const brightnessSlider = document.getElementById('brightnessSlider');
	    
    if (volumeSlider) {
        volumeSlider.addEventListener('input', function() {
            document.getElementById('volumeValue').textContent = this.value;
        });
    }
	    
	    if (brightnessSlider) {
	        brightnessSlider.addEventListener('input', function() {
	            document.getElementById('brightnessValue').textContent = this.value;
	        });
        
    }
    
    console.log('面板加载完成');
});

	// 快捷键支持

document.addEventListener('keydown', function(e) {
});

	// =====================================
	// GUI增强功能 - Office文件拖放处理
	// =====================================

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

// =====================================
// 加载用户体验增强功能
// =====================================
const script = document.createElement('script');
script.textContent = `
/* 
 * GUI增强功能 - 用户体验优化
 * 提供流畅的交互体验和视觉反馈
 */

class EnhancedUIFeatures {
    constructor() {
        this.init();
    }
    
    init() {
        this.bindOfficeDropZone();
        this.addKeyboardShortcuts();
        this.addNotificationSystem();
        console.log('GUI增强功能初始化完成');
    }
    
    
    bindCustomSlider(sliderId, callback) {
        const slider = document.getElementById(sliderId);
        if (!slider) return;
        
        // 实时值显示
        slider.addEventListener('input', function() {
            const valueElement = document.getElementById(this.id.replace('Slider', 'Value'));
            if (valueElement) {
                valueElement.textContent = this.value;
                this.style.background = 'linear-gradient(to right, #4CAF50 0%, #4CAF50 ' + this.value + '%, #ddd ' + this.value + '%, #ddd 100%)';
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
            // 系统面板快捷键已删除
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
        notificationContainer.style.cssText = 'position: fixed; top: 20px; left: 50%; transform: translateX(-50%); z-index: 10000; pointer-events: none;';
        document.body.appendChild(notificationContainer);
    }
    
    setupAutoHidePanel() {
        // 已删除系统面板，此方法不再需要
        
        let hideTimer;
        const hideDelay = 15000; // 15秒后开始渐隐
        
        // 鼠标移出时自动隐藏
        panel.addEventListener('mouseleave', () => {
            hideTimer = setTimeout(() => {
                panel.style.opacity = '0.2';
                panel.style.transform = 'translateX(200px)';
                panel.style.transition = 'all 0.5s ease';
            }, hideDelay);
        });
        
    }
    
    
    
    focusDropZone() {
        const dropZone = document.getElementById('officeDropZone');
        if (dropZone) {
            dropZone.scrollIntoView({ behavior: 'smooth' });
            dropZone.style.borderColor = '#FF9800';
            setTimeout(() => {
                dropZone.style.borderColor = '#4CAF50';
            }, 1000);
            this.showNotification('Office拖放区已高亮', 'info');
        }
    }
    
    // 通知系统
    showNotification(message, type = 'info') {
        const container = document.getElementById('gui-notifications');
        if (!container) return;
        
        const notification = document.createElement('div');
        notification.style.cssText = 'background: ' + this.getNotificationColor(type) + '; color: white; padding: 12px 20px; margin: 5px; border-radius: 6px; font-size: 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); animation: slideIn 0.3s ease; pointer-events: auto; cursor: pointer; z-index: 10001;';
        
        notification.textContent = message;
        notification.onclick = () => notification.remove();
        container.appendChild(notification);
        
        // 自动移除
        setTimeout(() => {
            if (notification.parentNode) {
                notification.style.animation = 'slideOut 0.3s ease';
                setTimeout(() => notification.remove(), 300);
            }
        }, 4000);
    }
    
    getNotificationColor(type) {
        const colors = {
            success: '#4CAF50',
            error: '#F44336', 
            Warning: '#FF9800',
            info: '#2196F3'
        };
        return colors[type] || colors.info;
    }
    
    showDropHint(message, type = 'info') {
        const dropZone = document.getElementById('officeDropZone');
        if (!dropZone) return;
        
        const hint = document.createElement('div');
        hint.style.cssText = 'position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); background: ' + this.getNotificationColor(type) + '; color: white; padding: 10px 20px; border-radius: 20px; font-size: 14px; z-index: 10; animation: pulse 1s ease-in-out infinite;';
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
        const command = '系统命令: ' + operation + ' ' + JSON.stringify(params);
        this.sendChatCommand(command);
    }
}

// 初始化增强功能
document.addEventListener('DOMContentLoaded', function() {
    window.enhancedUI = new EnhancedUIFeatures();
});
`;
document.head.appendChild(script);
	