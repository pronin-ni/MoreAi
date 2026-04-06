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
        }
    }

    // ── Helpers ──
    function truncate(str, maxLen) {
        return str.length > maxLen ? str.slice(0, maxLen) + '…' : str;
    }

    // ── Start ──
    init();
})();
