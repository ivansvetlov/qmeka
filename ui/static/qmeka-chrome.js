/* Shared header: theme, readiness dots, themed tips, first-run wizard. */
(function () {
  var THEME_KEY = "orch-theme";
  var root = document.documentElement;

  function currentTheme() {
    return root.getAttribute("data-theme") || "dark";
  }

  function setTheme(t) {
    root.setAttribute("data-theme", t);
    document.querySelectorAll(".theme-btn").forEach(function (b) {
      b.classList.toggle("active", b.dataset.theme === t);
    });
    localStorage.setItem(THEME_KEY, t);
  }

  var saved = localStorage.getItem(THEME_KEY) || localStorage.getItem("theme") || "dark";
  setTheme(saved);
  document.querySelectorAll(".theme-btn").forEach(function (b) {
    b.addEventListener("click", function () { setTheme(b.dataset.theme); });
  });

  function padReadyDots(el) {
    while (el.querySelectorAll(".ready-dot").length < 6) {
      el.insertAdjacentHTML("beforeend", '<span class="ready-dot ready-dot--pending"></span>');
    }
  }

  var READY_KEY = "qmeka-ready-html";
  var READY_TS = "qmeka-ready-ts";
  var READY_TTL = 5 * 60 * 1000;

  function paintReady(el, html) {
    if (!html) return;
    if (el.innerHTML === html) {
      padReadyDots(el);
      return;
    }
    el.innerHTML = html;
    padReadyDots(el);
  }

  function loadHeaderReady(force) {
    var el = document.getElementById("headerReady");
    if (!el) return;
    var cached = sessionStorage.getItem(READY_KEY);
    var ts = parseInt(sessionStorage.getItem(READY_TS) || "0", 10);
    var age = Date.now() - ts;
    if (cached) paintReady(el, cached);
    else padReadyDots(el);
    if (!force && cached && age < READY_TTL) return;
    fetch("/api/readiness?compact=1")
      .then(function (r) { return r.text(); })
      .then(function (html) {
        if (!html) return;
        sessionStorage.setItem(READY_KEY, html);
        sessionStorage.setItem(READY_TS, String(Date.now()));
        paintReady(el, html);
      })
      .catch(function () { padReadyDots(el); });
  }
  loadHeaderReady(false);
  setInterval(function () { loadHeaderReady(false); }, READY_TTL);

  function loadHud() {
    var label = document.getElementById("connectionLabel");
    var dot = document.getElementById("connectionDot");
    var ses = document.getElementById("currentSession");
    if (!label && !ses) return;
    fetch("/api/setup/status")
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (label) label.textContent = st.cdp ? "connected" : "disconnected";
        if (dot) dot.classList.toggle("disconnected", !st.cdp);
        if (ses) ses.textContent = st.project_name || st.session_name || "Scratch";
      })
      .catch(function () {});
  }
  loadHud();
  setInterval(loadHud, 30000);

  function browseFolder() {
    return fetch("/api/browse-folder", { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && d.path) return d.path;
        return null;
      })
      .catch(function () { return null; });
  }

  var STEPS = [
    {
      id: "welcome",
      title: "This is Qmeka Chrome",
      copy: "Not your everyday browser. One profile keeps model logins. Pinned tabs come back.",
      note: "Same idea as Docker Desktop or VS Code: a short checklist, we verify, then Next. Not a stack of OK boxes.",
      next: "Continue"
    },
    {
      id: "browser",
      title: "Open the workspace browser",
      copy: "We start Chrome with the Qmeka profile and CDP. If you already signed in, pinned chats restore. No extra tabs.",
      note: "Check looks for port 9222 on this machine.",
      next: "Open Chrome"
    },
    {
      id: "signin",
      title: "Sign in once per model",
      copy: "Log in inside that Chrome. We wait, then confirm. Tokens stay in the profile — not in git.",
      note: "z.ai, DeepSeek, Qwen, Google. Skip any you will not use.",
      next: "Recheck logins"
    },
    {
      id: "workspace",
      title: "Where should agents write?",
      copy: "Machine setup is done. This is the work mode — you can change it later on launch.",
      note: "Already have a repo? Attach it. Not started yet? Stay in the sandbox.",
      next: "Save choice"
    },
    {
      id: "ready",
      title: "You're set",
      copy: "Next launches skip this. Overlays land on the existing tabs. Start a task when you want.",
      note: "Preview only shows this menu. Real first-run on a new PC is the same steps.",
      next: "Go to launch"
    }
  ];

  var preview = false;
  var step = 0;
  var workspaceChoice = { mode: "scratch", path: null };
  var scrim = document.getElementById("setupScrim");
  var btnPreview = document.getElementById("btnSetupPreview");

  function pipHtml() {
    var html = "";
    for (var i = 0; i < STEPS.length; i++) {
      html += '<div class="setup-step-pip' + (i <= step ? " on" : "") + '"></div>';
    }
    return html;
  }

  function renderWizard() {
    var s = STEPS[step];
    document.getElementById("setupKicker").textContent = preview ? "Preview · new machine" : "First run";
    document.getElementById("setupPips").innerHTML = pipHtml();
    document.getElementById("setupTitle").textContent = s.title;
    document.getElementById("setupCopy").textContent = s.copy;
    document.getElementById("setupNote").textContent = s.note;
    document.getElementById("setupNext").textContent = s.next;
    document.getElementById("setupNext").disabled = false;
    var body = document.getElementById("setupBody");
    body.innerHTML = "";
    if (s.id === "browser" || s.id === "signin" || s.id === "workspace") {
      body.innerHTML = '<div class="setup-row"><span class="setup-row__name">Checking…</span><span class="setup-row__stat">·</span></div>';
      fetch("/api/setup/status")
        .then(function (r) { return r.json(); })
        .then(drawStatus)
        .catch(function () {
          body.innerHTML = '<div class="setup-row"><span>Could not read status</span><span class="setup-row__stat fail">error</span></div>';
        });
    }
  }

  function drawStatus(st) {
    var s = STEPS[step];
    var body = document.getElementById("setupBody");
    var rows = [];
    if (s.id === "browser") {
      rows.push(row("Profile on disk", st.profile_has_session || st.profile_exists, st.profile_has_session ? "saved logins" : "empty"));
      rows.push(row("CDP " + (st.cdp_port || 9222), st.cdp, st.cdp ? "connected" : "not running"));
    }
    if (s.id === "signin") {
      (st.providers || []).forEach(function (p) {
        rows.push(row(p.name || p.id, p.ok, p.ok ? "signed in" : "needs login", p.id));
      });
    }
    if (s.id === "workspace") {
      workspaceChoice.mode = st.target_mode || "scratch";
      workspaceChoice.path = st.target_project || null;
      body.innerHTML = drawWorkspace(st);
      return;
    }
    body.innerHTML = rows.join("") || "";
  }

  function drawWorkspace(st) {
    var scratchOn = workspaceChoice.mode !== "project" ? " on" : "";
    var projOn = workspaceChoice.mode === "project" ? " on" : "";
    var html = '<div class="setup-choices">';
    html += '<button type="button" class="setup-choice' + scratchOn + '" data-mode="scratch">';
    html += '<div class="setup-choice__title">Scratch<span class="setup-choice__tag">no repo yet</span></div>';
    html += '<div class="setup-choice__hint">Sketches stay in this run’s prototyping folder. Safe. Switch later when a repo exists.</div>';
    html += "</button>";
    html += '<button type="button" class="setup-choice' + projOn + '" data-mode="project">';
    html += '<div class="setup-choice__title">Existing project<span class="setup-choice__tag">already started</span></div>';
    html += '<div class="setup-choice__hint">Agents edit that folder. Qmeka still keeps plans and reviews in its own session.</div>';
    html += "</button></div>";
    var projects = st.projects || [];
    if (projects.length) {
      html += '<div class="setup-projects">';
      projects.forEach(function (p) {
        var on = workspaceChoice.path === p.path ? " on" : "";
        html += '<button type="button" class="setup-choice' + on + '" data-mode="project" data-path="' + p.path.replace(/"/g, "") + '">';
        html += '<div class="setup-choice__title">' + p.name + (p.has_git ? '<span class="setup-choice__tag">git</span>' : "") + "</div>";
        html += '<div class="setup-choice__hint">' + p.path + "</div></button>";
      });
      html += "</div>";
    }
    html += '<div class="setup-path-row">';
    html += '<input class="setup-path" id="setupCustomPath" placeholder="Or paste a folder path" value="' +
      (workspaceChoice.path || "") + '">';
    html += '<button type="button" class="setup-btn" id="setupBrowse">Browse…</button>';
    html += "</div>";
    return html;
  }

  function row(name, ok, stat, providerId) {
    var action = "";
    if (providerId && !ok) {
      action = ' <button type="button" class="setup-btn" data-login="' + providerId + '">Open</button>';
    }
    return '<div class="setup-row"><span class="setup-row__name">' + name + "</span>" +
      '<span><span class="setup-row__stat ' + (ok ? "ok" : "fail") + '">' + stat + "</span>" + action + "</span></div>";
  }

  function openWizard(isPreview) {
    preview = !!isPreview;
    step = 0;
    scrim.classList.add("open");
    renderWizard();
  }

  function closeWizard() {
    scrim.classList.remove("open");
    if (!preview) localStorage.setItem("qmeka_setup_seen", "1");
  }

  if (btnPreview) {
    btnPreview.addEventListener("click", function () { openWizard(false); });
  }
  document.getElementById("setupSkip").addEventListener("click", closeWizard);

  document.getElementById("setupBody").addEventListener("click", function (e) {
    var t = e.target;
    if (!t) return;
    var login = t.getAttribute("data-login") || (t.closest && t.closest("[data-login]") && t.closest("[data-login]").getAttribute("data-login"));
    if (login) {
      fetch("/api/session_pool/open-login/" + login, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function () { renderWizard(); });
      return;
    }
    if (t.id === "setupBrowse" || (t.closest && t.closest("#setupBrowse"))) {
      browseFolder().then(function (path) {
        if (!path) return;
        workspaceChoice.mode = "project";
        workspaceChoice.path = path;
        var inp = document.getElementById("setupCustomPath");
        if (inp) inp.value = path;
      });
      return;
    }
    var choice = t.closest ? t.closest("[data-mode]") : null;
    if (choice) {
      workspaceChoice.mode = choice.getAttribute("data-mode");
      workspaceChoice.path = choice.getAttribute("data-path") || null;
      var inp = document.getElementById("setupCustomPath");
      if (inp && workspaceChoice.path) inp.value = workspaceChoice.path;
      document.querySelectorAll("#setupBody [data-mode]").forEach(function (el) {
        var same = el.getAttribute("data-mode") === workspaceChoice.mode &&
          (el.getAttribute("data-path") || null) === workspaceChoice.path;
        el.classList.toggle("on", same || (workspaceChoice.mode === "scratch" && el.getAttribute("data-mode") === "scratch" && !el.getAttribute("data-path")));
      });
    }
  });

  document.getElementById("setupNext").addEventListener("click", function () {
    var s = STEPS[step];
    var btn = document.getElementById("setupNext");
    if (s.id === "browser") {
      btn.disabled = true;
      btn.textContent = "Checking…";
      fetch("/api/setup/ensure-chrome", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok || preview) { step += 1; renderWizard(); }
          else { btn.disabled = false; btn.textContent = "Retry"; }
        })
        .catch(function () { btn.disabled = false; btn.textContent = "Retry"; });
      return;
    }
    if (s.id === "signin") {
      btn.disabled = true;
      fetch("/api/setup/status")
        .then(function (r) { return r.json(); })
        .then(function (st) {
          drawStatus(st);
          step += 1;
          renderWizard();
        })
        .catch(function () { step += 1; renderWizard(); });
      return;
    }
    if (s.id === "workspace") {
      var inp = document.getElementById("setupCustomPath");
      if (inp && inp.value.trim()) {
        workspaceChoice.mode = "project";
        workspaceChoice.path = inp.value.trim();
      }
      btn.disabled = true;
      fetch("/api/setup/target", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(workspaceChoice)
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.ok || preview) { step += 1; renderWizard(); }
          else { btn.disabled = false; btn.textContent = d.error || "Retry"; }
        })
        .catch(function () { btn.disabled = false; btn.textContent = "Retry"; });
      return;
    }
    if (s.id === "ready") {
      closeWizard();
      window.location.href = "/launch";
      return;
    }
    step += 1;
    renderWizard();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && scrim && scrim.classList.contains("open")) closeWizard();
  });

  /* ── Project picker (header pill). Sessions stay runs-inside-project. ── */
  var picker = document.getElementById("projectPicker");
  var modal = document.getElementById("projectModal");
  var pickerList = document.getElementById("projectPickerList");
  var modeBtns = [document.getElementById("projectModeScratch"), document.getElementById("projectModeFolder")];
  var folderBox = document.getElementById("projectModalFolders");
  var pathInp = document.getElementById("projectModalPath");
  var pathRow = document.getElementById("projectPathRow");
  var nameInp = document.getElementById("projectModalName");
  var draft = { mode: "scratch", path: null, folders: [], activeId: null, projects: [] };

  function folderLabel(p) {
    if (!p) return "scratch";
    var parts = String(p).replace(/\\/g, "/").split("/");
    return parts[parts.length - 1] || p;
  }

  function markDeskSeen() {
    sessionStorage.setItem("qmeka-desk-seen", "1");
  }

  function renderPicker() {
    if (!pickerList) return;
    pickerList.innerHTML = "";
    (draft.projects || []).forEach(function (p) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "project-picker-item" + (p.id === draft.activeId ? " on" : "");
      var meta = p.mode === "project" ? folderLabel(p.path) : "scratch · runs live in Sessions";
      btn.innerHTML = '<span class="project-picker-item__name"></span><span class="project-picker-item__meta"></span>';
      btn.querySelector(".project-picker-item__name").textContent = p.name || p.id;
      btn.querySelector(".project-picker-item__meta").textContent = meta;
      btn.addEventListener("click", function () {
        markDeskSeen();
        if (p.id === draft.activeId) {
          closePicker();
          return;
        }
        fetch("/api/projects/" + encodeURIComponent(p.id) + "/activate", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!d.ok) return;
            closePicker();
            window.dispatchEvent(new CustomEvent("qmeka:project-change", { detail: d.project }));
            loadHud();
            var path = location.pathname;
            if (path === "/" || path === "/history") location.reload();
            else loadProjects();
          });
      });
      pickerList.appendChild(btn);
    });
  }

  function loadProjects() {
    fetch("/api/projects")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        draft.folders = data.folders || [];
        draft.projects = data.projects || [];
        draft.activeId = data.active_id;
        renderPicker();
      })
      .catch(function () {});
  }

  function setMode(mode) {
    draft.mode = mode;
    modeBtns.forEach(function (b) {
      if (b) b.classList.toggle("on", b.getAttribute("data-mode") === mode);
    });
    var show = mode === "project";
    if (folderBox) folderBox.hidden = !show;
    if (pathRow) pathRow.hidden = !show;
  }

  function renderFolders() {
    if (!folderBox) return;
    folderBox.innerHTML = "";
    (draft.folders || []).forEach(function (f) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "project-modal__folder" + (draft.path === f.path ? " on" : "");
      b.textContent = f.name + (f.has_git ? "  · git" : "");
      b.addEventListener("click", function () {
        draft.path = f.path;
        if (pathInp) pathInp.value = f.path;
        if (nameInp && !nameInp.value.trim()) nameInp.value = f.name;
        renderFolders();
      });
      folderBox.appendChild(b);
    });
  }

  function openPicker() {
    loadProjects();
    if (picker) picker.hidden = false;
  }
  function closePicker() {
    if (picker) picker.hidden = true;
  }
  function openCreate() {
    draft.mode = "scratch";
    draft.path = null;
    if (nameInp) nameInp.value = "";
    if (pathInp) pathInp.value = "";
    setMode("scratch");
    renderFolders();
    closePicker();
    if (modal) modal.hidden = false;
  }
  function closeCreate() {
    if (modal) modal.hidden = true;
  }

  var pill = document.getElementById("connectionPill");
  if (pill) {
    pill.addEventListener("click", function () { openPicker(); });
    pill.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openPicker(); }
    });
  }
  var stay = document.getElementById("projectPickerStay");
  if (stay) stay.addEventListener("click", function () { markDeskSeen(); closePicker(); });
  var neu = document.getElementById("projectPickerNew");
  if (neu) neu.addEventListener("click", openCreate);
  var cancel = document.getElementById("projectModalCancel");
  if (cancel) cancel.addEventListener("click", function () { closeCreate(); openPicker(); });
  modeBtns.forEach(function (b) {
    if (!b) return;
    b.addEventListener("click", function () { setMode(b.getAttribute("data-mode")); });
  });
  if (pathInp) {
    pathInp.addEventListener("input", function () {
      draft.path = pathInp.value.trim() || null;
    });
  }
  var browseBtn = document.getElementById("projectModalBrowse");
  if (browseBtn) {
    browseBtn.addEventListener("click", function () {
      browseFolder().then(function (path) {
        if (!path) return;
        draft.mode = "project";
        draft.path = path;
        setMode("project");
        if (pathInp) pathInp.value = path;
        if (nameInp && !nameInp.value.trim()) {
          var parts = path.replace(/\\/g, "/").split("/");
          nameInp.value = parts[parts.length - 1] || path;
        }
      });
    });
  }
  var saveBtn = document.getElementById("projectModalSave");
  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      var body = {
        name: nameInp ? nameInp.value.trim() : "",
        mode: draft.mode,
        path: draft.mode === "project" ? (pathInp && pathInp.value.trim()) : null
      };
      saveBtn.disabled = true;
      fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          saveBtn.disabled = false;
          if (!d.ok) { alert(d.error || "could not create"); return; }
          markDeskSeen();
          closeCreate();
          window.dispatchEvent(new CustomEvent("qmeka:project-change", { detail: d.project }));
          loadHud();
          var path = location.pathname;
          if (path === "/" || path === "/history") location.reload();
          else loadProjects();
        })
        .catch(function () { saveBtn.disabled = false; });
    });
  }
  if (picker) {
    picker.addEventListener("click", function (e) {
      if (e.target === picker) { markDeskSeen(); closePicker(); }
    });
  }
  if (modal) {
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeCreate();
    });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (modal && !modal.hidden) { closeCreate(); return; }
    if (picker && !picker.hidden) { markDeskSeen(); closePicker(); }
  });
  window.addEventListener("qmeka:project-change", function () {
    loadHud();
    loadProjects();
  });
  if (picker) {
    loadProjects();
    if (!sessionStorage.getItem("qmeka-desk-seen")) openPicker();
  }
})();
