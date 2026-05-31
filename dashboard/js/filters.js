/**
 * filters.js — Cargo type filter state shared across map and tables.
 */
window.ShipFilters = (() => {
  let activeFilter = 'ALL';

  const CARGO_TYPES = ['ALL', 'CRUDE', 'LNG', 'CNG', 'PETROLEUM'];

  function init() {
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeFilter = btn.dataset.cargo;
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        applyFilter();
      });
    });
  }

  function applyFilter() {
    // Filter table rows
    document.querySelectorAll('tr[data-cargo]').forEach(row => {
      if (activeFilter === 'ALL' || row.dataset.cargo === activeFilter) {
        row.style.display = '';
      } else {
        row.style.display = 'none';
      }
    });

    // Filter map markers
    if (window.ShipMap) {
      window.ShipMap.applyFilter(activeFilter);
    }
  }

  function getActive() { return activeFilter; }

  return { init, applyFilter, getActive, CARGO_TYPES };
})();
