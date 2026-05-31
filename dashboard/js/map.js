/**
 * map.js — Leaflet map with three marker layers: inbound (green), outbound (red), in-port (blue).
 */
window.ShipMap = (() => {
  let map = null;
  let activeFilter = 'ALL';
  const markers = {}; // mmsi → { marker, cargo, layer }

  const LAYERS = {
    inbound:  null,
    outbound: null,
    inPort:   null,
    ports:    null,
  };

  const CARGO_COLORS = {
    CRUDE:     '#d29922',
    LNG:       '#58a6ff',
    CNG:       '#3fb950',
    PETROLEUM: '#f0883e',
    OTHER:     '#8b949e',
  };

  const PORT_ICONS = {
    inbound:  '🟢',
    outbound: '🔴',
    inPort:   '🔵',
  };

  function init() {
    if (map) return;
    map = L.map('map', {
      center: [15.0, 74.0],
      zoom: 5,
      zoomControl: true,
      attributionControl: true,
    });

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '© OpenStreetMap contributors',
      maxZoom: 18,
    }).addTo(map);

    LAYERS.inbound  = L.layerGroup().addTo(map);
    LAYERS.outbound = L.layerGroup().addTo(map);
    LAYERS.inPort   = L.layerGroup().addTo(map);
    LAYERS.ports    = L.layerGroup().addTo(map);

    // Add port markers
    const PORTS = [
      {name:'Vadinar',  lat:22.90, lon:69.61}, {name:'Mundra',   lat:22.84, lon:69.99},
      {name:'JNPT',     lat:18.93, lon:72.94}, {name:'Hazira',   lat:21.08, lon:72.64},
      {name:'Dahej',    lat:21.72, lon:72.58}, {name:'Kochi',    lat: 9.96, lon:76.27},
      {name:'Mangalore',lat:12.92, lon:74.82}, {name:'Chennai',  lat:13.09, lon:80.29},
      {name:'Paradip',  lat:20.32, lon:86.62}, {name:'Vizag',    lat:17.69, lon:83.28},
    ];
    PORTS.forEach(p => {
      L.circleMarker([p.lat, p.lon], {
        radius: 6, color: '#30363d', fillColor: '#21262d',
        fillOpacity: 0.9, weight: 1,
      }).bindTooltip(p.name, { permanent: true, direction: 'top', className: 'port-label' })
        .addTo(LAYERS.ports);
    });

    // Layer control
    L.control.layers(null, {
      'Inbound Ships': LAYERS.inbound,
      'Outbound Ships': LAYERS.outbound,
      'In Port': LAYERS.inPort,
      'Ports': LAYERS.ports,
    }, { collapsed: false }).addTo(map);
  }

  function _makeIcon(emoji, color, course) {
    const rotation = course || 0;
    return L.divIcon({
      html: `<div class="ship-icon" style="transform:rotate(${rotation}deg);color:${color}">${emoji}</div>`,
      className: '',
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    });
  }

  function _popupHtml(s, type) {
    const cargo = s.cargo_category || 'OTHER';
    const color = CARGO_COLORS[cargo] || CARGO_COLORS.OTHER;
    return `
      <div class="popup-title">${s.ship_name || s.mmsi}</div>
      <div class="popup-row"><span class="popup-key">MMSI</span><span class="popup-val">${s.mmsi}</span></div>
      <div class="popup-row"><span class="popup-key">Cargo</span>
        <span class="cargo-badge badge-${cargo}">${cargo}</span></div>
      <div class="popup-row"><span class="popup-key">Speed</span>
        <span class="popup-val">${s.speed != null ? s.speed.toFixed(1)+' kn' : '—'}</span></div>
      ${type === 'inbound' ? `
        <div class="popup-row"><span class="popup-key">Destination</span>
          <span class="popup-val">${s.destination || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Distance</span>
          <span class="popup-val">${s.distance_to_port != null ? s.distance_to_port.toFixed(0)+' nm' : '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Nearest Port</span>
          <span class="popup-val">${s.nearest_port || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">ETA</span>
          <span class="popup-val">${s.eta ? new Date(s.eta).toLocaleString() : '—'}</span></div>
      ` : ''}
      ${type === 'outbound' ? `
        <div class="popup-row"><span class="popup-key">From Port</span>
          <span class="popup-val">${s.departure_port || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Heading To</span>
          <span class="popup-val">${s.destination || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Dist from Port</span>
          <span class="popup-val">${s.distance_from_port != null ? s.distance_from_port.toFixed(0)+' nm' : '—'}</span></div>
      ` : ''}
      ${type === 'inPort' ? `
        <div class="popup-row"><span class="popup-key">Port</span>
          <span class="popup-val">${s.port_name || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Berth</span>
          <span class="popup-val">${s.berth || '—'}</span></div>
        <div class="popup-row"><span class="popup-key">Activity</span>
          <span class="popup-val">${s.activity || '—'}</span></div>
      ` : ''}
      <div style="margin-top:8px;text-align:center">
        <a href="#" onclick="ShipTables.openShip('${s.mmsi}');return false"
           style="color:#58a6ff;font-size:0.75rem">View full details →</a>
      </div>
    `;
  }

  function _updateLayer(ships, layerKey, type, emoji) {
    if (!map) return;
    const layer = LAYERS[layerKey];
    const existing = new Set(Object.keys(markers).filter(m => markers[m].layer === layerKey));

    ships.forEach(s => {
      if (!s.current_lat || !s.current_lon) return;
      const lat = parseFloat(s.current_lat);
      const lon = parseFloat(s.current_lon);
      if (lat === 0 && lon === 0) return;

      const cargo = s.cargo_category || 'OTHER';
      const color = CARGO_COLORS[cargo] || CARGO_COLORS.OTHER;
      const course = parseFloat(s.course || 0);
      const icon = _makeIcon(emoji, color, course);
      const popup = _popupHtml(s, type);

      if (markers[s.mmsi]) {
        markers[s.mmsi].marker.setLatLng([lat, lon]);
        markers[s.mmsi].marker.setIcon(icon);
        markers[s.mmsi].marker.setPopupContent(popup);
        markers[s.mmsi].cargo = cargo;
        existing.delete(s.mmsi);
      } else {
        const marker = L.marker([lat, lon], { icon })
          .bindPopup(popup)
          .addTo(layer);
        markers[s.mmsi] = { marker, cargo, layer: layerKey };
      }
      existing.delete(s.mmsi);
    });

    // Remove markers for ships no longer in this layer
    existing.forEach(mmsi => {
      if (markers[mmsi] && markers[mmsi].layer === layerKey) {
        LAYERS[layerKey].removeLayer(markers[mmsi].marker);
        delete markers[mmsi];
      }
    });
  }

  function updateInbound(ships)  { _updateLayer(ships, 'inbound',  'inbound',  '▲'); }
  function updateOutbound(ships) { _updateLayer(ships, 'outbound', 'outbound', '▽'); }
  function updateInPort(ships)   { _updateLayer(ships, 'inPort',   'inPort',   '⬟'); }

  function applyFilter(filter) {
    activeFilter = filter;
    Object.values(markers).forEach(({ marker, cargo }) => {
      const el = marker.getElement();
      if (!el) return;
      el.style.opacity = (filter === 'ALL' || cargo === filter) ? '1' : '0.15';
    });
  }

  function invalidate() {
    if (map) setTimeout(() => map.invalidateSize(), 100);
  }

  return { init, updateInbound, updateOutbound, updateInPort, applyFilter, invalidate };
})();
