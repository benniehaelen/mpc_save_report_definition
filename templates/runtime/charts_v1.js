/* charts_v1 -- the generic report runtime.
 *
 * It reads the JSON data islands the template rendered, draws every [data-chart]
 * declaration, fills every bound table's <tbody>, and wires the tab bar.
 *
 * It DERIVES NOTHING. The reference artifact this was ported from computed
 * `gap = uhs - hca`, `pct = c25 / total * 100`, `share = h / m * 100`, the
 * year-over-year deltas, and the sort order right here in the browser. All of
 * that now lives in SQL, so a replayed report and its parity check agree by
 * construction. What remains below is axis scaling and number formatting --
 * the only arithmetic a renderer is allowed to do.
 */
(function () {
  "use strict";

  // --- formatting: mirrors the Jinja filters in runner/render.py -----------

  function toNumber(value) {
    if (typeof value === "number") return value;
    var text = String(value).trim().replace(/,/g, "").replace(/^\+/, "");
    text = text.replace(/(pp|%)$/, "");
    return parseFloat(text);
  }

  function alreadySigned(value) {
    return typeof value === "string" && /^[+-]/.test(value.trim());
  }

  var FILTERS = {
    thousands: function (value) {
      var n = toNumber(value);
      var text = n.toLocaleString("en-US");
      return n > 0 && alreadySigned(value) ? "+" + text : text;
    },
    signed: function (value) {
      var n = toNumber(value);
      var text = typeof value === "string" ? value.trim() : String(n);
      return n > 0 && !alreadySigned(text) ? "+" + text : text;
    },
    pct: function (value, decimals) {
      var n = toNumber(value);
      var text = n.toFixed(decimals === undefined ? 1 : decimals) + "%";
      return n > 0 && alreadySigned(value) ? "+" + text : text;
    },
    pp: function (value) {
      if (typeof value === "string") return value.trim() + "pp";
      return toNumber(value) + "pp";
    },
    round: function (value, decimals) {
      return toNumber(value).toFixed(decimals === undefined ? 0 : decimals);
    }
  };

  function applyFilters(value, filters) {
    return filters.reduce(function (acc, f) {
      var fn = FILTERS[f.name];
      return fn ? fn(acc, f.arg) : acc;
    }, value);
  }

  function signClass(value) {
    var n = toNumber(value);
    if (isNaN(n) || n === 0) return "flat";
    return n > 0 ? "growth" : "decline";
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // --- islands ------------------------------------------------------------

  var ISLANDS = {};
  function loadIslands() {
    var nodes = document.querySelectorAll('script[type="application/json"][data-result]');
    Array.prototype.forEach.call(nodes, function (node) {
      try {
        ISLANDS[node.getAttribute("data-result")] = JSON.parse(node.textContent);
      } catch (err) {
        console.error("charts_v1: island " + node.getAttribute("data-result") + " is not JSON", err);
      }
    });
  }

  function rowsFor(spec) {
    var rows = ISLANDS[spec.result];
    if (!rows) {
      console.error("charts_v1: no island named " + spec.result);
      return [];
    }
    if (!spec.filter) return rows;
    return rows.filter(function (row) {
      return Object.keys(spec.filter).every(function (key) {
        return String(row[key]) === String(spec.filter[key]);
      });
    });
  }

  // Every chart takes its display string from a precomputed column when one is
  // declared, and otherwise formats the raw value. Neither path computes it.
  function displayFor(spec, row, value) {
    if (spec.display_field) return String(row[spec.display_field]);
    if (spec.display && spec.display.field) {
      return applyFilters(row[spec.display.field], parseFilterList(spec.display.filters));
    }
    return FILTERS.thousands(value);
  }

  function parseFilterList(list) {
    return (list || []).map(function (entry) {
      return { name: entry[0], arg: (entry[1] || [])[0] };
    });
  }

  // --- SVG chart helpers (ported) -----------------------------------------

  function lineChart(el, series, labels, options) {
    var w = options.width || 520, h = options.height || 260;
    var margin = { top: 30, right: 90, bottom: 40, left: 55 };
    var plotW = w - margin.left - margin.right;
    var plotH = h - margin.top - margin.bottom;
    var all = series.reduce(function (a, s) { return a.concat(s.data); }, []);
    if (!all.length) { el.innerHTML = '<p class="flat">No data.</p>'; return; }
    var minV = Math.min.apply(null, all);
    var maxV = Math.max.apply(null, all);
    var pad = (maxV - minV) * 0.1 || 1;
    minV = Math.floor(minV - pad);
    maxV = Math.ceil(maxV + pad);
    var range = maxV - minV || 1;
    var suffix = options.suffix || "";

    var svg = '<svg viewBox="0 0 ' + w + " " + h + '" class="chart-svg" role="img">';
    for (var g = 0; g <= 4; g++) {
      var gy = margin.top + plotH - (g / 4) * plotH;
      var gval = (minV + (g / 4) * range).toFixed(0);
      svg += '<line x1="' + margin.left + '" y1="' + gy + '" x2="' + (margin.left + plotW) +
             '" y2="' + gy + '" class="grid-line"/>';
      svg += '<text x="' + (margin.left - 8) + '" y="' + (gy + 4) +
             '" text-anchor="end" style="font-size:10px;fill:#94a3b8;">' + gval + suffix + "</text>";
    }
    var step = labels.length > 1 ? plotW / (labels.length - 1) : 0;
    labels.forEach(function (label, i) {
      if (labels.length > 8 && i % 2) return;
      svg += '<text x="' + (margin.left + i * step) + '" y="' + (h - margin.bottom + 16) +
             '" text-anchor="middle" style="font-size:9px;fill:#94a3b8;">' + escapeHtml(label) + "</text>";
    });
    series.forEach(function (s) {
      var points = s.data.map(function (v, i) {
        var x = margin.left + i * step;
        var y = margin.top + plotH - ((v - minV) / range) * plotH;
        return x + "," + y;
      }).join(" ");
      svg += '<polyline points="' + points + '" fill="none" stroke="' + (s.color || "#E75925") +
             '" stroke-width="2.5" stroke-linejoin="round"/>';
      s.data.forEach(function (v, i) {
        var x = margin.left + i * step;
        var y = margin.top + plotH - ((v - minV) / range) * plotH;
        svg += '<circle cx="' + x + '" cy="' + y + '" r="2.5" fill="' + (s.color || "#E75925") + '"/>';
      });
      if (s.label) {
        var lastY = margin.top + plotH - ((s.data[s.data.length - 1] - minV) / range) * plotH;
        svg += '<text x="' + (margin.left + plotW + 8) + '" y="' + (lastY + 4) +
               '" style="font-size:11px;font-weight:600;fill:' + (s.color || "#E75925") + ';">' +
               escapeHtml(s.label) + "</text>";
      }
    });
    el.innerHTML = svg + "</svg>";
  }

  function barChart(el, data, options) {
    var w = options.width || 500, h = options.height || 280;
    var margin = { top: 20, right: 90, bottom: 20, left: 170 };
    var plotW = w - margin.left - margin.right;
    var plotH = h - margin.top - margin.bottom;
    if (!data.length) { el.innerHTML = '<p class="flat">No data.</p>'; return; }
    var maxVal = Math.max.apply(null, data.map(function (d) { return d.value; }));
    var barH = Math.min(28, (plotH - data.length * 4) / data.length);
    var svg = '<svg viewBox="0 0 ' + w + " " + h + '" class="chart-svg" role="img">';
    data.forEach(function (d, i) {
      var y = margin.top + i * (barH + 5);
      var barW = maxVal ? (d.value / maxVal) * plotW : 0;
      svg += '<text x="' + (margin.left - 8) + '" y="' + (y + barH / 2 + 4) +
             '" text-anchor="end" class="bar-label">' + escapeHtml(d.label) + "</text>";
      svg += '<rect x="' + margin.left + '" y="' + y + '" width="' + barW + '" height="' + barH +
             '" fill="' + d.color + '" rx="3"/>';
      svg += '<text x="' + (margin.left + barW + 6) + '" y="' + (y + barH / 2 + 4) +
             '" class="bar-value">' + escapeHtml(d.display) + "</text>";
    });
    el.innerHTML = svg + "</svg>";
  }

  function divergingBarChart(el, data, options) {
    var w = options.width || 520, h = options.height || 440;
    var margin = { top: 15, right: 70, bottom: 20, left: 170 };
    var plotW = w - margin.left - margin.right;
    var plotH = h - margin.top - margin.bottom;
    if (!data.length) { el.innerHTML = '<p class="flat">No data.</p>'; return; }
    var maxAbs = Math.max.apply(null, data.map(function (d) { return Math.abs(d.value); })) || 1;
    var barH = Math.min(20, (plotH - data.length * 3) / data.length);
    var zeroX = margin.left + plotW / 2;
    var svg = '<svg viewBox="0 0 ' + w + " " + h + '" class="chart-svg" role="img">';
    svg += '<line x1="' + zeroX + '" y1="' + margin.top + '" x2="' + zeroX + '" y2="' +
           (h - margin.bottom) + '" stroke="#e8edf3" stroke-width="1"/>';
    data.forEach(function (d, i) {
      var y = margin.top + i * (barH + 4);
      var barW = (Math.abs(d.value) / maxAbs) * (plotW / 2);
      var x = d.value >= 0 ? zeroX : zeroX - barW;
      svg += '<text x="' + (margin.left - 6) + '" y="' + (y + barH / 2 + 4) +
             '" text-anchor="end" class="bar-label">' + escapeHtml(d.label) + "</text>";
      svg += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="' + barH +
             '" fill="' + d.color + '" rx="2"/>';
      var tx = d.value >= 0 ? x + barW + 4 : x - 4;
      svg += '<text x="' + tx + '" y="' + (y + barH / 2 + 4) + '" text-anchor="' +
             (d.value >= 0 ? "start" : "end") + '" class="bar-value">' + escapeHtml(d.display) + "</text>";
    });
    el.innerHTML = svg + "</svg>";
  }

  // --- chart dispatch -----------------------------------------------------

  function drawChart(el) {
    var spec;
    try {
      spec = JSON.parse(el.getAttribute("data-chart"));
    } catch (err) {
      console.error("charts_v1: invalid chart declaration", err);
      return;
    }
    var rows = rowsFor(spec);
    var options = { width: spec.width, height: spec.height, suffix: spec.suffix };

    if (spec.type === "line") {
      var series = spec.series.map(function (s) {
        return {
          label: s.label,
          color: s.color,
          data: rows.map(function (row) { return Number(row[s.field]); })
        };
      });
      lineChart(el, series, rows.map(function (row) { return row[spec.x]; }), options);
      return;
    }

    var data = rows.map(function (row) {
      var value = Number(row[spec.value_field]);
      return {
        label: String(row[spec.label_field]),
        value: value,
        display: displayFor(spec, row, value),
        color: colorFor(spec, row, value)
      };
    });

    if (spec.type === "bar") barChart(el, data, options);
    else if (spec.type === "diverging_bar") divergingBarChart(el, data, options);
    else console.error("charts_v1: unknown chart type " + spec.type);
  }

  function colorFor(spec, row, value) {
    if (spec.type === "diverging_bar") {
      return value >= 0 ? spec.pos_color || "#E75925" : spec.neg_color || "#092240";
    }
    if (spec.highlight && String(row[spec.highlight.field]) === String(spec.highlight.value)) {
      return spec.highlight.color || "#E75925";
    }
    return spec.color || "#092240";
  }

  // --- bound tables -------------------------------------------------------
  //
  // `field:Header|thousands|style:sign` -- the same grammar server/artifact.py
  // parses, so the compiler and the runtime agree on what a column means.

  function parseColumns(spec) {
    return spec.split(",").map(function (chunk) {
      var parts = chunk.trim().split("|");
      var head = parts[0].split(":");
      var column = { field: head[0].trim(), filters: [], style: null };
      parts.slice(1).forEach(function (token) {
        token = token.trim();
        if (token.indexOf("style:") === 0) {
          column.style = token.slice(6);
          return;
        }
        var m = token.match(/^([a-z_]+)(?:\((-?\d+)\))?$/);
        if (m) column.filters.push({ name: m[1], arg: m[2] === undefined ? undefined : parseInt(m[2], 10) });
      });
      return column;
    });
  }

  function fillTable(table) {
    var name = table.getAttribute("data-result");
    var rows = ISLANDS[name];
    if (!rows) {
      console.error("charts_v1: bound table references missing island " + name);
      return;
    }
    var columns = parseColumns(table.getAttribute("data-columns"));
    var body = table.querySelector("tbody") || table.appendChild(document.createElement("tbody"));
    body.innerHTML = rows.map(function (row) {
      var cells = columns.map(function (column) {
        var raw = row[column.field];
        var text = escapeHtml(applyFilters(raw, column.filters));
        if (column.style === "sign") {
          return '<td><span class="' + signClass(raw) + '">' + text + "</span></td>";
        }
        return "<td>" + text + "</td>";
      });
      return "<tr>" + cells.join("") + "</tr>";
    }).join("");
  }

  // --- tabs ---------------------------------------------------------------

  function wireTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));
    var panels = document.querySelectorAll('[role="tabpanel"]');
    if (!tabs.length) return;

    function activate(tab) {
      tabs.forEach(function (t) {
        t.setAttribute("aria-selected", "false");
        t.setAttribute("tabindex", "-1");
      });
      Array.prototype.forEach.call(panels, function (p) { p.classList.remove("active"); });
      tab.setAttribute("aria-selected", "true");
      tab.setAttribute("tabindex", "0");
      tab.focus();
      var panel = document.getElementById(tab.getAttribute("aria-controls"));
      if (panel) panel.classList.add("active");
    }

    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () { activate(tab); });
      tab.addEventListener("keydown", function (event) {
        var i = tabs.indexOf(tab);
        if (event.key === "ArrowRight") { activate(tabs[(i + 1) % tabs.length]); event.preventDefault(); }
        if (event.key === "ArrowLeft") { activate(tabs[(i - 1 + tabs.length) % tabs.length]); event.preventDefault(); }
      });
    });
  }

  function start() {
    loadIslands();
    Array.prototype.forEach.call(document.querySelectorAll("[data-chart]"), drawChart);
    Array.prototype.forEach.call(
      document.querySelectorAll("table[data-result][data-columns]"), fillTable
    );
    wireTabs();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
