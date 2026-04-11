/**
 * MoreAI Studio — Client-side JS.
 *
 * Handles:
 * - Mode selection + localStorage persistence
 * - Chat input (Enter to send, Shift+Enter for newline, auto-resize)
 * - Loading/error states
 * - Copy message to clipboard
 * - Clear chat + retry
 * - Advanced model toggle
 */

(() => {
    'use strict';

    // ── Config ──
    const MODE_STORAGE_KEY = 'studio_selected_mode';
    const STUDIO_MODES = {
        fast: { label: 'Fast', isPipeline: false },
        balanced: { label: 'Balanced', isPipeline: false },
        quality: { label: 'Quality', isPipeline: true },
        review: { label: 'Review', isPipeline: true },
        deep: { label: 'Deep', isPipeline: true },
    };

    // ── State ──
    let conversation = [];
    let lastFailedMessage = '';
    let lastFailedMode = '';
    let currentExecutionId = '';
    let detailsCache = {};  // Cache details by execution_id
    let detailsOpen = false;

    // ── Init ──
    function init() {
        initModeSelection();
        initChatInput();
    }

    // ── Mode Selection ──
    function initModeSelection() {
        const savedMode = localStorage.getItem(MODE_STORAGE_KEY) || 'balanced';
        selectMode(savedMode);

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

        localStorage.setItem(MODE_STORAGE_KEY, mode);
    };

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

        // Auto-resize
        textarea.addEventListener('input', () => {
            textarea.style.height = 'auto';
            textarea.style.height = Math.min(textarea.scrollHeight, 150) + 'px';
        });

        // Enter to send (Shift+Enter for newline)
        textarea.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                const form = document.getElementById('studio-chat-form');
                if (form) {
                    // Trigger HTMX submission
                    htmx.trigger(form, 'submit');
                }
            }
        });
    }

    // ── HTMX Hooks ──
    window.onStudioRequest = function() {
        // Store message for retry
        const input = document.getElementById('studio-message-input');
        if (input && input.value.trim()) {
            lastFailedMessage = input.value.trim();
            lastFailedMode = document.getElementById('studio-mode-input')?.value || 'balanced';
        }
    };

    window.onStudioResponse = function(event) {
        // Check response status
        const stateDiv = document.querySelector('.studio-response-state');
        if (stateDiv) {
            const status = stateDiv.dataset.studioStatus;
            const execId = stateDiv.dataset.studioExecutionId;
            if (status === 'success') {
                // Clear input
                const input = document.getElementById('studio-message-input');
                if (input) {
                    input.value = '';
                    input.style.height = 'auto';
                }
                lastFailedMessage = '';

                // Store execution ID for details
                if (execId) {
                    currentExecutionId = execId;
                    // Reset details state when new message sent
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
            }
        }

        // Scroll to bottom
        scrollToBottom();
    };

    // ── Clear Chat ──
    window.clearChat = function() {
        conversation = [];
        const input = document.getElementById('studio-conversation-input');
        if (input) input.value = '[]';

        const messages = document.getElementById('studio-messages');
        if (messages) {
            messages.innerHTML = `
                <div class="studio-welcome">
                    <div class="studio-welcome-title">MoreAI Studio</div>
                    <div class="studio-welcome-desc">Chat cleared. Choose a mode and ask anything.</div>
                    <div class="studio-welcome-modes">
                        ${Object.keys(STUDIO_MODES).map(k =>
                            `<span class="studio-welcome-chip" onclick="selectMode('${k}')">${STUDIO_MODES[k].label}</span>`
                        ).join('')}
                    </div>
                </div>
            `;
        }
    };

    // ── Clear Execution Summary ──
    window.clearExecutionSummary = function() {
        const summary = document.getElementById('studio-execution-summary');
        if (summary) {
            summary.innerHTML = '<div class="studio-execution-empty">No execution yet. Send a message to see how it\'s processed.</div>';
        }
        const details = document.getElementById('studio-execution-details');
        if (details) {
            details.style.display = 'none';
            details.innerHTML = '';
        }
        // Reset state
        currentExecutionId = '';
        detailsCache = {};
        detailsOpen = false;
    };

    // ── Retry ──
    window.retryStudioMessage = function() {
        if (!lastFailedMessage) return;

        const input = document.getElementById('studio-message-input');
        if (input) input.value = lastFailedMessage;

        selectMode(lastFailedMode);

        const form = document.getElementById('studio-chat-form');
        if (form) {
            htmx.trigger(form, 'submit');
        }
    };

    // ── Toggle Execution Details ──
    window.toggleExecutionDetails = function() {
        const details = document.getElementById('studio-execution-details');
        const btn = document.querySelector('.studio-exec-details-btn');
        if (!details) return;

        if (detailsOpen) {
            // Close
            details.style.display = 'none';
            details.innerHTML = '';
            detailsOpen = false;
            if (btn) btn.textContent = 'Show details';
        } else {
            // Open — load details
            if (!currentExecutionId) {
                details.style.display = 'block';
                details.innerHTML = '<div class="studio-details-empty">No execution data available.</div>';
                detailsOpen = true;
                if (btn) btn.textContent = 'Hide details';
                return;
            }

            // Check cache
            if (detailsCache[currentExecutionId]) {
                details.style.display = 'block';
                details.innerHTML = detailsCache[currentExecutionId];
                detailsOpen = true;
                if (btn) btn.textContent = 'Hide details';
                return;
            }

            // Show loading
            details.style.display = 'block';
            details.innerHTML = '<div class="studio-details-empty"><div class="studio-loading-spinner"></div> Loading details...</div>';
            if (btn) btn.textContent = 'Loading...';

            // Fetch
            loadExecutionDetails(currentExecutionId);
        }
    };

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

                // Render using inline template approach
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

        // Verdict
        if (data.verdict) {
            html += '<div class="studio-details-verdict">';
            html += `<span class="studio-details-verdict-label">${data.verdict.label}</span>`;
            html += `<span class="studio-details-verdict-msg">${data.verdict.message}</span>`;
            html += '</div>';
        }

        // Stage cards
        if (data.stages && data.stages.length > 0) {
            data.stages.forEach((stage, i) => {
                html += '<div class="studio-details-stage">';
                html += '<div class="studio-details-stage-header">';
                html += `<span class="studio-details-stage-number">${i + 1}</span>`;
                html += `<span class="studio-details-stage-role">${stage.role_label}</span>`;
                const statusClass = stage.status || 'unknown';
                html += `<span class="studio-details-stage-status ${statusClass}">${stage.status_label}</span>`;
                if (stage.fallback_count > 0) {
                    html += '<span class="studio-details-fallback-badge">Fallback used</span>';
                }
                if (stage.retry_count > 0) {
                    html += `<span class="studio-details-retry-badge">${stage.retry_count} retry</span>`;
                }
                html += '</div>';

                html += '<div class="studio-details-stage-body">';
                html += `<div class="studio-details-explanation">${stage.explanation}</div>`;

                html += '<div class="studio-details-stage-meta">';
                html += '<div class="studio-details-meta-item">';
                html += '<span class="studio-details-meta-label">Model</span>';
                html += `<span class="studio-details-meta-value">${stage.model || '—'}</span>`;
                html += '</div>';
                if (stage.transport) {
                    html += '<div class="studio-details-meta-item">';
                    html += '<span class="studio-details-meta-label">Transport</span>';
                    html += `<span class="studio-details-meta-value">${stage.transport}</span>`;
                    html += '</div>';
                }
                html += '<div class="studio-details-meta-item">';
                html += '<span class="studio-details-meta-label">Duration</span>';
                const dur = stage.duration_ms >= 1000 ? (stage.duration_ms / 1000).toFixed(1) + 's' : Math.round(stage.duration_ms) + 'ms';
                html += `<span class="studio-details-meta-value">${dur}</span>`;
                html += '</div>';
                html += '</div>';

                // Fallback chain
                if (stage.fallbacks && stage.fallbacks.length > 0) {
                    html += '<div class="studio-details-fallbacks">';
                    html += '<div class="studio-details-fallbacks-label">Fallback chain:</div>';
                    stage.fallbacks.forEach(fb => {
                        html += '<div class="studio-details-fallback-item">';
                        html += `<span class="studio-details-fallback-from">${fb.from_model}</span>`;
                        html += '<span class="studio-details-fallback-arrow">→</span>';
                        html += `<span class="studio-details-fallback-to">${fb.to_model || 'next'}</span>`;
                        html += `<span class="studio-details-fallback-reason">(${fb.reason})</span>`;
                        html += '</div>';
                    });
                    html += '</div>';
                }

                // Quality
                if (stage.quality_label) {
                    html += '<div class="studio-details-quality">';
                    html += `<span class="studio-details-quality-label">${stage.quality_label}</span>`;
                    if (stage.quality_score) {
                        html += `<span class="studio-details-quality-score">${stage.quality_score}</span>`;
                    }
                    html += '</div>';
                }

                html += '</div>'; // stage-body
                html += '</div>'; // stage
            });
        }

        // Cross-stage
        if (data.cross_stage && Object.keys(data.cross_stage).length > 0) {
            html += '<div class="studio-details-cross-stage">';
            html += '<div class="studio-details-cross-stage-title">Cross-stage analysis</div>';
            const cs = data.cross_stage;
            if (cs.generate) {
                html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Draft quality:</span><span class="studio-details-cross-value">${cs.generate.summary}</span></div>`;
            }
            if (cs.review) {
                html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Review impact:</span><span class="studio-details-cross-value">${cs.review.summary}</span></div>`;
            }
            if (cs.refine) {
                html += `<div class="studio-details-cross-item"><span class="studio-details-cross-label">Refinement:</span><span class="studio-details-cross-value">${cs.refine.summary}</span></div>`;
            }
            html += '</div>';
        }

        html += '</div>'; // studio-details
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
        if (container) {
            container.scrollTop = container.scrollHeight;
        }
    }

    // ── Start ──
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
