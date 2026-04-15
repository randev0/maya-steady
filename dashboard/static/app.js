// Maya Steady Admin — shared JS utilities

// Auto-refresh analytics every 30s on the overview page
if (window.location.pathname === '/') {
  setInterval(async () => {
    try {
      const res = await fetch('/api/analytics');
      const data = await res.json();
      document.querySelectorAll('[data-metric]').forEach(el => {
        const key = el.dataset.metric;
        if (data[key] !== undefined) el.textContent = data[key];
      });
    } catch (_) {}
  }, 30000);
}
