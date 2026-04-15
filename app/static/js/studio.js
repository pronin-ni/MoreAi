/**
 * MoreAI Studio — Client-side JS with conversation persistence + multi-chat.
 *
 * Storage model (localStorage):
 *   studio_chats_v1 = {
 *     chats: { [chatId]: ChatRecord },
 *     activeChatId: string | null,
 *   }
 *
 * ChatRecord:
 *   { id, title, mode, created_at, updated_at, messages[], lastExecution }
 *
 * Limits: max 50 chats, max 100 messages per chat, oldest pruned first.
 * Corrupted data is silently discarded and fresh state is created.
 */

(() => {
    'use strict';

    // ── Config ──
    const STORAGE_KEY = 'studio_chats_v1';
    const MODE_STORAGE_KEY = 'studio_selected_mode';  // legacy, migrated on load
    const MAX_CHATS = 50;
    const MAX_MESSAGES = 100;
    const STUDIO_MODES = {
        fast: { label: 'Быстрый', isPipeline: false },
        balanced: { label: 'Сбалансированный', isPipeline: false },
        quality: { label: 'Качество', isPipeline: true },
        review: { label: 'Рецензия', isPipeline: true },
        deep: { label: 'Глубокий', isPipeline: true },
        web_search: { label: 'Web Search', isPipeline: true },
        explore: { label: 'Исследовать', isPipeline: true },
    };

    // ── Mode-aware progress sequences ──
    // Each step: { text, subtext, duration_ms }
    // durations are approximate — used for perceived progress, not real telemetry.
    const PROGRESS_SEQUENCES = {
        fast: [
            { text: 'Выбор модели…', subtext: '', duration_ms: 800 },
            { text: 'Генерация ответа…', subtext: '', duration_ms: 4000 },
        ],
        balanced: [
            { text: 'Выбор лучшей модели…', subtext: '', duration_ms: 1000 },
            { text: 'Генерация ответа…', subtext: '', duration_ms: 6000 },
        ],
        quality: [
            { text: 'Выбор лучшей модели…', subtext: '', duration_ms: 1200 },
            { text: 'Создание черновика…', subtext: 'Этот режим может занять больше времени для лучшего результата', duration_ms: 5000 },
            { text: 'Проверка ответа…', subtext: 'Проверка точности и полноты', duration_ms: 5000 },
            { text: 'Финальная доработка…', subtext: '', duration_ms: 5000 },
        ],
        review: [
            { text: 'Выбор лучшей модели…', subtext: '', duration_ms: 1200 },
            { text: 'Создание ответа…', subtext: '', duration_ms: 5000 },
            { text: 'Критика ответа…', subtext: 'Поиск ошибок и упущений', duration_ms: 5000 },
            { text: 'Улучшение ответа…', subtext: 'Этот режим может занять больше времени для лучшего результата', duration_ms: 5000 },
        ],
        deep: [
            { text: 'Выбор лучшей модели…', subtext: '', duration_ms: 1500 },
            { text: 'Создание ответа…', subtext: '', duration_ms: 5000 },
            { text: 'Верификация…', subtext: 'Перекрёстная проверка фактов и логики', duration_ms: 6000 },
            { text: 'Финализация результата…', subtext: 'Этот режим может занять больше времени для лучшего результата', duration_ms: 5000 },
        ],
        web_search: [
            { text: 'Поиск в интернете…', subtext: 'DuckDuckGo / SearXNG', duration_ms: 3000 },
            { text: 'Загрузка страниц…', subtext: 'Извлечение содержимого источников', duration_ms: 4000 },
            { text: 'Генерация ответа…', subtext: 'Создание ответа с цитатами источников', duration_ms: 5000 },
        ],
        explore: [
            { text: 'Выбор модели для тестирования…', subtext: 'Исследование новой модели', duration_ms: 1500 },
            { text: 'Выполнение запроса…', subtext: '', duration_ms: 5000 },
            { text: 'Анализ результатов…', subtext: 'Оценка качества модели', duration_ms: 2000 },
        ],
    };

    // ── State ──
    let chats = {};          // { chatId: ChatRecord }
    let activeChatId = null; // currently selected chat
    let lastFailedMessage = '';
    let lastFailedMode = '';
    let detailsCache = {};   // { executionId: html }
    let detailsOpen = false;

    // ── Progress state ──
    let progressTimer = null;
    let progressStepIndex = 0;
    let progressStartTime = 0;
    let progressElapsedTimer = null;

    // ── ChatRecord shape ──
    // { id, title, mode, created_at, updated_at, messages: [{role, content, timestamp}], lastExecution: {...} }

    // ── Persistence ──

    function loadStore() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return null;
            const data = JSON.parse(raw);
            if (!data || typeof data !== 'object') return null;
            if (!data.chats || typeof data.chats !== 'object') return null;
            return data;
        } catch {
            return null;  // corrupted — start fresh
        }
    }

    function saveStore() {
        try {
            localStorage.setItem(STORAGE_KEY, JSON.stringify({
                chats: chats,
                activeChatId: activeChatId,
            }));
        } catch (e) {
            // Storage full — prune oldest chat and retry once
            pruneOldestChat();
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify({
                    chats: chats,
                    activeChatId: activeChatId,
                }));
            } catch {
                // Still failing — give up silently
            }
        }
    }

    function pruneOldestChat() {
        const ids = Object.keys(chats);
        if (ids.length === 0) return;
        // Sort by updated_at, remove oldest
        ids.sort((a, b) => (chats[a].updated_at || 0) - (chats[b].updated_at || 0));
        delete chats[ids[0]];
        // If we deleted the active chat, pick a new one
        if (activeChatId && !chats[activeChatId]) {
            const remaining = Object.keys(chats);
            activeChatId = remaining.length > 0 ? remaining[remaining.length - 1] : null;
        }
    }

    function enforceLimits() {
        // Prune excess chats
        const ids = Object.keys(chats);
        if (ids.length > MAX_CHATS) {
            ids.sort((a, b) => (chats[a].updated_at || 0) - (chats[b].updated_at || 0));
            const toRemove = ids.slice(0, ids.length - MAX_CHATS);
            toRemove.forEach(id => delete chats[id]);
            if (activeChatId && !chats[activeChatId]) {
                const remaining = Object.keys(chats);
                activeChatId = remaining.length > 0 ? remaining[remaining.length - 1] : null;
            }
        }
    }

    // ── Chat CRUD ──

    function generateChatId() {
        return 'chat_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 7);
    }

    function createChat(mode) {
        const id = generateChatId();
        chats[id] = {
            id: id,
            title: 'New chat',
            mode: mode || 'balanced',
            created_at: Date.now(),
            updated_at: Date.now(),
            messages: [],
            lastExecution: null,
        };
        enforceLimits();
        saveStore();
        return id;
    }

    function setActiveChat(id) {
        // Save current chat state before switching
        saveCurrentChatState();

        activeChatId = id;
        saveStore();
        renderChatList();
        loadActiveChat();
    }

    function deleteChat(id) {
        delete chats[id];
        if (activeChatId === id) {
            const remaining = Object.keys(chats);
            if (remaining.length > 0) {
                // Switch to most recent
                remaining.sort((a, b) => (chats[b].updated_at || 0) - (chats[a].updated_at || 0));
                activeChatId = remaining[0];
            } else {
                // Create a fresh chat
                activeChatId = createChat('balanced');
            }
        }
        saveStore();
        renderChatList();
        loadActiveChat();
    }

    function saveCurrentChatState() {
        if (!activeChatId || !chats[activeChatId]) return;

        // Read conversation from hidden input
        const convInput = document.getElementById('studio-conversation-input');
        if (convInput) {
            try {
                chats[activeChatId].messages = JSON.parse(convInput.value) || [];
            } catch {
                chats[activeChatId].messages = [];
            }
        }

        // Prune excess messages
        if (chats[activeChatId].messages.length > MAX_MESSAGES) {
            chats[activeChatId].messages = chats[activeChatId].messages.slice(-MAX_MESSAGES);
        }

        // Auto-title from first user message
        if (chats[activeChatId].title === 'New chat' && chats[activeChatId].messages.length > 0) {
            const firstUser = chats[activeChatId].messages.find(m => m.role === 'user');
            if (firstUser && firstUser.content) {
                chats[activeChatId].title = firstUser.content.slice(0, 60) + (firstUser.content.length > 60 ? '…' : '');
            }
        }

        chats[activeChatId].updated_at = Date.now();
        saveStore();
    }

    // ── Init ──

    function init() {
        // Load persisted chats
        const store = loadStore();
        if (store && Object.keys(store.chats).length > 0) {
            chats = store.chats;
            activeChatId = store.activeChatId && chats[store.activeChatId]
                ? store.activeChatId
                : null;

            // If no active chat, pick most recent
            if (!activeChatId) {
                const ids = Object.keys(chats);
                ids.sort((a, b) => (chats[b].updated_at || 0) - (chats[a].updated_at || 0));
                activeChatId = ids.length > 0 ? ids[0] : null;
            }
        }

        // If still no chats, create one
        if (!activeChatId) {
            // Migrate legacy mode
            const legacyMode = localStorage.getItem(MODE_STORAGE_KEY) || 'balanced';
            activeChatId = createChat(legacyMode);
        }

        initModeSelection();
        initChatInput();
        renderChatList();
        loadActiveChat();
    }

    // ── Mode Selection ──

    function initModeSelection() {
        // Get mode from active chat
        const chatMode = activeChatId && chats[activeChatId] ? chats[activeChatId].mode : 'balanced';
        selectMode(chatMode);

        // Listen for radio changes
        document.querySelectorAll('input[name="studio-mode"]').forEach(radio => {
            radio.addEventListener('change', (e) => {
                selectMode(e.target.value);
            });
        });
    }

    window.selectMode = function(mode) {
        if (!STUDIO_MODES[mode]) return;

        // Update radio
        const radio = document.querySelector(`input[name="studio-mode"][value="${mode}"]`);
        if (radio) radio.checked = true;

        // Update selected class
        document.querySelectorAll('.studio-mode').forEach(el => {
            el.classList.toggle('selected', el.dataset.mode === mode);
        });

        // Update hidden input
        const input = document.getElementById('studio-mode-input');
        if (input) input.value = mode;

        // Update header badge
        const badge = document.getElementById('studio-mode-badge');
        if (badge) badge.textContent = STUDIO_MODES[mode].label;

        // Update pipeline indicator
        const indicator = document.getElementById('studio-pipeline-indicator');
        if (indicator) {
            indicator.style.display = STUDIO_MODES[mode].isPipeline ? 'inline' : 'none';
        }

        // Update loading text hint
        const loadingText = document.getElementById('studio-loading-text');
        if (loadingText) {
            loadingText.textContent = STUDIO_MODES[mode].isPipeline
                ? 'Running pipeline...'
                : 'Thinking...';
        }

        // Save mode to active chat
        if (activeChatId && chats[activeChatId]) {
            chats[activeChatId].mode = mode;
            chats[activeChatId].updated_at = Date.now();
            saveStore();
        }

        localStorage.setItem(MODE_STORAGE_KEY, mode);  // legacy compat
    };

    // ── Chat List Rendering ──

    function renderChatList() {
        const container = document.getElementById('studio-chat-list');
        if (!container) return;

        const ids = Object.keys(chats);
        if (ids.length === 0) {
            container.innerHTML = '<div class="studio-chat-list-empty">No chats yet</div>';
            return;
        }

        // Sort by updated_at, most recent first
        ids.sort((a, b) => (chats[b].updated_at || 0) - (chats[a].updated_at || 0));

        let html = '';
        ids.forEach(id => {
            const chat = chats[id];
            const isActive = id === activeChatId;
            const modeLabel = STUDIO_MODES[chat.mode]?.label || '?';
            const title = escapeHtml(chat.title || 'New chat');
            const timeStr = formatTimeAgo(chat.updated_at);

            html += `<div class="studio-chat-item ${isActive ? 'active' : ''}" onclick="setActiveChat('${id}')">
                <div class="studio-chat-item-content">
                    <div class="studio-chat-item-title">${title}</div>
                    <div class="studio-chat-item-meta">
                        <span class="studio-chat-item-mode">${modeLabel}</span>
                        <span class="studio-chat-item-time">${timeStr}</span>
                    </div>
                </div>
                <button class="studio-chat-item-delete" onclick="event.stopPropagation(); deleteChat('${id}')" title="Delete chat">✕</button>
            </div>`;
        });

        container.innerHTML = html;
    }

    window.setActiveChat = setActiveChat;
    window.deleteChat = deleteChat;

    function formatTimeAgo(ts) {
        if (!ts) return '';
        const diff = Date.now() - ts;
        if (diff < 60000) return 'now';
        if (diff < 3600000) return Math.floor(diff / 60000) + 'm';
        if (diff < 86400000) return Math.floor(diff / 3600000) + 'h';
        if (diff < 604800000) return Math.floor(diff / 86400000) + 'd';
        return new Date(ts).toLocaleDateString();
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ── Load Active Chat ──

    function loadActiveChat() {
        const chat = activeChatId && chats[activeChatId] ? chats[activeChatId] : null;

        if (!chat || chat.messages.length === 0) {
            // Show welcome
            renderMessages([], chat?.mode || 'balanced');
            resetExecutionPanel();
            // Set hidden conversation input
            const convInput = document.getElementById('studio-conversation-input');
            if (convInput) convInput.value = '[]';
            return;
        }

        // Restore mode
        selectMode(chat.mode);

        // Render messages
        renderMessages(chat.messages, chat.mode);

        // Restore hidden conversation input
        const convInput = document.getElementById('studio-conversation-input');
        if (convInput) convInput.value = JSON.stringify(chat.messages);

        // Restore last execution summary
        if (chat.lastExecution) {
            restoreExecutionSummary(chat.lastExecution);
        } else {
            resetExecutionPanel();
        }

        // Reset details
        detailsCache = {};
        detailsOpen = false;
        const details = document.getElementById('studio-execution-details');
        if (details) {
            details.style.display = 'none';
            details.innerHTML = '';
        }

        scrollToBottom();
    }

    function renderMessages(messages, mode) {
        const container = document.getElementById('studio-messages');
        if (!container) return;

        if (!messages || messages.length === 0) {
            const modeLabel = STUDIO_MODES[mode]?.label || mode || 'Balanced';
            const modeDesc = STUDIO_MODES[mode]?.isPipeline
                ? 'This mode runs a multi-stage pipeline for higher quality results.'
                : 'Fast single-model response.';
            container.innerHTML = `<div class="studio-welcome">
                <div class="studio-welcome-title">${modeLabel}</div>
                <div class="studio-welcome-desc">${modeDesc}</div>
                <div class="studio-welcome-modes">
                    ${Object.keys(STUDIO_MODES).map(k =>
                        `<span class="studio-welcome-chip" onclick="selectMode('${k}')">${STUDIO_MODES[k].label}</span>`
                    ).join('')}
                </div>
            </div>`;
            return;
        }

        let html = '';
        messages.forEach(msg => {
            const role = msg.role || 'user';
            const content = msg.content || '';
            if (role === 'assistant') {
                // Render markdown on client-side (simple)
                const mdHtml = renderMarkdown(content);
                html += `<div class="studio-message studio-message-assistant">
                    <div class="studio-message-role">assistant</div>
                    <div class="studio-message-content">${mdHtml}</div>
                    <button class="studio-copy-btn" onclick="copyStudioMessage(this)">Copy</button>
                </div>`;
            } else if (role === 'user') {
                html += `<div class="studio-message studio-message-user">
                    <div class="studio-message-role">you</div>
                    <div class="studio-message-content">${escapeHtml(content)}</div>
                </div>`;
            }
        });

        container.innerHTML = html;
    }

    function renderMarkdown(text) {
        // Lightweight client-side markdown → HTML
        let html = escapeHtml(text);
        html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
        html = html.replace(/`(.*?)`/g, '<code>$1</code>');
        html = html.replace(/\n/g, '<br>');
        return html;
    }

    // ── Progress UX ──

    function showProgress(mode) {
        clearProgress();  // ensure no stale progress

        const sequence = PROGRESS_SEQUENCES[mode] || PROGRESS_SEQUENCES.balanced;
        progressStepIndex = 0;
        progressStartTime = Date.now();

        // Show progress in main chat area
        const messagesContainer = document.getElementById('studio-messages');
        if (messagesContainer) {
            renderProgressBlock(messagesContainer, mode, sequence[0]);
        }

        // Show running state in right panel
        updateRightPanelRunning(mode, sequence);

        // Start step timer
        startProgressTimer(mode, sequence);

        // Start elapsed time display
        startElapsedTimer();
    }

    function renderProgressBlock(container, mode, step) {
        const modeLabel = STUDIO_MODES[mode]?.label || mode;
        const subtextHtml = step.subtext ? `<div class="studio-progress-sub">${step.subtext}</div>` : '';
        const elapsedHtml = '<span class="studio-progress-elapsed" id="studio-progress-elapsed">0s</span>';

        // If a progress block already exists, update it
        let progressBlock = container.querySelector('.studio-progress-block');
        if (progressBlock) {
            progressBlock.querySelector('.studio-progress-text').textContent = step.text;
            const subEl = progressBlock.querySelector('.studio-progress-sub');
            if (step.subtext) {
                if (subEl) subEl.textContent = step.subtext;
                else progressBlock.querySelector('.studio-progress-body').insertAdjacentHTML('beforeend', subtextHtml);
            } else if (subEl) {
                subEl.textContent = '';
            }
            return;
        }

        // Create new progress block
        const html = `<div class="studio-progress-block">
            <div class="studio-progress-mode-badge">${escapeHtml(modeLabel)}</div>
            <div class="studio-progress-header">
                <div class="studio-progress-spinner"></div>
                <div class="studio-progress-body">
                    <div class="studio-progress-text">${escapeHtml(step.text)}</div>
                    ${subtextHtml}
                </div>
                ${elapsedHtml}
            </div>
            <div class="studio-progress-steps">
                ${renderProgressDots(STUDIO_MODES[mode]?.isPipeline ? PROGRESS_SEQUENCES[mode]?.length || 0 : 0, 0)}
            </div>
        </div>`;
        container.insertAdjacentHTML('beforeend', html);
    }

    function renderProgressDots(total, current) {
        if (total <= 1) return '';
        let dots = '';
        for (let i = 0; i < total; i++) {
            dots += `<span class="studio-progress-dot ${i <= current ? 'active' : ''}"></span>`;
        }
        return dots;
    }

    function startProgressTimer(mode, sequence) {
        let cumulativeMs = 0;

        function advanceStep() {
            progressStepIndex++;
            if (progressStepIndex >= sequence.length) {
                // Stay on last step until real response arrives
                return;
            }

            const step = sequence[progressStepIndex];
            const messagesContainer = document.getElementById('studio-messages');
            if (messagesContainer) {
                const progressBlock = messagesContainer.querySelector('.studio-progress-block');
                if (progressBlock) {
                    progressBlock.querySelector('.studio-progress-text').textContent = step.text;
                    const subEl = progressBlock.querySelector('.studio-progress-sub');
                    if (step.subtext) {
                        if (subEl) subEl.textContent = step.subtext;
                        else {
                            progressBlock.querySelector('.studio-progress-body').insertAdjacentHTML(
                                'beforeend',
                                `<div class="studio-progress-sub">${step.subtext}</div>`
                            );
                        }
                    }
                    // Update dots
                    const dotsEl = progressBlock.querySelector('.studio-progress-steps');
                    if (dotsEl) {
                        dotsEl.innerHTML = renderProgressDots(sequence.length, progressStepIndex);
                    }
                }
            }

            // Update right panel
            updateRightPanelStep(mode, sequence, progressStepIndex);

            // Schedule next step
            cumulativeMs += step.duration_ms;
            progressTimer = setTimeout(advanceStep, step.duration_ms);
        }

        // First step advance after initial delay
        progressTimer = setTimeout(advanceStep, sequence[0]?.duration_ms || 1000);
    }

    function startElapsedTimer() {
        const elapsedEl = document.getElementById('studio-progress-elapsed');
        if (!elapsedEl) return;

        progressElapsedTimer = setInterval(() => {
            const elapsed = Math.floor((Date.now() - progressStartTime) / 1000);
            if (elapsed < 60) {
                elapsedEl.textContent = elapsed + 's';
            } else {
                elapsedEl.textContent = Math.floor(elapsed / 60) + 'm ' + (elapsed % 60) + 's';
            }
        }, 1000);
    }

    function updateRightPanelRunning(mode, sequence) {
        const summary = document.getElementById('studio-execution-summary');
        if (!summary) return;

        const modeLabel = STUDIO_MODES[mode]?.label || mode;
        const isPipeline = STUDIO_MODES[mode]?.isPipeline;

        summary.innerHTML = `<div class="studio-exec-result">
            <div class="studio-exec-mode">
                <span class="studio-exec-mode-label">${modeLabel}</span>
                ${isPipeline ? '<span class="studio-exec-pipeline-badge">Pipeline</span>' : '<span class="studio-exec-model-badge">Model</span>'}
            </div>
            <div class="studio-exec-running">
                <div class="studio-exec-running-spinner"></div>
                <span class="studio-exec-running-text">${sequence[0]?.text || 'Processing…'}</span>
            </div>
            <div class="studio-exec-stat">
                <span class="studio-exec-stat-label">Stages</span>
                <span class="studio-exec-stat-value">${isPipeline ? sequence.length : 1}</span>
            </div>
        </div>`;
    }

    function updateRightPanelStep(mode, sequence, stepIndex) {
        const runningText = document.querySelector('.studio-exec-running-text');
        if (runningText && sequence[stepIndex]) {
            runningText.textContent = sequence[stepIndex].text;
        }
    }

    function clearProgress() {
        if (progressTimer) {
            clearTimeout(progressTimer);
            progressTimer = null;
        }
        if (progressElapsedTimer) {
            clearInterval(progressElapsedTimer);
            progressElapsedTimer = null;
        }
        progressStepIndex = 0;
        progressStartTime = 0;

        // Remove progress block from messages
        const messagesContainer = document.getElementById('studio-messages');
        if (messagesContainer) {
            const progressBlock = messagesContainer.querySelector('.studio-progress-block');
            if (progressBlock) progressBlock.remove();
        }
    }

    function clearProgressOnError() {
        clearProgress();
        // Replace progress block with error
        const messagesContainer = document.getElementById('studio-messages');
        if (messagesContainer) {
            const progressBlock = messagesContainer.querySelector('.studio-progress-block');
            if (progressBlock) {
                progressBlock.innerHTML = `<div class="studio-progress-error">
                    <span>Request failed</span>
                    <button class="studio-progress-retry" onclick="retryStudioMessage()">Retry</button>
                </div>`;
            }
        }
        // Reset right panel running state
        const runningEl = document.querySelector('.studio-exec-running');
        if (runningEl) {
            runningEl.innerHTML = '<span class="studio-exec-running-error">Failed</span>';
        }
    }

    function resetExecutionPanel() {
        const summary = document.getElementById('studio-execution-summary');
        if (summary) {
            summary.innerHTML = '<div class="studio-execution-empty">No execution yet. Send a message to see how it\'s processed.</div>';
        }
        const details = document.getElementById('studio-execution-details');
        if (details) {
            details.style.display = 'none';
            details.innerHTML = '';
        }
    }

    function restoreExecutionSummary(exec) {
        if (!exec) return;

        const summary = document.getElementById('studio-execution-summary');
        if (!summary) return;

        const modeLabel = STUDIO_MODES[exec.mode]?.label || exec.mode || '?';
        const durStr = exec.duration_ms >= 1000
            ? (exec.duration_ms / 1000).toFixed(1) + 's'
            : Math.round(exec.duration_ms) + 'ms';

        let html = `<div class="studio-exec-result">
            <div class="studio-exec-mode">
                <span class="studio-exec-mode-label">${modeLabel}</span>
                ${exec.execution_type === 'pipeline'
                    ? '<span class="studio-exec-pipeline-badge">Pipeline</span>'
                    : '<span class="studio-exec-model-badge">Model</span>'}
            </div>
            <div class="studio-exec-stats">
                <div class="studio-exec-stat">
                    <span class="studio-exec-stat-label">Stages</span>
                    <span class="studio-exec-stat-value">${exec.stage_count || 0}</span>
                </div>
                <div class="studio-exec-stat">
                    <span class="studio-exec-stat-label">Models</span>
                    <span class="studio-exec-stat-value">${(exec.selected_models || []).length}</span>
                </div>
                <div class="studio-exec-stat">
                    <span class="studio-exec-stat-label">Fallbacks</span>
                    <span class="studio-exec-stat-value ${exec.fallback_count > 0 ? 'studio-fallback-used' : ''}">${exec.fallback_count || 0}</span>
                </div>
                <div class="studio-exec-stat">
                    <span class="studio-exec-stat-label">Duration</span>
                    <span class="studio-exec-stat-value">${durStr}</span>
                </div>
            </div>`;

        if (exec.quality_score > 0) {
            html += `<div class="studio-exec-quality">
                <span class="studio-exec-quality-label">Quality</span>
                <span class="studio-exec-quality-value">${exec.quality_score}</span>
            </div>`;
        }

        if (exec.selected_models && exec.selected_models.length > 0) {
            html += '<div class="studio-exec-models"><span class="studio-exec-models-label">Models:</span>';
            exec.selected_models.forEach(m => {
                html += `<span class="studio-exec-model-tag">${m}</span>`;
            });
            html += '</div>';
        }

        if (exec.execution_type === 'pipeline' && exec.pipeline_id) {
            html += `<div class="studio-exec-pipeline-id">Pipeline: <code>${exec.pipeline_id}</code></div>`;
        }

        html += `<button class="studio-exec-details-btn" onclick="toggleExecutionDetails()">Show details</button>
        </div>`;

        summary.innerHTML = html;

        // Store execution ID for details
        if (exec.execution_id) {
            const stateDiv = document.querySelector('.studio-response-state');
            if (stateDiv) {
                stateDiv.dataset.studioExecutionId = exec.execution_id;
            }
        }
    }

    // ── Advanced Toggle ──

    window.toggleAdvancedModel = function() {
        const type = document.getElementById('studio-advanced-type').value;
        const modelField = document.getElementById('studio-advanced-model-field');
        if (modelField) {
            modelField.style.display = type === 'custom_model' ? 'block' : 'none';
        }
    };

    // ── Chat Input ──

    function initChatInput() {
        const textarea = document.getElementById('studio-message-input');
        if (!textarea) return;

        textarea.addEventListener('input', () => {
            textarea.style.height = 'auto';
            textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
        });

        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                const form = document.getElementById('studio-chat-form');
                if (form) htmx.trigger(form, 'submit');
            }
        });
    }

    // ── HTMX Hooks ──

    window.onStudioRequest = function() {
        const input = document.getElementById('studio-message-input');
        if (input && input.value.trim()) {
            lastFailedMessage = input.value.trim();
            lastFailedMode = document.getElementById('studio-mode-input')?.value || 'balanced';
        }
        // Show progress
        const mode = document.getElementById('studio-mode-input')?.value || 'balanced';
        showProgress(mode);
    };

    window.onStudioResponse = function(event) {
        // Clear progress first
        clearProgress();

        const stateDiv = document.querySelector('.studio-response-state');
        if (stateDiv) {
            const status = stateDiv.dataset.studioStatus;
            const execId = stateDiv.dataset.studioExecutionId;
            if (status === 'success') {
                const input = document.getElementById('studio-message-input');
                if (input) {
                    input.value = '';
                    input.style.height = 'auto';
                }
                lastFailedMessage = '';

                // Save current chat state (messages + execution)
                saveCurrentChatState();

                // Update lastExecution on active chat
                if (activeChatId && chats[activeChatId]) {
                    // Read execution data from response
                    const execData = extractExecutionFromDOM();
                    if (execData) {
                        chats[activeChatId].lastExecution = execData;
                        chats[activeChatId].updated_at = Date.now();
                        saveStore();
                    }
                }

                // Update chat list (title may have changed)
                renderChatList();

                // Reset details
                if (execId) {
                    detailsOpen = false;
                    detailsCache = {};
                    const details = document.getElementById('studio-execution-details');
                    if (details) {
                        details.style.display = 'none';
                        details.innerHTML = '';
                    }
                    const btn = document.querySelector('.studio-exec-details-btn');
                    if (btn) btn.textContent = 'Show details';
                }
            } else if (status === 'error') {
                // Show error state in progress block
                clearProgressOnError();
            }
        }

        scrollToBottom();
    };

    function extractExecutionFromDOM() {
        // Extract execution data from the rendered summary
        const summary = document.getElementById('studio-execution-summary');
        if (!summary) return null;

        const stateDiv = document.querySelector('.studio-response-state');
        const mode = stateDiv?.dataset.studioMode || '';
        const execId = stateDiv?.dataset.studioExecutionId || '';

        // Parse values from DOM (rough extraction)
        const statValues = summary.querySelectorAll('.studio-exec-stat-value');
        const stageCount = parseInt(statValues[0]?.textContent) || 0;
        const modelCount = parseInt(statValues[1]?.textContent) || 0;
        const fallbackCount = parseInt(statValues[2]?.textContent) || 0;
        const durText = statValues[3]?.textContent || '0ms';
        const durationMs = durText.includes('s') ? parseFloat(durText) * 1000 : parseFloat(durText);

        const qualityEl = summary.querySelector('.studio-exec-quality-value');
        const qualityScore = parseFloat(qualityEl?.textContent) || 0;

        const isPipeline = !!summary.querySelector('.studio-exec-pipeline-badge');
        const pipelineEl = summary.querySelector('.studio-exec-pipeline-id code');
        const pipelineId = pipelineEl?.textContent || '';

        const modelTags = summary.querySelectorAll('.studio-exec-model-tag');
        const selectedModels = Array.from(modelTags).map(el => el.textContent);

        return {
            execution_id: execId,
            mode: mode,
            execution_type: isPipeline ? 'pipeline' : 'model',
            pipeline_id: pipelineId,
            stage_count: stageCount,
            selected_models: selectedModels,
            fallback_count: fallbackCount,
            quality_score: qualityScore,
            duration_ms: durationMs || 0,
            status: 'success',
        };
    }

    // ── New Chat ──

    window.clearChat = function() {
        // Create new chat, keep current mode for convenience
        const currentMode = activeChatId && chats[activeChatId] ? chats[activeChatId].mode : 'balanced';
        const newId = createChat(currentMode);
        setActiveChat(newId);
    };

    // ── Retry ──

    window.retryStudioMessage = function() {
        if (!lastFailedMessage) return;

        const input = document.getElementById('studio-message-input');
        if (input) input.value = lastFailedMessage;

        selectMode(lastFailedMode);

        const form = document.getElementById('studio-chat-form');
        if (form) htmx.trigger(form, 'submit');
    };

    // ── Toggle Execution Details ──

    window.toggleExecutionDetails = function() {
        const details = document.getElementById('studio-execution-details');
        const btn = document.querySelector('.studio-exec-details-btn');
        if (!details) return;

        if (detailsOpen) {
            details.style.display = 'none';
            details.innerHTML = '';
            detailsOpen = false;
            if (btn) btn.textContent = 'Show details';
        } else {
            const execId = getExecutionId();
            if (!execId) {
                details.style.display = 'block';
                details.innerHTML = '<div class="studio-details-empty">No execution data available.</div>';
                detailsOpen = true;
                if (btn) btn.textContent = 'Hide details';
                return;
            }

            if (detailsCache[execId]) {
                details.style.display = 'block';
                details.innerHTML = detailsCache[execId];
                detailsOpen = true;
                if (btn) btn.textContent = 'Hide details';
                return;
            }

            details.style.display = 'block';
            details.innerHTML = '<div class="studio-details-empty"><div class="studio-loading-spinner"></div> Loading details...</div>';
            if (btn) btn.textContent = 'Loading...';

            loadExecutionDetails(execId);
        }
    };

    function getExecutionId() {
        const stateDiv = document.querySelector('.studio-response-state');
        return stateDiv?.dataset.studioExecutionId || '';
    }

    window.loadExecutionDetails = function(executionId) {
        const details = document.getElementById('studio-execution-details');
        const btn = document.querySelector('.studio-exec-details-btn');

        fetch(`/studio/executions/${encodeURIComponent(executionId)}`)
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    details.innerHTML = `<div class="studio-details-error"><span>${data.error}</span><button class="studio-details-retry" onclick="loadExecutionDetails('${executionId}')">Retry</button></div>`;
                    return;
                }

                const html = renderExecutionDetails(data);
                details.innerHTML = html;
                detailsCache[executionId] = html;
                detailsOpen = true;
                if (btn) btn.textContent = 'Hide details';
            })
            .catch(err => {
                details.innerHTML = `<div class="studio-details-error"><span>Failed to load details: ${err.message}</span><button class="studio-details-retry" onclick="loadExecutionDetails('${executionId}')">Retry</button></div>`;
            });
    };

    function renderExecutionDetails(data) {
        let html = '<div class="studio-details">';
        html += '<div class="studio-details-header">';
        html += '<span class="studio-details-title">Execution Details</span>';
        html += '<button class="studio-details-close" onclick="toggleExecutionDetails()" title="Close">✕</button>';
        html += '</div>';

        if (data.verdict) {
            html += `<div class="studio-details-verdict">
                <span class="studio-details-verdict-label">${data.verdict.label}</span>
                <span class="studio-details-verdict-msg">${data.verdict.message}</span>
            </div>`;
        }

        if (data.stages && data.stages.length > 0) {
            data.stages.forEach((stage, i) => {
                html += '<div class="studio-details-stage">';
                html += '<div class="studio-details-stage-header">';
                html += `<span class="studio-details-stage-number">${i + 1}</span>`;
                html += `<span class="studio-details-stage-role">${stage.role_label}</span>`;
                const statusClass = stage.status || 'unknown';
                html += `<span class="studio-details-stage-status ${statusClass}">${stage.status_label}</span>`;
                if (stage.fallback_count > 0) html += '<span class="studio-details-fallback-badge">Fallback used</span>';
                if (stage.retry_count > 0) html += `<span class="studio-details-retry-badge">${stage.retry_count} retry</span>`;
                html += '</div>';

                html += '<div class="studio-details-stage-body">';
                html += `<div class="studio-details-explanation">${stage.explanation}</div>`;
                html += '<div class="studio-details-stage-meta">';
                html += `<div class="studio-details-meta-item"><span class="studio-details-meta-label">Model</span><span class="studio-details-meta-value">${stage.model || '—'}</span></div>`;
                if (stage.transport) html += `<div class="studio-details-meta-item"><span class="studio-details-meta-label">Transport</span><span class="studio-details-meta-value">${stage.transport}</span></div>`;
                const dur = stage.duration_ms >= 1000 ? (stage.duration_ms / 1000).toFixed(1) + 's' : Math.round(stage.duration_ms) + 'ms';
                html += `<div class="studio-details-meta-item"><span class="studio-details-meta-label">Duration</span><span class="studio-details-meta-value">${dur}</span></div>`;
                html += '</div>';

                if (stage.fallbacks && stage.fallbacks.length > 0) {
                    html += '<div class="studio-details-fallbacks"><div class="studio-details-fallbacks-label">Fallback chain:</div>';
                    stage.fallbacks.forEach(fb => {
                        html += `<div class="studio-details-fallback-item">
                            <span class="studio-details-fallback-from">${fb.from_model}</span>
                            <span class="studio-details-fallback-arrow">→</span>
                            <span class="studio-details-fallback-to">${fb.to_model || 'next'}</span>
                            <span class="studio-details-fallback-reason">(${fb.reason})</span>
                        </div>`;
                    });
                    html += '</div>';
                }

                if (stage.quality_label) {
                    html += `<div class="studio-details-quality">
                        <span class="studio-details-quality-label">${stage.quality_label}</span>
                        ${stage.quality_score ? `<span class="studio-details-quality-score">${stage.quality_score}</span>` : ''}
                    </div>`;
                }

                html += '</div></div>';
            });
        }

        if (data.cross_stage && Object.keys(data.cross_stage).length > 0) {
            html += '<div class="studio-details-cross-stage"><div class="studio-details-cross-stage-title">Cross-stage analysis</div>';
            const cs = data.cross_stage;
            if (cs.generate) html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Draft quality:</span><span class="studio-details-cross-value">${cs.generate.summary}</span></div>`;
            if (cs.review) html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Review impact:</span><span class="studio-details-cross-value">${cs.review.summary}</span></div>`;
            if (cs.refine) html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Refinement:</span><span class="studio-details-cross-value">${cs.refine.summary}</span></div>`;
            html += '</div>';
        }

        html += '</div>';
        return html;
    }

    // ── Copy Message ──

    window.copyStudioMessage = function(btn) {
        const content = btn.parentElement.querySelector('.studio-message-content');
        if (!content) return;
        const text = content.innerText;
        navigator.clipboard.writeText(text).then(() => {
            btn.textContent = 'Copied!';
            setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        }).catch(() => {
            btn.textContent = 'Failed';
            setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
        });
    };

    // ── Scroll ──

    function scrollToBottom() {
        const container = document.getElementById('studio-messages');
        if (container) container.scrollTop = container.scrollHeight;
    }

    // ── Start ──

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
