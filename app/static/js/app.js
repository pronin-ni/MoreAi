document.addEventListener('DOMContentLoaded', function() {
    initModelSelection();
    initChatInput();
    initCopyButtons();
    initDiagnosticsToggle();
});

function initModelSelection() {
    const selectedInput = document.getElementById('models-selected-input');
    const savedModel = localStorage.getItem('selected_model') || selectedInput?.value || '';
    let radio = null;

    if (savedModel) {
        radio = document.querySelector(`input[name="model_selection"][value="${savedModel}"]`);
    }

    if (!radio) {
        radio = document.querySelector('input[name="model_selection"]:checked')
            || document.querySelector('input[name="model_selection"]:not(:disabled)');
    }

    if (radio) {
        radio.checked = true;
        selectModel(radio.value);
    }
}

function selectModel(modelId) {
    localStorage.setItem('selected_model', modelId);

    const modelInput = document.querySelector('input[name="model"]');
    const textarea = document.querySelector('textarea[name="message"]');
    const submitBtn = document.querySelector('.btn-send');
    const selectedRadio = document.querySelector(`input[name="model_selection"][value="${modelId}"]`);
    const selectedModelInput = document.getElementById('models-selected-input');
    const modelName = document.querySelector('.model-name');
    const selectedModelDisplay = document.querySelector('.selected-model-display');
    const currentSelectionDisplay = document.querySelector('.current-selection-display');
    const currentSelectionId = document.querySelector('.current-selection-id');
    const transportBadge = document.querySelector('.chat-model-badge .badge');
    const searchInput = document.getElementById('models-search-input');
    const displayName = selectedRadio?.dataset.displayName || modelId;

    if (selectedRadio) {
        selectedRadio.checked = true;
        syncSelectedModelState(modelId);
        selectedRadio.closest('.model-item')?.scrollIntoView({ block: 'nearest' });
    }

    if (modelInput) {
        modelInput.value = modelId;
    }
    if (selectedModelInput) {
        selectedModelInput.value = modelId;
    }
    if (textarea) {
        textarea.disabled = false;
        textarea.placeholder = `Message ${displayName}...`;
        textarea.focus();
    }
    if (submitBtn) {
        submitBtn.disabled = false;
    }

    if (modelName) {
        modelName.textContent = modelId;
    }
    if (selectedModelDisplay) {
        selectedModelDisplay.textContent = displayName;
    }
    if (currentSelectionDisplay) {
        currentSelectionDisplay.textContent = displayName;
    }
    if (currentSelectionId) {
        currentSelectionId.textContent = modelId;
    }

    if (transportBadge && selectedRadio) {
        const transport = selectedRadio.dataset.transport || '';
        transportBadge.className = `badge badge-${transport}`;
        transportBadge.textContent = transport.toUpperCase();
    }

    if (searchInput && !searchInput.value) {
        searchInput.blur();
    }
    
    updateDiagnostics(modelId);
}

function syncSelectedModelState(modelId) {
    document.querySelectorAll('.model-item').forEach((item) => {
        item.classList.remove('selected');
        const badge = item.querySelector('.badge-selected');
        if (badge) {
            badge.remove();
        }
    });

    const selectedRadio = document.querySelector(`input[name="model_selection"][value="${modelId}"]`);
    const selectedItem = selectedRadio?.closest('.model-item');
    if (!selectedItem) {
        return;
    }

    selectedItem.classList.add('selected');
    if (!selectedItem.querySelector('.badge-selected')) {
        const badge = document.createElement('span');
        badge.className = 'badge badge-selected';
        badge.textContent = 'Selected';
        const label = selectedItem.querySelector('.model-label');
        label?.appendChild(badge);
    }
}

function updateDiagnostics(modelId) {
    fetch(`/ui/diagnostics?model=${encodeURIComponent(modelId)}`)
        .then(r => r.text())
        .then(html => {
            const panel = document.getElementById('diagnostics-content-target');
            if (panel) {
                panel.innerHTML = html;
            }
        })
        .catch(err => console.error('Failed to update diagnostics:', err));
}

function initChatInput() {
    const textarea = document.querySelector('textarea[name="message"]');
    if (!textarea) return;
    
    textarea.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const form = document.getElementById('chat-form');
            if (form && this.value.trim()) {
                form.requestSubmit();
            }
        }
    });
    
    textarea.addEventListener('input', function() {
        this.style.height = '';
        this.style.height = Math.min(this.scrollHeight, 200) + 'px';
    });
}

function initCopyButtons() {
    window.copyMessage = function(btn) {
        const messageContent = btn.closest('.message-actions').previousElementSibling;
        const text = messageContent.innerText;
        
        navigator.clipboard.writeText(text).then(() => {
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>';
            setTimeout(() => {
                btn.innerHTML = originalHTML;
            }, 1500);
        }).catch(err => {
            console.error('Failed to copy:', err);
        });
    };
}

function initDiagnosticsToggle() {
    const panel = document.querySelector('.diagnostics-panel');
    if (!panel) return;
    
    const header = panel.querySelector('.diagnostics-header');
    if (!header) return;
    
    header.addEventListener('click', function() {
        panel.classList.toggle('expanded');
    });
}

document.body.addEventListener('htmx:afterSwap', function(evt) {
    if (evt.target?.id === 'models-panel') {
        initModelSelection();
    }

    if (evt.target?.id === 'chat-response-target') {
        const state = evt.target.querySelector('.chat-response-state');
        const status = state?.dataset.chatStatus || '';
        const textarea = document.querySelector('textarea[name="message"]');

        if (textarea && (status === 'success' || status === 'cleared')) {
            textarea.value = '';
            textarea.style.height = '';
        }

        if (textarea) {
            textarea.focus();
        }
    }
});
