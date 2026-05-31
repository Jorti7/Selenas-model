/* Houston Home Finder — client-side interactions */

// Like / Dislike / Neutral feedback
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.btn-feedback');
  if (!btn) return;

  var id = btn.dataset.id;
  var action = btn.dataset.action;
  if (!id || !action) return;

  fetch('/api/feedback/' + id, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: action }),
  })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (!data.ok) return;

    // Update button states in the same card / panel
    var container = btn.closest('.feedback-row, .feedback-row-large');
    if (container) {
      container.querySelectorAll('.btn-feedback').forEach(function(b) {
        b.classList.remove('active');
      });
      btn.classList.add('active');
    }

    // Update score bar if present in the same card
    var card = btn.closest('.listing-card');
    if (card) {
      var bar = card.querySelector('.score-bar');
      if (bar) {
        var pct = Math.round(data.new_ml_score * 100);
        bar.style.width = pct + '%';
        bar.closest('.score-bar-wrap').title = 'Match score: ' + pct + '%';
      }
      if (action === 'like') card.classList.add('liked');
      else card.classList.remove('liked');
      if (action === 'dislike') card.classList.add('disliked');
      else card.classList.remove('disliked');
    }
  })
  .catch(function(err) { console.error('Feedback error:', err); });
});

// Run scraper button(s)
function triggerScraper() {
  var btns = document.querySelectorAll('#btn-run-scraper, #btn-run-scraper-inline');
  btns.forEach(function(b) { b.disabled = true; b.textContent = 'Running...'; });

  fetch('/api/run-scraper', { method: 'POST' })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    btns.forEach(function(b) { b.textContent = 'Scraper Started!'; });
    setTimeout(function() {
      btns.forEach(function(b) { b.disabled = false; b.textContent = 'Run Scraper Now'; });
    }, 5000);
  })
  .catch(function() {
    btns.forEach(function(b) { b.disabled = false; b.textContent = 'Run Scraper Now'; });
  });
}

document.addEventListener('click', function(e) {
  if (e.target.id === 'btn-run-scraper' || e.target.id === 'btn-run-scraper-inline') {
    triggerScraper();
  }
});
