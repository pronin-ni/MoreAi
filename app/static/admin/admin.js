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
        }
    }

    // ── Helpers ──
    function truncate(str, maxLen) {
        return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
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
    });
})();
