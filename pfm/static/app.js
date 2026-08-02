/* Two features, both progressive enhancement:
     1. Privacy mode - blur every monetary figure.
     2. Sortable holdings tables.

   On privacy mode and capture detection
   -------------------------------------
   No web API reports that a page is being screen-shared or screenshotted. The
   whole Screen Capture API (getDisplayMedia, CaptureController, CropTarget,
   displaySurface) is written from the capturing page's point of view; there is
   no inverse, deliberately, because it would be a fingerprinting vector.
   FLAG_SECURE on Android and UIScreen.isCaptured on iOS are native-app only.

   So this does not detect capture. It hides amounts on the signals a page can
   actually observe:
     * focus loss        - reliable. Starting a screen share, alt-tabbing into a
                           call, or opening a snipping tool all move focus away.
     * tab hidden        - reliable, via visibilitychange.
     * printing          - reliable, via a @media print rule and beforeprint.
     * screenshot keys   - BEST EFFORT ONLY. PrintScreen, Cmd+Shift+3/4/5 and
                           Win+Shift+S are usually swallowed by the OS before the
                           page sees them. Treated as a bonus, never relied on.
     * idle              - a timer, for walking away from the desk. */

(function () {
  'use strict';

  var body = document.body;
  var toggle = document.getElementById('privacy-toggle');
  var flash = document.getElementById('privacy-flash');
  var label = toggle && toggle.querySelector('.privacy-label');
  var STORAGE_KEY = 'pfm.privacy';

  function flagOn(name) {
    return body.getAttribute('data-privacy-' + name) === '1';
  }

  function stored() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;   // private browsing, or storage disabled
    }
  }

  function remember(on) {
    try {
      window.localStorage.setItem(STORAGE_KEY, on ? '1' : '0');
    } catch (e) { /* not fatal - the toggle still works for this session */ }
  }

  function isOn() {
    return body.classList.contains('privacy-on');
  }

  function say(message) {
    if (!flash) return;
    if (!message) {
      flash.hidden = true;
      flash.textContent = '';
      return;
    }
    flash.textContent = message;
    flash.hidden = false;
    window.clearTimeout(say._timer);
    say._timer = window.setTimeout(function () { flash.hidden = true; }, 6000);
  }

  /* SVG <title> cannot be blurred by CSS, so the chart's tooltips are swapped
     for amount-free variants that the server rendered alongside them. */
  function syncChartTooltips(on) {
    var dots = document.querySelectorAll('.dot[data-full]');
    Array.prototype.forEach.call(dots, function (dot) {
      var title = dot.querySelector('title');
      if (!title) return;
      title.textContent = on
        ? (dot.getAttribute('data-safe') || '')
        : (dot.getAttribute('data-full') || '');
    });
  }

  function setPrivacy(on, opts) {
    opts = opts || {};
    body.classList.toggle('privacy-on', !!on);
    if (!on) body.classList.remove('privacy-peek');
    if (toggle) {
      toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (label) label.textContent = on ? 'Amounts hidden' : 'Hide amounts';
    }
    syncChartTooltips(!!on);
    if (opts.remember !== false) remember(on);
    if (opts.reason) say(opts.reason);
    else if (!on) say('');
  }

  // Initial state: whatever was last chosen, else the server-side default.
  var saved = stored();
  setPrivacy(saved === null ? flagOn('default') : saved === '1',
             { remember: false });

  if (toggle) {
    toggle.addEventListener('click', function () { setPrivacy(!isOn()); });
  }

  // -- keyboard -----------------------------------------------------------
  document.addEventListener('keydown', function (event) {
    var typing = /^(INPUT|TEXTAREA|SELECT)$/.test(
      (event.target && event.target.tagName) || '');
    if (typing) return;

    // p toggles.
    if (!event.metaKey && !event.ctrlKey && !event.altKey &&
        (event.key === 'p' || event.key === 'P')) {
      event.preventDefault();
      setPrivacy(!isOn());
      return;
    }

    // Shift peeks while held.
    if (event.key === 'Shift' && isOn()) {
      body.classList.add('privacy-peek');
      return;
    }

    if (!flagOn('blur-on-keys')) return;

    // Best effort: most of these never reach the page.
    var key = event.key;
    var screenshotKey =
      key === 'PrintScreen' ||
      ((event.metaKey || event.ctrlKey) && event.shiftKey &&
       (key === '3' || key === '4' || key === '5' || key === 'S' || key === 's')) ||
      ((event.metaKey || event.ctrlKey) && (key === 'p' || key === 'P'));

    if (screenshotKey && !isOn()) {
      setPrivacy(true, {
        remember: false,
        reason: 'Amounts hidden: a screenshot or print shortcut was detected. ' +
                'Press p to show them again.'
      });
    }
  });

  document.addEventListener('keyup', function (event) {
    if (event.key === 'Shift') body.classList.remove('privacy-peek');
  });

  // Releasing Shift outside the window would otherwise leave peek stuck on.
  window.addEventListener('blur', function () {
    body.classList.remove('privacy-peek');
  });

  // -- press and hold one figure -----------------------------------------
  document.addEventListener('pointerdown', function (event) {
    var cell = event.target && event.target.closest &&
               event.target.closest('.amt');
    if (cell && isOn()) {
      cell.classList.add('peek');
      event.preventDefault();
    }
  });

  ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (name) {
    document.addEventListener(name, function () {
      var peeking = document.querySelectorAll('.amt.peek');
      Array.prototype.forEach.call(peeking, function (el) {
        el.classList.remove('peek');
      });
    });
  });

  // -- focus and visibility ----------------------------------------------
  if (flagOn('blur-on-blur')) {
    window.addEventListener('blur', function () {
      if (!isOn()) {
        setPrivacy(true, {
          remember: false,
          reason: 'Amounts hidden because the window lost focus. Press p to show them.'
        });
      }
    });
  }

  if (flagOn('blur-on-hidden')) {
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden' && !isOn()) {
        setPrivacy(true, { remember: false });
      }
    });
  }

  // Printing is the one capture path a page can reliably intercept. The @media
  // print rule already blurs; this also flips the on-screen state so the
  // preview matches what will come out.
  window.addEventListener('beforeprint', function () {
    if (!isOn()) setPrivacy(true, { remember: false });
  });

  // -- idle ---------------------------------------------------------------
  var idleSeconds = parseInt(body.getAttribute('data-privacy-idle-seconds'), 10);
  if (idleSeconds > 0) {
    var idleTimer = null;
    var resetIdle = function () {
      window.clearTimeout(idleTimer);
      idleTimer = window.setTimeout(function () {
        if (!isOn()) {
          setPrivacy(true, {
            remember: false,
            reason: 'Amounts hidden after ' + idleSeconds +
                    ' seconds of inactivity. Press p to show them.'
          });
        }
      }, idleSeconds * 1000);
    };
    ['pointermove', 'keydown', 'scroll', 'pointerdown'].forEach(function (name) {
      window.addEventListener(name, resetIdle, { passive: true });
    });
    resetIdle();
  }

  function cellValue(row, index) {
    var cell = row.cells[index];
    if (!cell) return '';
    var raw = cell.getAttribute('data-sort');
    if (raw !== null) {
      var num = parseFloat(raw);
      return isNaN(num) ? raw : num;
    }
    return cell.textContent.trim();
  }

  function sortTable(table, index, ascending) {
    var body = table.tBodies[0];
    if (!body) return;

    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var x = cellValue(a, index);
      var y = cellValue(b, index);
      if (typeof x === 'number' && typeof y === 'number') {
        return ascending ? x - y : y - x;
      }
      return ascending
        ? String(x).localeCompare(String(y))
        : String(y).localeCompare(String(x));
    });

    // Re-append in the new order; a fragment keeps this to one reflow.
    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) { fragment.appendChild(row); });
    body.appendChild(fragment);
  }

  document.querySelectorAll('table.sortable').forEach(function (table) {
    var headers = table.querySelectorAll('thead th');
    headers.forEach(function (header, index) {
      header.setAttribute('tabindex', '0');
      header.setAttribute('role', 'button');
      header.title = 'Sort by ' + header.textContent.trim();

      function activate() {
        // Text columns default to A-Z, numeric columns to largest first.
        var isText = header.getAttribute('data-type') === 'text';
        var current = header.getAttribute('aria-sort');
        var ascending = current
          ? current === 'descending'
          : isText;

        headers.forEach(function (other) { other.removeAttribute('aria-sort'); });
        header.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
        sortTable(table, index, ascending);
      }

      header.addEventListener('click', activate);
      header.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate();
        }
      });
    });
  });
})();
