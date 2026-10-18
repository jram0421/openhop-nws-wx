(function () {
  "use strict";
  var DATA = window.NWS_DATA;
  var match = location.pathname.match(/\/plugins\/([^/]+)\//);
  var PLUGIN_ID = match ? decodeURIComponent(match[1]) : "nws-wx";
  var SETTINGS_URL = "/api/plugins/settings";
  var EMAIL_RE = /^[^@\s,;()<>]+@[^@\s,;()<>]+\.[^@\s,;()<>]+$/;
  var ZONE_RE = /^[A-Z]{2}[CZ]\d{3}$/;

  var DEFAULTS = {
    contact_email: "", frame_host: "127.0.0.1", frame_port: 5000,
    wx_channel: "WX", alerts_channel: "WX-Alerts", bot_enabled: true, alerts_enabled: true,
    states: [], counties: [], zones: [], min_severity: "Moderate", events: [],
    poll_seconds: 120, alert_cooldown_seconds: 60, alert_hourly_cap: 10
  };

  var $ = function (id) { return document.getElementById(id); };
  var loaded = {};
  var sel = { states: new Set(), counties: new Set(), events: new Set() };
  var countyState = {};
  Object.keys(DATA.counties).forEach(function (st) {
    DATA.counties[st].forEach(function (c) { countyState[c[0]] = st; });
  });

  function checklist(box, items, set, filterText, onChange) {
    var q = filterText.trim().toLowerCase();
    box.textContent = "";
    var shown = [];
    items.forEach(function (it) {
      if (q && it.label.toLowerCase().indexOf(q) === -1) return;
      shown.push(it.value);
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = set.has(it.value);
      cb.addEventListener("change", function () {
        if (cb.checked) set.add(it.value); else set.delete(it.value);
        onChange();
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(it.label));
      box.appendChild(label);
    });
    if (!shown.length) {
      var empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "Nothing to show.";
      box.appendChild(empty);
    }
    return shown;
  }

  function stateItems() {
    return Object.keys(DATA.states).sort(function (a, b) {
      return DATA.states[a].localeCompare(DATA.states[b]);
    }).map(function (st) { return { value: st, label: DATA.states[st] + " (" + st + ")" }; });
  }

  function countyItems() {
    var out = [];
    Array.from(sel.states).sort().forEach(function (st) {
      (DATA.counties[st] || []).forEach(function (c) {
        out.push({ value: c[0], label: c[1] + ", " + st });
      });
    });
    return out;
  }

  var shownCounties = [];
  function renderStates() {
    checklist($("states"), stateItems(), sel.states, $("state_filter").value, function () {
      sel.counties.forEach(function (code) {
        if (!sel.states.has(countyState[code])) sel.counties.delete(code);
      });
      renderCounties();
    });
  }
  function renderCounties() {
    shownCounties = checklist($("counties"), countyItems(), sel.counties, $("county_filter").value, updateCounts);
    if (!sel.states.size) $("counties").firstChild.textContent = "Select a state first.";
    updateCounts();
  }
  function renderEvents() {
    var items = DATA.events.map(function (e) { return { value: e, label: e }; });
    checklist($("events"), items, sel.events, $("event_filter").value, updateCounts);
    updateCounts();
  }
  function updateCounts() {
    $("county_count").textContent = sel.counties.size ? "(" + sel.counties.size + " selected)" : "(whole state)";
    $("event_count").textContent = sel.events.size ? "(" + sel.events.size + " selected)" : "(all events)";
  }

  function fill(cfg) {
    var c = Object.assign({}, DEFAULTS, cfg);
    loaded = Object.assign({}, cfg);
    ["contact_email", "frame_host", "frame_port", "wx_channel", "alerts_channel", "min_severity",
     "poll_seconds", "alert_cooldown_seconds", "alert_hourly_cap"].forEach(function (k) { $(k).value = c[k]; });
    $("bot_enabled").checked = c.bot_enabled !== false;
    $("alerts_enabled").checked = c.alerts_enabled !== false;
    $("zones").value = (c.zones || []).join("\n");
    sel.states = new Set(c.states || []);
    sel.counties = new Set(c.counties || []);
    sel.events = new Set(c.events || []);
    renderStates(); renderCounties(); renderEvents();
  }

  function setError(id, msg) { $(id).textContent = msg || ""; return !msg; }

  function collect() {
    var ok = true;
    var email = $("contact_email").value.trim();
    ok = setError("err_contact_email", EMAIL_RE.test(email) ? "" : "Enter a valid email address.") && ok;
    var zones = $("zones").value.split(/[\s,]+/).map(function (z) { return z.trim().toUpperCase(); }).filter(Boolean);
    var badZone = zones.filter(function (z) { return !ZONE_RE.test(z); });
    ok = setError("err_zones", badZone.length ? "Invalid zone code: " + badZone[0] : "") && ok;
    ok = setError("err_states", $("alerts_enabled").checked && !sel.states.size ? "Pick at least one state for alerts." : "") && ok;
    function num(id, lo, hi) {
      var v = Number($(id).value);
      return Number.isInteger(v) && v >= lo && v <= hi ? v : null;
    }
    var nums = {
      frame_port: num("frame_port", 1, 65535), poll_seconds: num("poll_seconds", 60, 3600),
      alert_cooldown_seconds: num("alert_cooldown_seconds", 0, 3600), alert_hourly_cap: num("alert_hourly_cap", 1, 60)
    };
    var badNum = Object.keys(nums).filter(function (k) { return nums[k] === null; });
    ok = setError("err_numbers", badNum.length ? "Check the number fields: " + badNum.join(", ") : "") && ok;
    ["wx_channel", "alerts_channel", "frame_host"].forEach(function (k) {
      if (!$(k).value.trim()) { ok = false; setStatus(k + " cannot be empty.", false); }
    });
    if (!ok) return null;
    // Settings replace the whole object, so keep keys this page doesn't know about.
    return Object.assign({}, loaded, nums, {
      contact_email: email,
      frame_host: $("frame_host").value.trim(),
      wx_channel: $("wx_channel").value.trim(),
      alerts_channel: $("alerts_channel").value.trim(),
      bot_enabled: $("bot_enabled").checked,
      alerts_enabled: $("alerts_enabled").checked,
      states: Array.from(sel.states).sort(),
      counties: Array.from(sel.counties).sort(),
      zones: Array.from(new Set(zones)).sort(),
      min_severity: $("min_severity").value,
      events: Array.from(sel.events).sort()
    });
  }

  function setStatus(msg, good) {
    var el = $("status");
    el.textContent = msg;
    el.className = good === true ? "ok" : good === false ? "bad" : "";
  }

  function showManual(cfg) {
    $("json").textContent = JSON.stringify(cfg, null, 2);
    $("fallback").hidden = false;
  }

  function authHint(status) {
    return status === 401 || status === 403
      ? "The dashboard rejected this page's request (" + status + "): plugin pages are not given the dashboard login. Use the manual JSON below."
      : "Save failed (" + status + ").";
  }

  function load() {
    setStatus("Loading…");
    return fetch(SETTINGS_URL + "?id=" + encodeURIComponent(PLUGIN_ID), { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw { status: r.status };
        return r.json();
      })
      .then(function (body) {
        fill(body.saved ? body.config : Object.assign({}, body.defaults, body.config));
        setStatus("");
      })
      .catch(function (e) {
        fill({});
        setStatus(e && e.status ? authHint(e.status).replace("Save failed", "Could not load settings") :
          "Could not reach the settings API; showing defaults.", false);
      });
  }

  function save() {
    var cfg = collect();
    if (!cfg) { if (!$("status").textContent) setStatus("Fix the highlighted fields.", false); return; }
    setStatus("Saving…");
    $("save").disabled = true;
    fetch(SETTINGS_URL, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: PLUGIN_ID, restart: true, config: cfg })
    }).then(function (r) {
      if (!r.ok) throw { status: r.status };
      loaded = cfg;
      $("fallback").hidden = true;
      setStatus("Saved. The plugin is restarting with the new settings.", true);
    }).catch(function (e) {
      showManual(cfg);
      setStatus(e && e.status ? authHint(e.status) : "Could not reach the settings API.", false);
    }).then(function () { $("save").disabled = false; });
  }

  $("state_filter").addEventListener("input", renderStates);
  $("county_filter").addEventListener("input", renderCounties);
  $("event_filter").addEventListener("input", renderEvents);
  $("county_all").addEventListener("click", function () {
    shownCounties.forEach(function (c) { sel.counties.add(c); }); renderCounties();
  });
  $("county_none").addEventListener("click", function () {
    shownCounties.forEach(function (c) { sel.counties.delete(c); }); renderCounties();
  });
  $("save").addEventListener("click", save);
  $("reload").addEventListener("click", load);
  $("copy").addEventListener("click", function () {
    var cfg = collect();
    if (!cfg) return;
    showManual(cfg);
    if (navigator.clipboard) {
      navigator.clipboard.writeText(JSON.stringify(cfg, null, 2)).then(
        function () { setStatus("Copied.", true); },
        function () { setStatus("Copy blocked by the browser; select the JSON below.", false); });
    }
  });

  load();
})();
