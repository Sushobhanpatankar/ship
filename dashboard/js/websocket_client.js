/**
 * websocket_client.js — WebSocket connection to /ws/updates.
 * Falls back to polling REST endpoints every 60s if WS is unavailable.
 */
window.ShipWS = (() => {
  let ws = null;
  let reconnectTimer = null;
  let pollTimer = null;
  let connected = false;
  const RECONNECT_DELAY = 5000;
  const POLL_INTERVAL = 60000;

  function setStatus(online) {
    connected = online;
    const dot = document.getElementById('ws-dot');
    const label = document.getElementById('ws-status');
    if (dot) dot.className = 'ws-dot' + (online ? '' : ' offline');
    if (label) label.textContent = online ? 'Live' : 'Polling';
  }

  function setLastUpdated() {
    const el = document.getElementById('last-updated');
    if (el) el.textContent = 'Updated: ' + new Date().toLocaleTimeString();
  }

  function connect() {
    if (ws) { try { ws.close(); } catch(_) {} }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/updates`);

    ws.onopen = () => {
      setStatus(true);
      clearTimeout(reconnectTimer);
      // Stop polling fallback when WS is live
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === 'stats' && msg.data) {
          window.ShipApp && window.ShipApp.handleStats(msg.data);
        }
        if (msg.type === 'positions' && msg.data) {
          window.ShipApp && window.ShipApp.handlePositions(msg.data);
        }
        if (msg.type === 'heartbeat' || msg.type === 'keepalive') {
          setLastUpdated();
          // On heartbeat, trigger a fresh REST poll
          window.ShipApp && window.ShipApp.fetchAll();
        }
      } catch (_) {}
    };

    ws.onclose = () => {
      setStatus(false);
      startPolling();
      reconnectTimer = setTimeout(connect, RECONNECT_DELAY);
    };

    ws.onerror = () => {
      setStatus(false);
    };
  }

  function startPolling() {
    if (pollTimer) return;
    // Immediate fetch + then every 60s
    window.ShipApp && window.ShipApp.fetchAll();
    pollTimer = setInterval(() => {
      window.ShipApp && window.ShipApp.fetchAll();
    }, POLL_INTERVAL);
  }

  function init() {
    connect();
  }

  return { init };
})();
