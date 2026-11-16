/* Pipeline DAG builder: ports, fork/join, judge 2–4 inputs, expand, user templates. */
(function () {
  "use strict";

  var COL_W = 200;
  var NODE_W = 156;
  var NODE_H = 96;
  var ROW_GAP = 28;
  var PAD_X = 36;
  var PAD_Y = 40;
  var TASK_ID = "__task__";

  var stages = [];
  var selectedIndex = -1;
  var roles = [];
  var allAgents = [];
  var pipelineId = "";
  var loops = {};
  var loopCounter = 0;
  var selectedLoopStageIdx = -1;
  var expanded = false;
  var wireDrag = null;
  var nodeDrag = null;

  var canvasContainer = document.getElementById("canvasContainer");
  var canvasNodes = document.getElementById("canvasNodes");
  var canvasWires = document.getElementById("canvasWires");
  var canvasSection = document.getElementById("canvasSection");
  var configPanel = document.getElementById("configPanel");
  var pipelineName = document.getElementById("pipelineName");
  var toastEl = document.getElementById("toast");

  function toast(msg, type) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.className = "toast " + (type || "") + " show";
    setTimeout(function () { toastEl.classList.remove("show"); }, 2500);
  }

  function stageType(agent) {
    var a = (agent || "").toLowerCase();
    if (a === "opencode" || a === "claude" || a === "codex" || a === "grok" || a === "hermes" || a === "cursor") {
      return "opencode";
    }
    return "reviewer";
  }
  function stageIcon(agent) { return stageType(agent) === "opencode" ? "⚙" : "🔍"; }

  function agentService(agent) {
    var a = (agent || "").toLowerCase();
    if (a.indexOf("zai") >= 0 || a.indexOf("z.ai") >= 0) return "zai";
    if (a.indexOf("deepseek") >= 0) return "deepseek";
    if (a.indexOf("qwen") >= 0) return "qwen";
    if (a.indexOf("google") >= 0 || a.indexOf("gemini") >= 0) return "google";
    if (stageType(agent) === "opencode") return "";
    return a.replace(/\s+/g, "");
  }

  function agentLabel(id) {
    var a = allAgents.find(function (x) { return x.id === id || x.name === id; });
    return a ? a.name : id;
  }

  function isJudge(s) {
    var r = ((s && s.role) || "") + " " + ((s && s.id) || "");
    return /judge|meta-review/i.test(r);
  }

  function inPortCount(s) {
    var wired = (s.inputs || []).length;
    var n = s.in_slots != null ? s.in_slots : wired;
    if (isJudge(s)) return Math.max(2, Math.min(4, n || 2));
    return Math.max(1, n || wired || 1);
  }

  function genId() { return "stg_" + (++loopCounter) + "_" + Math.random().toString(36).slice(2, 6); }

  function normalizeStage(s) {
    var agent = s.agent || s.service || (s.type === "reviewer" ? "zai" : "opencode");
    if (/z\.ai/i.test(agent)) agent = "zai";
    var type = s.type || stageType(agent);
    var svc = s.service || agentService(agent);
    return {
      id: s.id || genId(),
      type: type,
      role: s.role || "plan",
      agent: agent,
      service: svc || undefined,
      prompt: s.prompt || "",
      inputs: (s.inputs || []).slice(),
      outputs: (s.outputs || []).slice(),
      artifact_dir: s.artifact_dir,
      parallel_group: s.parallel_group || s.parallel || "",
      in_slots: s.in_slots,
      x: s.x,
      y: s.y
    };
  }

  function producersMap() {
    var producers = { task: TASK_ID };
    stages.forEach(function (s) {
      (s.outputs || []).forEach(function (o) { producers[o] = s.id; });
    });
    return producers;
  }

  function deriveParallel() {
    var producers = producersMap();
    var buckets = {};
    stages.forEach(function (s) {
      var parents = [];
      (s.inputs || []).forEach(function (inp) {
        if (inp === "task") return;
        var pid = producers[inp];
        if (pid && pid !== s.id && pid !== TASK_ID) parents.push(pid);
      });
      var key = parents.slice().sort().join("|");
      (buckets[key] = buckets[key] || []).push(s);
      s.parallel_group = "";
    });
    Object.keys(buckets).forEach(function (key) {
      var group = buckets[key];
      if (group.length < 2) return;
      var gid = key ? "fork-" + key.replace(/\|/g, "-") : "fork-start";
      if (gid.length > 48) gid = gid.slice(0, 48);
      group.forEach(function (s) { s.parallel_group = gid; });
    });
  }

  function layout(force) {
    var producers = producersMap();
    var byId = {};
    stages.forEach(function (s) { byId[s.id] = s; });
    var memo = {};
    function depthOf(s) {
      if (memo[s.id] != null) return memo[s.id];
      memo[s.id] = 1;
      var d = 1;
      (s.inputs || []).forEach(function (inp) {
        if (inp === "task") { d = Math.max(d, 1); return; }
        var pid = producers[inp];
        if (pid && byId[pid] && pid !== s.id) d = Math.max(d, depthOf(byId[pid]) + 1);
      });
      return (memo[s.id] = d);
    }
    var cols = {};
    stages.forEach(function (s) {
      var d = depthOf(s);
      (cols[d] = cols[d] || []).push(s);
    });
    Object.keys(cols).forEach(function (k) {
      cols[k].forEach(function (s, row) {
        if (force || s.x == null || s.y == null) {
          s.x = PAD_X + k * COL_W;
          s.y = PAD_Y + row * (NODE_H + ROW_GAP);
        }
      });
    });
  }

  function contentSize() {
    var maxX = 280, maxY = 420;
    stages.forEach(function (s) {
      maxX = Math.max(maxX, (s.x || 0) + NODE_W + 80);
      maxY = Math.max(maxY, (s.y || 0) + NODE_H + 80);
    });
    return { w: maxX, h: maxY };
  }

  function portY(count, i, h) {
    if (count <= 1) return h / 2;
    var pad = 18;
    return pad + i * ((h - pad * 2) / Math.max(1, count - 1));
  }

  function bezier(x1, y1, x2, y2) {
    var dx = Math.max(40, Math.abs(x2 - x1) * 0.45);
    return "M " + x1 + " " + y1 + " C " + (x1 + dx) + " " + y1 + ", " + (x2 - dx) + " " + y2 + ", " + x2 + " " + y2;
  }

  function nodeById(id) {
    for (var i = 0; i < stages.length; i++) if (stages[i].id === id) return stages[i];
    return null;
  }

  function listWires() {
    var producers = producersMap();
    var wires = [];
    stages.forEach(function (s) {
      (s.inputs || []).forEach(function (file, slot) {
        var srcId = producers[file];
        if (!srcId) return;
        wires.push({ fromId: srcId, fromFile: file, toId: s.id, toSlot: slot, file: file });
      });
    });
    return wires;
  }

  function defaultOutput(s) {
    if (s.outputs && s.outputs.length) return s.outputs[0];
    return (s.id || "stage") + ".md";
  }

  function connect(fromId, file, toId, toSlot) {
    if (fromId === toId) return;
    var target = nodeById(toId);
    if (!target) return;
    var n = inPortCount(target);
    target.inputs = target.inputs || [];
    while (target.inputs.length < n) target.inputs.push("");
    if (toSlot == null) {
      toSlot = target.inputs.findIndex(function (x) { return !x; });
      if (toSlot < 0) toSlot = Math.min(target.inputs.length, n - 1);
    }
    if (target.inputs.indexOf(file) !== -1 && target.inputs[toSlot] !== file) return;
    target.inputs[toSlot] = file;
    target.inputs = target.inputs.filter(function (x, i) {
      return x || i < inPortCount(target);
    });
    deriveParallel();
    render();
    if (selectedIndex >= 0) showConfig(selectedIndex);
  }

  function disconnect(toId, file) {
    var target = nodeById(toId);
    if (!target) return;
    target.inputs = (target.inputs || []).filter(function (x) { return x !== file; });
    deriveParallel();
    render();
    if (selectedIndex >= 0) showConfig(selectedIndex);
  }

  function render() {
    if (!canvasNodes || !canvasWires || !canvasContainer) return;
    deriveParallel();
    layout(false);
    var size = contentSize();
    canvasNodes.style.width = size.w + "px";
    canvasNodes.style.height = size.h + "px";
    canvasWires.setAttribute("width", size.w);
    canvasWires.setAttribute("height", size.h);
    canvasWires.style.width = size.w + "px";
    canvasWires.style.height = size.h + "px";
    canvasNodes.innerHTML = "";
    canvasWires.innerHTML = "";

    if (stages.length === 0) {
      var empty = document.createElement("div");
      empty.className = "canvas-empty";
      empty.textContent = "no stages — add a stage or load a preset";
      canvasNodes.appendChild(empty);
    }

    canvasNodes.appendChild(makeTaskNode());
    stages.forEach(function (s, i) {
      canvasNodes.appendChild(makeStageNode(s, i));
    });
    drawWires();
  }

  function makeTaskNode() {
    var el = document.createElement("div");
    el.className = "gnode";
    el.setAttribute("data-type", "task");
    el.setAttribute("data-id", TASK_ID);
    el.style.left = PAD_X + "px";
    el.style.top = PAD_Y + "px";
    el.innerHTML =
      '<div class="gnode__icon">◎</div>' +
      '<div class="gnode__role">task</div>' +
      '<div class="gnode__agent">prompt</div>' +
      '<div class="gport gport--out gport--task" data-sid="' + TASK_ID + '" data-file="task" data-tip="task"></div>';
    bindOutPort(el.querySelector(".gport--out"));
    return el;
  }

  function makeStageNode(s, i) {
    var el = document.createElement("div");
    var kind = isJudge(s) ? "judge" : stageType(s.agent);
    el.className = "gnode" + (i === selectedIndex ? " selected" : "") + (loops[s.id] ? " looped" : "");
    el.setAttribute("data-type", kind);
    el.setAttribute("data-id", s.id);
    el.style.left = (s.x || PAD_X) + "px";
    el.style.top = (s.y || PAD_Y) + "px";
    el.style.width = NODE_W + "px";

    var loop = document.createElement("div");
    loop.className = "gnode__loop";
    if (loops[s.id]) loop.textContent = "↻×" + loops[s.id].count;
    el.appendChild(loop);

    var icon = document.createElement("div");
    icon.className = "gnode__icon";
    icon.textContent = isJudge(s) ? "⚖" : stageIcon(s.agent);
    el.appendChild(icon);

    var role = document.createElement("div");
    role.className = "gnode__role";
    role.textContent = s.role;
    el.appendChild(role);

    var agent = document.createElement("div");
    agent.className = "gnode__agent";
    agent.textContent = agentLabel(s.agent || s.service || "");
    el.appendChild(agent);

    if (s.parallel_group) {
      var fork = document.createElement("div");
      fork.className = "gnode__fork";
      fork.textContent = "∥ " + s.parallel_group;
      el.appendChild(fork);
    }

    var del = document.createElement("button");
    del.className = "gnode__delete";
    del.type = "button";
    del.textContent = "×";
    del.addEventListener("click", function (e) {
      e.stopPropagation();
      deleteStage(i);
    });
    el.appendChild(del);

    var nIn = inPortCount(s);
    var h = Math.max(NODE_H, 24 + nIn * 18);
    el.style.minHeight = h + "px";
    for (var p = 0; p < nIn; p++) {
      var file = (s.inputs || [])[p] || "";
      var pin = document.createElement("div");
      pin.className = "gport gport--in" + (file ? "" : " gport--empty");
      pin.style.top = portY(nIn, p, h) + "px";
      pin.setAttribute("data-sid", s.id);
      pin.setAttribute("data-slot", String(p));
      pin.setAttribute("data-file", file);
      pin.setAttribute("data-tip", file || "input " + (p + 1));
      bindInPort(pin);
      el.appendChild(pin);
    }
    var outs = (s.outputs && s.outputs.length) ? s.outputs : [defaultOutput(s)];
    if (!s.outputs || !s.outputs.length) s.outputs = outs.slice();
    outs.forEach(function (o, oi) {
      var po = document.createElement("div");
      po.className = "gport gport--out";
      po.style.top = portY(outs.length, oi, h) + "px";
      po.setAttribute("data-sid", s.id);
      po.setAttribute("data-file", o);
      po.setAttribute("data-tip", o);
      bindOutPort(po);
      el.appendChild(po);
    });

    el.addEventListener("mousedown", function (e) {
      if (e.target.classList.contains("gport") || e.target.classList.contains("gnode__delete")) return;
      startNodeDrag(i, e);
    });
    el.addEventListener("click", function (e) {
      if (e.target.classList.contains("gport") || e.target.classList.contains("gnode__delete")) return;
      if (nodeDrag && nodeDrag.moved) return;
      selectStage(i);
    });
    return el;
  }

  function drawWires(preview) {
    canvasWires.innerHTML = "";
    var wires = listWires();
    wires.forEach(function (w) {
      var a = portEl("out", w.fromId, w.fromFile);
      var b = portEl("in", w.toId, null, w.toSlot);
      if (!a || !b) return;
      var pa = portCenter(a);
      var pb = portCenter(b);
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", bezier(pa.x, pa.y, pb.x, pb.y));
      path.setAttribute("data-to", w.toId);
      path.setAttribute("data-file", w.file);
      var tip = document.createElementNS("http://www.w3.org/2000/svg", "title");
      tip.textContent = "click to disconnect " + w.file;
      path.appendChild(tip);
      path.addEventListener("click", function (e) {
        e.stopPropagation();
        disconnect(w.toId, w.file);
        toast("disconnected " + w.file, "success");
      });
      canvasWires.appendChild(path);
    });
    if (preview) {
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", bezier(preview.x1, preview.y1, preview.x2, preview.y2));
      path.setAttribute("class", "preview");
      canvasWires.appendChild(path);
    }
  }

  function portEl(kind, sid, file, slot) {
    var sel = '.gport--' + kind + '[data-sid="' + sid + '"]';
    var nodes = canvasNodes.querySelectorAll(sel);
    if (kind === "out") {
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].getAttribute("data-file") === file) return nodes[i];
      }
      return nodes[0] || null;
    }
    if (slot != null && nodes[slot]) return nodes[slot];
    return nodes[0] || null;
  }

  function portCenter(el) {
    var cr = canvasContainer.getBoundingClientRect();
    var r = el.getBoundingClientRect();
    return {
      x: r.left + r.width / 2 - cr.left + canvasContainer.scrollLeft,
      y: r.top + r.height / 2 - cr.top + canvasContainer.scrollTop
    };
  }

  function bindOutPort(el) {
    el.addEventListener("mousedown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var c = portCenter(el);
      wireDrag = {
        fromId: el.getAttribute("data-sid"),
        file: el.getAttribute("data-file"),
        x1: c.x,
        y1: c.y
      };
    });
  }

  function bindInPort(el) {
    el.addEventListener("mouseup", function (e) {
      if (!wireDrag) return;
      e.stopPropagation();
      connect(wireDrag.fromId, wireDrag.file, el.getAttribute("data-sid"), parseInt(el.getAttribute("data-slot"), 10));
      wireDrag = null;
      drawWires();
    });
    el.addEventListener("click", function (e) {
      if (wireDrag) return;
      var file = el.getAttribute("data-file");
      if (!file) return;
      e.stopPropagation();
      disconnect(el.getAttribute("data-sid"), file);
      toast("disconnected " + file, "success");
    });
  }

  function startNodeDrag(idx, e) {
    var s = stages[idx];
    nodeDrag = {
      idx: idx,
      moved: false,
      ox: e.clientX - (s.x || 0),
      oy: e.clientY - (s.y || 0)
    };
  }

  document.addEventListener("mousemove", function (e) {
    if (wireDrag) {
      var cr = canvasContainer.getBoundingClientRect();
      drawWires({
        x1: wireDrag.x1,
        y1: wireDrag.y1,
        x2: e.clientX - cr.left + canvasContainer.scrollLeft,
        y2: e.clientY - cr.top + canvasContainer.scrollTop
      });
    }
    if (nodeDrag) {
      var s = stages[nodeDrag.idx];
      var nx = e.clientX - nodeDrag.ox;
      var ny = e.clientY - nodeDrag.oy;
      if (Math.abs(nx - (s.x || 0)) > 3 || Math.abs(ny - (s.y || 0)) > 3) nodeDrag.moved = true;
      s.x = Math.max(8, nx);
      s.y = Math.max(8, ny);
      var el = canvasNodes.querySelector('.gnode[data-id="' + s.id + '"]');
      if (el) {
        el.classList.add("dragging");
        el.style.left = s.x + "px";
        el.style.top = s.y + "px";
      }
      drawWires();
    }
  });
  document.addEventListener("mouseup", function () {
    if (wireDrag) { wireDrag = null; drawWires(); }
    if (nodeDrag) {
      var el = canvasNodes.querySelector(".gnode.dragging");
      if (el) el.classList.remove("dragging");
      setTimeout(function () { nodeDrag = null; }, 0);
    }
  });

  function selectStage(idx) {
    if (selectedIndex === idx) {
      selectedIndex = -1;
      configPanel.classList.remove("visible");
      render();
      return;
    }
    selectedIndex = idx;
    showConfig(idx);
    render();
  }

  function showConfig(idx) {
    var s = stages[idx];
    configPanel.classList.add("visible");
    document.getElementById("cfgRole").innerHTML = roles.map(function (r) {
      return '<option value="' + r + '"' + (r === s.role ? " selected" : "") + ">" + r + "</option>";
    }).join("");

    var installed = allAgents.filter(function (a) { return a.installed; });
    document.getElementById("cfgAgent").innerHTML = installed.map(function (a) {
      var val = a.id || a.name;
      var cur = s.agent || s.service || "";
      var sel = val === cur || a.name === cur || a.id === cur;
      return '<option value="' + val + '"' + (sel ? " selected" : "") + ">" + a.name + "</option>";
    }).join("");

    document.getElementById("cfgPrompt").value = s.prompt || "";
    document.getElementById("cfgOutputs").value = (s.outputs || []).join(", ");
    document.getElementById("cfgParallel").value = s.parallel_group || "";
    var slots = document.getElementById("cfgInSlots");
    slots.value = inPortCount(s);
    slots.disabled = !isJudge(s);
    slots.parentElement.style.opacity = isJudge(s) ? "1" : ".45";

    var inputsDiv = document.getElementById("cfgInputs");
    inputsDiv.innerHTML = "";
    var prev = [{ output: "task" }];
    stages.forEach(function (ps, pi) {
      if (pi === idx) return;
      (ps.outputs || []).forEach(function (o) { prev.push({ output: o }); });
    });
    prev.forEach(function (po) {
      var label = document.createElement("label");
      var checked = (s.inputs || []).indexOf(po.output) !== -1;
      label.innerHTML = '<input type="checkbox" value="' + po.output + '"' + (checked ? " checked" : "") + "><span>" + po.output + "</span>";
      inputsDiv.appendChild(label);
    });
  }

  function saveConfigFromPanel() {
    if (selectedIndex < 0 || selectedIndex >= stages.length) return;
    var s = stages[selectedIndex];
    s.role = document.getElementById("cfgRole").value;
    s.agent = document.getElementById("cfgAgent").value;
    s.service = agentService(s.agent) || undefined;
    s.type = stageType(s.agent);
    s.prompt = document.getElementById("cfgPrompt").value;
    s.outputs = document.getElementById("cfgOutputs").value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    if (isJudge(s)) {
      var n = parseInt(document.getElementById("cfgInSlots").value, 10) || 2;
      s.in_slots = Math.max(2, Math.min(4, n));
    }
    var checks = document.querySelectorAll('#cfgInputs input[type="checkbox"]:checked');
    s.inputs = Array.from(checks).map(function (c) { return c.value; });
    deriveParallel();
  }

  ["cfgRole", "cfgAgent", "cfgInSlots"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", function () {
      saveConfigFromPanel();
      render();
      if (selectedIndex >= 0) showConfig(selectedIndex);
    });
  });
  document.getElementById("cfgPrompt").addEventListener("input", saveConfigFromPanel);
  document.getElementById("cfgOutputs").addEventListener("input", function () {
    saveConfigFromPanel();
    render();
  });
  document.getElementById("cfgInputs").addEventListener("change", function () {
    saveConfigFromPanel();
    render();
    if (selectedIndex >= 0) showConfig(selectedIndex);
  });

  function addStage() {
    var id = genId();
    var prev = stages[stages.length - 1];
    var inputs = prev && prev.outputs && prev.outputs[0] ? [prev.outputs[0]] : ["task"];
    stages.push({
      id: id,
      type: "opencode",
      role: roles.indexOf("plan") >= 0 ? "plan" : (roles[0] || "plan"),
      agent: "opencode",
      prompt: "",
      inputs: inputs,
      outputs: [id + ".md"],
      parallel_group: "",
      artifact_dir: "spec"
    });
    layout(false);
    selectStage(stages.length - 1);
    render();
  }

  function deleteStage(idx) {
    var removed = stages.splice(idx, 1)[0];
    if (removed && loops[removed.id]) {
      var l = loops[removed.id];
      l.stages = l.stages.filter(function (sid) { return sid !== removed.id; });
      if (!l.stages.length) delete loops[removed.id];
    }
    stages.forEach(function (s) {
      s.inputs = (s.inputs || []).filter(function (inp) {
        return (removed.outputs || []).indexOf(inp) === -1;
      });
    });
    if (selectedIndex === idx) { selectedIndex = -1; configPanel.classList.remove("visible"); }
    else if (selectedIndex > idx) selectedIndex--;
    deriveParallel();
    render();
  }

  document.getElementById("btnAddStage").addEventListener("click", addStage);
  document.getElementById("btnDeleteStage").addEventListener("click", function () {
    if (selectedIndex >= 0) deleteStage(selectedIndex);
  });

  document.getElementById("btnClean").addEventListener("click", function () {
    stages.forEach(function (s) { s.x = null; s.y = null; });
    layout(true);
    render();
    toast("layout cleaned", "success");
  });

  document.getElementById("btnExpand").addEventListener("click", function () {
    expanded = !expanded;
    canvasSection.classList.toggle("expanded", expanded);
    this.textContent = expanded ? "close" : "expand";
    render();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && expanded) {
      expanded = false;
      canvasSection.classList.remove("expanded");
      document.getElementById("btnExpand").textContent = "expand";
      render();
    }
  });

  document.getElementById("btnLoopStage").addEventListener("click", function () {
    if (selectedIndex < 0) { toast("select a stage first", "error"); return; }
    selectedLoopStageIdx = selectedIndex;
    document.getElementById("loopCount").value = 3;
    document.getElementById("loopModal").classList.add("visible");
  });
  document.getElementById("loopCancel").addEventListener("click", function () {
    document.getElementById("loopModal").classList.remove("visible");
  });
  document.getElementById("loopConfirm").addEventListener("click", function () {
    var count = parseInt(document.getElementById("loopCount").value, 10) || 3;
    count = Math.max(2, Math.min(10, count));
    if (selectedLoopStageIdx < 0) return;
    var stageId = stages[selectedLoopStageIdx].id;
    if (loops[stageId]) loops[stageId].count = count;
    else {
      var groupStages = [stageId];
      for (var i = selectedLoopStageIdx + 1; i < stages.length; i++) {
        if (!loops[stages[i].id]) groupStages.push(stages[i].id);
        else break;
      }
      var loopData = { count: count, stages: groupStages };
      groupStages.forEach(function (sid) { loops[sid] = loopData; });
    }
    document.getElementById("loopModal").classList.remove("visible");
    render();
    toast("loop set: ×" + count, "success");
  });

  function fillPresetRow(el, list, canDelete) {
    el.innerHTML = "";
    if (!list.length) {
      el.innerHTML = '<span style="font-size:12px;color:var(--text-dim);font-style:italic;">none yet</span>';
      return;
    }
    list.forEach(function (p) {
      var card = document.createElement("div");
      card.className = "preset-card";
      card.innerHTML =
        '<span class="preset-card__icon">' + (p.icon || "▸") + "</span>" +
        '<div class="preset-card__info">' +
          '<span class="preset-card__name">' + p.name + "</span>" +
          '<span class="preset-card__meta">' + (p.stages != null ? p.stages : ((p.pipeline && p.pipeline.stages && p.pipeline.stages.length) || 0)) + " stages</span>" +
        "</div>";
      card.addEventListener("click", function () { applyPreset(p.id); });
      if (canDelete) {
        var x = document.createElement("button");
        x.type = "button";
        x.textContent = "×";
        x.style.cssText = "margin-left:8px;border:0;background:transparent;color:var(--text-dim);cursor:pointer;font-size:14px";
        x.addEventListener("click", function (e) {
          e.stopPropagation();
          fetch("/api/presets/" + encodeURIComponent(p.id), { method: "DELETE" })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              if (d.ok) { toast("template removed", "success"); loadPresets(); }
              else toast(d.error || "cannot remove", "error");
            });
        });
        card.appendChild(x);
      }
      el.appendChild(card);
    });
  }

  function renderPresets(data) {
    var builtin = [], user = [];
    if (Array.isArray(data)) builtin = data;
    else {
      builtin = (data && data.builtin) || [];
      user = (data && data.user) || [];
    }
    fillPresetRow(document.getElementById("presetsBuiltin"), builtin, false);
    fillPresetRow(document.getElementById("presetsUser"), user, true);
  }

  function loadPresets() {
    fetch("/api/presets").then(function (r) { return r.json(); }).then(renderPresets).catch(function () {});
  }

  function applyPreset(id) {
    fetch("/api/presets/apply/" + encodeURIComponent(id), { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok && data.pipeline) {
          loadPipeline(data.pipeline);
          toast("preset loaded", "success");
        } else toast("error: " + (data.error || "unknown"), "error");
      })
      .catch(function () { toast("network error", "error"); });
  }

  function renderAgents(agents) {
    allAgents = agents;
    var grid = document.getElementById("agentGrid");
    grid.innerHTML = "";
    var sel = document.getElementById("startAgentSelect");
    sel.innerHTML = "";
    agents.forEach(function (a) {
      var chip = document.createElement("div");
      chip.className = "agent-chip " + (a.installed ? (a.type === "browser" ? "browser" : "installed") : "missing");
      chip.textContent = a.name + " " + (a.installed ? "✓" : "✗");
      grid.appendChild(chip);
      if (a.installed) {
        var opt = document.createElement("option");
        opt.value = a.id || a.name;
        opt.textContent = a.name;
        sel.appendChild(opt);
      }
    });
    var startOpt = document.createElement("option");
    startOpt.value = "";
    startOpt.textContent = "-- select start agent --";
    sel.insertBefore(startOpt, sel.firstChild);
    sel.value = "";
  }

  function isUnnamed(n) {
    n = (n || "").trim();
    return !n || /^default$/i.test(n);
  }

  function loadPipeline(p) {
    if (pipelineName) {
      pipelineName.value = (p && p.name) ? p.name : "Default";
      if (isUnnamed(pipelineName.value)) pipelineName.value = "Default";
    }
    if (p.id) pipelineId = p.id;
    loopCounter = 0;
    stages = (p.stages || []).map(normalizeStage);
    loops = {};
    if (p.loops) {
      Object.keys(p.loops).forEach(function (key) {
        var l = p.loops[key];
        (l.stages || []).forEach(function (sid) { loops[sid] = l; });
      });
    }
    selectedIndex = -1;
    configPanel.classList.remove("visible");
    layout(false);
    render();
  }

  function currentPayload() {
    deriveParallel();
    var payload = {
      id: pipelineId || undefined,
      name: pipelineName.value,
      stages: stages.map(function (s) {
        var copy = Object.assign({}, s);
        copy.inputs = (s.inputs || []).filter(Boolean);
        return copy;
      }),
      loops: {}
    };
    var seen = {};
    Object.keys(loops).forEach(function (sid) {
      var l = loops[sid];
      var key = (l.stages || []).slice().sort().join(",");
      if (!seen[key]) {
        seen[key] = true;
        payload.loops[l.stages[0]] = { count: l.count, stages: l.stages };
      }
    });
    return payload;
  }

  var saveMode = "desk";

  function persistDesk() {
    fetch("/api/pipeline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentPayload())
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) {
        toast("pipeline saved to this desk", "success");
        if (data.pipeline) loadPipeline(data.pipeline);
      } else toast("error: " + (data.error || "unknown"), "error");
    }).catch(function () { toast("network error", "error"); });
  }

  function openNameModal(mode) {
    saveMode = mode;
    document.getElementById("tplName").value = isUnnamed(pipelineName.value) ? "" : pipelineName.value;
    document.querySelector("#tplModal .modal__title").textContent =
      mode === "template" ? "save as named template" : "name this pipeline before saving";
    document.getElementById("tplModal").classList.add("visible");
    document.getElementById("tplName").focus();
  }

  document.getElementById("btnSave").addEventListener("click", function () {
    if (isUnnamed(pipelineName.value)) openNameModal("desk");
    else persistDesk();
  });

  document.getElementById("btnSaveTemplate").addEventListener("click", function () {
    openNameModal("template");
  });
  document.getElementById("tplCancel").addEventListener("click", function () {
    document.getElementById("tplModal").classList.remove("visible");
  });
  document.getElementById("tplConfirm").addEventListener("click", function () {
    var name = document.getElementById("tplName").value.trim();
    if (isUnnamed(name)) { toast("give it a real name — not Default", "error"); return; }
    pipelineName.value = name;
    document.getElementById("tplModal").classList.remove("visible");
    if (saveMode === "desk") {
      persistDesk();
      return;
    }
    fetch("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name, pipeline: currentPayload() })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) {
        toast("saved to yours", "success");
        loadPresets();
      } else toast(d.error || "could not save", "error");
    }).catch(function () { toast("network error", "error"); });
  });

  document.getElementById("btnReset").addEventListener("click", function () {
    if (!confirm("reset to default pipeline?")) return;
    applyPreset("dual-planner");
  });

  document.getElementById("btnLaunch").addEventListener("click", function () {
    if (!stages.length) { toast("add stages first", "error"); return; }
    fetch("/api/pipeline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentPayload())
    }).then(function () { window.location.href = "/"; });
  });

  window.addEventListener("qmeka:project-change", function () {
    fetch("/api/pipeline").then(function (r) { return r.json(); }).then(function (p) {
      if (p && p.stages) loadPipeline(p);
      else { stages = []; render(); }
    });
  });

  var glow = document.getElementById("mouseGlow");
  if (glow) {
    var glowTimeout;
    document.addEventListener("mousemove", function (e) {
      glow.style.left = e.clientX + "px";
      glow.style.top = e.clientY + "px";
      glow.classList.add("active");
      clearTimeout(glowTimeout);
      glowTimeout = setTimeout(function () { glow.classList.remove("active"); }, 1500);
    });
  }
  var dotsContainer = document.getElementById("floatDots");
  if (dotsContainer) {
    for (var i = 0; i < 16; i++) {
      var dot = document.createElement("div");
      dot.className = "float-dot";
      dot.style.left = Math.random() * 100 + "%";
      dot.style.animationDuration = (8 + Math.random() * 12) + "s";
      dot.style.animationDelay = (Math.random() * 10) + "s";
      dot.style.width = dot.style.height = (2 + Math.random() * 3) + "px";
      dotsContainer.appendChild(dot);
    }
  }

  function init() {
    Promise.all([
      fetch("/api/pipeline").then(function (r) { return r.json(); }).catch(function () { return {}; }),
      fetch("/api/roles").then(function (r) { return r.json(); }).catch(function () { return []; }),
      fetch("/api/agents").then(function (r) { return r.json(); }).catch(function () { return []; }),
      fetch("/api/presets").then(function (r) { return r.json(); }).catch(function () { return {}; })
    ]).then(function (results) {
      var pipeline = results[0] || {};
      var rl = results[1] || [];
      var ag = results[2] || [];
      var pr = results[3] || {};
      if (Array.isArray(rl)) roles = rl;
      else if (rl.roles) roles = rl.roles;
      if (Array.isArray(ag)) renderAgents(ag);
      else if (ag.agents) renderAgents(ag.agents);
      renderPresets(pr);
      if (pipeline.stages && pipeline.stages.length) loadPipeline(pipeline);
      else {
        if (pipelineName) pipelineName.value = "Default";
        render();
      }
    });
  }

  init();
})();
