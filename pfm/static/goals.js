/* Goals: live loan calculator and affordability modelling.

   The amortisation runs in the browser so dragging a slider redraws instantly
   with no round-trip — that is the whole point of the view. The same model is
   implemented in goals.py and tested against the spreadsheet; this is the
   interactive twin of it, and the two are kept deliberately identical:

     interest = balance * rate / 12
     principal = EMI - interest
     every 12th month: balance -= baseEmi * extraEmis;  EMI *= 1 + hike

   Only pressing Save talks to the server. */

(function () {
  'use strict';

  var stateEl = document.getElementById('goals-state');
  if (!stateEl) return;

  var STATE = JSON.parse(stateEl.textContent || '{}');
  var goals = (STATE.goals || []).slice();
  var editing = null;          // index into goals, or null
  var dirty = false;

  var listEl = document.getElementById('goal-list');
  var editorEl = document.getElementById('goal-editor');
  var titleEl = document.getElementById('goal-editor-title');
  var formEl = document.getElementById('goal-form');
  var resultEl = document.getElementById('goal-result');
  var noteEl = document.getElementById('goal-saved-note');

  var MAX_MONTHS = 600;
  var EPSILON = 0.5;

  // ---------------------------------------------------------------- utils
  function rupees(value, decimals) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    return '₹' + Number(value).toLocaleString('en-IN', {
      minimumFractionDigits: decimals || 0, maximumFractionDigits: decimals || 0
    });
  }

  function amt(value) {
    // Same wrapper the reports use, so privacy mode blurs these too.
    return '<span class="amt">' + rupees(value) + '</span>';
  }

  function years(months) {
    if (!months) return '—';
    var y = Math.floor(months / 12), m = months % 12;
    return y + 'y' + (m ? ' ' + m + 'm' : '');
  }

  function esc(text) {
    var div = document.createElement('div');
    div.textContent = text === null || text === undefined ? '' : String(text);
    return div.innerHTML;
  }

  function num(id) {
    var el = document.getElementById(id);
    if (!el) return 0;
    var value = parseFloat(el.value);
    return isNaN(value) ? 0 : value;
  }

  // ------------------------------------------------------------- the model
  function buildSchedule(principal, annualRate, emi, opts) {
    opts = opts || {};
    var extra = opts.extraEmis || 0;
    var hike = opts.hikePct || 0;
    var lump = opts.lumpSum || 0;

    var rows = [], warnings = [];
    var balance = principal, emiCurrent = emi, baseEmi = emi;
    var totalInterest = 0, totalPrincipal = 0, totalPrepaid = 0, month = 0;

    if (principal <= 0) return { rows: rows, months: 0, totalInterest: 0,
      totalPrincipal: 0, totalPrepaid: 0, cleared: true, warnings: warnings };

    if (lump > 0) {
      var applied = Math.min(lump, balance);
      balance -= applied; totalPrepaid += applied;
    }

    var floor = balance * Math.max(annualRate, 0) / 12;
    if (emi <= floor && balance > EPSILON && extra <= 0 && hike <= 0) {
      warnings.push('An EMI of ' + rupees(emi) + ' does not cover the first month’s '
        + 'interest of ' + rupees(floor) + ', so the balance would never fall.');
      return { rows: rows, months: 0, totalInterest: 0, totalPrincipal: 0,
               totalPrepaid: totalPrepaid, cleared: false, warnings: warnings };
    }

    while (balance > EPSILON && month < MAX_MONTHS) {
      month += 1;
      var interest = balance * annualRate / 12;
      var principalPart = emiCurrent - interest;
      if (principalPart > balance) principalPart = balance;
      if (principalPart < 0) principalPart = 0;

      balance -= principalPart;
      totalInterest += interest;
      totalPrincipal += principalPart;

      var prepayment = 0;
      if (month % 12 === 0 && balance > EPSILON) {
        if (extra > 0) {
          prepayment = Math.min(baseEmi * extra, balance);
          balance -= prepayment;
          totalPrepaid += prepayment;
        }
        if (hike) emiCurrent *= 1 + hike / 100;
      }

      rows.push({ m: month, p: principalPart, i: interest,
                  pre: prepayment, bal: Math.max(balance, 0) });
    }

    var cleared = balance <= EPSILON;
    if (!cleared) {
      warnings.push('Still ' + rupees(balance) + ' outstanding after '
        + (MAX_MONTHS / 12) + ' years. The EMI is too small for this balance and rate.');
    }
    return { rows: rows, months: month, totalInterest: totalInterest,
             totalPrincipal: totalPrincipal + totalPrepaid,
             totalPrepaid: totalPrepaid, cleared: cleared, warnings: warnings };
  }

  function monthlyEmi(principal, annualRate, yrs) {
    if (principal <= 0 || yrs <= 0) return null;
    var months = Math.round(yrs * 12);
    if (months <= 0) return null;
    if (annualRate <= 0) return principal / months;
    var r = annualRate / 12;
    var factor = Math.pow(1 + r, months);
    return principal * r * factor / (factor - 1);
  }

  // ------------------------------------------------------------- the chart
  function drawChart(plan, baseline) {
    if (!plan.rows.length) return '<p class="empty">Nothing to chart yet.</p>';

    var W = 760, H = 240, padL = 8, padR = 8, padT = 10, padB = 22;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var n = plan.rows.length;
    var slot = plotW / n;
    // Leave a hairline gap so a long schedule still reads as monthly bars
    // rather than one solid block, but never thin them below a visible pixel.
    var barW = Math.max(slot * 0.82, 0.7);

    // Stacked principal and interest per month, as in the sheet's bar chart.
    var maxPay = 0;
    plan.rows.forEach(function (r) { maxPay = Math.max(maxPay, r.p + r.i); });
    if (maxPay <= 0) maxPay = 1;

    var bars = plan.rows.map(function (r, idx) {
      var x = padL + idx * slot + (slot - barW) / 2;
      var hP = (r.p / maxPay) * plotH;
      var hI = (r.i / maxPay) * plotH;
      var yI = padT + plotH - hI;
      var yP = yI - hP;
      return '<rect class="bar-i" x="' + x.toFixed(2) + '" y="' + yI.toFixed(2)
        + '" width="' + barW.toFixed(2) + '" height="' + Math.max(hI, 0).toFixed(2) + '"/>'
        + '<rect class="bar-p" x="' + x.toFixed(2) + '" y="' + yP.toFixed(2)
        + '" width="' + barW.toFixed(2) + '" height="' + Math.max(hP, 0).toFixed(2) + '"/>';
    }).join('');

    // Outstanding balance over the same axis, plan against baseline.
    var maxBal = plan.rows[0].bal + plan.rows[0].p;
    function line(rows, cls) {
      if (!rows.length) return '';
      var pts = rows.map(function (r, idx) {
        var x = padL + (idx + 0.5) * (plotW / n);
        var y = padT + plotH - (r.bal / maxBal) * plotH;
        return x.toFixed(1) + ',' + y.toFixed(1);
      }).join(' ');
      return '<polyline class="' + cls + '" points="' + pts + '"/>';
    }

    // A tick each anniversary, so the prepayment steps can be placed in time.
    var ticks = '';
    for (var y = 12; y < n; y += 12) {
      var tx = (padL + y * slot).toFixed(1);
      ticks += '<line class="year-tick" x1="' + tx + '" y1="' + padT
        + '" x2="' + tx + '" y2="' + (padT + plotH) + '"/>';
    }

    var baseLine = '';
    if (baseline && baseline.rows.length) {
      // Baseline runs longer, so sample it onto the plan's x-axis.
      var pts = [];
      for (var idx = 0; idx < baseline.rows.length; idx++) {
        var x = padL + (idx + 0.5) * (plotW / n);
        if (x > W - padR) break;
        var y = padT + plotH - (baseline.rows[idx].bal / maxBal) * plotH;
        pts.push(x.toFixed(1) + ',' + y.toFixed(1));
      }
      baseLine = '<polyline class="bal-baseline" points="' + pts.join(' ') + '"/>';
    }

    return '<figure class="chart goal-chart">'
      + '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" preserveAspectRatio="none"'
      + ' aria-label="Principal and interest per month, with the outstanding balance">'
      + ticks + bars + baseLine + line(plan.rows, 'bal-plan') + '</svg>'
      + '<figcaption><span><i class="key key-p"></i>Towards loan</span>'
      + '<span><i class="key key-i"></i>Towards interest</span>'
      + '<span><i class="key key-bal"></i>Balance, your plan</span>'
      + (baseLine ? '<span><i class="key key-base"></i>Balance, EMI only</span>' : '')
      + '<span>' + years(plan.months) + '</span></figcaption></figure>';
  }

  // ------------------------------------------------------------- rendering
  function renderList() {
    if (!goals.length) {
      listEl.innerHTML = '<p class="empty">No goals yet. '
        + 'Add one to model paying off a loan, or buying something.</p>';
      return;
    }
    listEl.innerHTML = goals.map(function (g, idx) {
      var summary = '';
      if (g.kind === 'payoff') {
        var s = buildSchedule(g.principal, g.annual_rate, g.emi, {
          extraEmis: g.extra_emis_per_year, hikePct: g.annual_hike_pct,
          lumpSum: g.lump_sum });
        summary = amt(g.principal) + ' outstanding · clear in ' + years(s.months);
      } else {
        summary = amt(g.target_cost) + ' target';
      }
      return '<button type="button" class="goal-row" data-index="' + idx + '">'
        + '<span class="goal-kind pill ' + (g.kind === 'payoff' ? 'down' : 'none') + '">'
        + (g.kind === 'payoff' ? 'Pay off' : 'Buy') + '</span>'
        + '<span class="goal-name">' + esc(g.name) + '</span>'
        + '<span class="goal-summary">' + summary + '</span></button>';
    }).join('');
  }

  function field(id, label, value, step, suffix) {
    var shown = value === null || value === undefined ? '' : String(value);
    return '<label class="goal-field"><span>' + esc(label) + '</span>'
      + '<input type="number" id="' + id + '" value="' + esc(shown)
      + '" step="' + (step || 'any') + '">'
      + (suffix ? '<small>' + esc(suffix) + '</small>' : '') + '</label>';
  }

  function textField(id, label, value, maxLength) {
    var shown = value === null || value === undefined ? '' : String(value);
    return '<label class="goal-field"><span>' + esc(label) + '</span>'
      + '<input type="text" id="' + id + '" maxlength="' + maxLength
      + '" value="' + esc(shown) + '"></label>';
  }

  function slider(id, label, value, min, max, step) {
    return '<label class="goal-slider"><span>' + esc(label)
      + ' <output id="' + id + '-out">' + value + '</output></span>'
      + '<input type="range" id="' + id + '" value="' + value + '" min="' + min
      + '" max="' + max + '" step="' + step + '"></label>';
  }

  function renderPayoffForm(g) {
    formEl.innerHTML =
      '<div class="goal-grid">'
      + textField('g-name', 'Goal name', g.name, 80)
      + textField('g-lender', 'Lender (optional)', g.lender || '', 60)
      + field('g-principal', 'Outstanding balance', g.principal, '1000')
      + field('g-rate', 'Rate of interest', (g.annual_rate * 100).toFixed(2), '0.05', '% per year')
      + field('g-emi', 'EMI', g.emi, '100')
      + field('g-tenure', 'Original tenure', g.tenure_years, '0.5', 'years, for reference')
      + '</div>'
      + '<div class="goal-grid">'
      + slider('g-extra', 'Extra EMIs paid once a year', g.extra_emis_per_year || 0, 0, 12, 0.5)
      + slider('g-hike', 'Hike the EMI each year', g.annual_hike_pct || 0, 0, 25, 0.5)
      + field('g-lump', 'One-off payment now', g.lump_sum || 0, '10000')
      + '</div>'
      + '<p class="hint" id="g-emi-hint"></p>';
  }

  function renderPurchaseForm(g) {
    var tiers = STATE.tiers || {};
    var chosen = g.use_tiers || ['ready', 'sellable'];
    var boxes = ['ready', 'sellable', 'locked'].map(function (key) {
      var t = tiers[key] || {};
      return '<label class="goal-check"><input type="checkbox" class="tier-box" '
        + 'value="' + key + '"' + (chosen.indexOf(key) >= 0 ? ' checked' : '') + '>'
        + '<span>' + esc(t.label || key) + ' ' + amt(t.total || 0) + '</span></label>';
    }).join('');

    formEl.innerHTML =
      '<div class="goal-grid">'
      + textField('g-name', 'Goal name', g.name, 80)
      + field('g-target', 'What it costs', g.target_cost, '10000')
      + field('g-cash', 'Cash outside INDmoney', g.existing_cash || 0, '10000')
      + field('g-cap', 'Cap the downpayment at (optional)',
              g.downpayment_cap === null || g.downpayment_cap === undefined
                ? '' : g.downpayment_cap, '10000')
      + field('g-loanrate', 'Loan rate', ((g.loan_rate || 0.09) * 100).toFixed(2),
              '0.05', '% per year')
      + field('g-loanyears', 'Loan tenure', g.loan_years || 7, '0.5', 'years')
      + '</div>'
      + '<fieldset class="goal-tiers"><legend>Fund the downpayment from</legend>'
      + boxes + '</fieldset>';
  }

  function recalc() {
    var g = goals[editing];
    if (!g) return;

    if (g.kind === 'payoff') {
      var principal = num('g-principal');
      var rate = num('g-rate') / 100;
      var emi = num('g-emi');
      var extra = num('g-extra');
      var hike = num('g-hike');
      var lump = num('g-lump');

      ['g-extra', 'g-hike'].forEach(function (id) {
        var out = document.getElementById(id + '-out');
        if (out) out.textContent = num(id);
      });

      var hint = document.getElementById('g-emi-hint');
      var suggested = monthlyEmi(principal, rate, num('g-tenure'));
      if (hint && suggested) {
        hint.innerHTML = 'For this balance, rate and tenure the computed EMI would be '
          + amt(suggested) + '. Your actual EMI is whatever the bank charges — '
          + 'that is why it is an input, not derived.';
      }

      var plan = buildSchedule(principal, rate, emi,
        { extraEmis: extra, hikePct: hike, lumpSum: lump });
      var baseline = buildSchedule(principal, rate, emi);

      var saved = baseline.totalInterest - plan.totalInterest;
      var monthsSaved = baseline.months - plan.months;

      var warn = plan.warnings.concat(baseline.warnings).map(function (w) {
        return '<p class="warn">' + esc(w) + '</p>';
      }).join('');

      resultEl.innerHTML = warn
        + '<div class="stats">'
        + stat('Cleared in', years(plan.months))
        + stat('Interest you pay', amt(plan.totalInterest))
        + stat('Interest saved', amt(saved), saved > 0 ? 'up' : '')
        + stat('Time saved', monthsSaved > 0 ? years(monthsSaved) : '—',
               monthsSaved > 0 ? 'up' : '')
        + stat('Total prepaid', amt(plan.totalPrepaid))
        + stat('Total outlay', amt(plan.totalInterest + plan.totalPrincipal))
        + '</div>'
        + drawChart(plan, baseline)
        + yearTable(plan);

    } else {
      var target = num('g-target');
      var cash = num('g-cash');
      var capEl = document.getElementById('g-cap');
      var cap = capEl && capEl.value !== '' ? num('g-cap') : null;
      var loanRate = num('g-loanrate') / 100;
      var loanYears = num('g-loanyears');

      var chosen = [];
      Array.prototype.forEach.call(document.querySelectorAll('.tier-box'),
        function (box) { if (box.checked) chosen.push(box.value); });

      var tiers = STATE.tiers || {};
      var available = cash;
      chosen.forEach(function (key) { available += (tiers[key] || {}).total || 0; });

      var downpayment = Math.min(available, target);
      if (cap !== null) downpayment = Math.min(downpayment, cap);
      var loanNeeded = Math.max(target - downpayment, 0);
      var shortfall = Math.max(target - available, 0);

      var emi2 = loanNeeded > 0 ? monthlyEmi(loanNeeded, loanRate, loanYears) : null;
      var sched = emi2 ? buildSchedule(loanNeeded, loanRate, emi2) : null;

      var notes = [];
      if (chosen.indexOf('locked') >= 0 && (tiers.locked || {}).total > 0) {
        notes.push('Locked holdings (PPF, EPF, NPS) are included. They have lock-ins '
          + 'and withdrawal rules, so treat that as theoretical rather than money '
          + 'you can reach this month.');
      }
      if (chosen.indexOf('sellable') >= 0 && (tiers.sellable || {}).total > 0) {
        notes.push('Selling investments can trigger capital gains tax and exit loads, '
          + 'and realises whatever price the market offers that day. These figures '
          + 'are gross and ignore both.');
      }
      if (shortfall > 0) {
        notes.push('Even using everything ticked, you are ' + rupees(shortfall)
          + ' short of the full cost — the rest has to be borrowed or saved.');
      }

      resultEl.innerHTML = notes.map(function (n) {
        return '<p class="warn">' + esc(n) + '</p>'; }).join('')
        + '<div class="stats">'
        + stat('Target', amt(target))
        + stat('Available', amt(available))
        + stat('Downpayment', amt(downpayment))
        + stat('Loan needed', amt(loanNeeded), loanNeeded > 0 ? 'down' : 'up')
        + stat('EMI', emi2 ? amt(emi2) : '—')
        + stat('Interest over ' + loanYears + 'y',
               sched ? amt(sched.totalInterest) : '—')
        + '</div>'
        + (sched ? drawChart(sched, null) : '');
    }
  }

  function stat(label, value, tone) {
    return '<div class="stat"><span class="stat-label">' + esc(label) + '</span>'
      + '<span class="stat-value ' + (tone || '') + '">' + value + '</span></div>';
  }

  function yearTable(plan) {
    if (!plan.rows.length) return '';
    var rows = '';
    for (var start = 0; start < plan.rows.length; start += 12) {
      var chunk = plan.rows.slice(start, start + 12);
      var p = 0, i = 0, pre = 0;
      chunk.forEach(function (r) { p += r.p; i += r.i; pre += r.pre; });
      rows += '<tr><td>' + (start / 12 + 1) + '</td><td>' + amt(p) + '</td><td>'
        + amt(i) + '</td><td>' + (pre ? amt(pre) : '—') + '</td><td>'
        + amt(chunk[chunk.length - 1].bal) + '</td></tr>';
    }
    return '<div class="table-wrap"><table class="holdings"><thead><tr>'
      + '<th>Year</th><th>Towards loan</th><th>Towards interest</th>'
      + '<th>Prepaid</th><th>Closing balance</th></tr></thead><tbody>'
      + rows + '</tbody></table></div>';
  }

  // --------------------------------------------------------------- editing
  function readForm() {
    var g = goals[editing];
    if (!g) return;
    g.name = (document.getElementById('g-name') || {}).value || g.name;
    titleEl.textContent = g.name;
    if (g.kind === 'payoff') {
      g.lender = (document.getElementById('g-lender') || {}).value || '';
      g.principal = num('g-principal');
      g.annual_rate = num('g-rate') / 100;
      g.emi = num('g-emi');
      g.tenure_years = num('g-tenure');
      g.extra_emis_per_year = num('g-extra');
      g.annual_hike_pct = num('g-hike');
      g.lump_sum = num('g-lump');
    } else {
      g.target_cost = num('g-target');
      g.existing_cash = num('g-cash');
      var capEl = document.getElementById('g-cap');
      g.downpayment_cap = capEl && capEl.value !== '' ? num('g-cap') : null;
      g.loan_rate = num('g-loanrate') / 100;
      g.loan_years = num('g-loanyears');
      g.use_tiers = [];
      Array.prototype.forEach.call(document.querySelectorAll('.tier-box'),
        function (box) { if (box.checked) g.use_tiers.push(box.value); });
    }
  }

  function openGoal(index) {
    editing = index;
    var g = goals[index];
    editorEl.hidden = false;
    titleEl.textContent = g.name || 'Goal';
    if (g.kind === 'payoff') renderPayoffForm(g); else renderPurchaseForm(g);
    recalc();
    editorEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function markDirty() {
    dirty = true;
    if (noteEl) { noteEl.hidden = false; noteEl.textContent = 'Unsaved changes.'; }
  }

  function save() {
    readForm();
    var button = document.getElementById('goal-save');
    if (button) { button.disabled = true; button.textContent = 'Saving…'; }

    fetch('/api/goals', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ goals: goals })
    }).then(function (res) {
      return res.json().then(function (data) { return { ok: res.ok, data: data }; });
    }).then(function (out) {
      if (!out.ok) {
        var detail = (out.data.details || []).join('; ') || out.data.error || 'unknown error';
        noteEl.hidden = false;
        noteEl.textContent = 'Not saved: ' + detail;
        return;
      }
      goals = out.data.goals || goals;
      dirty = false;
      noteEl.hidden = false;
      noteEl.textContent = 'Saved ' + new Date().toLocaleTimeString() + '.';
      renderList();
    }).catch(function (err) {
      noteEl.hidden = false;
      noteEl.textContent = 'Not saved: ' + err;
    }).then(function () {
      if (button) { button.disabled = false; button.textContent = 'Save'; }
    });
  }

  // ----------------------------------------------------------------- wiring
  listEl.addEventListener('click', function (event) {
    var row = event.target.closest ? event.target.closest('.goal-row') : null;
    if (row) openGoal(parseInt(row.getAttribute('data-index'), 10));
  });

  function onEdit() { readForm(); recalc(); markDirty(); }
  formEl.addEventListener('input', onEdit);
  formEl.addEventListener('change', onEdit);

  document.getElementById('goal-new').addEventListener('click', function () {
    var kind = window.prompt('Goal type — type "payoff" to clear a loan, '
      + 'or "buy" for something you want to purchase:', 'payoff');
    if (!kind) return;
    kind = kind.trim().toLowerCase() === 'buy' ? 'purchase' : 'payoff';
    var g = kind === 'payoff'
      ? { kind: 'payoff', name: 'New loan payoff', principal: 1000000,
          annual_rate: 0.085, emi: 15000, tenure_years: 10,
          extra_emis_per_year: 0, annual_hike_pct: 0, lump_sum: 0, lender: '' }
      : { kind: 'purchase', name: 'New purchase', target_cost: 1000000,
          loan_rate: 0.09, loan_years: 7, existing_cash: 0,
          downpayment_cap: null, use_tiers: ['ready', 'sellable'] };
    goals.push(g);
    renderList();
    openGoal(goals.length - 1);
  });

  document.getElementById('goal-save').addEventListener('click', save);
  document.getElementById('goal-close').addEventListener('click', function () {
    editorEl.hidden = true; editing = null;
  });
  document.getElementById('goal-delete').addEventListener('click', function () {
    if (editing === null) return;
    if (!window.confirm('Delete "' + goals[editing].name + '"?')) return;
    goals.splice(editing, 1);
    editing = null;
    editorEl.hidden = true;
    renderList();
    save();
  });

  window.addEventListener('beforeunload', function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });

  renderList();
})();
