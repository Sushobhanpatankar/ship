/**
 * tables.js — Renders data tables for each agent view.
 */
window.ShipTables = (() => {

  // ---------- helpers ----------

  function cargoBadge(cat) {
    return `<span class="cargo-badge badge-${cat || 'OTHER'}">${cat || 'OTHER'}</span>`;
  }

  function activityBadge(act) {
    return `<span class="activity-badge act-${act || 'UNKNOWN'}">${act || '—'}</span>`;
  }

  function num(v, decimals = 1) {
    return `<span class="num">${v != null ? parseFloat(v).toFixed(decimals) : '—'}</span>`;
  }

  function coord(lat, lon) {
    if (!lat && !lon) return '—';
    return `${parseFloat(lat).toFixed(2)}°N, ${parseFloat(lon).toFixed(2)}°E`;
  }

  function shortDt(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString('en-IN', {
        month: 'short', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      });
    } catch { return iso; }
  }

  function noData(msg) {
    return `<tr><td colspan="20" class="no-data">${msg}</td></tr>`;
  }

  // ---------- Inbound table ----------

  function renderInbound(ships) {
    const tbody = document.querySelector('#inbound-table tbody');
    if (!tbody) return;
    if (!ships || ships.length === 0) {
      tbody.innerHTML = noData('No inbound energy ships detected yet. Waiting for AIS data…');
      return;
    }
    tbody.innerHTML = ships.map(s => `
      <tr data-cargo="${s.cargo_category || 'OTHER'}" data-mmsi="${s.mmsi}"
          class="clickable-row" onclick="ShipTables.openShip('${s.mmsi}')">
        <td><strong>${s.ship_name || s.mmsi}</strong><br>
            <small style="color:#8b949e">${s.mmsi}</small></td>
        <td>${cargoBadge(s.cargo_category)}</td>
        <td>${coord(s.current_lat, s.current_lon)}</td>
        <td>${num(s.speed)} kn</td>
        <td>${s.destination || '—'}</td>
        <td>${num(s.distance_to_port, 0)} nm</td>
        <td class="port-name">${s.nearest_port || '—'}</td>
        <td>${shortDt(s.eta)}</td>
        <td><span style="color:${s.status==='UNDERWAY'?'#3fb950':'#f0883e'}">${s.status || '—'}</span></td>
      </tr>
    `).join('');
  }

  // ---------- Outbound table ----------

  function renderOutbound(ships) {
    const tbody = document.querySelector('#outbound-table tbody');
    if (!tbody) return;
    if (!ships || ships.length === 0) {
      tbody.innerHTML = noData('No outbound ships detected recently.');
      return;
    }
    tbody.innerHTML = ships.map(s => `
      <tr data-cargo="${s.cargo_category || 'OTHER'}" data-mmsi="${s.mmsi}"
          class="clickable-row" onclick="ShipTables.openShip('${s.mmsi}')">
        <td><strong>${s.ship_name || s.mmsi}</strong><br>
            <small style="color:#8b949e">${s.mmsi}</small></td>
        <td>${cargoBadge(s.cargo_category)}</td>
        <td>${coord(s.current_lat, s.current_lon)}</td>
        <td>${num(s.speed)} kn</td>
        <td class="port-name">${s.departure_port || '—'}</td>
        <td>${shortDt(s.departure_time)}</td>
        <td>${s.destination || '—'}</td>
        <td>${s.ballast_confirmed ? '✓ Confirmed' : '—'}</td>
      </tr>
    `).join('');
  }

  // ---------- Port Activity table ----------

  function renderPortActivity(ships) {
    const tbody = document.querySelector('#port-table tbody');
    if (!tbody) return;
    if (!ships || ships.length === 0) {
      tbody.innerHTML = noData('No ships currently in Indian port zones. Check back in a few minutes.');
      return;
    }
    tbody.innerHTML = ships.map(s => `
      <tr data-cargo="${s.cargo_category || 'OTHER'}" data-mmsi="${s.mmsi}"
          class="clickable-row" onclick="ShipTables.openShip('${s.mmsi}')">
        <td><strong>${s.ship_name || s.mmsi}</strong><br>
            <small style="color:#8b949e">${s.mmsi}</small></td>
        <td>${cargoBadge(s.cargo_category)}</td>
        <td class="port-name">${s.port_name || '—'}</td>
        <td>${s.berth || '—'}</td>
        <td>${activityBadge(s.activity)}</td>
        <td>${shortDt(s.arrival_time)}</td>
        <td>${shortDt(s.expected_departure)}</td>
        <td><span class="source-chip">${s.source || 'AIS'}</span></td>
      </tr>
    `).join('');
  }

  // ---------- Ship detail modal ----------

  function openShip(mmsi) {
    if (!mmsi || mmsi.startsWith('SCRAPE_')) return;
    fetch(`/api/ships/${mmsi}`)
      .then(r => r.json())
      .then(data => showModal(data))
      .catch(() => {});
  }

  function showModal(data) {
    let modal = document.getElementById('ship-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'ship-modal';
      modal.style.cssText = `
        position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;
        display:flex;align-items:center;justify-content:center;
      `;
      document.body.appendChild(modal);
    }
    const pos = (data.recent_positions || [])[0] || {};
    modal.innerHTML = `
      <div style="background:#161b22;border:1px solid #30363d;border-radius:12px;
                  padding:24px;max-width:520px;width:90%;max-height:80vh;overflow:auto;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h2 style="font-size:1rem;color:#58a6ff">${data.ship_name || 'Unknown Vessel'}</h2>
          <button onclick="document.getElementById('ship-modal').remove()"
                  style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:1.2rem">✕</button>
        </div>
        ${row('MMSI', data.mmsi)} ${row('IMO', data.imo)}
        ${row('Cargo', `<span class="cargo-badge badge-${data.cargo_category}">${data.cargo_category}</span>`)}
        ${row('Flag / CallSign', data.flag)} ${row('Length', data.length ? data.length + ' m' : '—')}
        ${row('Width', data.width ? data.width + ' m' : '—')} ${row('Draft', data.draft ? data.draft + ' m' : '—')}
        ${row('Last Speed', pos.speed != null ? pos.speed + ' kn' : '—')}
        ${row('Last Position', pos.latitude ? `${pos.latitude.toFixed(3)}°N, ${pos.longitude.toFixed(3)}°E` : '—')}
        ${row('Last Seen', pos.timestamp ? new Date(pos.timestamp).toLocaleString() : '—')}
        <div style="margin-top:14px;font-size:0.75rem;color:#8b949e">
          Showing last ${(data.recent_positions||[]).length} AIS positions
        </div>
      </div>
    `;
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  }

  function row(label, value) {
    return `<div class="popup-row" style="padding:5px 0;border-bottom:1px solid #21262d">
      <span class="popup-key">${label}</span>
      <span class="popup-val">${value || '—'}</span>
    </div>`;
  }

  return { renderInbound, renderOutbound, renderPortActivity, openShip };
})();
