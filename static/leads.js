/* ==========================================================================
   Leads review-queue interactions.

   The new /leads page is a dense, filterable, sortable, selectable table
   with a side drawer and bulk actions. This module owns:

   - Filter form: debounced client-side search (haystack), slider readouts,
     status/product chip groups that act as radios.
   - Sortable column headers (re-submits the form with a different `sort`).
   - Row click → opens the side drawer; checkboxes stop propagation.
   - Bulk selection: header checkbox, per-row checkbox, sticky bulk bar.
   - Bulk approve/reject: POSTs to /api/leads/bulk-status and reloads.
   - Drawer: fetch /api/leads/{id}, render Overview / Evidence / Reason,
     approve / reject buttons inside the drawer.
   - Keyboard: j/k/x/a/r/o for review, `/` to focus search, `?` for help.

   The list behind the drawer stays in the DOM so the reviewer's filters
   and selection are preserved.
   ========================================================================== */
(function () {
  'use strict';

  var Motion = window.Motion || null;
  var prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var table = document.getElementById('leads-table');
  if (!table) return; // page doesn't have the table (dashboard, etc.)

  /* ---- Helpers ------------------------------------------------------- */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function animate(el, props, opts) {
    if (!Motion || prefersReduced) return null;
    return Motion.animate(el, props, Object.assign({ duration: 0.22, ease: 'easeOut' }, opts || {}));
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments, ctx = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(ctx, args); }, ms);
    };
  }

  function fmtConf(value) {
    if (value === null || value === undefined || value === '') return '—';
    var n = Number(value);
    if (Number.isNaN(n)) return '—';
    return n + '%';
  }

  function fitTier(score) {
    if (score === null || score === undefined || score === '') return { label: '—', cls: 'text-slate-400' };
    if (score >= 80) return { label: 'Strong', cls: 'text-emerald-400' };
    if (score >= 65) return { label: 'Good', cls: 'text-amber-400' };
    return { label: 'Weak', cls: 'text-rose-400' };
  }

  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function leadHref(website) {
    if (!website) return '';
    return String(website).replace(/^https?:\/\//, '').replace(/\/$/, '');
  }

  /* =================================================================
     FILTER FORM
     ================================================================= */

  function initFilterForm() {
    var form = $('#leads-filter');
    if (!form) return;

    // Range readouts: live number next to each slider.
    $all('input[type=range][data-range-output]', form).forEach(function (input) {
      var out = document.getElementById(input.dataset.rangeOutput);
      if (!out) return;
      out.textContent = input.value;
      input.addEventListener('input', function () { out.textContent = input.value; });
    });

    // Client-side search: filter visible rows by the per-row data-haystack
    // attribute. Server still re-filters on submit so the URL is shareable.
    var search = $('#filter-search', form);
    var rows = $all('tr.leads-row', table);
    var countShown = $('[data-load-count]');
    var countTotal = $('[data-total-count]');

    function applyClientSearch() {
      var needle = (search.value || '').trim().toLowerCase();
      var visible = 0;
      rows.forEach(function (row) {
        var hay = row.dataset.haystack || '';
        var match = !needle || hay.indexOf(needle) !== -1;
        row.hidden = !match;
        if (match) visible++;
      });
      if (countShown) countShown.textContent = visible;
      if (countTotal) countTotal.textContent = rows.length;
    }

    if (search) {
      search.addEventListener('input', debounce(applyClientSearch, 120));
    }

    // Status / product chips: clicking a chip toggles the hidden radio.
    $all('[data-status-chip]', form).forEach(function (chip) {
      chip.addEventListener('click', function (e) {
        if (e.target.tagName === 'INPUT') return; // native radio handles it
        var input = chip.querySelector('input');
        if (!input) return;
        $all('[data-status-chip] .is-active, [data-status-chip].is-active', form).forEach(function (c) { c.classList.remove('is-active'); });
        input.checked = true;
        chip.classList.add('is-active');
        $all('[data-status-clear]').forEach(function (c) { c.classList.remove('is-active'); });
      });
    });
    $all('[data-product-chip]', form).forEach(function (chip) {
      chip.addEventListener('click', function (e) {
        if (e.target.tagName === 'INPUT') return;
        var input = chip.querySelector('input');
        if (!input) return;
        $all('[data-product-chip].is-active', form).forEach(function (c) { c.classList.remove('is-active'); });
        input.checked = true;
        chip.classList.add('is-active');
        $all('[data-product-clear]').forEach(function (c) { c.classList.remove('is-active'); });
      });
    });
    $all('[data-status-clear]', form).forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        $all('[data-status-chip] input', form).forEach(function (i) { i.checked = false; });
        $all('[data-status-chip].is-active', form).forEach(function (c) { c.classList.remove('is-active'); });
        $all('[data-status-clear]').forEach(function (c) { c.classList.add('is-active'); });
      });
    });
    $all('[data-product-clear]', form).forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        $all('[data-product-chip] input', form).forEach(function (i) { i.checked = false; });
        $all('[data-product-chip].is-active', form).forEach(function (c) { c.classList.remove('is-active'); });
        $all('[data-product-clear]').forEach(function (c) { c.classList.add('is-active'); });
      });
    });

    // Sortable column headers: clicking one re-submits the form with the
    // requested sort key (and the same filters). Reviewer never loses state.
    $all('th.leads-sortable', table).forEach(function (th) {
      th.addEventListener('click', function () {
        var key = th.dataset.sortKey;
        if (!key) return;
        // Toggle direction for already-sorted columns (server expects specific
        // keys like fit_desc / fit_asc; we only support the four the server
        // recognises, so toggling would require server cooperation. For now:
        // clicking just sets the key.)
        var sortInput = form.querySelector('input[name=sort]');
        if (!sortInput) {
          sortInput = document.createElement('input');
          sortInput.type = 'hidden';
          sortInput.name = 'sort';
          form.appendChild(sortInput);
        }
        sortInput.value = key;
        form.submit();
      });
    });
  }

  /* =================================================================
     ROW INTERACTION
     ================================================================= */

  var focusedRow = null;

  function focusRow(row) {
    if (focusedRow) focusedRow.classList.remove('is-focused');
    focusedRow = row;
    if (row) row.classList.add('is-focused');
  }

  function initRowInteraction() {
    var rows = $all('tr.leads-row', table);
    rows.forEach(function (row) {
      // Click on the row → open the drawer (unless the click was on a
      // control that opts out via [data-stop-row-click]).
      row.addEventListener('click', function (e) {
        if (e.target.closest('[data-stop-row-click]')) return;
        if (e.target.closest('a, button, input, label')) return;
        focusRow(row);
        openDrawer(row.dataset.leadId);
      });
    });
  }

  /* =================================================================
     BULK SELECTION
     ================================================================= */

  function getSelectedIds() {
    return $all('input[data-bulk-row]:checked').map(function (i) { return i.value; });
  }

  function updateBulkBar() {
    var ids = getSelectedIds();
    var bar = $('#bulk-bar');
    var countEl = $('[data-bulk-count]', bar);
    if (!bar) return;
    if (ids.length === 0) {
      bar.hidden = true;
    } else {
      bar.hidden = false;
      if (countEl) countEl.textContent = ids.length;
    }
    // Header checkbox tri-state.
    var all = $('input[data-bulk-select-all]');
    var totalRows = $all('input[data-bulk-row]').length;
    if (all) {
      all.checked = totalRows > 0 && ids.length === totalRows;
      all.indeterminate = ids.length > 0 && ids.length < totalRows;
    }
  }

  function initBulk() {
    // Per-row checkboxes
    $all('input[data-bulk-row]').forEach(function (cb) {
      cb.addEventListener('change', function () {
        var row = cb.closest('tr');
        if (row) row.classList.toggle('is-selected', cb.checked);
        updateBulkBar();
      });
    });

    // Header "select all"
    var selectAll = $('input[data-bulk-select-all]');
    if (selectAll) {
      selectAll.addEventListener('change', function () {
        var checked = selectAll.checked;
        $all('input[data-bulk-row]').forEach(function (cb) {
          cb.checked = checked;
          var row = cb.closest('tr');
          if (row) row.classList.toggle('is-selected', checked);
        });
        updateBulkBar();
      });
    }

    // Bulk action buttons
    $all('[data-bulk-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var status = btn.dataset.bulkAction;
        var ids = getSelectedIds();
        if (!ids.length) return;
        var original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = 'Working…';
        fetch('/api/leads/bulk-status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: status, ids: ids })
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            btn.innerHTML = 'Updated ' + (data.updated || 0);
            setTimeout(function () { window.location.reload(); }, 500);
          })
          .catch(function () {
            btn.innerHTML = 'Failed';
            setTimeout(function () { btn.innerHTML = original; btn.disabled = false; }, 1200);
          });
      });
    });

    var clearBtn = $('[data-bulk-clear]');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        $all('input[data-bulk-row]').forEach(function (cb) {
          cb.checked = false;
          var row = cb.closest('tr');
          if (row) row.classList.remove('is-selected');
        });
        updateBulkBar();
      });
    }
  }

  /* =================================================================
     DRAWER
     ================================================================= */

  var drawer = $('#lead-drawer');
  var drawerPanel = drawer ? drawer.querySelector('.drawer__panel') : null;

  function openDrawer(leadId) {
    if (!drawer) return;
    drawer.hidden = false;
    drawer.setAttribute('aria-hidden', 'false');
    // Force reflow so the transition fires.
    void drawer.offsetWidth;
    drawer.classList.add('is-open');
    // Reset to Overview tab.
    $all('.drawer__tab', drawer).forEach(function (t) { t.classList.toggle('is-active', t.dataset.drawerTab === 'overview'); });
    $all('.drawer__section', drawer).forEach(function (s) { s.classList.toggle('is-active', s.dataset.drawerPane === 'overview'); });

    // Skeleton placeholders while we fetch.
    $all('.drawer__pane', drawer).forEach(function (p) {
      p.innerHTML = '<div class="drawer__skeleton" style="width:60%"></div>'
        + '<div class="drawer__skeleton" style="width:90%"></div>'
        + '<div class="drawer__skeleton" style="width:80%"></div>';
    });
    $('#drawer-title').textContent = 'Loading lead…';
    $('#drawer-eyebrow').textContent = '';
    $('#drawer-meta').textContent = '';
    var openFull = $('#drawer-open-full');
    if (openFull) openFull.href = '/leads/' + encodeURIComponent(leadId);

    fetch('/api/leads/' + encodeURIComponent(leadId))
      .then(function (r) {
        if (!r.ok) throw new Error('fetch failed');
        return r.json();
      })
      .then(function (lead) { renderDrawer(lead); })
      .catch(function () {
        $('#drawer-title').textContent = 'Could not load lead';
        $all('.drawer__pane', drawer).forEach(function (p) { p.innerHTML = '<p>Try opening the <a href="/leads/' + encodeURIComponent(leadId) + '">full lead page</a>.</p>'; });
      });

    if (drawerPanel) drawerPanel.focus();
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.classList.remove('is-open');
    drawer.setAttribute('aria-hidden', 'true');
    setTimeout(function () { drawer.hidden = true; }, 240);
  }

  function renderDrawer(lead) {
    var cp = lead.company_profile || {};
    var sd = lead.scraped_data || {};
    var conf = lead.confidence_score !== null && lead.confidence_score !== undefined
      ? lead.confidence_score : (sd.confidence_score !== undefined ? sd.confidence_score : null);

    $('#drawer-eyebrow').innerHTML =
      '<span class="status ' + (lead.status || 'raw') + '">' + esc(lead.status || 'raw').replace(/_/g, ' ') + '</span>';
    $('#drawer-title').textContent = lead.company_name || 'Untitled lead';
    var metaBits = [];
    if (lead.target_product) metaBits.push(esc(lead.target_product));
    if (lead.website) metaBits.push('<a href="' + esc(lead.website) + '" target="_blank" rel="noopener">' + esc(leadHref(lead.website)) + '</a>');
    if (lead.created_at) metaBits.push('<span title="' + esc(lead.created_at) + '">' + esc((lead.created_at || '').slice(0, 10)) + '</span>');
    $('#drawer-meta').innerHTML = metaBits.join(' · ');

    // Overview pane
    var dm = (cp.decision_makers || [])[0] || {};
    var fitTier_ = fitTier(lead.fit_score);
    var overviewRows = [
      ['Fit score', '<span class="' + fitTier_.cls + '" style="font-weight:700">' + (lead.fit_score !== null && lead.fit_score !== undefined ? lead.fit_score : '—') + '</span> <span style="color:var(--faint)">· ' + fitTier_.label + '</span>'],
      ['Confidence', conf !== null ? conf + '%' : '—'],
      ['Status', '<span class="status ' + (lead.status || 'raw') + '">' + esc((lead.status || 'raw').replace(/_/g, ' ')) + '</span>'],
      ['Persona', lead.buyer_persona ? esc(lead.buyer_persona) : '—'],
      ['Matches persona', (lead.matches_persona || (cp && cp.matches_persona)) ? 'Yes' : 'No'],
    ];
    if (cp.segment) overviewRows.push(['Segment', esc((cp.segment || '').replace(/_/g, ' '))]);
    if (cp.industry) overviewRows.push(['Industry', esc(cp.industry)]);
    if (cp.employee_range) overviewRows.push(['Employees', esc(cp.employee_range)]);
    if (lead.tech_stack && lead.tech_stack.length) overviewRows.push(['Tech stack', (lead.tech_stack || []).map(esc).join(', ')]);

    var overviewHtml = '<h4>Lead</h4><dl class="meta-grid">'
      + overviewRows.map(function (r) { return '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>'; }).join('')
      + '</dl>';

    if (dm && (dm.name || dm.email || dm.title)) {
      overviewHtml += '<h4>Decision-maker</h4><dl class="meta-grid">'
        + (dm.name ? '<dt>Name</dt><dd>' + esc(dm.name) + '</dd>' : '')
        + (dm.title ? '<dt>Title</dt><dd>' + esc(dm.title) + '</dd>' : '')
        + (dm.email ? '<dt>Email</dt><dd><a href="mailto:' + esc(dm.email) + '">' + esc(dm.email) + '</a></dd>' : '')
        + '</dl>';
    }

    if (cp.post_title || cp.post_snippet) {
      overviewHtml += '<h4>Intent signal</h4>'
        + '<div class="reasoning-block">'
        + (cp.post_title ? '<div style="font-weight:600;color:var(--text-strong)">' + esc(cp.post_title) + '</div>' : '')
        + (cp.post_snippet ? '<div class="reasoning-quote">' + esc(cp.post_snippet) + '</div>' : '')
        + ((cp.source || cp.source_name) ? '<div style="font-size:0.6875rem;color:var(--faint);margin-top:0.5rem">via ' + esc(cp.source || '') + (cp.source_name ? ' · ' + esc(cp.source_name) : '') + '</div>' : '')
        + (cp.source_url ? '<div style="margin-top:0.5rem"><a href="' + esc(cp.source_url) + '" target="_blank" rel="noopener">View source →</a></div>' : '')
        + '</div>';
    }

    if (sd.buying_signals && sd.buying_signals.length) {
      overviewHtml += '<h4>Buying signals</h4><ul class="evidence-list">'
        + sd.buying_signals.map(function (s) {
          var text = typeof s === 'string' ? s : (s.signal || s.reason || JSON.stringify(s));
          return '<li>' + esc(text) + '</li>';
        }).join('')
        + '</ul>';
    }

    $('#drawer-overview').innerHTML = overviewHtml;

    // Evidence pane
    var evidenceHtml = '';
    var findings = sd.evidence || [];
    if (findings.length) {
      evidenceHtml += '<h4>Research findings (' + findings.length + ')</h4><ul class="evidence-list">'
        + findings.map(function (f) {
          var t = typeof f === 'string' ? f : (f.finding || f.text || f.summary || JSON.stringify(f));
          return '<li>' + esc(t) + '</li>';
        }).join('')
        + '</ul>';
    } else {
      evidenceHtml += '<p style="color:var(--faint)">No research findings yet for this lead.</p>';
    }
    if (sd.pages_crawled && sd.pages_crawled.length) {
      evidenceHtml += '<h4>Pages crawled</h4><ul class="evidence-list">'
        + sd.pages_crawled.map(function (p) { return '<li>' + esc(p) + '</li>'; }).join('')
        + '</ul>';
    }
    if (sd.extractor) {
      evidenceHtml += '<h4>Extractor</h4><p style="color:var(--muted)">' + esc(sd.extractor) + '</p>';
    }
    $('#drawer-evidence').innerHTML = evidenceHtml || '<p style="color:var(--faint)">No evidence recorded.</p>';

    // Reason pane
    var reason = (lead.reasoning || '').trim();
    var reasonHtml = '';
    if (reason) {
      reasonHtml += '<h4>Why this lead was picked</h4>'
        + '<div class="reasoning-block">' + esc(reason) + '</div>';
    } else {
      reasonHtml += '<p style="color:var(--faint)">No reasoning recorded yet.</p>';
    }
    var topReasons = sd.top_reasons || [];
    if (topReasons.length) {
      reasonHtml += '<h4>Top reasons</h4><ul class="evidence-list">'
        + topReasons.map(function (r) {
          return '<li><strong>' + esc(r.reason || '') + '</strong>'
            + (r.quote ? '<div class="reasoning-quote">' + esc(r.quote) + '</div>' : '')
            + '</li>';
        }).join('')
        + '</ul>';
    }
    $('#drawer-reason').innerHTML = reasonHtml;
  }

  function initDrawer() {
    if (!drawer) return;

    $all('[data-drawer-close]', drawer).forEach(function (el) {
      el.addEventListener('click', closeDrawer);
    });

    $all('.drawer__tab', drawer).forEach(function (tab) {
      tab.addEventListener('click', function () {
        var key = tab.dataset.drawerTab;
        $all('.drawer__tab', drawer).forEach(function (t) { t.classList.toggle('is-active', t === tab); });
        $all('.drawer__section', drawer).forEach(function (s) { s.classList.toggle('is-active', s.dataset.drawerPane === key); });
      });
    });

    $all('[data-drawer-action]', drawer).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var status = btn.dataset.drawerAction;
        var leadId = (drawerPanel && drawerPanel.dataset.leadId) || '';
        // Fallback: read from "Open full page" link.
        if (!leadId) {
          var full = $('#drawer-open-full');
          if (full) leadId = (full.getAttribute('href') || '').split('/').pop();
        }
        if (!leadId) return;
        var original = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = 'Working…';
        fetch('/api/leads/' + encodeURIComponent(leadId) + '/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: status })
        })
          .then(function (r) { return r.json(); })
          .then(function () {
            btn.innerHTML = status === 'approved' ? 'Approved ✓' : 'Rejected ✓';
            setTimeout(function () { window.location.reload(); }, 500);
          })
          .catch(function () {
            btn.innerHTML = 'Failed';
            setTimeout(function () { btn.innerHTML = original; btn.disabled = false; }, 1200);
          });
      });
    });
  }

  /* =================================================================
     KEYBOARD SHORTCUTS
     ================================================================= */

  function focusNextRow(direction) {
    var rows = $all('tr.leads-row:not([hidden])', table);
    if (!rows.length) return;
    var idx = focusedRow ? rows.indexOf(focusedRow) : -1;
    var next = rows[Math.max(0, Math.min(rows.length - 1, idx + direction))];
    if (next) {
      focusRow(next);
      next.scrollIntoView({ block: 'nearest', behavior: prefersReduced ? 'auto' : 'smooth' });
    }
  }

  function initKeyboard() {
    var kbdSheet = $('#kbdsheet');
    function openKbd() { if (kbdSheet) kbdSheet.hidden = false; }
    function closeKbd() { if (kbdSheet) kbdSheet.hidden = true; }

    $all('[data-kbd-close]', kbdSheet).forEach(function (el) {
      el.addEventListener('click', closeKbd);
    });

    document.addEventListener('keydown', function (e) {
      // Don't steal keys when the user is typing in an input.
      var tag = (e.target && e.target.tagName) || '';
      var isTyping = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (e.target && e.target.isContentEditable);

      if (e.key === 'Escape') {
        if (kbdSheet && !kbdSheet.hidden) { closeKbd(); return; }
        if (drawer && !drawer.hidden) { closeDrawer(); return; }
      }

      if (isTyping) return;

      if (e.key === '/') {
        e.preventDefault();
        var s = $('#filter-search');
        if (s) s.focus();
        return;
      }
      if (e.key === '?') {
        e.preventDefault();
        if (kbdSheet && kbdSheet.hidden) openKbd(); else closeKbd();
        return;
      }
      if (e.key === 'j') { e.preventDefault(); focusNextRow(1); return; }
      if (e.key === 'k') { e.preventDefault(); focusNextRow(-1); return; }
      if (e.key === 'x' && focusedRow) {
        e.preventDefault();
        var cb = focusedRow.querySelector('input[data-bulk-row]');
        if (cb) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); }
        return;
      }
      if (e.key === 'o' || e.key === 'Enter') {
        if (focusedRow) { e.preventDefault(); openDrawer(focusedRow.dataset.leadId); }
        return;
      }
      if (e.key === 'a' && focusedRow) {
        e.preventDefault();
        quickStatus(focusedRow.dataset.leadId, 'approved');
        return;
      }
      if (e.key === 'r' && focusedRow) {
        e.preventDefault();
        quickStatus(focusedRow.dataset.leadId, 'rejected');
        return;
      }
    });
  }

  function quickStatus(leadId, status) {
    fetch('/api/leads/' + encodeURIComponent(leadId) + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: status })
    }).then(function () { window.location.reload(); }).catch(function () {});
  }

  /* =================================================================
     BOOT
     ================================================================= */

  document.addEventListener('DOMContentLoaded', function () {
    initFilterForm();
    initRowInteraction();
    initBulk();
    initDrawer();
    initKeyboard();
    updateBulkBar();
  });
})();
