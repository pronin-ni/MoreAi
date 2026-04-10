/**
 * MoreAI Admin — frontend JS.
 *
 * Login → stores token in localStorage → fetches admin API with token.
 * No frameworks, no HTMX — vanilla fetch + DOM.
 */

(() => {
    'use strict';

    // ── Config ──
    const API_BASE = '';
    const TOKEN_KEY = 'moreai_admin_token';

    // ── DOM refs ──
    const loginScreen = document.getElementById('login-screen');
    const dashboardScreen = document.getElementById('dashboard-screen');
    const loginForm = document.getElementById('login-form');
    const tokenInput = document.getElementById('admin-token-input');
    const loginError = document.getElementById('login-error');
    const loginBtn = document.getElementById('login-btn');
    const logoutBtn = document.getElementById('logout-btn');
    const refreshBtn = document.getElementById('refresh-btn');
    const clearTokenLink = document.getElementById('clear-token-link');
    const rollbackBtn = document.getElementById('rollback-btn');

    // ── State ──
    let adminToken = localStorage.getItem(TOKEN_KEY) || '';
    let currentTab = 'Overview';
    let allProviders = {};
    let allModels = [];
    let allActions = [];

    // ── Init ──
    function init() {
        if (adminToken) {
            showDashboard();
            loadAll();
        } else {
            showLogin();
        }

        loginForm.addEventListener('submit', onLogin);
        logoutBtn.addEventListener('click', onLogout);
        refreshBtn.addEventListener('click', loadAll);
        clearTokenLink.addEventListener('click', (e) => {
            e.preventDefault();
            clearToken();
            showLogin();
        });
        rollbackBtn.addEventListener('click', onRollback);

        // Tab navigation
        document.querySelectorAll('.nav-tab').forEach(btn => {
            btn.addEventListener('click', () => switchTab(btn.dataset.tab));
        });

        // Model search/filter
        const searchInput = document.getElementById('models-search');
        const transportFilter = document.getElementById('models-transport-filter');
        const statusFilter = document.getElementById('models-status-filter');
        const selectAllCheckbox = document.getElementById('models-select-all');
        searchInput.addEventListener('input', renderModels);
        transportFilter.addEventListener('change', renderModels);
        statusFilter.addEventListener('change', renderModels);
        if (selectAllCheckbox) {
            selectAllCheckbox.addEventListener('change', (e) => window.__admin.toggleSelectAll(e.target.checked));
        }
    }

    // ── Auth ──
    function showLogin() {
        loginScreen.classList.remove('hidden');
        dashboardScreen.classList.add('hidden');
        tokenInput.value = '';
        loginError.classList.add('hidden');
        tokenInput.focus();
    }

    function showDashboard() {
        loginScreen.classList.add('hidden');
        dashboardScreen.classList.remove('hidden');
    }

    function setToken(token) {
        adminToken = token;
        if (token) {
            localStorage.setItem(TOKEN_KEY, token);
        } else {
            localStorage.removeItem(TOKEN_KEY);
        }
    }

    function clearToken() {
        setToken('');
    }

    async function onLogin(e) {
        e.preventDefault();
        const token = tokenInput.value.trim();
        if (!token) return;

        loginBtn.textContent = 'Signing in...';
        loginBtn.disabled = true;
        loginError.classList.add('hidden');

        // Set token BEFORE making the request so fetchApi uses it
        setToken(token);

        try {
            const resp = await fetchApi('/admin/health');
            if (resp.ok) {
                showDashboard();
                await loadAll();
            } else {
                clearToken();
                throw new Error('Invalid token');
            }
        } catch (err) {
            loginError.textContent = err.message || 'Login failed';
            loginError.classList.remove('hidden');
        } finally {
            loginBtn.textContent = 'Sign In';
            loginBtn.disabled = false;
        }
    }

    function onLogout() {
        clearToken();
        showLogin();
    }

    // ── API ──
    async function fetchApi(path, options = {}) {
        const resp = await fetch(`${API_BASE}${path}`, {
            ...options,
            headers: {
                'X-Admin-Token': adminToken,
                'Content-Type': 'application/json',
                ...(options.headers || {}),
            },
        });
        if (resp.status === 401) {
            clearToken();
            showLogin();
            throw new Error('Session expired — please login again');
        }
        return resp;
    }

    async function loadAll() {
        await Promise.allSettled([
            loadStatus(),
            loadProviders(),
            loadModels(),
            loadActions(),
            loadEffectiveConfig(),
            loadPipelineData(),
        ]);
    }

    // ── Status ──
    async function loadStatus() {
        try {
            const resp = await fetchApi('/admin/status');
            const data = await resp.json();
            document.getElementById('stat-providers').textContent = data.provider_count || '—';
            document.getElementById('stat-models').textContent = data.model_count || '—';
            document.getElementById('stat-agent-models').textContent = data.agent_model_count || '—';
            document.getElementById('stat-config-version').textContent = data.config_version || '—';
            document.getElementById('stat-config-state').textContent = data.config_state || '—';
            document.getElementById('stat-version').textContent = data.version || '—';
            document.getElementById('system-status-json').textContent = JSON.stringify(data, null, 2);
        } catch (err) {
            console.error('Failed to load status:', err);
        }
    }

    // ── Providers ──
    async function loadProviders() {
        try {
            const resp = await fetchApi('/admin/providers');
            allProviders = await resp.json();
            renderProviders();
        } catch (err) {
            console.error('Failed to load providers:', err);
        }
    }

    function renderProviders() {
        const tbody = document.getElementById('providers-tbody');
        const rows = Object.entries(allProviders).map(([id, data]) => {
            const effectiveEnabled = data.enabled.effective_value;
            const source = data.enabled.source;
            const overrideState = data.override_state || 'none';

            let overrideBadge = '';
            if (source === 'override') {
                overrideBadge = '<span class="badge-badge badge-overridden">overridden</span>';
            } else {
                overrideBadge = '<span class="badge-badge badge-base">from base</span>';
            }

            return `
                <tr>
                    <td class="provider-name">${id}</td>
                    <td class="provider-transport">${data.override_applied_at ? '—' : '—'}</td>
                    <td>
                        <span class="status-dot ${effectiveEnabled ? 'status-on' : 'status-off'}"></span>
                        ${effectiveEnabled ? 'Enabled' : 'Disabled'}
                    </td>
                    <td>${overrideBadge}</td>
                    <td>
                        <span class="status-dot ${effectiveEnabled ? 'status-on' : 'status-off'}"></span>
                        ${effectiveEnabled ? 'Enabled' : 'Disabled'}
                    </td>
                    <td class="actions-cell">
                        <button
                            class="btn-sm ${effectiveEnabled ? 'btn-danger' : 'btn-success'}"
                            onclick="window.__admin.toggleProvider('${id}', ${!effectiveEnabled})"
                        >
                            ${effectiveEnabled ? 'Disable' : 'Enable'}
                        </button>
                    </td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = rows || '<tr><td colspan="6" class="loading-cell">No providers found</td></tr>';
    }

    window.__admin = window.__admin || {};
    window.__admin.toggleProvider = async (providerId, newEnabled) => {
        try {
            await fetchApi(`/admin/providers/${providerId}`, {
                method: 'PATCH',
                body: JSON.stringify({ enabled: newEnabled }),
            });
            await loadProviders();
            await loadStatus();
        } catch (err) {
            alert(`Failed to toggle provider: ${err.message}`);
        }
    };

    // ── Models ──
    let selectedModelIds = new Set();

    async function loadModels() {
        try {
            const resp = await fetchApi('/admin/models');
            allModels = await resp.json();
            selectedModelIds.clear();
            renderModels();
            updateBulkBar();
        } catch (err) {
            console.error('Failed to load models:', err);
        }
    }

    function renderModels() {
        const tbody = document.getElementById('models-tbody');
        const search = (document.getElementById('models-search').value || '').toLowerCase();
        const transportFilter = document.getElementById('models-transport-filter').value;
        const statusFilter = document.getElementById('models-status-filter').value;

        let filtered = allModels;
        if (search) {
            filtered = filtered.filter(m =>
                m.id.toLowerCase().includes(search) ||
                m.provider_id.toLowerCase().includes(search)
            );
        }
        if (transportFilter) {
            filtered = filtered.filter(m => m.transport === transportFilter);
        }
        if (statusFilter === 'enabled') {
            filtered = filtered.filter(m => m.enabled);
        } else if (statusFilter === 'disabled') {
            filtered = filtered.filter(m => !m.enabled);
        } else if (statusFilter === 'hidden') {
            filtered = filtered.filter(m => (m.visibility || 'public') === 'hidden');
        }

        const countBadge = document.getElementById('models-count-badge');
        countBadge.textContent = `${filtered.length} / ${allModels.length}`;

        const rows = filtered.slice(0, 200).map(m => {
            const visibility = m.visibility || 'public';
            const visibilityBadgeClass = visibility === 'hidden' ? 'badge-hidden' : 'badge-public';
            const visibilityLabel = visibility.charAt(0).toUpperCase() + visibility.slice(1);
            const escapedId = m.id.replace(/'/g, "\\'");
            const isSelected = selectedModelIds.has(m.id);

            return `
            <tr class="${isSelected ? 'row-selected' : ''}">
                <td class="col-checkbox">
                    <input type="checkbox" class="form-checkbox model-checkbox" value="${escapedId}" ${isSelected ? 'checked' : ''} onchange="window.__admin.toggleSelect('${escapedId}', this.checked)">
                </td>
                <td class="model-id" title="${m.id}">${truncate(m.id, 60)}</td>
                <td><span class="badge-badge badge-${m.transport}">${m.transport}</span></td>
                <td>${m.provider_id}</td>
                <td>
                    <span class="status-dot ${m.enabled ? 'status-on' : 'status-off'}"></span>
                    ${m.enabled ? 'Yes' : 'No'}
                </td>
                <td>
                    <span class="badge-badge ${visibilityBadgeClass}">${visibilityLabel}</span>
                </td>
                <td>
                    <span class="status-dot ${m.available ? 'status-on' : 'status-off'}"></span>
                    ${m.available ? 'Yes' : 'No'}
                </td>
                <td class="actions-cell">
                    <button
                        class="btn-sm ${m.enabled ? 'btn-danger' : 'btn-success'}"
                        onclick="window.__admin.toggleModel('${escapedId}', ${!m.enabled})"
                    >
                        ${m.enabled ? 'Disable' : 'Enable'}
                    </button>
                    <button
                        class="btn-sm btn-secondary"
                        onclick="window.__admin.toggleVisibility('${escapedId}', '${visibility}')"
                    >
                        ${visibility === 'hidden' ? 'Show' : 'Hide'}
                    </button>
                </td>
            </tr>
        `}).join('');

        tbody.innerHTML = rows || '<tr><td colspan="8" class="loading-cell">No models match your filters</td></tr>';
        updateSelectAllCheckbox();
    }

    window.__admin = window.__admin || {};

    window.__admin.toggleSelect = (modelId, checked) => {
        if (checked) {
            selectedModelIds.add(modelId);
        } else {
            selectedModelIds.delete(modelId);
        }
        updateBulkBar();
        updateSelectAllCheckbox();
    };

    window.__admin.toggleSelectAll = (checked) => {
        const search = (document.getElementById('models-search').value || '').toLowerCase();
        const transportFilter = document.getElementById('models-transport-filter').value;
        const statusFilter = document.getElementById('models-status-filter').value;

        let filtered = allModels;
        if (search) {
            filtered = filtered.filter(m =>
                m.id.toLowerCase().includes(search) ||
                m.provider_id.toLowerCase().includes(search)
            );
        }
        if (transportFilter) {
            filtered = filtered.filter(m => m.transport === transportFilter);
        }
        if (statusFilter === 'enabled') {
            filtered = filtered.filter(m => m.enabled);
        } else if (statusFilter === 'disabled') {
            filtered = filtered.filter(m => !m.enabled);
        } else if (statusFilter === 'hidden') {
            filtered = filtered.filter(m => (m.visibility || 'public') === 'hidden');
        }

        const visibleIds = new Set(filtered.slice(0, 200).map(m => m.id));

        if (checked) {
            visibleIds.forEach(id => selectedModelIds.add(id));
        } else {
            visibleIds.forEach(id => selectedModelIds.delete(id));
        }

        renderModels();
        updateBulkBar();
    };

    window.__admin.clearSelection = () => {
        selectedModelIds.clear();
        renderModels();
        updateBulkBar();
    };

    function updateBulkBar() {
        const bulkBar = document.getElementById('models-bulk-bar');
        const bulkCount = document.getElementById('models-bulk-count');

        if (selectedModelIds.size > 0) {
            bulkBar.classList.remove('hidden');
            bulkCount.textContent = `${selectedModelIds.size} selected`;
        } else {
            bulkBar.classList.add('hidden');
            bulkCount.textContent = '0 selected';
        }
    }

    function updateSelectAllCheckbox() {
        const checkbox = document.getElementById('models-select-all');
        if (!checkbox) return;

        const search = (document.getElementById('models-search').value || '').toLowerCase();
        const transportFilter = document.getElementById('models-transport-filter').value;
        const statusFilter = document.getElementById('models-status-filter').value;

        let filtered = allModels;
        if (search) {
            filtered = filtered.filter(m =>
                m.id.toLowerCase().includes(search) ||
                m.provider_id.toLowerCase().includes(search)
            );
        }
        if (transportFilter) {
            filtered = filtered.filter(m => m.transport === transportFilter);
        }
        if (statusFilter === 'enabled') {
            filtered = filtered.filter(m => m.enabled);
        } else if (statusFilter === 'disabled') {
            filtered = filtered.filter(m => !m.enabled);
        } else if (statusFilter === 'hidden') {
            filtered = filtered.filter(m => (m.visibility || 'public') === 'hidden');
        }

        const visibleIds = new Set(filtered.slice(0, 200).map(m => m.id));
        const selectedVisible = [...visibleIds].filter(id => selectedModelIds.has(id)).length;

        checkbox.checked = visibleIds.size > 0 && selectedVisible === visibleIds.size;
        checkbox.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.size;
    }

    window.__admin.bulkAction = async (action) => {
        if (selectedModelIds.size === 0) return;

        const count = selectedModelIds.size;
        const actionName = action.charAt(0).toUpperCase() + action.slice(1);

        if (!confirm(`${actionName} ${count} model(s)?`)) return;

        let patch = {};
        if (action === 'enable') {
            patch = { enabled: true };
        } else if (action === 'disable') {
            patch = { enabled: false };
        } else if (action === 'hide') {
            patch = { visibility: 'hidden' };
        } else if (action === 'show') {
            patch = { visibility: 'public' };
        }

        const promises = [...selectedModelIds].map(id =>
            fetchApi(`/admin/models/${id}`, {
                method: 'PATCH',
                body: JSON.stringify(patch),
            }).catch(err => console.error(`Failed to ${action} ${id}:`, err))
        );

        await Promise.allSettled(promises);
        selectedModelIds.clear();
        await loadModels();
        await loadStatus();
    };

    window.__admin.toggleModel = async (modelId, newEnabled) => {
        try {
            await fetchApi(`/admin/models/${modelId}`, {
                method: 'PATCH',
                body: JSON.stringify({ enabled: newEnabled }),
            });
            await loadModels();
            await loadStatus();
        } catch (err) {
            alert(`Failed to toggle model: ${err.message}`);
        }
    };

    window.__admin.toggleVisibility = async (modelId, currentVisibility) => {
        try {
            const newVisibility = currentVisibility === 'hidden' ? 'public' : 'hidden';
            await fetchApi(`/admin/models/${modelId}`, {
                method: 'PATCH',
                body: JSON.stringify({ visibility: newVisibility }),
            });
            await loadModels();
            await loadStatus();
        } catch (err) {
            alert(`Failed to toggle visibility: ${err.message}`);
        }
    };

    // ── Health Tab ──
    async function loadHealth() {
        try {
            const resp = await fetchApi('/diagnostics/status');
            const data = await resp.json();
            renderHealth(data);
        } catch (err) {
            console.error('Failed to load health data:', err);
        }
        try {
            const resp = await fetchApi('/diagnostics/failures');
            const data = await resp.json();
            document.getElementById('recent-failures-json').textContent =
                JSON.stringify(data, null, 2);
        } catch (err) {
            console.error('Failed to load failures:', err);
        }
        try {
            const resp = await fetchApi('/diagnostics/routing');
            const data = await resp.json();
            document.getElementById('routing-decisions-json').textContent =
                JSON.stringify(data, null, 2);
        } catch (err) {
            console.error('Failed to load routing:', err);
        }
    }

    function renderHealth(data) {
        const degradedCount = data.degraded_components?.length || 0;
        document.getElementById('health-status').textContent =
            degradedCount > 0 ? 'degraded' : 'healthy';
        document.getElementById('health-status').style.color =
            degradedCount > 0 ? 'var(--admin-danger)' : 'var(--admin-success)';
        document.getElementById('health-workers').textContent =
            data.worker_pool?.active_workers ?? '—';
        document.getElementById('health-queue').textContent =
            data.queue_stats?.current_size ?? '—';
        document.getElementById('health-degraded').textContent = degradedCount;

        const degradedCard = document.getElementById('health-degraded-card');
        const degradedList = document.getElementById('degraded-list');
        if (degradedCount > 0) {
            degradedCard.classList.remove('hidden');
            degradedList.innerHTML = data.degraded_components
                .map(c => `<li>${c}</li>`).join('');
        } else {
            degradedCard.classList.add('hidden');
        }
    }

    window.__admin.lookupRoutingPlan = async () => {
        const modelInput = document.getElementById('routing-plan-model');
        const model = modelInput.value.trim();
        if (!model) return;

        try {
            const resp = await fetchApi(`/diagnostics/routing/plan?model=${encodeURIComponent(model)}`);
            const data = await resp.json();
            document.getElementById('routing-plan-json').textContent =
                JSON.stringify(data, null, 2);
        } catch (err) {
            document.getElementById('routing-plan-json').textContent =
                `Error: ${err.message}`;
        }
    };

    // ── Actions ──
    async function loadActions() {
        try {
            const resp = await fetchApi('/admin/actions');
            allActions = await resp.json();
            renderActions();
        } catch (err) {
            console.error('Failed to load actions:', err);
        }
    }

    function renderActions() {
        const container = document.getElementById('actions-list');
        container.innerHTML = allActions.map(a => `
            <div class="action-card ${a.destructive ? 'action-destructive' : ''}">
                <div class="action-info">
                    <h4 class="action-title">${a.display_name}</h4>
                    <p class="action-desc">${a.description}</p>
                    <span class="badge-badge badge-category">${a.category}</span>
                    ${a.destructive ? '<span class="badge-badge badge-destructive">destructive</span>' : ''}
                </div>
                <button
                    class="btn-sm btn-action"
                    onclick="window.__admin.executeAction('${a.id}')"
                >
                    Execute
                </button>
            </div>
        `).join('');
    }

    window.__admin.executeAction = async (actionId) => {
        if (!confirm(`Execute action "${actionId}"?`)) return;

        const resultDiv = document.getElementById('rollback-result');
        resultDiv.classList.remove('hidden');
        resultDiv.textContent = 'Executing...';

        try {
            const resp = await fetchApi(`/admin/actions/${actionId}`, { method: 'POST' });
            const data = await resp.json();
            resultDiv.className = `action-result ${data.status === 'success' ? 'result-success' : 'result-error'}`;
            resultDiv.textContent = JSON.stringify(data, null, 2);
            await loadStatus();
        } catch (err) {
            resultDiv.className = 'action-result result-error';
            resultDiv.textContent = err.message;
        }
    };

    async function onRollback() {
        if (!confirm('Rollback config to previous version?')) return;

        const resultDiv = document.getElementById('rollback-result');
        resultDiv.classList.remove('hidden');
        resultDiv.textContent = 'Rolling back...';

        try {
            const resp = await fetchApi('/admin/rollback/last', { method: 'POST' });
            const data = await resp.json();
            resultDiv.className = 'action-result result-success';
            resultDiv.textContent = JSON.stringify(data, null, 2);
            await loadAll();
        } catch (err) {
            resultDiv.className = 'action-result result-error';
            resultDiv.textContent = err.message;
        }
    }

    // ── Effective Config ──
    async function loadEffectiveConfig() {
        try {
            const resp = await fetchApi('/admin/config/effective');
            const data = await resp.json();
            document.getElementById('effective-config-json').textContent = JSON.stringify(data, null, 2);
        } catch (err) {
            console.error('Failed to load effective config:', err);
        }
    }

    // ── Tabs ──
    function switchTab(tabName) {
        currentTab = tabName;

        document.querySelectorAll('.nav-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabName);
        });
        document.querySelectorAll('.tab-content').forEach(el => {
            el.classList.toggle('active', el.id === `tab-${tabName}`);
        });

        // Load data for tab if not yet loaded
        if (tabName === 'Providers' && Object.keys(allProviders).length === 0) {
            loadProviders();
        } else if (tabName === 'Models' && allModels.length === 0) {
            loadModels();
        } else if (tabName === 'Health') {
            loadHealth();
        } else if (tabName === 'Actions' && allActions.length === 0) {
            loadActions();
        } else if (tabName === 'Config') {
            loadEffectiveConfig();
        } else if (tabName === 'Sandbox') {
            loadSandboxModels();
        } else if (tabName === 'Analytics') {
            loadAnalytics();
        } else if (tabName === 'Canary') {
            loadCanary();
        } else if (tabName === 'Healing') {
            loadHealing();
        } else if (tabName === 'Recon') {
            loadRecon();
        } else if (tabName === 'Baseline') {
            loadBaseline();
        } else if (tabName === 'Maintenance') {
            loadMaintenance();
        } else if (tabName === 'Pipelines') {
            loadPipelineData();
        } else if (tabName === 'Scoring') {
            loadScoringData();
        } else if (tabName === 'Trends') {
            loadTrendsData();
        }
    }

    // ── Helpers ──
    function truncate(str, maxLen) {
        return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
    }

    function downloadJson(data, filename) {
        const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
    }

    // ── Sandbox ──
    async function loadSandboxModels() {
        try {
            const resp = await fetchApi('/admin/sandbox/models');
            const models = await resp.json();
            const select = document.getElementById('sandbox-model');
            if (!select) return;
            select.innerHTML = '';
            models.forEach(m => {
                const opt = document.createElement('option');
                opt.value = m.id;
                opt.textContent = `${m.id} (${m.transport})`;
                select.appendChild(opt);
            });
        } catch (e) {
            console.error('Failed to load sandbox models:', e);
        }
    }

    async function runSandboxPrompt() {
        const modelId = document.getElementById('sandbox-model').value;
        const prompt = document.getElementById('sandbox-prompt').value;
        const maxTokens = parseInt(document.getElementById('sandbox-max-tokens').value) || 2048;
        const temperature = parseFloat(document.getElementById('sandbox-temperature').value) || 0.7;
        const resultDiv = document.getElementById('sandbox-result');
        const btn = document.getElementById('sandbox-run-btn');

        if (!prompt.trim()) { alert('Please enter a prompt'); return; }

        btn.disabled = true;
        btn.textContent = 'Running...';
        resultDiv.classList.add('hidden');

        try {
            const resp = await fetchApi('/admin/sandbox/run', {
                method: 'POST',
                body: JSON.stringify({ prompt, model_id: modelId, max_tokens: maxTokens, temperature }),
            });
            const result = await resp.json();
            resultDiv.textContent = JSON.stringify(result, null, 2);
            resultDiv.classList.remove('hidden');
        } catch (e) {
            resultDiv.textContent = `Error: ${e.message}`;
            resultDiv.classList.remove('hidden');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Run Prompt';
        }
    }

    async function runCompare() {
        const modelsInput = document.getElementById('compare-models').value;
        const prompt = document.getElementById('compare-prompt').value;
        const resultDiv = document.getElementById('compare-result');
        const btn = document.getElementById('compare-run-btn');

        const modelIds = modelsInput.split(',').map(s => s.trim()).filter(Boolean);
        if (modelIds.length < 2) { alert('Please enter at least 2 models (comma-separated)'); return; }
        if (!prompt.trim()) { alert('Please enter a prompt'); return; }

        btn.disabled = true;
        btn.textContent = 'Running...';
        resultDiv.classList.add('hidden');

        try {
            const resp = await fetchApi('/admin/sandbox/compare', {
                method: 'POST',
                body: JSON.stringify({ prompt, model_ids: modelIds }),
            });
            const result = await resp.json();
            resultDiv.textContent = JSON.stringify(result, null, 2);
            resultDiv.classList.remove('hidden');
        } catch (e) {
            resultDiv.textContent = `Error: ${e.message}`;
            resultDiv.classList.remove('hidden');
        } finally {
            btn.disabled = false;
            btn.textContent = 'Run Comparison';
        }
    }

    // ── Analytics ──
    async function loadAnalytics() {
        try {
            const resp = await fetchApi('/admin/analytics/usage');
            const data = await resp.json();

            const totalRequests = (data.top_models || []).reduce((sum, m) => sum + (m.request_count || 0), 0);
            const totalErrors = (data.top_models || []).reduce((sum, m) => sum + (m.error_count || 0), 0);
            const errorRate = totalRequests > 0 ? ((totalErrors / totalRequests) * 100).toFixed(1) + '%' : '0%';

            const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            set('analytics-total-requests', totalRequests || '—');
            set('analytics-error-rate', errorRate);
            set('analytics-fallbacks', (data.fallback_summary && data.fallback_summary.total_fallbacks) || '—');
            set('analytics-models-count', (data.top_models || []).length || '—');

            const setJson = (id, val, fallback) => {
                const el = document.getElementById(id);
                if (el) el.textContent = (val && val.length) ? JSON.stringify(val, null, 2) : fallback;
            };
            setJson('analytics-top-models', data.top_models, 'No data yet — make some requests first');
            setJson('analytics-top-providers', data.top_providers, 'No data yet');
            setJson('analytics-errors', data.error_summary, 'No errors');
            setJson('analytics-fallbacks-detail', data.fallback_summary ? [data.fallback_summary] : null, 'No fallbacks');
        } catch (e) {
            console.error('Failed to load analytics:', e);
            const el = document.getElementById('analytics-top-models');
            if (el) el.textContent = `Error: ${e.message}`;
        }
    }

    // ── Canary ──
    async function loadCanary() {
        try {
            const resp1 = await fetchApi('/admin/canary/active');
            const active = await resp1.json();
            const el1 = document.getElementById('canary-active');
            if (el1) el1.textContent = active.length ? JSON.stringify(active, null, 2) : 'No active canaries';

            const resp2 = await fetchApi('/admin/canary/history');
            const history = await resp2.json();
            const el2 = document.getElementById('canary-history-list');
            if (el2) el2.textContent = history.length ? JSON.stringify(history, null, 2) : 'No history';
        } catch (e) {
            console.error('Failed to load canary:', e);
        }
    }

    async function registerCanary() {
        const modelId = document.getElementById('canary-model-id').value;
        const providerId = document.getElementById('canary-provider-id').value;
        const status = document.getElementById('canary-status').value;
        const traffic = parseFloat(document.getElementById('canary-traffic').value) || 10;
        const errorThreshold = parseFloat(document.getElementById('canary-error-threshold').value) || 0.1;
        const notes = document.getElementById('canary-notes').value;

        if (!modelId || !providerId) { alert('Please fill in Model ID and Provider ID'); return; }

        try {
            const resp = await fetchApi('/admin/canary', {
                method: 'POST',
                body: JSON.stringify({
                    model_id: modelId, provider_id: providerId, status,
                    traffic_percentage: traffic, error_threshold: errorThreshold, notes,
                }),
            });
            const result = await resp.json();
            alert(`Canary registered: ${result.model_id} (${result.status})`);
            loadCanary();
        } catch (e) {
            alert(`Failed to register canary: ${e.message}`);
        }
    }

    // ── Healing ──
    async function loadHealing() {
        try {
            const resp = await fetchApi('/admin/healing/health');
            const data = await resp.json();

            document.getElementById('healing-total').textContent = data.summary.total || '—';
            document.getElementById('healing-healthy').textContent = data.summary.healthy || '—';
            document.getElementById('healing-degrading').textContent = data.summary.degrading || '—';
            document.getElementById('healing-broken').textContent = data.summary.broken || '—';

            // Render health table
            const tbody = document.getElementById('healing-health-tbody');
            if (data.all && data.all.length > 0) {
                tbody.innerHTML = data.all.map(h => {
                    const statusColor = h.status === 'healthy' ? '#4ade80' : h.status === 'degrading' ? '#fbbf24' : '#f87171';
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${h.provider_id}</td>
                        <td style="padding: 0.4rem;">${h.role}</td>
                        <td style="padding: 0.4rem; text-align: center; font-weight: 500; color: ${statusColor};">${h.health_score.toFixed(2)}</td>
                        <td style="padding: 0.4rem; text-align: center;"><span style="background: ${statusColor}22; color: ${statusColor}; padding: 0.15rem 0.5rem; border-radius: 3px;">${h.status}</span></td>
                        <td style="padding: 0.4rem; text-align: center;">${h.primary_success_rate.toFixed(2)}</td>
                        <td style="padding: 0.4rem; text-align: center;">${h.fallback_success_rate.toFixed(2)}</td>
                        <td style="padding: 0.4rem; text-align: center;">${h.healing_usage_rate.toFixed(2)}</td>
                        <td style="padding: 0.4rem; text-align: center;">${h.avg_confidence.toFixed(2)}</td>
                    </tr>`;
                }).join('');
            } else {
                tbody.innerHTML = '<tr><td colspan="8" style="padding: 1rem; text-align: center;">No data yet — make some requests first</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load healing health:', e);
            const tbody = document.getElementById('healing-health-tbody');
            if (tbody) tbody.innerHTML = `<tr><td colspan="8" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${e.message}</td></tr>`;
        }

        // Load cache
        try {
            const resp = await fetchApi('/admin/healing/cache');
            const cache = await resp.json();
            document.getElementById('healing-cache').textContent =
                cache.length ? JSON.stringify(cache, null, 2) : 'No cached entries';
        } catch (e) {
            document.getElementById('healing-cache').textContent = `Error: ${e.message}`;
        }

        // Load candidates
        try {
            const resp = await fetchApi('/admin/healing/candidates');
            const candidates = await resp.json();
            document.getElementById('healing-candidates').textContent =
                candidates.length ? JSON.stringify(candidates, null, 2) : 'No recent candidates';
        } catch (e) {
            document.getElementById('healing-candidates').textContent = `Error: ${e.message}`;
        }
    }

    // ── Recon ──
    async function loadRecon() {
        try {
            const resp = await fetchApi('/admin/recon/snapshot');
            const data = await resp.json();

            document.getElementById('recon-attempts').textContent = data.total_attempts || '—';
            document.getElementById('recon-successes').textContent = data.total_successes || '—';
            document.getElementById('recon-partials').textContent = data.total_partials || '—';
            document.getElementById('recon-failures').textContent = data.total_failures || '—';
            document.getElementById('recon-success-rate').textContent = data.overall_success_rate ? (data.overall_success_rate * 100).toFixed(0) + '%' : '—';

            // Per-provider table
            const tbody = document.getElementById('recon-provider-tbody');
            if (data.per_provider && data.per_provider.length > 0) {
                tbody.innerHTML = data.per_provider.map(p => {
                    const rateColor = p.success_rate > 0.7 ? '#4ade80' : p.success_rate > 0.4 ? '#fbbf24' : '#f87171';
                    const lastResult = p.last_recovery_recovered ? '✅ recovered' : p.last_recovery_reason ? '⚠️ ' + p.last_recovery_reason.substring(0, 30) : '—';
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${p.provider_id}</td>
                        <td style="padding: 0.4rem; text-align: center;">${p.attempts}</td>
                        <td style="padding: 0.4rem; text-align: center; color: ${rateColor};">${(p.success_rate * 100).toFixed(0)}%</td>
                        <td style="padding: 0.4rem; text-align: center;">${p.partials}</td>
                        <td style="padding: 0.4rem; text-align: center;">${p.failures}</td>
                        <td style="padding: 0.4rem; text-align: center;">${p.avg_duration_ms.toFixed(0)}ms</td>
                        <td style="padding: 0.4rem; text-align: center; font-size: 0.7rem;">${lastResult}</td>
                    </tr>`;
                }).join('');
            } else {
                tbody.innerHTML = '<tr><td colspan="7" style="padding: 1rem; text-align: center;">No recon events yet</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load recon snapshot:', e);
            document.getElementById('recon-provider-tbody').innerHTML = `<tr><td colspan="7" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${e.message}</td></tr>`;
        }

        // Recent events
        try {
            const resp = await fetchApi('/admin/recon/events');
            const events = await resp.json();
            const tbody = document.getElementById('recon-events-tbody');
            if (events && events.length > 0) {
                tbody.innerHTML = events.slice(-15).reverse().map(e => {
                    const resultColor = e.result === 'success' ? '#4ade80' : e.result === 'partial' ? '#fbbf24' : '#f87171';
                    const resultLabel = e.result === 'success' ? '✅' : e.result === 'partial' ? '⚠️ partial' : '❌ failed';
                    const replayLabel = e.replay_succeeded ? '✅' : e.replay_succeeded === false ? '❌' : '—';
                    const reason = e.reason || e.trigger_reason || e.blocking_state || '';
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.3rem;">${e.provider_id}</td>
                        <td style="padding: 0.3rem; font-size: 0.7rem;">${e.action}</td>
                        <td style="padding: 0.3rem; text-align: center; color: ${resultColor};">${resultLabel}</td>
                        <td style="padding: 0.3rem; text-align: center;">${e.duration_ms ? e.duration_ms.toFixed(0) + 'ms' : '—'}</td>
                        <td style="padding: 0.3rem; text-align: center;">${replayLabel}</td>
                        <td style="padding: 0.3rem; font-size: 0.7rem;">${reason.substring(0, 50)}</td>
                    </tr>`;
                }).join('');
            } else {
                tbody.innerHTML = '<tr><td colspan="6" style="padding: 1rem; text-align: center;">No recent events</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load recon events:', e);
        }
    }

    // ── DOM Baseline ──
    async function loadBaseline() {
        try {
            const resp = await fetchApi('/admin/dom-baseline/providers');
            const data = await resp.json();

            document.getElementById('baseline-total').textContent = data.total_baselines || '—';
            document.getElementById('baseline-providers').textContent = Object.keys(data.providers || {}).length || '—';

            // Coverage table
            const covTbody = document.getElementById('baseline-coverage-tbody');
            if (data.providers && Object.keys(data.providers).length > 0) {
                covTbody.innerHTML = Object.entries(data.providers).map(([pid, roles]) =>
                    `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${pid}</td>
                        <td style="padding: 0.4rem;">${roles.map(r => `<span style="background: var(--admin-surface-2); padding: 0.1rem 0.3rem; border-radius: 2px; font-size: 0.7rem;">${r}</span>`).join(' ')}</td>
                    </tr>`
                ).join('');
            } else {
                covTbody.innerHTML = '<tr><td colspan="2" style="padding: 1rem; text-align: center;">No baselines captured yet</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load baseline coverage:', e);
            document.getElementById('baseline-coverage-tbody').innerHTML = `<tr><td colspan="2" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${e.message}</td></tr>`;
        }

        // Drift events
        try {
            const resp = await fetchApi('/admin/dom-diff/recent');
            const data = await resp.json();
            document.getElementById('drift-total').textContent = data.total_events || '—';

            const highCount = (data.events || []).filter(e => e.diff_result && e.diff_result.drift_severity === 'high').length;
            document.getElementById('drift-high').textContent = highCount || '—';

            const tbody = document.getElementById('drift-events-tbody');
            if (data.events && data.events.length > 0) {
                tbody.innerHTML = data.events.slice(-15).reverse().map(e => {
                    const sev = e.diff_result ? e.diff_result.drift_severity : 'none';
                    const sevColor = sev === 'high' ? '#f87171' : sev === 'medium' ? '#fbbf24' : sev === 'low' ? '#60a5fa' : '#4ade80';
                    const summary = e.diff_result ? e.diff_result.human_summary : '';
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.3rem;">${e.provider_id}</td>
                        <td style="padding: 0.3rem;">${e.role}</td>
                        <td style="padding: 0.3rem; text-align: center; color: ${sevColor};">${sev}</td>
                        <td style="padding: 0.3rem; font-size: 0.7rem;">${summary.substring(0, 60)}</td>
                    </tr>`;
                }).join('');
            } else {
                tbody.innerHTML = '<tr><td colspan="4" style="padding: 1rem; text-align: center;">No drift events</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load drift events:', e);
        }

        // Refresh status
        try {
            const resp = await fetchApi('/dom-refresh/status');
            const data = await resp.json();
            const statusTbody = document.getElementById('refresh-status-tbody');
            if (data.per_provider && Object.keys(data.per_provider).length > 0) {
                statusTbody.innerHTML = Object.entries(data.per_provider).map(([pid, stats]) => {
                    const statusColor = stats.last_result === 'success' ? '#4ade80' : stats.last_result === 'partial' ? '#fbbf24' : stats.last_result === 'aborted' ? '#60a5fa' : '#f87171';
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${pid}</td>
                        <td style="padding: 0.4rem; text-align: center;">${stats.attempts || 0}</td>
                        <td style="padding: 0.4rem; text-align: center;">${stats.successes || 0}</td>
                        <td style="padding: 0.4rem; text-align: center; color: ${statusColor};">${stats.last_result || '—'}</td>
                        <td style="padding: 0.4rem; text-align: center;">
                            <button class="btn-refresh-single" data-provider="${pid}" style="padding: 2px 6px; background: #60a5fa; border: none; border-radius: 2px; cursor: pointer; color: #fff;">↻</button>
                        </td>
                    </tr>`;
                }).join('');
            } else {
                statusTbody.innerHTML = '<tr><td colspan="5" style="padding: 1rem; text-align: center;">No refresh data yet</td></tr>';
            }

            // Recent refresh events
            const eventsResp = await fetchApi('/dom-refresh/events');
            const eventsData = await eventsResp.json();
            const eventsTbody = document.getElementById('refresh-events-tbody');
            if (eventsData && eventsData.length > 0) {
                eventsTbody.innerHTML = eventsData.slice(-10).reverse().map(e => {
                    const statusColor = e.status === 'success' ? '#4ade80' : e.status === 'partial' ? '#fbbf24' : e.status === 'aborted' ? '#60a5fa' : '#f87171';
                    const driftInfo = e.drift_detected ? `⚠️ drift: ${e.drift_summary.substring(0, 30)}` : (e.abort_reason ? `🚫 ${e.abort_reason.substring(0, 30)}` : '—');
                    return `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.3rem;">${e.provider_id}</td>
                        <td style="padding: 0.3rem; text-align: center; color: ${statusColor};">${e.status}</td>
                        <td style="padding: 0.3rem; text-align: center;">${e.duration_ms ? e.duration_ms.toFixed(0) + 'ms' : '—'}</td>
                        <td style="padding: 0.3rem; text-align: center;">${e.baseline_updates || 0}</td>
                        <td style="padding: 0.3rem; font-size: 0.7rem;">${driftInfo}</td>
                    </tr>`;
                }).join('');
            }
        } catch (e) {
            console.error('Failed to load refresh status:', e);
        }
    }

    // ── Selector Maintenance ──
    async function loadMaintenance() {
        try {
            // Load stats
            const statsResp = await fetchApi('/admin/dom-baseline/providers');
            const statsData = await statsResp.json();

            // Load suggestions
            const suggResp = await fetchApi('/selector-suggestions');
            const suggData = await suggResp.json();
            document.getElementById('maintenance-pending').textContent = suggData.count || '—';

            // Load suggestions by status for approved/rejected counts
            const approvedResp = await fetchApi('/selector-suggestions?status=approved');
            const approvedData = await approvedResp.json();
            document.getElementById('maintenance-approved').textContent = approvedData.count || '—';

            const rejectedResp = await fetchApi('/selector-suggestions?status=rejected');
            const rejectedData = await rejectedResp.json();
            document.getElementById('maintenance-rejected').textContent = rejectedData.count || '—';

            // Load overrides
            const ovResp = await fetchApi('/selector-overrides');
            const ovData = await ovResp.json();
            document.getElementById('maintenance-overrides').textContent = ovData.length || '—';

            // Render suggestions table
            const suggTbody = document.getElementById('suggestions-tbody');
            if (suggData.suggestions && suggData.suggestions.length > 0) {
                suggTbody.innerHTML = suggData.suggestions.map(s =>
                    `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${s.provider_id}</td>
                        <td style="padding: 0.4rem;">${s.role}</td>
                        <td style="padding: 0.4rem; font-size: 0.7rem; max-width: 200px; overflow: hidden; text-overflow: ellipsis;">${s.suggested_selector}</td>
                        <td style="padding: 0.4rem; text-align: center;">${(s.confidence * 100).toFixed(0)}%</td>
                        <td style="padding: 0.4rem; text-align: center;">${s.times_observed}</td>
                        <td style="padding: 0.4rem; text-align: center;">
                            <button class="btn-approve" data-id="${s.id}" style="margin: 0 2px; padding: 2px 6px; background: #4ade80; border: none; border-radius: 2px; cursor: pointer; color: #000;">✓</button>
                            <button class="btn-reject" data-id="${s.id}" style="margin: 0 2px; padding: 2px 6px; background: #f87171; border: none; border-radius: 2px; cursor: pointer; color: #fff;">✗</button>
                            <button class="btn-dismiss" data-id="${s.id}" style="margin: 0 2px; padding: 2px 6px; background: #fbbf24; border: none; border-radius: 2px; cursor: pointer; color: #000;">⌫</button>
                        </td>
                    </tr>`
                ).join('');
            } else {
                suggTbody.innerHTML = '<tr><td colspan="6" style="padding: 1rem; text-align: center;">No pending suggestions</td></tr>';
            }

            // Render overrides table
            const ovTbody = document.getElementById('overrides-tbody');
            if (ovData && ovData.length > 0) {
                ovTbody.innerHTML = ovData.map(o =>
                    `<tr style="border-bottom: 1px solid var(--admin-border);">
                        <td style="padding: 0.4rem;">${o.provider_id}</td>
                        <td style="padding: 0.4rem;">${o.role}</td>
                        <td style="padding: 0.4rem; font-size: 0.7rem;">${o.selector}</td>
                        <td style="padding: 0.4rem; text-align: center;">${o.source}</td>
                        <td style="padding: 0.4rem; text-align: center;">
                            <button class="btn-reset-override" data-provider="${o.provider_id}" data-role="${o.role}" style="padding: 2px 6px; background: #f87171; border: none; border-radius: 2px; cursor: pointer; color: #fff;">Reset</button>
                        </td>
                    </tr>`
                ).join('');
            } else {
                ovTbody.innerHTML = '<tr><td colspan="5" style="padding: 1rem; text-align: center;">No active overrides</td></tr>';
            }
        } catch (e) {
            console.error('Failed to load maintenance data:', e);
            document.getElementById('suggestions-tbody').innerHTML = `<tr><td colspan="6" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${e.message}</td></tr>`;
        }
    }

    // ── Start ──
    init();

    // ── Event listeners for new tabs ──
    document.addEventListener('DOMContentLoaded', () => {
        const sandboxRunBtn = document.getElementById('sandbox-run-btn');
        const compareRunBtn = document.getElementById('compare-run-btn');
        const canaryRegisterBtn = document.getElementById('canary-register-btn');
        const reconRefreshBtn = document.getElementById('recon-refresh-btn');
        const reconClearBtn = document.getElementById('recon-clear-btn');
        const baselineRefreshBtn = document.getElementById('baseline-refresh-btn');
        const baselineClearBtn = document.getElementById('baseline-clear-btn');

        if (sandboxRunBtn) sandboxRunBtn.addEventListener('click', runSandboxPrompt);
        if (compareRunBtn) compareRunBtn.addEventListener('click', runCompare);
        if (canaryRegisterBtn) canaryRegisterBtn.addEventListener('click', registerCanary);
        if (reconRefreshBtn) reconRefreshBtn.addEventListener('click', loadRecon);
        if (reconClearBtn) reconClearBtn.addEventListener('click', async () => {
            if (!confirm('Clear all recon telemetry data?')) return;
            try {
                await fetchApi('/admin/recon/clear', { method: 'POST' });
                loadRecon();
            } catch (e) {
                alert(`Failed to clear recon data: ${e.message}`);
            }
        });
        if (baselineRefreshBtn) baselineRefreshBtn.addEventListener('click', loadBaseline);
        if (baselineClearBtn) baselineClearBtn.addEventListener('click', async () => {
            if (!confirm('Clear ALL DOM baselines?')) return;
            try {
                await fetchApi('/admin/dom-baseline/clear/all', { method: 'POST' });
                loadBaseline();
            } catch (e) {
                alert(`Failed to clear baselines: ${e.message}`);
            }
        });

        // Refresh all
        const refreshAllBtn = document.getElementById('refresh-all-btn');
        if (refreshAllBtn) refreshAllBtn.addEventListener('click', async () => {
            if (!confirm('Run baseline refresh for ALL providers? This may take a while.')) return;
            refreshAllBtn.disabled = true;
            refreshAllBtn.textContent = 'Running...';
            try {
                await fetchApi('/dom-refresh/run-all', { method: 'POST' });
                loadBaseline();
            } catch (e) {
                alert(`Refresh failed: ${e.message}`);
            } finally {
                refreshAllBtn.disabled = false;
                refreshAllBtn.textContent = '↻ Refresh All Providers';
            }
        });

        // Export baselines
        const exportBaselinesBtn = document.getElementById('export-baselines-btn');
        if (exportBaselinesBtn) exportBaselinesBtn.addEventListener('click', async () => {
            try {
                const resp = await fetchApi('/dom-baseline/export');
                const data = await resp.json();
                downloadJson(data, `dom-baselines-${new Date().toISOString().slice(0, 10)}.json`);
            } catch (e) {
                alert(`Export failed: ${e.message}`);
            }
        });

        // Export overrides
        const exportOverridesBtn = document.getElementById('export-overrides-btn');
        if (exportOverridesBtn) exportOverridesBtn.addEventListener('click', async () => {
            try {
                const resp = await fetchApi('/selector-overrides/export');
                const data = await resp.json();
                downloadJson(data, `selector-overrides-${new Date().toISOString().slice(0, 10)}.json`);
            } catch (e) {
                alert(`Export failed: ${e.message}`);
            }
        });

        // Import baselines
        const importBaselinesBtn = document.getElementById('import-baselines-btn');
        const importFileInput = document.getElementById('import-file-input');
        if (importBaselinesBtn) importBaselinesBtn.addEventListener('click', () => {
            importFileInput.dataset.importType = 'baselines';
            importFileInput.click();
        });

        // Import overrides
        const importOverridesBtn = document.getElementById('import-overrides-btn');
        if (importOverridesBtn) importOverridesBtn.addEventListener('click', () => {
            importFileInput.dataset.importType = 'overrides';
            importFileInput.click();
        });

        // Handle file import
        if (importFileInput) {
            importFileInput.addEventListener('change', async (e) => {
                const file = e.target.files[0];
                if (!file) return;

                const importType = importFileInput.dataset.importType;
                try {
                    const content = await file.text();
                    const data = JSON.parse(content);

                    // Dry run first
                    const dryRunResp = await fetchApi(
                        importType === 'baselines' ? '/dom-baseline/import?dry_run=true' : '/selector-overrides/import?dry_run=true',
                        { method: 'POST', body: JSON.stringify(data) }
                    );
                    const dryRunData = await dryRunResp.json();

                    const msg = `Import preview:\n${dryRunData.total_to_import} items to import\n${dryRunData.new} new, ${dryRunData.updates} updates`;
                    if (!confirm(msg + '\n\nProceed with import?')) return;

                    // Actual import
                    const importResp = await fetchApi(
                        importType === 'baselines' ? '/dom-baseline/import' : '/selector-overrides/import',
                        { method: 'POST', body: JSON.stringify(data) }
                    );
                    const importData = await importResp.json();
                    document.getElementById('import-result').innerHTML = `<span style="color: var(--admin-success);">✅ Imported ${importData.imported} new, updated ${importData.updated}</span>`;
                    loadBaseline();
                } catch (err) {
                    document.getElementById('import-result').innerHTML = `<span style="color: var(--admin-error);">❌ Import failed: ${err.message}</span>`;
                }
                e.target.value = '';
            });
        }
    });

    // Event delegation for dynamic suggestion/override buttons
    document.addEventListener('click', async (e) => {
        const target = e.target;

        // Approve suggestion
        if (target.classList.contains('btn-approve')) {
            const id = target.dataset.id;
            if (!confirm(`Approve suggestion #${id}?`)) return;
            try {
                await fetchApi(`/selector-suggestions/${id}/approve`, { method: 'POST' });
                loadMaintenance();
            } catch (err) {
                alert(`Failed to approve: ${err.message}`);
            }
        }

        // Reject suggestion
        if (target.classList.contains('btn-reject')) {
            const id = target.dataset.id;
            if (!confirm(`Reject suggestion #${id}?`)) return;
            try {
                await fetchApi(`/selector-suggestions/${id}/reject`, { method: 'POST' });
                loadMaintenance();
            } catch (err) {
                alert(`Failed to reject: ${err.message}`);
            }
        }

        // Dismiss suggestion
        if (target.classList.contains('btn-dismiss')) {
            const id = target.dataset.id;
            try {
                await fetchApi(`/selector-suggestions/${id}/dismiss`, { method: 'POST' });
                loadMaintenance();
            } catch (err) {
                alert(`Failed to dismiss: ${err.message}`);
            }
        }

        // Reset override
        if (target.classList.contains('btn-reset-override')) {
            const provider = target.dataset.provider;
            const role = target.dataset.role;
            if (!confirm(`Reset override for ${provider}/${role}?`)) return;
            try {
                await fetchApi(`/selector-overrides/reset/${provider}?role=${encodeURIComponent(role)}`, { method: 'POST' });
                loadMaintenance();
            } catch (err) {
                alert(`Failed to reset override: ${err.message}`);
            }
        }

        // Refresh single provider
        if (target.classList.contains('btn-refresh-single')) {
            const provider = target.dataset.provider;
            if (!confirm(`Run baseline refresh for ${provider}?`)) return;
            try {
                await fetchApi(`/dom-refresh/run/${provider}`, { method: 'POST' });
                loadBaseline();
            } catch (err) {
                alert(`Refresh failed: ${err.message}`);
            }
        }
    });

    // ── Pipelines Tab ──

    async function loadPipelineData() {
        const [definitions, stats, executions] = await Promise.all([
            fetchApi('/admin/pipelines'),
            fetchApi('/admin/pipelines/stats'),
            fetchApi('/admin/pipelines/executions?limit=50'),
        ]);

        // Update overview cards
        document.getElementById('pl-stat-total').textContent = definitions.total;
        document.getElementById('pl-stat-enabled').textContent = definitions.pipelines.filter(p => p.enabled).length;
        document.getElementById('pl-stat-executions').textContent = executions.total;

        // Calculate overall success rate
        const totalExecutions = Object.values(stats.stats || {}).reduce((sum, s) => sum + s.executions, 0);
        const totalSuccess = Object.values(stats.stats || {}).reduce((sum, s) => sum + s.success_count, 0);
        const rate = totalExecutions > 0 ? Math.round(totalSuccess / totalExecutions * 100) + '%' : '—';
        document.getElementById('pl-stat-success').textContent = rate;

        // Populate pipeline filter dropdown
        const filterSelect = document.getElementById('pl-exec-pipeline-filter');
        const currentFilter = filterSelect.value;
        filterSelect.innerHTML = '<option value="">All Pipelines</option>';
        definitions.pipelines.forEach(p => {
            filterSelect.innerHTML += `<option value="${p.pipeline_id}" ${currentFilter === p.pipeline_id ? 'selected' : ''}>${p.pipeline_id}</option>`;
        });

        renderPipelineDefinitions(definitions.pipelines, stats.stats);
        renderRecentExecutions(executions.executions);
    }

    function renderPipelineDefinitions(pipelines, stats) {
        const tbody = document.getElementById('pl-definitions-tbody');
        if (!pipelines.length) {
            tbody.innerHTML = '<tr><td colspan="7" style="padding: 1rem; text-align: center;">No pipelines found</td></tr>';
            return;
        }

        tbody.innerHTML = pipelines.map(p => {
            const s = stats[p.pipeline_id] || {};
            const successRate = s.executions > 0 ? Math.round(s.success_rate * 100) + '%' : '—';
            const avgDuration = s.avg_latency_ms ? Math.round(s.avg_latency_ms) + 'ms' : '—';

            return `<tr style="border-bottom: 1px solid var(--admin-border);">
                <td style="padding: 0.5rem; font-family: var(--admin-font-mono); font-size: 0.75rem;">${p.pipeline_id}</td>
                <td style="padding: 0.5rem;">${p.display_name}</td>
                <td style="padding: 0.5rem; text-align: center;">${p.stage_count}</td>
                <td style="padding: 0.5rem; text-align: center;">
                    <span class="badge ${p.enabled ? 'badge-green' : 'badge-gray'}">${p.enabled ? 'enabled' : 'disabled'}</span>
                </td>
                <td style="padding: 0.5rem; text-align: center;">${successRate}</td>
                <td style="padding: 0.5rem; text-align: center;">${avgDuration}</td>
                <td style="padding: 0.5rem; text-align: center;">
                    <button class="btn-secondary" onclick="inspectPipeline('${p.pipeline_id}')" style="font-size: 0.7rem; padding: 0.2rem 0.4rem;">Inspect</button>
                    ${p.enabled
                        ? `<button class="btn-danger" onclick="togglePipeline('${p.pipeline_id}', false)" style="font-size: 0.7rem; padding: 0.2rem 0.4rem;">Disable</button>`
                        : `<button class="btn-success" onclick="togglePipeline('${p.pipeline_id}', true)" style="font-size: 0.7rem; padding: 0.2rem 0.4rem;">Enable</button>`
                    }
                    <button class="btn-secondary" onclick="runPipelineTest('${p.pipeline_id}')" style="font-size: 0.7rem; padding: 0.2rem 0.4rem;">Test</button>
                </td>
            </tr>`;
        }).join('');
    }

    function renderRecentExecutions(executions) {
        const tbody = document.getElementById('pl-executions-tbody');
        if (!executions || !executions.length) {
            tbody.innerHTML = '<tr><td colspan="7" style="padding: 1rem; text-align: center;">No executions yet</td></tr>';
            return;
        }

        tbody.innerHTML = executions.map(e => {
            const statusBadge = e.status === 'success' ? 'badge-green' : e.status === 'failed' ? 'badge-red' : 'badge-yellow';
            const stagesStr = `${e.stages_completed}/${e.stage_count}`;

            return `<tr style="border-bottom: 1px solid var(--admin-border);">
                <td style="padding: 0.5rem; font-family: var(--admin-font-mono); font-size: 0.7rem;">${e.execution_id}</td>
                <td style="padding: 0.5rem; font-family: var(--admin-font-mono); font-size: 0.75rem;">${e.pipeline_id}</td>
                <td style="padding: 0.5rem; text-align: center;">
                    <span class="badge ${statusBadge}">${e.status}</span>
                </td>
                <td style="padding: 0.5rem; text-align: center;">${Math.round(e.duration_ms)}ms</td>
                <td style="padding: 0.5rem; text-align: center;">${stagesStr}</td>
                <td style="padding: 0.5rem; text-align: center;">${e.total_fallbacks || 0}</td>
                <td style="padding: 0.5rem; text-align: center;">
                    <button class="btn-secondary" onclick="viewExecutionTrace('${e.execution_id}')" style="font-size: 0.7rem; padding: 0.2rem 0.4rem;">Trace</button>
                </td>
            </tr>`;
        }).join('');
    }

    window.loadPipelineDefinitions = async function() {
        try {
            await loadPipelineData();
        } catch (err) {
            console.error('Failed to load pipeline definitions:', err);
            document.getElementById('pl-definitions-tbody').innerHTML =
                `<tr><td colspan="7" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${err.message}</td></tr>`;
        }
    };

    window.loadRecentExecutions = async function() {
        const pipelineId = document.getElementById('pl-exec-pipeline-filter').value;
        const status = document.getElementById('pl-exec-status-filter').value;

        try {
            const params = new URLSearchParams({ limit: '50' });
            if (pipelineId) params.set('pipeline_id', pipelineId);
            if (status) params.set('status', status);

            const data = await fetchApi(`/admin/pipelines/executions?${params}`);
            renderRecentExecutions(data.executions);
        } catch (err) {
            console.error('Failed to load executions:', err);
            document.getElementById('pl-executions-tbody').innerHTML =
                `<tr><td colspan="7" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${err.message}</td></tr>`;
        }
    };

    window.togglePipeline = async function(pipelineId, enabled) {
        try {
            const endpoint = enabled ? 'enable' : 'disable';
            await fetchApi(`/admin/pipelines/${pipelineId}/${endpoint}`, { method: 'POST' });
            await loadPipelineData();
        } catch (err) {
            alert(`Failed to ${enabled ? 'enable' : 'disable'} pipeline: ${err.message}`);
        }
    };

    window.inspectPipeline = async function(pipelineId) {
        try {
            const data = await fetchApi(`/admin/pipelines/${pipelineId}`);
            const content = document.createElement('div');
            content.style.cssText = 'font-size: 0.8rem; font-family: var(--admin-font-mono);';
            content.innerHTML = `
                <div style="margin-bottom: 1rem;">
                    <strong>Pipeline:</strong> ${data.pipeline_id}<br>
                    <strong>Display Name:</strong> ${data.display_name}<br>
                    <strong>Model ID:</strong> ${data.model_id}<br>
                    <strong>Description:</strong> ${data.description || '—'}<br>
                    <strong>Enabled:</strong> ${data.enabled ? 'Yes' : 'No'}<br>
                    <strong>Max Total Time:</strong> ${data.max_total_time_ms}ms<br>
                    <strong>Max Stage Retries:</strong> ${data.max_stage_retries}
                </div>
                <h4 style="margin: 1rem 0 0.5rem;">Stages:</h4>
                <table style="width: 100%; border-collapse: collapse; font-size: 0.75rem;">
                    <thead>
                        <tr style="border-bottom: 1px solid var(--admin-border);">
                            <th style="padding: 0.3rem; text-align: left;">Stage ID</th>
                            <th style="padding: 0.3rem; text-align: left;">Role</th>
                            <th style="padding: 0.3rem; text-align: left;">Target Model</th>
                            <th style="padding: 0.3rem; text-align: left;">Failure Policy</th>
                            <th style="padding: 0.3rem; text-align: left;">Retries</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.stages.map(s => `<tr style="border-bottom: 1px solid var(--admin-border);">
                            <td style="padding: 0.3rem;">${s.stage_id}</td>
                            <td style="padding: 0.3rem;">${s.role}</td>
                            <td style="padding: 0.3rem;">${s.target_model || '(intelligent selection)'}</td>
                            <td style="padding: 0.3rem;">${s.failure_policy}</td>
                            <td style="padding: 0.3rem;">${s.max_retries}</td>
                        </tr>`).join('')}
                    </tbody>
                </table>
            `;
            document.getElementById('pl-trace-content').innerHTML = '';
            document.getElementById('pl-trace-content').appendChild(content);
            document.getElementById('pl-trace-modal').classList.remove('hidden');
        } catch (err) {
            alert(`Failed to inspect pipeline: ${err.message}`);
        }
    };

    window.runPipelineTest = async function(pipelineId) {
        const prompt = prompt('Enter test prompt (or leave empty for default):');
        if (prompt === null) return;

        try {
            const body = prompt ? { prompt } : undefined;
            const data = await fetchApi(`/admin/pipelines/${pipelineId}/run-test`, {
                method: 'POST',
                body: body ? JSON.stringify(body) : undefined,
            });

            alert(`Pipeline test ${data.status}\nDuration: ${data.duration_ms}ms\nOutput: ${data.output_preview || data.error}`);
            // Refresh executions
            await loadRecentExecutions();
        } catch (err) {
            alert(`Test execution failed: ${err.message}`);
        }
    };

    window.viewExecutionTrace = async function(executionId) {
        try {
            const data = await fetchApi(`/admin/pipelines/executions/${executionId}`);
            renderExecutionTrace(data);
            document.getElementById('pl-trace-modal').classList.remove('hidden');
        } catch (err) {
            alert(`Failed to load execution trace: ${err.message}`);
        }
    };

    function renderExecutionTrace(data) {
        const container = document.getElementById('pl-trace-content');
        const statusBadge = data.status === 'success' ? 'badge-green' : data.status === 'failed' ? 'badge-red' : 'badge-yellow';

        let html = `
            <div style="margin-bottom: 1rem;">
                <strong>Execution ID:</strong> <code>${data.execution_id}</code><br>
                <strong>Pipeline:</strong> ${data.pipeline_display_name || data.pipeline_id}<br>
                <strong>Status:</strong> <span class="badge ${statusBadge}">${data.status}</span><br>
                <strong>Duration:</strong> ${Math.round(data.duration_ms)}ms / ${data.total_budget_ms}ms (${data.budget_consumed_pct}%)<br>
                <strong>Stages:</strong> ${data.stages_completed}/${data.stage_count}<br>
                <strong>Retries:</strong> ${data.total_retries} | <strong>Fallbacks:</strong> ${data.total_fallbacks}
            </div>
        `;

        // Stage timeline
        html += '<h4 style="margin: 1rem 0 0.5rem;">Stage Timeline:</h4>';
        html += '<div style="display: flex; flex-direction: column; gap: 0.5rem;">';

        (data.stages || []).forEach((stage, i) => {
            const stageStatus = stage.status === 'completed' ? 'badge-green' : stage.status === 'failed' ? 'badge-red' : 'badge-gray';
            const fallbackBadge = stage.fallback_count > 0 ? `<span class="badge badge-yellow" style="font-size: 0.65rem;">${stage.fallback_count} fallback</span>` : '';
            const retryBadge = stage.retry_count > 0 ? `<span class="badge badge-blue" style="font-size: 0.65rem;">${stage.retry_count} retry</span>` : '';

            html += `
                <div style="border: 1px solid var(--admin-border); border-radius: 4px; padding: 0.5rem; font-size: 0.75rem;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.3rem;">
                        <div>
                            <strong>${i + 1}. ${stage.stage_id}</strong>
                            <span class="badge" style="font-size: 0.6rem; background: var(--admin-muted); color: var(--admin-text-light); margin-left: 0.3rem;">${stage.stage_role}</span>
                            <span class="badge ${stageStatus}" style="font-size: 0.6rem; margin-left: 0.3rem;">${stage.status}</span>
                            ${fallbackBadge} ${retryBadge}
                        </div>
                        <div style="font-family: var(--admin-font-mono);">${Math.round(stage.duration_ms)}ms</div>
                    </div>
                    <div style="color: var(--admin-text-muted); font-size: 0.7rem;">
                        Model: ${stage.selected_model || '—'} | Provider: ${stage.selected_provider || '—'}
                    </div>
                    ${stage.budget_remaining_ms !== undefined ? `<div style="color: var(--admin-text-muted); font-size: 0.7rem;">Budget remaining: ${Math.round(stage.budget_remaining_ms)}ms</div>` : ''}
                    ${stage.failure_reason ? `<div style="color: var(--admin-error); font-size: 0.7rem; margin-top: 0.3rem;">Error: ${stage.failure_reason}</div>` : ''}
                    ${stage.output_summary ? `<details style="margin-top: 0.3rem;"><summary style="cursor: pointer; font-size: 0.7rem; color: var(--admin-text-muted);">Output Summary</summary><pre style="margin: 0.3rem 0 0; font-size: 0.65rem; max-height: 100px; overflow-y: auto; background: var(--admin-bg-secondary); padding: 0.3rem; border-radius: 2px; white-space: pre-wrap;">${escapeHtml(stage.output_summary)}</pre></details>` : ''}
                </div>
            `;
        });

        html += '</div>';

        // Failure analysis
        if (data.failure_analysis) {
            const fa = data.failure_analysis;
            html += `
                <div style="margin-top: 1rem; border: 1px solid var(--admin-error); border-radius: 4px; padding: 0.5rem; font-size: 0.75rem;">
                    <h4 style="color: var(--admin-error); margin: 0 0 0.3rem;">Failure Analysis</h4>
                    <strong>Failed Stage:</strong> ${fa.failed_stage} (${fa.failed_stage_role})<br>
                    <strong>Root Cause:</strong> ${fa.root_cause}<br>
                    <strong>Detail:</strong> ${fa.root_cause_detail || '—'}<br>
                    <strong>Retries:</strong> ${fa.retry_count} | <strong>Fallbacks:</strong> ${fa.fallback_count}<br>
                    <strong>Candidates Exhausted:</strong> ${fa.candidates_exhausted ? 'Yes' : 'No'}<br>
                    <strong>Budget Exceeded:</strong> ${fa.budget_exceeded ? 'Yes' : 'No'}
                </div>
            `;
        }

        container.innerHTML = html;
    }

    window.closeTraceModal = function() {
        document.getElementById('pl-trace-modal').classList.add('hidden');
    };

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ── Trends Tab ──

    window.loadTrendsData = async function() {
        const role = document.getElementById('trends-role-select').value;
        const window = document.getElementById('trends-window-select').value;

        const params = new URLSearchParams();
        if (role) params.set('role', role);
        params.set('window', window);

        try {
            const data = await fetchApi(`/admin/pipelines/scoring-trends?${params}`);
            renderTrends(data);
        } catch (err) {
            console.error('Failed to load trends:', err);
            document.getElementById('trends-tbody').innerHTML =
                `<tr><td colspan="10" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${err.message}</td></tr>`;
        }
    };

    window.triggerSnapshot = async function() {
        try {
            const data = await fetchApi('/admin/pipelines/scoring-history/snapshot', { method: 'POST' });
            alert(`Snapshot captured: ${data.snapshots_recorded} snapshots recorded`);
            loadTrendsData();
        } catch (err) {
            console.error('Failed to trigger snapshot:', err);
            alert(`Failed to capture snapshot: ${err.message}`);
        }
    };

    function renderTrends(data) {
        const trends = data.trends || [];
        const improvers = data.top_improvers || [];
        const decliners = data.top_decliners || [];
        const unstable = data.unstable_models || [];

        // Count by trend status
        const statusCounts = { improving: 0, declining: 0, unstable: 0, stable: 0 };
        trends.forEach(t => {
            if (statusCounts.hasOwnProperty(t.overall_trend)) {
                statusCounts[t.overall_trend]++;
            }
        });

        document.getElementById('trend-improvers-count').textContent = statusCounts.improving;
        document.getElementById('trend-decliners-count').textContent = statusCounts.declining;
        document.getElementById('trend-unstable-count').textContent = statusCounts.unstable;
        document.getElementById('trend-stable-count').textContent = statusCounts.stable;

        // Render sub-tables
        renderTrendTable('improvers-tbody', improvers, true);
        renderTrendTable('decliners-tbody', decliners, true);
        renderTrendTable('unstable-tbody', unstable, true);
        renderTrendTable('trends-tbody', trends, false);
    }

    function renderTrendTable(tbodyId, items, compact) {
        const tbody = document.getElementById(tbodyId);

        if (!items || items.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${compact ? 9 : 10}" style="padding: 1rem; text-align: center;">No data</td></tr>`;
            return;
        }

        tbody.innerHTML = items.map(t => {
            const scoreDeltaStr = (t.score_delta >= 0 ? '+' : '') + t.score_delta.toFixed(3);
            const srDeltaStr = (t.success_rate_delta >= 0 ? '+' : '') + (t.success_rate_delta * 100).toFixed(1) + '%';
            const frDeltaStr = (t.fallback_rate_delta >= 0 ? '+' : '') + (t.fallback_rate_delta * 100).toFixed(1) + '%';
            const durDeltaStr = (t.duration_delta_ms >= 0 ? '+' : '') + t.duration_delta_ms.toFixed(0) + 'ms';

            const scoreColor = t.score_delta >= 0.05 ? 'var(--admin-success)' :
                               t.score_delta <= -0.05 ? 'var(--admin-error)' : 'var(--admin-text-muted)';
            const srColor = t.success_rate_delta >= 0 ? 'var(--admin-success)' : 'var(--admin-error)';
            const frColor = t.fallback_rate_delta <= 0 ? 'var(--admin-success)' : 'var(--admin-error)';
            const durColor = t.duration_delta_ms <= 0 ? 'var(--admin-success)' : 'var(--admin-error)';

            let statusBadge = '';
            if (!compact) {
                const badgeClass = t.overall_trend === 'improving' ? 'badge-green' :
                                   t.overall_trend === 'declining' ? 'badge-yellow' :
                                   t.overall_trend === 'unstable' ? 'badge-red' : 'badge-gray';
                const label = t.overall_trend === 'improving' ? '↑ rising' :
                              t.overall_trend === 'declining' ? '↓ falling' :
                              t.overall_trend === 'unstable' ? '⚡ unstable' : '— stable';
                statusBadge = `<td style="padding: 0.3rem; text-align: center;"><span class="badge ${badgeClass}" style="font-size: 0.6rem;">${label}</span></td>`;
            }

            const driverLabel = t.main_driver ?
                `<span style="font-size: 0.65rem;">${formatDriverLabel(t.main_driver)}</span>` :
                '<span style="font-size: 0.65rem; color: var(--admin-text-muted);">—</span>';

            return `<tr style="border-bottom: 1px solid var(--admin-border);">
                <td style="padding: 0.3rem; font-family: var(--admin-font-mono); font-size: 0.7rem;">${t.model_id}</td>
                <td style="padding: 0.3rem; font-size: 0.7rem;">${t.role}</td>
                ${statusBadge}
                <td style="padding: 0.3rem; text-align: center; font-family: var(--admin-font-mono); font-size: 0.7rem;">${t.current_score.toFixed(2)}</td>
                <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${scoreColor};">${scoreDeltaStr}</td>
                <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${srColor};">${srDeltaStr}</td>
                <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${frColor};">${frDeltaStr}</td>
                <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${durColor};">${durDeltaStr}</td>
                <td style="padding: 0.3rem; text-align: center;">${driverLabel}</td>
                <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${t.data_points}</td>
            </tr>`;
        }).join('');
    }

    function formatDriverLabel(driver) {
        const labels = {
            'success_rate_improved': '✓ success ↑',
            'success_rate_worsened': '✗ success ↓',
            'fallback_rate_improved': '✓ fallback ↓',
            'fallback_rate_worsened': '✗ fallback ↑',
            'duration_improved': '✓ faster',
            'duration_worsened': '✗ slower',
            'no_significant_change': '—',
            'no_change': '—',
            'scoring_adjustment': '⚙ scoring',
        };
        return labels[driver] || driver;
    }

    // ── Scoring Tab ──

    window.loadScoringData = async function() {
        const role = document.getElementById('scoring-role-select').value;
        const tbody = document.getElementById('scoring-tbody');

        try {
            const data = await fetchApi(`/admin/pipelines/stage-scoring?stage_role=${role}`);
            if (!data.scoring || data.scoring.length === 0) {
                tbody.innerHTML = '<tr><td colspan="14" style="padding: 1rem; text-align: center;">No scoring data available</td></tr>';
                return;
            }

            tbody.innerHTML = data.scoring.map(s => {
                const scoreBar = renderScoreBar(s.final_score, s.base_static_score, s.dynamic_adjustment, s.failure_penalty);
                const badges = [];
                if (s.cold_start) badges.push('<span class="badge badge-gray" style="font-size: 0.6rem;">cold_start</span>');
                if (s.fallback_heavy) badges.push('<span class="badge badge-yellow" style="font-size: 0.6rem;">fallback_heavy</span>');
                if (s.top_performer) badges.push('<span class="badge badge-green" style="font-size: 0.6rem;">top</span>');
                if (s.high_quality) badges.push('<span class="badge badge-green" style="font-size: 0.6rem;">high_quality</span>');
                if (s.low_quality) badges.push('<span class="badge badge-red" style="font-size: 0.6rem;">low_quality</span>');

                const qualityColor = s.quality_sample_count >= 3 ?
                    (s.quality_score >= 0.6 ? 'var(--admin-success)' : s.quality_score < 0.4 ? 'var(--admin-error)' : 'var(--admin-text-muted)') :
                    'var(--admin-text-muted)';
                const qAdjColor = s.quality_adjustment > 0.01 ? 'var(--admin-success)' :
                                  s.quality_adjustment < -0.01 ? 'var(--admin-error)' : 'var(--admin-text-muted)';
                const qAdjStr = s.quality_sample_count >= 3 ?
                    (s.quality_adjustment >= 0 ? '+' : '') + s.quality_adjustment.toFixed(3) :
                    '—';

                return `<tr style="border-bottom: 1px solid var(--admin-border);">
                    <td style="padding: 0.3rem; font-family: var(--admin-font-mono); font-size: 0.7rem;">${s.model_id}</td>
                    <td style="padding: 0.3rem; font-size: 0.7rem;">${s.provider_id || '—'}</td>
                    <td style="padding: 0.3rem; text-align: center; font-family: var(--admin-font-mono);">${scoreBar}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${s.base_static_score.toFixed(2)}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${s.dynamic_adjustment > 0 ? 'var(--admin-success)' : s.dynamic_adjustment < 0 ? 'var(--admin-error)' : 'var(--admin-text-muted)'};">${s.dynamic_adjustment >= 0 ? '+' : ''}${s.dynamic_adjustment.toFixed(2)}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${qualityColor};">${s.quality_sample_count >= 3 ? s.quality_score.toFixed(2) : '—'}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${qAdjColor};">${qAdjStr}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem; color: ${s.failure_penalty > 0 ? 'var(--admin-error)' : 'var(--admin-text-muted)'};">${s.failure_penalty > 0 ? '-' + s.failure_penalty.toFixed(2) : '0.00'}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${(s.success_rate * 100).toFixed(0)}%</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${(s.fallback_rate * 100).toFixed(0)}%</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${s.sample_count}</td>
                    <td style="padding: 0.3rem; text-align: center; font-size: 0.7rem;">${(s.data_confidence * 100).toFixed(0)}%</td>
                    <td style="padding: 0.3rem; text-align: center;">${badges.join(' ') || '—'}</td>
                    <td style="padding: 0.3rem; font-size: 0.65rem; max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${s.tags.join(', ')}">${s.tags.join(', ') || '—'}</td>
                </tr>`;
            }).join('');
        } catch (err) {
            console.error('Failed to load scoring:', err);
            tbody.innerHTML = `<tr><td colspan="14" style="padding: 1rem; text-align: center; color: var(--admin-error);">Error: ${err.message}</td></tr>`;
        }
    };

    function renderScoreBar(final, base, dynamic, penalty) {
        const pct = Math.round(final * 100);
        const color = final >= 0.7 ? 'var(--admin-success)' : final >= 0.4 ? 'var(--admin-warning)' : 'var(--admin-error)';
        return `<div style="display: flex; align-items: center; gap: 0.3rem;">
            <div style="width: 60px; height: 8px; background: var(--admin-bg-secondary); border-radius: 4px; overflow: hidden;">
                <div style="width: ${pct}%; height: 100%; background: ${color}; border-radius: 4px;"></div>
            </div>
            <span style="font-size: 0.7rem; font-weight: 500; min-width: 30px;">${final.toFixed(2)}</span>
        </div>`;
    }
})();
