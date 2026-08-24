/* ==========================================================================
   Conftech UI interactions — vanilla JS + Motion (UMD global `Motion`).

   Owns: page-load reveal, expandable rows, load-more, copyDraft, the job
   banner transitions, and button press feedback. Everything fast (<=300ms),
   nothing decorative. Respects prefers-reduced-motion.
   ========================================================================== */
(function () {
  'use strict';

  var Motion = window.Motion || null;
  var prefersReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* Only hide [data-reveal] elements when this script is present, so a JS
     failure never leaves a page blank. */
  document.documentElement.classList.add('js-anim');

  /* ---- Page-load reveal: [data-reveal] stagger in (once) ---- */
  function reveal() {
    var els = document.querySelectorAll('[data-reveal]:not([hidden])');
    if (!els.length) return;
    if (prefersReduced || !Motion) {
      els.forEach(function (el) { el.classList.add('is-shown'); });
      return;
    }
    Motion.animate(
      els,
      { opacity: [0, 1], y: [6, 0] },
      {
        delay: Motion.stagger(0.035),
        duration: 0.24,
        ease: 'easeOut',
        onComplete: function () {
          els.forEach(function (el) { el.classList.add('is-shown'); });
        }
      }
    );
  }

  /* ---- Expandable sections: button toggles .is-open on aria-controls ---- */
  function bindExpanders() {
    document.querySelectorAll('[data-expander]').forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.addEventListener('click', function () {
        var target = document.getElementById(btn.getAttribute('aria-controls'));
        if (!target) return;
        var open = target.classList.toggle('is-open');
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.classList.toggle('is-open', open);
        if (btn.dataset.expandLabel) {
          btn.textContent = open ? btn.dataset.collapseLabel : btn.dataset.expandLabel;
        }
      });
    });
  }

  /* ---- Load more: reveal hidden [data-hideable] rows in batches of 25 ---- */
  var LOAD_BATCH = 25;
  function bindLoadMore() {
    var btn = document.querySelector('[data-load-more]');
    if (!btn || btn.dataset.bound) return;
    btn.dataset.bound = '1';
    var total = document.querySelectorAll('[data-hideable]').length;
    var shown = total ? Math.max(0, total - document.querySelectorAll('[data-hideable][hidden]').length) : 0;

    function updateCount() {
      var el = document.querySelector('[data-load-count]');
      if (el) el.textContent = shown + ' of ' + total;
      btn.hidden = (total > 0 && shown >= total);
    }

    btn.addEventListener('click', function () {
      var hidden = document.querySelectorAll('[data-hideable][hidden]');
      if (!hidden.length) { updateCount(); return; }
      var batch = Array.prototype.slice.call(hidden, 0, LOAD_BATCH);
      batch.forEach(function (el) {
        el.removeAttribute('hidden');
        if (prefersReduced || !Motion) { el.classList.add('is-shown'); return; }
        Motion.animate(el, { opacity: [0, 1], y: [6, 0] }, {
          duration: 0.2,
          ease: 'easeOut',
          onComplete: function () { el.classList.add('is-shown'); }
        });
      });
      shown += batch.length;
      updateCount();
    });

    updateCount();
  }

  /* ---- copyDraft: shared by lead_detail + drafts ---- */
  function copyDraft(id, btn) {
    var el = document.getElementById('draft-body-' + id);
    if (!el) return Promise.reject(new Error('draft body not found'));
    return navigator.clipboard.writeText(el.innerText).then(function () {
      var original = btn.innerHTML;
      btn.innerHTML = '<span style="color:var(--ok)">Copied!</span>';
      if (Motion && !prefersReduced) {
        Motion.animate(btn, { scale: [1, 1.05, 1] }, { duration: 0.3, ease: 'easeOut' });
      }
      setTimeout(function () { btn.innerHTML = original; }, 1500);
    });
  }

  /* ---- Job banner: eased show/hide driven by the poller in base.html ---- */
  var banner = document.getElementById('job-banner');
  var bannerVisible = false;

  function showJobBanner(opts) {
    if (!banner) return;
    var titleEl = document.getElementById('job-title');
    var stageEl = document.getElementById('job-stage');
    var spinnerEl = document.getElementById('job-spinner');
    if (titleEl) titleEl.textContent = opts.title || '';
    if (stageEl) stageEl.textContent = opts.stage || '';
    if (spinnerEl) spinnerEl.classList.toggle('hidden', !!opts.error);
    banner.classList.toggle('is-error', !!opts.error);
    if (bannerVisible) return;
    bannerVisible = true;
    banner.classList.add('is-visible');
    if (prefersReduced || !Motion) { banner.style.opacity = '1'; return; }
    Motion.animate(banner, { opacity: [0, 1], y: [14, 0] }, { duration: 0.28, ease: 'easeOut' });
  }

  function hideJobBanner() {
    if (!banner || !bannerVisible) return;
    bannerVisible = false;
    if (prefersReduced || !Motion) {
      banner.style.opacity = '0';
      banner.classList.remove('is-visible');
      return;
    }
    Motion.animate(banner, { opacity: [1, 0], y: [0, 8] }, {
      duration: 0.2,
      ease: 'easeIn',
      onComplete: function () {
        banner.classList.remove('is-visible');
      }
    });
  }

  /* ---- Button press feedback (forms redirect, so just a quick pulse) ---- */
  function bindPressFeedback() {
    document.querySelectorAll('[data-press]').forEach(function (btn) {
      if (btn.dataset.pressBound) return;
      btn.dataset.pressBound = '1';
      btn.addEventListener('pointerdown', function () {
        if (prefersReduced || !Motion) return;
        Motion.animate(btn, { scale: [1, 0.96, 1] }, { duration: 0.16, ease: 'easeOut' });
      });
    });
  }

  /* ---- Public API ---- */
  window.ConftechUI = {
    copyDraft: copyDraft,
    showJobBanner: showJobBanner,
    hideJobBanner: hideJobBanner,
    reveal: reveal
  };
  /* Legacy inline callers (templates call copyDraft(id, btn)) */
  window.copyDraft = copyDraft;

  document.addEventListener('DOMContentLoaded', function () {
    reveal();
    bindExpanders();
    bindLoadMore();
    bindPressFeedback();
  });
})();
