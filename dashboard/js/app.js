/**
 * app.js — Main application: tab management, stats updates, data fetch orchestration.
 */
window.ShipApp = (() => {

  // ---------- Tab management ----------

  function initTabs() {
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(target + '-panel').classList.add('active');
        if (target === 'map') {
          window.ShipMap && window.ShipMap.invalidate();
        }
      });
    });
  }

  // ---------- Stats strip ----------

  function updateStats(stats) {
    if (!stats) return;
    _setText('stat-inbound',   stats.total_inbound   ?? 0);
    _setText('stat-outbound',  stats.total_outbound  ?? 0);
    _setText('stat-inport',    stats.total_in_port   ?? 0);
    _setText('stat-crude',     stats.crude_count     ?? 0);
    _setText('stat-lng',       stats.lng_count       ?? 0);
    _setText('stat-cng',       stats.cng_count       ?? 0);
    _setText('stat-petroleum', stats.petroleum_count ?? 0);
    _setText('stat-port',      stats.busiest_port    || '—');
  }

  function _setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  // ---------- Data fetching ----------

  async function fetchAll() {
    const cargo = window.ShipFilters ? window.ShipFilters.getActive() : 'ALL';
    const qp = cargo && cargo !== 'ALL' ? `?cargo_type=${cargo}` : '';

    try {
      const [inbound, outbound, port, summary] = await Promise.all([
        fetch(`/api/inbound${qp}`).then(r => r.json()).catch(() => ({ ships: [] })),
        fetch(`/api/outbound${qp}`).then(r => r.json()).catch(() => ({ ships: [] })),
        fetch(`/api/port-activity${qp}`).then(r => r.json()).catch(() => ({ ships: [] })),
        fetch('/api/summary').then(r => r.json()).catch(() => null),
      ]);

      // Update tables
      window.ShipTables.renderInbound(inbound.ships || []);
      window.ShipTables.renderOutbound(outbound.ships || []);
      window.ShipTables.renderPortActivity(port.ships || []);

      // Update map
      window.ShipMap.updateInbound(inbound.ships || []);
      window.ShipMap.updateOutbound(outbound.ships || []);
      window.ShipMap.updateInPort(port.ships || []);

      // Re-apply active filter to map markers
      window.ShipFilters.applyFilter();

      // Update stats strip
      if (summary) updateStats(summary);

      // Update timestamp
      const el = document.getElementById('last-updated');
      if (el) el.textContent = 'Updated: ' + new Date().toLocaleTimeString();

    } catch (e) {
      console.warn('fetchAll error:', e);
    }
  }

  function handleStats(data) {
    updateStats(data);
  }

  function handlePositions(data) {
    // Called when WS sends a positions push (future enhancement)
  }

  // ---------- Bootstrap ----------

  function init() {
    initTabs();
    window.ShipFilters.init();
    window.ShipMap.init();

    // Initial data load
    fetchAll();

    // Start WebSocket
    window.ShipWS.init();

    // Poll every 60s as well, regardless of WS (ensures freshness)
    setInterval(fetchAll, 60000);
  }

  // Auto-init when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  return { fetchAll, handleStats, handlePositions };
})();
