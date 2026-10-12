(function () {
  "use strict";

  const state = {
    q: "",
    year: "",
    top: "",
    folder: "",
    kind: "image",
    page: 1,
    pages: 1,
    total: 0,
    selected: new Set(),
    pendingDelete: [],
    view: "browse",
    dupPage: 1,
    dupPages: 1,
    selectedDupGroups: new Set(), // content hashes
    selectedDupGroupExtras: new Map(), // hash -> [extra rel_paths]
    lbPath: "",
    setup: null,
  };

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtDate(item) {
    if (item.year && item.month && item.day) {
      return `${item.year}-${String(item.month).padStart(2, "0")}-${String(item.day).padStart(2, "0")}`;
    }
    if (item.year) return String(item.year);
    return "";
  }

  function fmtSize(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }

  function updateSelCount() {
    const n = state.selected.size;
    $("selCount").textContent = n + " selected";
    $("deleteSelected").disabled = n === 0;
  }

  function fmtBytes(n) {
    if (!n && n !== 0) return "0 B";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(1) + " GB";
  }

  function updateDupGroupSelCount() {
    const n = state.selectedDupGroups.size;
    const countEl = $("dupGroupSelCount");
    const btn = $("deleteSelectedGroups");
    if (countEl) {
      countEl.textContent = n + " group" + (n === 1 ? "" : "s") + " selected";
    }
    if (btn) btn.disabled = n === 0;
  }

  function extrasForGroup(group) {
    if (!group || !group.items) return [];
    return group.items
      .filter((i) => i.rel_path !== group.keep)
      .map((i) => i.rel_path);
  }

  function selectDupGroup(hash, group, selected) {
    if (selected) {
      state.selectedDupGroups.add(hash);
      state.selectedDupGroupExtras.set(hash, extrasForGroup(group));
    } else {
      state.selectedDupGroups.delete(hash);
      state.selectedDupGroupExtras.delete(hash);
    }
  }

  function updateTrashInfo(trash) {
    const info = $("trashInfo");
    const btn = $("emptyTrash");
    if (!trash || !trash.exists || !trash.files) {
      info.textContent = "Trash: empty";
      btn.disabled = true;
      state.trash = trash || { files: 0, bytes: 0 };
      return;
    }
    state.trash = trash;
    info.textContent =
      `Trash: ${trash.files} file${trash.files === 1 ? "" : "s"} · ${fmtBytes(trash.bytes)}` +
      (trash.batches ? ` · ${trash.batches} batch${trash.batches === 1 ? "" : "es"}` : "");
    btn.disabled = false;
  }

  async function loadMeta() {
    const res = await fetch("/api/meta");
    const data = await res.json();
    if (!res.ok) {
      $("status").textContent = data.error || "Index missing";
      $("grid").innerHTML = `<div class="empty">${esc(data.error || "Open Setup, choose a folder, then build the index.")}</div>`;
      if (data.setup) state.setup = data.setup;
      return data;
    }

    const tops = $("tops");
    tops.innerHTML =
      `<button type="button" class="chip ${state.top ? "" : "active"}" data-top="">All (${data.total})</button>` +
      (data.tops || [])
        .slice(0, 80)
        .map(
          (t) =>
            `<button type="button" class="chip ${state.top === t.name ? "active" : ""}" data-top="${esc(t.name)}">${esc(t.name)} (${t.count})</button>`
        )
        .join("");

    tops.onclick = (e) => {
      const btn = e.target.closest("[data-top]");
      if (!btn) return;
      state.top = btn.getAttribute("data-top") || "";
      state.folder = "";
      state.page = 1;
      refreshFolders();
      search();
      loadMeta();
    };

    const years = $("years");
    years.innerHTML =
      `<button type="button" class="chip ${state.year ? "" : "active"}" data-year="">All years</button>` +
      (data.years || [])
        .map(
          (y) =>
            `<button type="button" class="chip ${String(state.year) === String(y) ? "active" : ""}" data-year="${y}">${y}</button>`
        )
        .join("");

    years.onclick = (e) => {
      const btn = e.target.closest("[data-year]");
      if (!btn) return;
      state.year = btn.getAttribute("data-year") || "";
      state.page = 1;
      search();
      loadMeta();
    };

    const prog = data.progress || {};
    let indexNote = document.getElementById("indexNote");
    if (!indexNote) {
      indexNote = document.createElement("p");
      indexNote.id = "indexNote";
      indexNote.className = "hint";
      $("status").insertAdjacentElement("afterend", indexNote);
    }
    if (prog.status === "running") {
      indexNote.textContent =
        `Indexing in progress… ${prog.upserted || 0} media so far` +
        (prog.current_folder ? ` · ${prog.current_folder}` : "");
    } else if (prog.status === "complete") {
      indexNote.textContent = `Index complete · ${prog.upserted || data.total || 0} media files`;
    } else {
      indexNote.textContent = "";
    }

    const d = data.duplicates || {};
    $("dupStats").textContent =
      `${d.size_candidate_files || 0} files share sizes · ` +
      `${d.hashed_candidates || 0} hashed · ` +
      `${d.confirmed_groups || 0} confirmed duplicate groups` +
      (d.extra_copies ? ` (~${d.extra_copies} extra copies)` : "");

    updateTrashInfo(data.trash || null);

    return data;
  }

  async function refreshFolders() {
    const url = state.top
      ? `/api/folders?top=${encodeURIComponent(state.top)}`
      : "/api/folders";
    const res = await fetch(url);
    const data = await res.json();
    const list = $("folders");
    const folders = data.folders || [];
    if (!state.top) {
      list.innerHTML = folders
        .slice(0, 200)
        .map(
          (f) =>
            `<li><button type="button" data-folder="" data-top="${esc(f.path)}" class="${state.top === f.path ? "active" : ""}"><span>${esc(f.path)}</span><span class="count">${f.count}</span></button></li>`
        )
        .join("");
    } else {
      list.innerHTML =
        `<li><button type="button" data-folder="" class="${!state.folder ? "active" : ""}"><span>All in ${esc(state.top)}</span></button></li>` +
        folders
          .slice(0, 400)
          .map(
            (f) =>
              `<li><button type="button" data-folder="${esc(f.path)}" class="${state.folder === f.path ? "active" : ""}"><span>${esc(f.path)}</span><span class="count">${f.count}</span></button></li>`
          )
          .join("");
    }

    list.onclick = (e) => {
      const btn = e.target.closest("button[data-folder], button[data-top]");
      if (!btn) return;
      if (btn.hasAttribute("data-top") && btn.getAttribute("data-top")) {
        state.top = btn.getAttribute("data-top");
        state.folder = "";
      } else {
        state.folder = btn.getAttribute("data-folder") || "";
      }
      state.page = 1;
      refreshFolders();
      search();
      loadMeta();
    };
  }

  async function search() {
    const params = new URLSearchParams();
    if (state.q) params.set("q", state.q);
    if (state.year) params.set("year", state.year);
    if (state.top) params.set("top", state.top);
    if (state.folder) params.set("folder", state.folder);
    params.set("kind", state.kind);
    params.set("page", String(state.page));
    params.set("page_size", "48");

    $("status").textContent = "Searching…";
    const res = await fetch("/api/search?" + params.toString());
    const data = await res.json();
    if (!res.ok) {
      $("status").textContent = data.error || "Search failed";
      return;
    }

    state.total = data.total;
    state.pages = data.pages;
    state.page = data.page;

    const label = [
      `${data.total} item${data.total === 1 ? "" : "s"}`,
      state.top ? `in ${state.top}` : null,
      state.folder ? `· ${state.folder}` : null,
      state.year ? `· ${state.year}` : null,
      `· page ${state.page} of ${state.pages}`,
    ]
      .filter(Boolean)
      .join(" ");
    $("status").textContent = label;

    const grid = $("grid");
    if (!data.items.length) {
      grid.innerHTML =
        '<div class="empty">No matches. Try another year, clear the album filter, or wait for indexing to finish.</div>';
      $("selectPage").checked = false;
    } else {
      grid.innerHTML = data.items
        .map((item) => {
          const date = fmtDate(item);
          const checked = state.selected.has(item.rel_path) ? "checked" : "";
          const selected = checked ? "selected" : "";
          const preview = item.can_preview
            ? `<img src="${esc(item.thumb)}" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'ph',textContent:'No preview'}))" />`
            : `<div class="ph">${esc((item.ext || "").toUpperCase() || "FILE")}<br/>no preview</div>`;
          return `<article class="card ${selected}" data-path="${esc(item.rel_path)}" data-original="${esc(item.original)}" data-name="${esc(item.name)}" data-folder="${esc(item.folder)}" data-date="${esc(date)}" data-size="${esc(fmtSize(item.size))}" data-can="${item.can_preview ? "1" : "0"}">
            <input class="pick" type="checkbox" ${checked} data-path="${esc(item.rel_path)}" aria-label="Select ${esc(item.name)}" />
            ${preview}
            <div class="card-body">
              ${date ? `<div class="date">${esc(date)}</div>` : ""}
              <div class="name">${esc(item.name)}</div>
              <div class="meta">${esc(item.folder || item.top || "")} · ${esc(fmtSize(item.size))}</div>
            </div>
          </article>`;
        })
        .join("");

      const pagePaths = data.items.map((i) => i.rel_path);
      $("selectPage").checked =
        pagePaths.length > 0 && pagePaths.every((p) => state.selected.has(p));
    }

    $("pager").hidden = state.pages <= 1;
    $("pageLabel").textContent = `Page ${state.page} / ${state.pages}`;
    $("prev").disabled = state.page <= 1;
    $("next").disabled = state.page >= state.pages;
    updateSelCount();
  }

  function openLightbox(card) {
    const can = card.getAttribute("data-can") === "1";
    const original = card.getAttribute("data-original");
    const name = card.getAttribute("data-name");
    const folder = card.getAttribute("data-folder");
    const date = card.getAttribute("data-date");
    const size = card.getAttribute("data-size");
    state.lbPath = card.getAttribute("data-path") || "";
    $("lbMeta").textContent = [name, folder, date, size].filter(Boolean).join(" · ");
    $("lbOpen").href = original;
    if (can) {
      $("lbImg").style.display = "block";
      $("lbImg").src = original;
    } else {
      $("lbImg").removeAttribute("src");
      $("lbImg").style.display = "none";
      $("lbMeta").textContent +=
        " — preview not available for this file type; use Open full size / Finder.";
    }
    $("lightbox").classList.add("open");
  }

  function openDeleteModal(paths) {
    state.pendingDelete = paths.slice();
    $("deleteSummary").textContent = `You are about to remove ${paths.length} file${paths.length === 1 ? "" : "s"}.`;
    $("deleteList").innerHTML = paths
      .slice(0, 40)
      .map((p) => `<li>${esc(p)}</li>`)
      .join("") + (paths.length > 40 ? `<li>…and ${paths.length - 40} more</li>` : "");
    $("deleteError").textContent = "";
    $("deletePhrase").value = "";
    document.querySelector('input[name="delMode"][value="trash"]').checked = true;
    $("deletePhrase").hidden = true;
    $("deleteModal").hidden = false;
  }

  async function confirmDelete() {
    const mode = document.querySelector('input[name="delMode"]:checked').value;
    const body = {
      paths: state.pendingDelete,
      mode,
      confirm: true,
    };
    if (mode === "permanent") {
      body.confirm_phrase = $("deletePhrase").value.trim();
      if (body.confirm_phrase !== "DELETE") {
        $("deleteError").textContent = 'Type DELETE exactly to permanently remove files.';
        return;
      }
    }
    $("confirmDelete").disabled = true;
    $("deleteError").textContent = "Working…";
    try {
      const res = await fetch("/api/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok && res.status !== 207) {
        $("deleteError").textContent = data.error || "Delete failed";
        $("confirmDelete").disabled = false;
        return;
      }
      for (const r of data.results || []) {
        if (r.ok) state.selected.delete(r.path);
      }
      if (state._pendingDupGroupHashes && state._pendingDupGroupHashes.length) {
        state._pendingDupGroupHashes.forEach((h) => {
          state.selectedDupGroups.delete(h);
          state.selectedDupGroupExtras.delete(h);
        });
        state._pendingDupGroupHashes = [];
      }
      $("deleteModal").hidden = true;
      $("lightbox").classList.remove("open");
      updateSelCount();
      updateDupGroupSelCount();
      await search();
      await loadMeta();
      if (state.view === "duplicates") await loadDuplicates();
      const msg =
        mode === "trash"
          ? `Moved ${data.succeeded} to ${data.trash_batch || ".explorer-trash"}`
          : `Permanently deleted ${data.succeeded}`;
      $("status").textContent = msg + (data.failed ? ` · ${data.failed} failed` : "");
      if (state.view === "duplicates") {
        $("dupStatus").textContent = msg + (data.failed ? ` · ${data.failed} failed` : "");
      }
    } catch (err) {
      $("deleteError").textContent = String(err && err.message ? err.message : err);
    } finally {
      $("confirmDelete").disabled = false;
    }
  }

  async function loadDuplicates() {
    $("dupStatus").textContent = "Loading duplicates…";
    const res = await fetch(`/api/duplicates?page=${state.dupPage}&page_size=15`);
    const data = await res.json();
    const prog = data.progress || {};
    const stats = data.stats || {};

    let progLine = "";
    if (prog.status === "running") {
      progLine = `Scanning… hashed ${prog.hashed || 0}/${prog.candidates || "?"} · refresh to see new groups`;
    } else if (prog.status === "complete") {
      progLine = `Last scan complete · ${prog.duplicate_groups || stats.confirmed_groups || 0} groups`;
    } else if (prog.status === "error") {
      progLine = "Scan error: " + (prog.error || "unknown");
    } else {
      progLine = "No content scan yet — click Find / continue duplicates (hashes only same-size candidates).";
    }
    $("dupStatus").textContent = progLine;
    $("dupStats").textContent =
      `${stats.size_candidate_files || 0} size candidates · ${stats.hashed_candidates || 0} hashed · ${stats.confirmed_groups || 0} groups · ~${stats.extra_copies || 0} extra copies`;

    state.dupPages = data.pages || 1;
    state.dupPage = data.page || 1;
    $("dupPager").hidden = state.dupPages <= 1;
    $("dupPageLabel").textContent = `Page ${state.dupPage} / ${state.dupPages}`;
    $("dupPrev").disabled = state.dupPage <= 1;
    $("dupNext").disabled = state.dupPage >= state.dupPages;

    const host = $("dupGroups");
    if (!(data.groups || []).length) {
      host.innerHTML =
        '<div class="empty">No confirmed duplicate groups yet. Start a scan, then refresh. Exact matches appear as fingerprints finish.</div>';
      $("selectDupPage").checked = false;
      updateDupGroupSelCount();
      return;
    }

    host.innerHTML = data.groups
      .map((g, gi) => {
        const groupChecked = state.selectedDupGroups.has(g.hash) ? "checked" : "";
        const groupSelected = groupChecked ? "selected-group" : "";
        const rows = g.items
          .map((item, idx) => {
            const isKeep = item.rel_path === g.keep;
            const preview = item.can_preview
              ? `<img src="${esc(item.thumb)}" alt="" loading="lazy" />`
              : `<div class="ph">${esc(item.ext)}</div>`;
            return `<div class="dup-row" data-path="${esc(item.rel_path)}">
              <input type="checkbox" class="dup-pick" data-path="${esc(item.rel_path)}" ${isKeep ? "" : "checked"} ${isKeep ? "disabled" : ""} />
              ${preview}
              <div>
                <div><strong>${esc(item.name)}</strong>${isKeep ? '<span class="keep-tag">keep</span>' : ""}</div>
                <div class="meta">${esc(item.folder || item.top || "")}</div>
                <div class="meta">${esc(fmtSize(item.size))} · ${esc(fmtDate(item))}</div>
              </div>
              <div class="dup-btns">
                <button type="button" class="ghost dup-open" data-path="${esc(item.rel_path)}" data-original="${esc(item.original)}" data-name="${esc(item.name)}" data-folder="${esc(item.folder)}" data-date="${esc(fmtDate(item))}" data-size="${esc(fmtSize(item.size))}" data-can="${item.can_preview ? "1" : "0"}" data-native="${item.open_native ? "1" : "0"}" data-native-app="${esc(item.open_native_app || "")}">View</button>
                <button type="button" class="ghost dup-folder" data-path="${esc(item.rel_path)}">Folder</button>
              </div>
            </div>`;
          })
          .join("");
        return `<section class="dup-group ${groupSelected}" data-hash="${esc(g.hash)}" data-gi="${gi}">
          <h3>
            <label class="dup-group-check">
              <input type="checkbox" class="dup-group-pick" data-hash="${esc(g.hash)}" ${groupChecked} />
              <span>${g.count} copies · ${esc(fmtSize(g.size))} each</span>
            </label>
            <button type="button" class="ghost dup-del-extras" data-gi="${gi}">Delete extras in group…</button>
          </h3>
          ${rows}
        </section>`;
      })
      .join("");

    // stash groups for delete-extras buttons
    host._groups = data.groups;
    const pageHashes = data.groups.map((g) => g.hash);
    $("selectDupPage").checked =
      pageHashes.length > 0 && pageHashes.every((h) => state.selectedDupGroups.has(h));
    updateDupGroupSelCount();

    host.onchange = (e) => {
      const groupPick = e.target.closest(".dup-group-pick");
      if (!groupPick) return;
      const hash = groupPick.getAttribute("data-hash");
      const section = groupPick.closest(".dup-group");
      const gi = section ? Number(section.getAttribute("data-gi")) : -1;
      const group = host._groups[gi];
      selectDupGroup(hash, group || { hash, items: [], keep: null }, groupPick.checked);
      if (section) section.classList.toggle("selected-group", groupPick.checked);
      $("selectDupPage").checked =
        pageHashes.length > 0 && pageHashes.every((h) => state.selectedDupGroups.has(h));
      updateDupGroupSelCount();
    };

    host.onclick = async (e) => {
      const folderBtn = e.target.closest(".dup-folder");
      if (folderBtn) {
        folderBtn.disabled = true;
        try {
          const res = await fetch("/api/reveal", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: folderBtn.getAttribute("data-path") }),
          });
          const data = await res.json();
          if (!data.ok) {
            $("dupStatus").textContent = data.error || "Could not open folder in Finder";
          } else {
            $("dupStatus").textContent = "Opened in Finder: " + (data.path || "");
          }
        } catch (err) {
          $("dupStatus").textContent = String(err && err.message ? err.message : err);
        } finally {
          folderBtn.disabled = false;
        }
        return;
      }
      const openBtn = e.target.closest(".dup-open");
      if (openBtn) {
        if (openBtn.getAttribute("data-native") === "1") {
          const appLabel = openBtn.getAttribute("data-native-app") || "app";
          openBtn.disabled = true;
          try {
            const res = await fetch("/api/open-native", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ path: openBtn.getAttribute("data-path") }),
            });
            const data = await res.json();
            if (!data.ok) {
              $("dupStatus").textContent = data.error || ("Could not open in " + appLabel);
            } else {
              $("dupStatus").textContent =
                "Opened in " + (data.label || appLabel) + ": " + (data.path || "");
            }
          } catch (err) {
            $("dupStatus").textContent = String(err && err.message ? err.message : err);
          } finally {
            openBtn.disabled = false;
          }
          return;
        }
        openLightbox(openBtn);
        return;
      }
      const delBtn = e.target.closest(".dup-del-extras");
      if (delBtn) {
        const group = host._groups[Number(delBtn.getAttribute("data-gi"))];
        if (!group) return;
        const extras = group.items
          .filter((i) => i.rel_path !== group.keep)
          .map((i) => i.rel_path);
        if (!extras.length) return;
        openDeleteModal(extras);
        return;
      }
    };
  }

  function showView(view) {
    state.view = view;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.getAttribute("data-view") === view);
    });
    $("view-setup").hidden = view !== "setup";
    $("view-browse").hidden = view !== "browse";
    $("view-duplicates").hidden = view !== "duplicates";
    if (view === "duplicates") loadDuplicates();
    if (view === "setup") loadSetup();
  }

  async function loadSetup() {
    const res = await fetch("/api/setup");
    const data = await res.json();
    state.setup = data;
    $("setupRoot").value = data.photos_root || "";
    $("chooseFolder").hidden = !data.can_choose_folder;
    let line = "";
    if (!data.root_exists) {
      line = "No valid photo folder yet. Enter a path or choose a folder, then save.";
    } else if (!data.index_exists || !(data.indexed_files > 0)) {
      line = `Folder ready: ${data.photos_root}. Next: build the index.`;
    } else {
      line = `Using ${data.photos_root} · ${data.indexed_files} indexed files`;
    }
    if (data.indexing || (data.index_progress && data.index_progress.status === "running")) {
      const p = data.index_progress || {};
      line += ` · indexing ${p.upserted || 0}…`;
    }
    $("setupStatus").textContent = line;
    $("setupHint").textContent =
      "Config is stored in data/config.json on this computer. The photo folder itself is never modified except when you delete or empty trash.";
    return data;
  }

  // Tabs
  $("tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-view]");
    if (!btn) return;
    showView(btn.getAttribute("data-view"));
  });

  $("saveSetup").addEventListener("click", async () => {
    const root = $("setupRoot").value.trim();
    if (!root) {
      $("setupStatus").textContent = "Enter a folder path first.";
      return;
    }
    $("saveSetup").disabled = true;
    try {
      const res = await fetch("/api/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photos_root: root }),
      });
      const data = await res.json();
      if (!data.ok) {
        $("setupStatus").textContent = data.error || "Could not save folder";
        return;
      }
      await loadSetup();
      $("setupStatus").textContent = "Saved. You can build the index now.";
    } catch (err) {
      $("setupStatus").textContent = String(err && err.message ? err.message : err);
    } finally {
      $("saveSetup").disabled = false;
    }
  });

  $("chooseFolder").addEventListener("click", async () => {
    $("chooseFolder").disabled = true;
    $("setupStatus").textContent = "Waiting for folder picker…";
    try {
      const res = await fetch("/api/choose-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (data.cancelled) {
        $("setupStatus").textContent = "Folder picker cancelled.";
        return;
      }
      if (!data.ok) {
        $("setupStatus").textContent = data.error || "Could not choose folder";
        return;
      }
      $("setupRoot").value = data.photos_root || "";
      await loadSetup();
      $("setupStatus").textContent = "Saved folder: " + (data.photos_root || "");
    } catch (err) {
      $("setupStatus").textContent = String(err && err.message ? err.message : err);
    } finally {
      $("chooseFolder").disabled = false;
    }
  });

  $("startIndex").addEventListener("click", async () => {
    $("startIndex").disabled = true;
    $("setupStatus").textContent = "Starting index…";
    try {
      const res = await fetch("/api/index/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!data.ok) {
        $("setupStatus").textContent = data.error || "Could not start index";
        return;
      }
      $("setupStatus").textContent = "Indexing started. Browse will fill in as files are found.";
      const poll = setInterval(async () => {
        const s = await loadSetup();
        if (!s.indexing && s.index_progress && s.index_progress.status !== "running") {
          clearInterval(poll);
          await loadMeta();
          await refreshFolders();
          await search();
        }
      }, 3000);
    } catch (err) {
      $("setupStatus").textContent = String(err && err.message ? err.message : err);
    } finally {
      $("startIndex").disabled = false;
    }
  });

  $("form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.q = $("q").value.trim();
    state.kind = $("kind").value;
    state.page = 1;
    search();
  });

  $("clear").addEventListener("click", () => {
    state.q = "";
    state.year = "";
    state.top = "";
    state.folder = "";
    state.kind = "image";
    state.page = 1;
    state.selected.clear();
    $("q").value = "";
    $("kind").value = "image";
    refreshFolders();
    loadMeta();
    search();
  });

  $("prev").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      search();
      window.scrollTo(0, 0);
    }
  });
  $("next").addEventListener("click", () => {
    if (state.page < state.pages) {
      state.page += 1;
      search();
      window.scrollTo(0, 0);
    }
  });

  $("selectPage").addEventListener("change", () => {
    const cards = [...document.querySelectorAll("#grid .card")];
    cards.forEach((card) => {
      const path = card.getAttribute("data-path");
      const pick = card.querySelector(".pick");
      if ($("selectPage").checked) {
        state.selected.add(path);
        if (pick) pick.checked = true;
        card.classList.add("selected");
      } else {
        state.selected.delete(path);
        if (pick) pick.checked = false;
        card.classList.remove("selected");
      }
    });
    updateSelCount();
  });

  $("grid").addEventListener("click", (e) => {
    if (e.target.classList.contains("pick")) {
      const path = e.target.getAttribute("data-path");
      const card = e.target.closest(".card");
      if (e.target.checked) {
        state.selected.add(path);
        card.classList.add("selected");
      } else {
        state.selected.delete(path);
        card.classList.remove("selected");
      }
      updateSelCount();
      e.stopPropagation();
      return;
    }
    const card = e.target.closest(".card");
    if (card) openLightbox(card);
  });

  $("deleteSelected").addEventListener("click", () => {
    openDeleteModal([...state.selected]);
  });

  $("lbDelete").addEventListener("click", () => {
    if (state.lbPath) openDeleteModal([state.lbPath]);
  });

  document.querySelectorAll('input[name="delMode"]').forEach((el) => {
    el.addEventListener("change", () => {
      const mode = document.querySelector('input[name="delMode"]:checked').value;
      $("deletePhrase").hidden = mode !== "permanent";
    });
  });
  $("confirmDelete").addEventListener("click", confirmDelete);
  $("cancelDelete").addEventListener("click", () => {
    $("deleteModal").hidden = true;
  });

  $("emptyTrash").addEventListener("click", () => {
    const t = state.trash || {};
    $("emptyTrashSummary").textContent =
      `Permanently delete ${t.files || 0} file${(t.files || 0) === 1 ? "" : "s"} ` +
      `(${fmtBytes(t.bytes || 0)}) inside Photos/.explorer-trash/. This cannot be undone.`;
    $("emptyTrashPhrase").value = "";
    $("emptyTrashError").textContent = "";
    $("emptyTrashModal").hidden = false;
  });

  $("cancelEmptyTrash").addEventListener("click", () => {
    $("emptyTrashModal").hidden = true;
  });

  $("confirmEmptyTrash").addEventListener("click", async () => {
    const phrase = $("emptyTrashPhrase").value.trim();
    if (phrase !== "EMPTY") {
      $("emptyTrashError").textContent = 'Type EMPTY exactly to permanently empty the trash.';
      return;
    }
    $("confirmEmptyTrash").disabled = true;
    $("emptyTrashError").textContent = "Emptying trash… this can take a while over SMB.";
    try {
      const res = await fetch("/api/trash/empty", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true, confirm_phrase: "EMPTY" }),
      });
      const data = await res.json();
      if (!data.ok) {
        $("emptyTrashError").textContent =
          (data.error || "Failed") +
          (data.errors && data.errors.length ? " · " + data.errors.slice(0, 3).join("; ") : "");
        $("confirmEmptyTrash").disabled = false;
        return;
      }
      $("emptyTrashModal").hidden = true;
      $("status").textContent =
        `Emptied trash · removed ${data.removed_files || 0} files (${fmtBytes(data.removed_bytes || 0)})`;
      await loadMeta();
    } catch (err) {
      $("emptyTrashError").textContent = String(err && err.message ? err.message : err);
    } finally {
      $("confirmEmptyTrash").disabled = false;
    }
  });

  $("lbClose").addEventListener("click", () => $("lightbox").classList.remove("open"));
  $("lightbox").addEventListener("click", (e) => {
    if (e.target.id === "lightbox") $("lightbox").classList.remove("open");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      $("lightbox").classList.remove("open");
      $("deleteModal").hidden = true;
      $("emptyTrashModal").hidden = true;
    }
  });

  $("startDupScan").addEventListener("click", async () => {
    $("startDupScan").disabled = true;
    $("dupStatus").textContent = "Starting duplicate scan…";
    try {
      const res = await fetch("/api/duplicates/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const data = await res.json();
      if (!data.ok && data.error) {
        $("dupStatus").textContent = data.error;
      } else {
        $("dupStatus").textContent = "Scan running on your Mac (hashes same-size candidates over SMB). Refresh periodically.";
      }
    } catch (err) {
      $("dupStatus").textContent = String(err);
    } finally {
      $("startDupScan").disabled = false;
      setTimeout(loadDuplicates, 1500);
    }
  });

  $("refreshDups").addEventListener("click", () => loadDuplicates());

  $("selectDupPage").addEventListener("change", () => {
    const host = $("dupGroups");
    const groups = host._groups || [];
    const on = $("selectDupPage").checked;
    groups.forEach((g) => {
      const section = host.querySelector('.dup-group[data-hash="' + g.hash.replace(/"/g, '\\"') + '"]');
      const pick = section && section.querySelector(".dup-group-pick");
      selectDupGroup(g.hash, g, on);
      if (pick) pick.checked = on;
      if (section) section.classList.toggle("selected-group", on);
    });
    updateDupGroupSelCount();
  });

  $("deleteSelectedGroups").addEventListener("click", () => {
    const extras = [];
    const usedHashes = [];
    state.selectedDupGroupExtras.forEach((paths, hash) => {
      if (!state.selectedDupGroups.has(hash)) return;
      usedHashes.push(hash);
      extras.push(...paths);
    });
    // Unique paths in case of odd overlap
    const unique = [...new Set(extras)];
    if (!unique.length) {
      $("dupStatus").textContent = "No extras to delete in the selected groups.";
      return;
    }
    state._pendingDupGroupHashes = usedHashes.slice();
    openDeleteModal(unique);
  });

  $("dupPrev").addEventListener("click", () => {
    if (state.dupPage > 1) {
      state.dupPage -= 1;
      loadDuplicates();
    }
  });
  $("dupNext").addEventListener("click", () => {
    if (state.dupPage < state.dupPages) {
      state.dupPage += 1;
      loadDuplicates();
    }
  });

  Promise.all([loadSetup(), loadMeta().catch(() => null), refreshFolders().catch(() => null)])
    .then(async ([setup]) => {
      if (!setup || !setup.root_exists) {
        showView("setup");
        $("status").textContent = "Choose a photo folder in Setup to get started.";
        return;
      }
      if (!setup.index_exists || !(setup.indexed_files > 0)) {
        showView("setup");
        $("status").textContent = "Folder is set. Build the index from Setup.";
        return;
      }
      showView("browse");
      await search();
    })
    .catch((err) => {
      $("status").textContent =
        "Could not load explorer: " + (err && err.message ? err.message : err);
      showView("setup");
    });
})();
