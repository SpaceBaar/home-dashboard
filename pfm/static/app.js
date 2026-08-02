/* Client-side table sorting for the holdings table.
   Progressive enhancement only: the table is fully readable and correctly
   ordered without JavaScript. Numeric cells carry a data-sort attribute with
   the raw value, so sorting never has to parse formatted currency strings. */

(function () {
  'use strict';

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
