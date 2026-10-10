
    const API_BASE = (window.location.port === "8000" || window.location.port === "") ? "" : "http://localhost:8000";

    // Application state
    let state = {
      nodes: {},       // id -> Node
      links: {},       // id -> Link
      demands: {},     // id -> Demand
      simulationActive: false,
      selectedItem: null, // { type: 'node'|'link', id: string }
      zoom: 1,
      pan: { x: 0, y: 0 },
      isDraggingNode: null,
      dragOffset: { x: 0, y: 0 },
      isPanning: false,
      panStart: { x: 0, y: 0 },
      lastMetrics: null
    };

    // Node Type Icons
    const ICONS = {
      router: "🌐",
      switch: "🔀",
      server: "🖥️",
      gateway: "🚪",
      endpoint: "📱"
    };

    function showToast(text, icon = "ℹ️") {
      const toast = document.getElementById('toast-msg');
      document.getElementById('toast-icon').textContent = icon;
      document.getElementById('toast-text').textContent = text;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3500);
    }

    function openModal(id) { document.getElementById(id).classList.add('active'); }
    function closeModal(id) { document.getElementById(id).classList.remove('active'); }

    // API Helpers
    async function parseApiError(res) {
      try {
        const errJson = await res.json();
        if (Array.isArray(errJson.detail)) {
          return errJson.detail.map(e => `${e.loc ? e.loc.slice(-1)[0] : 'field'}: ${e.msg}`).join(', ');
        } else if (typeof errJson.detail === 'string') {
          return errJson.detail;
        } else if (errJson.detail) {
          return JSON.stringify(errJson.detail);
        }
      } catch (_) {}
      return res.statusText || `HTTP ${res.status}`;
    }

    async function apiGet(path) {
      const res = await fetch(`${API_BASE}${path}`);
      if (!res.ok) throw new Error(await parseApiError(res));
      return await res.json();
    }
    async function apiPost(path, body = null) {
      const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' } };
      if (body) opts.body = JSON.stringify(body);
      const res = await fetch(`${API_BASE}${path}`, opts);
      if (!res.ok) throw new Error(await parseApiError(res));
      return await res.json();
    }
    async function apiPut(path, body) {
      const res = await fetch(`${API_BASE}${path}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!res.ok) throw new Error(await parseApiError(res));
      return await res.json();
    }
    async function apiDelete(path) {
      const res = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(await parseApiError(res));
      return await res.json();
    }

    // Sync from Backend
    async function syncState() {
      try {
        const topData = await apiGet('/topology');
        const demData = await apiGet('/demands');
        
        state.nodes = {};
        (topData.nodes || []).forEach(n => state.nodes[n.id] = n);
        
        state.links = {};
        (topData.links || []).forEach(l => state.links[l.id] = l);

        state.demands = {};
        (demData.demands || []).forEach(d => state.demands[d.id] = d);

        // Fetch metrics if available
        try {
          state.lastMetrics = await apiGet('/metrics');
        } catch (e) {}

        renderCanvas();
        renderDemandsTable();
        renderMetrics();
        renderInspector();
        updateWorkflowSteps();
        loadProtectionPlan();
      } catch (err) {
        console.error("State sync failed:", err);
      }
    }

    // WORKFLOW STEPS UPDATE
    function updateWorkflowSteps() {
      const n = Object.keys(state.nodes).length;
      const l = Object.keys(state.links).length;
      const d = Object.keys(state.demands).length;

      document.querySelectorAll('.wf-step').forEach(s => s.classList.remove('active'));
      if (n === 0 || l === 0) {
        document.getElementById('wf-step-1').classList.add('active');
      } else if (d === 0 && !state.simulationActive) {
        document.getElementById('wf-step-2').classList.add('active');
      } else if (!state.simulationActive) {
        document.getElementById('wf-step-3').classList.add('active');
      } else {
        document.getElementById('wf-step-4').classList.add('active');
      }

      // Empty state guide
      const emptyGuide = document.getElementById('empty-state-guide');
      if (n === 0) {
        emptyGuide.style.display = 'block';
      } else {
        emptyGuide.style.display = 'none';
      }

      // Fail button enabled if simulation active
      const failBtn = document.getElementById('fail-btn');
      if (failBtn) failBtn.disabled = !state.simulationActive;
    }

    // SIMULATION TOGGLE
    async function toggleSimulation() {
      if (!state.simulationActive) {
        // Start simulation
        try {
          await apiPost('/simulate?algorithm=pacar');
          state.simulationActive = true;
          document.getElementById('sim-status').className = 'sim-status-pill active';
          document.getElementById('sim-status-text').textContent = 'Live Simulation Active';
          document.getElementById('sim-btn-icon').textContent = '⏹';
          document.getElementById('sim-btn-text').textContent = 'Stop Simulation';
          document.getElementById('sim-toggle-btn').className = 'btn btn-danger sim-toggle-btn';
          showToast("Simulation Active. Link failures and traffic edits will recalculate live.", "▶");
          await syncState();
        } catch (err) {
          showToast(`Cannot start simulation: ${err.message}`, "⚠️");
        }
      } else {
        // Stop simulation
        state.simulationActive = false;
        document.getElementById('sim-status').className = 'sim-status-pill draft';
        document.getElementById('sim-status-text').textContent = 'Draft Network';
        document.getElementById('sim-btn-icon').textContent = '▶';
        document.getElementById('sim-btn-text').textContent = 'Start Simulation';
        document.getElementById('sim-toggle-btn').className = 'btn btn-primary sim-toggle-btn';
        showToast("Simulation stopped. Network returned to draft mode.", "⏹");
        await syncState();
      }
    }

    // AUTO RE-SIMULATE IF RUNNING
    async function reSimulateIfActive() {
      if (state.simulationActive) {
        try {
          await apiPost('/simulate?algorithm=pacar');
        } catch (e) {
          console.warn("Auto-resimulate failed:", e);
        }
      }
      await syncState();
    }

    // ADD NODE
    function openAddNodeModal() {
      const nodes = state.nodes || {};
      let nextNum = 1;
      while (nodes[`R${nextNum}`]) nextNum++;
      document.getElementById('node-input-id').value = `R${nextNum}`;
      document.getElementById('node-input-name').value = `Node ${nextNum}`;
      document.getElementById('node-input-type').value = "router";
      openModal('modal-add-node');
    }

    async function submitAddNode() {
      const id = document.getElementById('node-input-id').value.trim();
      const name = document.getElementById('node-input-name').value.trim() || id;
      const type = document.getElementById('node-input-type').value;

      if (!id) return showToast("Node ID is required", "⚠️");
      if (state.nodes && state.nodes[id]) {
        return showToast(`Node ID '${id}' already exists! Please choose a unique ID.`, "⚠️");
      }

      // Place node near center with slight offset
      const svg = document.getElementById('network-canvas');
      const rect = svg ? svg.getBoundingClientRect() : { width: 800, height: 600 };
      const count = Object.keys(state.nodes || {}).length;
      const angle = count * 0.8;
      const radius = 80 + count * 25;
      const x = Math.round((rect.width / 2) + Math.cos(angle) * Math.min(radius, 250));
      const y = Math.round((rect.height / 2) + Math.sin(angle) * Math.min(radius, 180));

      try {
        await apiPost('/nodes', { id, name, node_type: type, x, y });
        closeModal('modal-add-node');
        showToast(`Node ${id} created`, "✅");
        await syncState();
        selectItem('node', id);
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    // CONNECT NODES
    function openConnectModal(defaultSrc = null) {
      const nodes = Object.values(state.nodes || {});
      if (nodes.length < 2) return showToast("Create at least 2 nodes first", "⚠️");

      const s = document.getElementById('link-input-src');
      const d = document.getElementById('link-input-dst');
      s.innerHTML = ""; d.innerHTML = "";
      nodes.forEach((n, idx) => {
        s.add(new Option(`${n.id} (${n.name || n.node_type})`, n.id));
        d.add(new Option(`${n.id} (${n.name || n.node_type})`, n.id));
      });
      if (defaultSrc) s.value = defaultSrc;
      if (s.selectedIndex === 0 && nodes.length > 1) d.selectedIndex = 1;
      openModal('modal-connect');
    }

    async function submitConnectNodes() {
      const src = document.getElementById('link-input-src').value;
      const dst = document.getElementById('link-input-dst').value;
      const cap = parseFloat(document.getElementById('link-input-cap').value);
      const lat = parseFloat(document.getElementById('link-input-lat').value);

      if (src === dst) return showToast("Source and destination must be different", "⚠️");

      // Check if parallel link already exists
      const existing = Object.values(state.links || {}).find(l =>
        (l.source === src && l.target === dst) || (l.source === dst && l.target === src)
      );
      if (existing) {
        return showToast(`A link between ${src} and ${dst} already exists (${existing.id})!`, "⚠️");
      }

      // Generate Link ID
      let lid = `L_${src}_${dst}`;
      if (state.links && state.links[lid]) {
        let n = 1;
        while (state.links[`L${n}`]) n++;
        lid = `L${n}`;
      }

      try {
        await apiPost('/links', { id: lid, source: src, target: dst, capacity: cap, latency: lat });
        closeModal('modal-connect');
        showToast(`Connected ${src} ↔ ${dst} (${cap} Mbps)`, "✅");
        await syncState();
        selectItem('link', lid);
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    // ADD / EDIT DEMAND
    function openAddDemandModal(editDemand = null) {
      const nodes = Object.values(state.nodes);
      if (nodes.length < 2) return showToast("Create at least 2 nodes before adding traffic", "⚠️");

      const s = document.getElementById('demand-input-src');
      const d = document.getElementById('demand-input-dst');
      s.innerHTML = ""; d.innerHTML = "";
      nodes.forEach(n => {
        s.add(new Option(`${n.id} (${n.name || n.node_type})`, n.id));
        d.add(new Option(`${n.id} (${n.name || n.node_type})`, n.id));
      });

      if (editDemand) {
        document.getElementById('modal-demand-title').textContent = `✏️ Edit Demand ${editDemand.id}`;
        document.getElementById('demand-edit-id').value = editDemand.id;
        document.getElementById('demand-input-id').value = editDemand.id;
        document.getElementById('demand-input-id').disabled = true;
        s.value = editDemand.source;
        d.value = editDemand.destination;
        document.getElementById('demand-input-cat').value = editDemand.category;
        document.getElementById('demand-input-bw').value = editDemand.bandwidth;
      } else {
        document.getElementById('modal-demand-title').textContent = `⚡ Add Traffic Demand`;
        document.getElementById('demand-edit-id').value = "";
        let nextD = Object.keys(state.demands).length + 1;
        while (state.demands[`D${nextD}`]) nextD++;
        document.getElementById('demand-input-id').value = `D${nextD}`;
        document.getElementById('demand-input-id').disabled = false;
        if (s.selectedIndex === 0 && nodes.length > 1) d.selectedIndex = 1;
        document.getElementById('demand-input-bw').value = 30;
      }
      openModal('modal-demand');
    }

    async function submitDemandForm() {
      const editId = document.getElementById('demand-edit-id').value;
      const id = document.getElementById('demand-input-id').value.trim();
      const src = document.getElementById('demand-input-src').value;
      const dst = document.getElementById('demand-input-dst').value;
      const cat = document.getElementById('demand-input-cat').value;
      const bw = parseFloat(document.getElementById('demand-input-bw').value);

      if (src === dst) return showToast("Source and destination must be different", "⚠️");

      try {
        if (editId) {
          await apiPut(`/demands/${editId}`, { bandwidth: bw, category: cat, source: src, destination: dst });
          showToast(`Demand ${editId} updated`, "✅");
        } else {
          await apiPost('/demands', { id, source: src, destination: dst, bandwidth: bw, category: cat });
          showToast(`Demand ${id} added`, "✅");
        }
        closeModal('modal-demand');
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    async function deleteDemand(id) {
      if (!confirm(`Delete traffic demand ${id}?`)) return;
      try {
        await apiDelete(`/demands/${id}`);
        showToast(`Demand ${id} removed`, "🗑️");
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    // FAIL / RESTORE LINK
    async function toggleLinkFailure(linkId) {
      const link = state.links[linkId];
      if (!link) return;
      const isFailed = link.status === "failed";
      const action = isFailed ? "restore" : "fail";
      try {
        const result = await apiPost(`/links/${linkId}/${action}`);
        const changedRoutes = result.route_changes || 0;
        if (action === "fail") {
          showToast(`Link ${link.source}–${link.target} failed! ${changedRoutes} routes adjusted.`, "💥");
        } else {
          showToast(`Link ${link.source}–${link.target} restored. Routes recovered.`, "🔄");
        }
        await syncState();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    function failSelectedOrRandom() {
      if (state.selectedItem && state.selectedItem.type === 'link') {
        toggleLinkFailure(state.selectedItem.id);
      } else {
        const activeLinks = Object.values(state.links).filter(l => l.status !== 'failed');
        if (!activeLinks.length) return showToast("No active links available to fail", "⚠️");
        toggleLinkFailure(activeLinks[0].id);
      }
    }

    async function restoreAllLinks() {
      const failed = Object.values(state.links).filter(l => l.status === 'failed');
      if (!failed.length) return showToast("All links are already active", "ℹ️");
      for (const l of failed) {
        await apiPost(`/links/${l.id}/restore`);
      }
      showToast("All links restored to normal", "🔄");
      await syncState();
    }

    // PRESET & CLEAR
    async function loadSamplePreset() {
      try {
        await apiPost('/topology/generate', { preset: 'normal_campus' });
        showToast("Loaded Normal Campus Sample Network", "🧪");
        await syncState();
      } catch (err) {
        showToast(`Failed to load preset: ${err.message}`, "❌");
      }
    }

    async function promptClearNetwork() {
      if (!confirm("Are you sure you want to clear the entire network canvas?")) return;
      try {
        await apiPost('/simulation/reset');
        clearSelection();
        showToast("Network canvas cleared", "🗑️");
        await syncState();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    async function exportNetworkJson() {
      window.open(`${API_BASE}/export`, '_blank');
    }

    // CANVAS SELECTION & RENDERING
    function selectItem(type, id) {
      state.selectedItem = { type, id };
      renderCanvas();
      renderInspector();
    }
    function clearSelection() {
      state.selectedItem = null;
      renderCanvas();
      renderInspector();
    }

    // RENDER INSPECTOR
    function renderInspector() {
      const body = document.getElementById('inspector-body');
      const title = document.getElementById('inspector-title');
      if (!body || !title) return;

      const nodes = state.nodes || {};
      const links = state.links || {};
      const demands = state.demands || {};
      const nodeCount = Object.keys(nodes).length;
      const linkCount = Object.keys(links).length;
      const demandCount = Object.keys(demands).length;
      const failedCount = Object.values(links).filter(l => l && l.status === 'failed').length;

      // Safely update basic counters if elements exist in DOM
      const statNodes = document.getElementById('stat-nodes');
      if (statNodes) statNodes.textContent = nodeCount;
      const statLinks = document.getElementById('stat-links');
      if (statLinks) statLinks.textContent = linkCount;
      const statDemands = document.getElementById('stat-demands');
      if (statDemands) statDemands.textContent = demandCount;
      const statFailed = document.getElementById('stat-failed');
      if (statFailed) statFailed.textContent = failedCount;

      if (!state.selectedItem) {
        title.textContent = "Inspector";
        body.innerHTML = `
          <div class="info-card">
            <div style="font-weight:700; margin-bottom:0.4rem; color:var(--cyan); font-size:0.85rem;">Guided Tips</div>
            <p style="font-size:0.78rem; color:var(--text-muted); line-height:1.45;">
              ${nodeCount === 0 ? "Start by adding network nodes using the '+ Add Node' button." :
                linkCount === 0 ? "Connect your nodes using '+ Connect Nodes' to form communication links." :
                !state.simulationActive ? "Add traffic demands below and click 'Start Simulation' to route traffic." :
                "Simulation active! Click any link to fail or restore it and observe dynamic rerouting."}
            </p>
          </div>
          <div class="info-card">
            <div style="font-weight:700; margin-bottom:0.5rem; font-size:0.82rem;">Network Overview</div>
            <div class="info-row"><span class="info-label">Total Nodes:</span><span class="info-val" id="stat-nodes">${nodeCount}</span></div>
            <div class="info-row"><span class="info-label">Total Links:</span><span class="info-val" id="stat-links">${linkCount}</span></div>
            <div class="info-row"><span class="info-label">Active Demands:</span><span class="info-val" id="stat-demands">${demandCount}</span></div>
            <div class="info-row"><span class="info-label">Failed Links:</span><span class="info-val" id="stat-failed" style="color:var(--red);">${failedCount}</span></div>
          </div>
        `;
        return;
      }

      if (state.selectedItem.type === 'node') {
        const node = nodes[state.selectedItem.id];
        if (!node) {
          state.selectedItem = null;
          return renderInspector();
        }
        title.textContent = `Node: ${node.id}`;
        const connectedLinks = Object.values(links).filter(l => l && (l.source === node.id || l.target === node.id));

        body.innerHTML = `
          <div class="info-card">
            <div style="font-size:1.1rem; font-weight:700; margin-bottom:0.4rem; display:flex; align-items:center; gap:0.4rem;">
              <span>${ICONS[node.node_type] || '🌐'}</span> ${node.id}
            </div>
            <div class="info-row"><span class="info-label">Name:</span><span class="info-val">${node.name || node.id}</span></div>
            <div class="info-row"><span class="info-label">Type:</span><span class="info-val">${(node.node_type || 'router').toUpperCase()}</span></div>
            <div class="info-row"><span class="info-label">Connected Links:</span><span class="info-val">${connectedLinks.length}</span></div>
          </div>
          <div style="display:flex; flex-direction:column; gap:0.5rem;">
            <button class="btn btn-secondary" onclick="openConnectModal('${node.id}')">🔗 Connect to Node...</button>
            <button class="btn btn-danger" onclick="deleteNode('${node.id}')">🗑️ Delete Node</button>
          </div>
        `;
      } else if (state.selectedItem.type === 'link') {
        const link = links[state.selectedItem.id];
        if (!link) {
          state.selectedItem = null;
          return renderInspector();
        }
        const isFailed = link.status === "failed";
        title.textContent = `Link: ${link.id}`;

        body.innerHTML = `
          <div class="info-card">
            <div style="font-size:1rem; font-weight:700; margin-bottom:0.4rem; color:var(--cyan);">
              ${link.source} ↔ ${link.target}
            </div>
            <div class="info-row"><span class="info-label">Status:</span>
              <span class="info-val" style="color:${isFailed ? 'var(--red)' : 'var(--green)'};">
                ${isFailed ? '❌ FAILED' : 'ACTIVE'}
              </span>
            </div>
            <div class="info-row"><span class="info-label">Capacity:</span><span class="info-val">${link.capacity} Mbps</span></div>
            <div class="info-row"><span class="info-label">Latency:</span><span class="info-val">${link.latency} ms</span></div>
            <div class="info-row"><span class="info-label">Allocated Load:</span><span class="info-val">${(link.allocated_load || 0).toFixed(1)} Mbps (${(link.utilization || 0).toFixed(0)}%)</span></div>
          </div>

          <div style="display:flex; flex-direction:column; gap:0.5rem;">
            <button class="btn ${isFailed ? 'btn-success' : 'btn-danger'}" onclick="toggleLinkFailure('${link.id}')">
              ${isFailed ? '🔄 Restore Link' : '💥 Fail Link'}
            </button>
            <button class="btn btn-secondary" onclick="editLinkCapacity('${link.id}')">✏️ Edit Capacity</button>
            <button class="btn btn-secondary" onclick="deleteLink('${link.id}')">🗑️ Delete Link</button>
          </div>
        `;
      } else {
        state.selectedItem = null;
        return renderInspector();
      }
    }

    async function deleteNode(id) {
      if (!confirm(`Delete node ${id} and all its connected links?`)) return;
      try {
        await apiDelete(`/nodes/${id}`);
        showToast(`Node ${id} deleted`, "🗑️");
        clearSelection();
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    async function deleteLink(id) {
      if (!confirm(`Delete link ${id}?`)) return;
      try {
        await apiDelete(`/links/${id}`);
        showToast(`Link ${id} deleted`, "🗑️");
        clearSelection();
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    async function editLinkCapacity(id) {
      const link = state.links[id];
      const newCap = prompt("Enter new capacity in Mbps:", link.capacity);
      if (!newCap || isNaN(newCap)) return;
      try {
        await apiPut(`/links/${id}`, { capacity: parseFloat(newCap) });
        showToast(`Link ${id} capacity updated to ${newCap} Mbps`, "✅");
        await reSimulateIfActive();
      } catch (err) {
        showToast(`Error: ${err.message}`, "❌");
      }
    }

    // RENDER SVG GRAPH CANVAS
    function renderCanvas() {
      const linksLayer = document.getElementById('links-layer');
      const flowsLayer = document.getElementById('flows-layer');
      const nodesLayer = document.getElementById('nodes-layer');
      const world = document.getElementById('canvas-world');

      world.setAttribute('transform', `translate(${state.pan.x}, ${state.pan.y}) scale(${state.zoom})`);

      linksLayer.innerHTML = "";
      flowsLayer.innerHTML = "";
      nodesLayer.innerHTML = "";

      // 1. Draw Links
      Object.values(state.links).forEach(link => {
        const src = state.nodes[link.source];
        const dst = state.nodes[link.target];
        if (!src || !dst) return;

        const isSelected = state.selectedItem && state.selectedItem.type === 'link' && state.selectedItem.id === link.id;
        const isFailed = link.status === "failed";
        const util = link.utilization || 0;

        let strokeColor = "#10b981";
        if (util >= 80) strokeColor = "#ef4444";
        else if (util >= 50) strokeColor = "#f59e0b";
        if (isFailed) strokeColor = "#ff0055";

        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("x1", src.x);
        line.setAttribute("y1", src.y);
        line.setAttribute("x2", dst.x);
        line.setAttribute("y2", dst.y);
        line.setAttribute("stroke", strokeColor);
        line.setAttribute("stroke-width", isSelected ? "5" : (Math.max(3, Math.min(link.capacity / 25, 8))));
        
        if (isFailed) {
          line.setAttribute("stroke-dasharray", "8,6");
          line.setAttribute("filter", "url(#glow-red)");
        } else if (isSelected) {
          line.setAttribute("filter", "url(#glow-cyan)");
        }

        line.style.cursor = "pointer";
        line.onclick = (e) => { e.stopPropagation(); selectItem('link', link.id); };
        linksLayer.appendChild(line);

        // Midpoint badge
        const mx = (src.x + dst.x) / 2;
        const my = (src.y + dst.y) / 2;
        const txt = document.createElementNS("http://www.w3.org/2000/svg", "text");
        txt.setAttribute("x", mx);
        txt.setAttribute("y", my - 6);
        txt.setAttribute("text-anchor", "middle");
        txt.setAttribute("fill", isFailed ? "#ff0055" : "rgba(255,255,255,0.75)");
        txt.setAttribute("font-size", "11px");
        txt.setAttribute("font-family", "Fira Code, monospace");
        txt.setAttribute("font-weight", "600");
        txt.textContent = isFailed ? "❌ FAILED" : `${link.capacity}M (${util.toFixed(0)}%)`;
        linksLayer.appendChild(txt);

        // Animated flow particles if active traffic
        if (!isFailed && (link.allocated_load || 0) > 0 && state.simulationActive) {
          const flowLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
          flowLine.setAttribute("x1", src.x);
          flowLine.setAttribute("y1", src.y);
          flowLine.setAttribute("x2", dst.x);
          flowLine.setAttribute("y2", dst.y);
          flowLine.setAttribute("stroke", "#00f0ff");
          flowLine.setAttribute("stroke-width", "3");
          flowLine.setAttribute("stroke-dasharray", "4,12");
          flowLine.setAttribute("class", "flow-active");
          flowsLayer.appendChild(flowLine);
        }
      });

      // 2. Draw Nodes
      Object.values(state.nodes).forEach(node => {
        const isSelected = state.selectedItem && state.selectedItem.type === 'node' && state.selectedItem.id === node.id;
        const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
        g.setAttribute("transform", `translate(${node.x}, ${node.y})`);
        g.style.cursor = "move";

        // Outer glow circle
        const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        circle.setAttribute("r", "20");
        circle.setAttribute("fill", "#0f172a");
        circle.setAttribute("stroke", isSelected ? "#00f0ff" : "rgba(56, 189, 248, 0.6)");
        circle.setAttribute("stroke-width", isSelected ? "3.5" : "2");
        if (isSelected) circle.setAttribute("filter", "url(#glow-cyan)");

        // Icon text
        const icon = document.createElementNS("http://www.w3.org/2000/svg", "text");
        icon.setAttribute("text-anchor", "middle");
        icon.setAttribute("y", "5");
        icon.setAttribute("font-size", "14px");
        icon.textContent = ICONS[node.node_type] || "🌐";

        // Label underneath
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("y", "34");
        label.setAttribute("fill", "#ffffff");
        label.setAttribute("font-size", "11px");
        label.setAttribute("font-weight", "700");
        label.setAttribute("font-family", "Inter, sans-serif");
        label.textContent = node.id;

        g.appendChild(circle);
        g.appendChild(icon);
        g.appendChild(label);

        // Dragging & Clicking handlers
        g.onmousedown = (e) => {
          e.stopPropagation();
          state.isDraggingNode = node.id;
          state.dragOffset = { x: e.clientX - node.x, y: e.clientY - node.y };
          selectItem('node', node.id);
        };

        nodesLayer.appendChild(g);
      });
    }

    // MOUSE PAN & DRAG HANDLERS
    const canvasViewport = document.getElementById('canvas-viewport');
    canvasViewport.onmousedown = (e) => {
      if (e.target.id === 'network-canvas' || e.target.id === 'canvas-world') {
        state.isPanning = true;
        state.panStart = { x: e.clientX - state.pan.x, y: e.clientY - state.pan.y };
        clearSelection();
      }
    };
    window.onmousemove = (e) => {
      if (state.isDraggingNode) {
        const node = state.nodes[state.isDraggingNode];
        if (node) {
          node.x = Math.round(e.clientX - state.dragOffset.x);
          node.y = Math.round(e.clientY - state.dragOffset.y);
          renderCanvas();
        }
      } else if (state.isPanning) {
        state.pan.x = e.clientX - state.panStart.x;
        state.pan.y = e.clientY - state.panStart.y;
        renderCanvas();
      }
    };
    window.onmouseup = async () => {
      if (state.isDraggingNode) {
        const nid = state.isDraggingNode;
        state.isDraggingNode = null;
        // Save node position to backend
        try {
          const n = state.nodes[nid];
          if (n) await apiPut(`/nodes/${nid}`, { x: n.x, y: n.y });
        } catch (e) {}
      }
      state.isPanning = false;
    };
    canvasViewport.onwheel = (e) => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
      state.zoom = Math.max(0.3, Math.min(state.zoom * zoomFactor, 3.0));
      renderCanvas();
    };

    function zoomIn() { state.zoom = Math.min(state.zoom * 1.2, 3.0); renderCanvas(); }
    function zoomOut() { state.zoom = Math.max(state.zoom / 1.2, 0.3); renderCanvas(); }
    function resetView() { state.zoom = 1; state.pan = { x: 0, y: 0 }; renderCanvas(); }
    function fitView() { resetView(); }

    // RENDER DEMANDS TABLE
    function renderDemandsTable() {
      const tbody = document.getElementById('demands-table-body');
      const demands = Object.values(state.demands);
      document.getElementById('badge-demands-count').textContent = `(${demands.length})`;

      if (!demands.length) {
        tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color:var(--text-muted); padding:1rem;">No traffic demands created yet. Click "+ Add Demand" to inject flows.</td></tr>`;
        return;
      }

      tbody.innerHTML = demands.map(d => {
        let statusClass = "status-ready";
        let statusLabel = "Ready";
        if (state.simulationActive) {
          if (d.status === "fully_served") { statusClass = "status-served"; statusLabel = "Fully Served"; }
          else if (d.status === "partially_served") { statusClass = "status-partial"; statusLabel = "Partially Served"; }
          else if (d.status === "unreachable") { statusClass = "status-unserved"; statusLabel = "Unreachable"; }
          else if (d.status === "unserved_congestion") { statusClass = "status-unserved"; statusLabel = "Congested"; }
        }

        const routeStr = (d.route && d.route.length > 1) ? d.route.join(" → ") : (d.route ? d.route[0] : "—");

        return `
          <tr>
            <td style="font-family:'Fira Code'; font-weight:600; color:var(--cyan);">${d.id}</td>
            <td style="font-weight:600;">${d.source} → ${d.destination}</td>
            <td><span class="pill pill-${d.category}">${d.category.toUpperCase()}</span></td>
            <td style="font-family:'Fira Code';">${d.bandwidth} Mbps</td>
            <td style="font-family:'Fira Code'; color:var(--green);">${(d.allocated_bandwidth || 0).toFixed(1)} Mbps</td>
            <td style="font-family:'Fira Code'; color:var(--text-muted);">${(d.unserved_bandwidth || 0).toFixed(1)} Mbps</td>
            <td class="${statusClass}">${statusLabel}</td>
            <td style="font-family:'Fira Code'; font-size:0.75rem; color:var(--cyan);">${routeStr}</td>
            <td style="text-align:right;">
              <button class="btn btn-secondary" style="padding:0.2rem 0.45rem; font-size:0.7rem;" onclick="openAddDemandModal(state.demands['${d.id}'])">✏️</button>
              <button class="btn btn-secondary" style="padding:0.2rem 0.45rem; font-size:0.7rem; color:var(--red);" onclick="deleteDemand('${d.id}')">🗑️</button>
            </td>
          </tr>
        `;
      }).join('');
    }

    // RENDER METRICS
    function renderMetrics() {
      const m = state.lastMetrics;
      if (!m) return;
      const req = m.total_requested || 0;
      const del = m.total_delivered || 0;
      const pct = req > 0 ? (del / req * 100) : 100;
      document.getElementById('metric-deliv-rate').textContent = `${pct.toFixed(1)}%`;
      document.getElementById('metric-deliv-bw').textContent = `${del.toFixed(0)} / ${req.toFixed(0)} Mbps`;
      document.getElementById('metric-crit-rate').textContent = `${(m.critical_delivery_pct || 100).toFixed(1)}%`;
      const congested = (m.link_utilizations || []).filter(l => (l.utilization || 0) >= 80).length;
      document.getElementById('metric-congested').textContent = congested;
      document.getElementById('metric-latency').textContent = `${(m.avg_latency || 0).toFixed(1)} ms`;
      document.getElementById('metric-failed-count').textContent = m.failed_links || 0;
    }

    // TRAY TABS
    function switchTrayTab(tabId) {
      document.querySelectorAll('.tray-tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tray-content').forEach(c => c.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById(tabId).classList.add('active');

      if (tabId === 'tray-protection') loadProtectionPlan();
      if (tabId === 'tray-whatif') populateWhatIfLinks();
      if (tabId === 'tray-events') loadEvents();
    }

    function toggleTrayCollapse() {
      const ws = document.getElementById('app-workspace');
      const btn = document.getElementById('tray-collapse-btn');
      const tray = document.getElementById('bottom-tray');
      if (ws.classList.contains('tray-collapsed')) {
        ws.classList.remove('tray-collapsed');
        tray.style.height = "260px";
        btn.textContent = "Hide Tray ▼";
      } else {
        ws.classList.add('tray-collapsed');
        tray.style.height = "38px";
        btn.textContent = "Show Tray ▲";
      }
    }

    // PROTECTION GUARANTEES LOADER
    async function loadProtectionPlan() {
      try {
        const data = await apiGet('/protection/plan');
        const badge = document.getElementById('badge-protection-status');
        const sumBox = document.getElementById('protection-plan-summary-box');
        const tbody = document.getElementById('protection-table-body');
        if (!badge || !sumBox || !tbody) return;

        if (!data.active) {
          badge.textContent = "(None)";
          badge.style.color = "var(--text-muted)";
          sumBox.innerHTML = `<div style="font-size:0.8rem; color:var(--text-muted); padding:0.5rem; background:var(--bg-card); border-radius:6px;">${data.message}</div>`;
          tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:1rem;">Add P1 Emergency or P2 Essential traffic demands to generate verified protection guarantees.</td></tr>`;
          return;
        }

        const isVerif = data.verified;
        badge.textContent = isVerif ? "(Verified)" : "(Unverified)";
        badge.style.color = isVerif ? "var(--green)" : "var(--amber)";

        sumBox.innerHTML = `
          <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:0.6rem; font-size:0.8rem;">
            <div class="info-card">
              <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">Covered Scenarios</div>
              <div style="font-size:1.1rem; font-weight:700; color:var(--cyan);">${data.covered_scenarios_count}</div>
              <div style="font-size:0.7rem; color:var(--text-dim);">All Single-Link + Baseline</div>
            </div>
            <div class="info-card">
              <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">Reflex Engine State</div>
              <div style="font-size:1.1rem; font-weight:700; color:${isVerif ? 'var(--green)' : 'var(--amber)'};">${isVerif ? 'ACTIVE (O(1) Instant)' : 'Pending'}</div>
              <div style="font-size:0.7rem; color:var(--text-dim);">${isVerif ? 'Guaranteed Fast Failover' : 'Solver Suboptimal'}</div>
            </div>
            <div class="info-card">
              <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">LP Solver Status</div>
              <div style="font-size:1.1rem; font-weight:700; color:var(--text);">${data.solver_status.toUpperCase()}</div>
              <div style="font-size:0.7rem; color:var(--text-dim);">${data.runtime_ms} ms (HiGHS LP)</div>
            </div>
            <div class="info-card">
              <div style="font-size:0.7rem; color:var(--text-muted); text-transform:uppercase;">Guarantee Checker</div>
              <div style="font-size:1.1rem; font-weight:700; color:${isVerif ? 'var(--green)' : 'var(--red)'};">${isVerif ? '100% PASS' : 'FAILED'}</div>
              <div style="font-size:0.7rem; color:var(--text-dim);">Independent Scenario Check</div>
            </div>
          </div>
        `;

        const flows = data.critical_flows || [];
        if (!flows.length) {
          tbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:1rem;">No critical flows configured.</td></tr>`;
          return;
        }

        tbody.innerHTML = flows.map(f => {
          const statusColor = f.status === "Full" ? "var(--green)" : (f.status === "Partial" ? "var(--amber)" : "var(--red)");
          const pillClass = f.priority === 1 ? "emergency" : "essential";
          return `
            <tr>
              <td style="font-family:'Fira Code'; font-weight:600; color:var(--cyan);">${f.flow_id}</td>
              <td><span class="pill pill-${pillClass}">P${f.priority}</span></td>
              <td style="font-family:'Fira Code';">${f.requested_mbps} Mbps</td>
              <td style="font-family:'Fira Code'; font-weight:700; color:var(--green);">${f.guaranteed_mbps} Mbps</td>
              <td style="font-family:'Fira Code'; color:var(--text);">${f.delivered_mbps} Mbps</td>
              <td style="font-family:'Fira Code'; font-weight:600;">${f.protection_pct}%</td>
              <td style="font-weight:700; color:${statusColor};">${f.status === 'Full' ? '🛡️ FULL' : (f.status === 'Partial' ? '⚠️ PARTIAL' : '❌ NONE')}</td>
            </tr>
          `;
        }).join('');
      } catch (err) {
        console.warn("Failed to load protection plan:", err);
      }
    }

    async function recomputeProtectionPlan() {
      try {
        showToast("Recomputing proactive protection plan...", "🛡️");
        await apiPost('/protection/replan');
        showToast("Protection plan computed & independently verified.", "✅");
        await loadProtectionPlan();
      } catch (err) {
        showToast(`Replan failed: ${err.message}`, "❌");
      }
    }

    // ALGORITHM COMPARISON
    async function runComparison() {
      const box = document.getElementById('compare-results-box');
      box.innerHTML = `<span style="color:var(--text-muted);">Running 3-algorithm comparison (PACAR vs Reactive LP vs Baseline Dijkstra)...</span>`;
      try {
        const data = await apiPost('/compare');
        const p = data.pacar || {};
        const lp = data.reactive_lp || {};
        const b = data.baseline || {};

        const pDel = p.total_requested > 0 ? (p.total_delivered / p.total_requested * 100) : 100;
        const lpDel = lp.total_requested > 0 ? (lp.total_delivered / lp.total_requested * 100) : 100;
        const bDel = b.total_requested > 0 ? (b.total_delivered / b.total_requested * 100) : 100;

        box.innerHTML = `
          <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:0.75rem; margin-top:0.4rem;">
            <div class="info-card" style="border-color:var(--cyan);">
              <h4 style="color:var(--cyan); margin-bottom:0.4rem;">🚀 PACAR Heuristic</h4>
              <div class="info-row"><span class="info-label">Delivery Rate:</span><span class="info-val">${pDel.toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Delivered:</span><span class="info-val">${(p.total_delivered || 0).toFixed(0)} Mbps</span></div>
              <div class="info-row"><span class="info-label">Critical QoS:</span><span class="info-val">${(p.critical_delivery_pct || 100).toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Max Link Load:</span><span class="info-val">${(p.max_link_utilization || 0).toFixed(0)}%</span></div>
              <div class="info-row"><span class="info-label">Solve Time:</span><span class="info-val">${p.computation_time_ms || 0} ms</span></div>
            </div>
            <div class="info-card" style="border-color:var(--green);">
              <h4 style="color:var(--green); margin-bottom:0.4rem;">⚡ Reactive Multicommodity LP</h4>
              <div class="info-row"><span class="info-label">Delivery Rate:</span><span class="info-val">${lpDel.toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Delivered:</span><span class="info-val">${(lp.total_delivered || 0).toFixed(0)} Mbps</span></div>
              <div class="info-row"><span class="info-label">Critical QoS:</span><span class="info-val">${(lp.critical_delivery_pct || 100).toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Max Link Load:</span><span class="info-val">${(lp.max_link_utilization || 0).toFixed(0)}%</span></div>
              <div class="info-row"><span class="info-label">Solve Time:</span><span class="info-val">${lp.computation_time_ms || 0} ms</span></div>
            </div>
            <div class="info-card" style="border-color:var(--border);">
              <h4 style="color:var(--text-muted); margin-bottom:0.4rem;">🌐 Baseline Dijkstra</h4>
              <div class="info-row"><span class="info-label">Delivery Rate:</span><span class="info-val">${bDel.toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Delivered:</span><span class="info-val">${(b.total_delivered || 0).toFixed(0)} Mbps</span></div>
              <div class="info-row"><span class="info-label">Critical QoS:</span><span class="info-val">${(b.critical_delivery_pct || 100).toFixed(1)}%</span></div>
              <div class="info-row"><span class="info-label">Max Link Load:</span><span class="info-val">${(b.max_link_utilization || 0).toFixed(0)}%</span></div>
              <div class="info-row"><span class="info-label">Solve Time:</span><span class="info-val">${b.computation_time_ms || 0} ms</span></div>
            </div>
          </div>
        `;
      } catch (err) {
        box.innerHTML = `<span style="color:var(--red);">Comparison failed: ${err.message}</span>`;
      }
    }

    // WHAT-IF ANALYSIS
    function populateWhatIfLinks() {
      const container = document.getElementById('whatif-link-selectors');
      const links = Object.values(state.links);
      if (!links.length) {
        container.innerHTML = `<span style="color:var(--text-muted);">No links in network.</span>`;
        return;
      }
      container.innerHTML = links.map(l => `
        <label style="background:var(--bg-card); border:1px solid var(--border); padding:0.3rem 0.6rem; border-radius:6px; font-size:0.75rem; display:inline-flex; align-items:center; gap:0.35rem; cursor:pointer;">
          <input type="checkbox" value="${l.id}" class="whatif-checkbox">
          <span>${l.id} (${l.source}↔${l.target})</span>
        </label>
      `).join('');
    }

    async function executeWhatIf() {
      const checked = Array.from(document.querySelectorAll('.whatif-checkbox:checked')).map(c => c.value);
      if (!checked.length) return showToast("Select at least one link to simulate failure", "⚠️");
      const box = document.getElementById('whatif-results-box');
      box.innerHTML = `<span style="color:var(--text-muted);">Simulating snapshot disaster...</span>`;
      try {
        const res = await apiPost('/analyze/what-if', { link_ids: checked });
        box.innerHTML = `
          <div class="info-card" style="margin-top:0.5rem;">
            <div style="font-weight:700; color:var(--amber); margin-bottom:0.35rem;">What-If Disaster Assessment (${checked.join(', ')})</div>
            <div class="info-row"><span class="info-label">Delivered Before:</span><span class="info-val">${res.total_delivered_before.toFixed(0)} Mbps</span></div>
            <div class="info-row"><span class="info-label">Delivered After:</span><span class="info-val">${res.total_delivered_after.toFixed(0)} Mbps</span></div>
            <div class="info-row"><span class="info-label">Bandwidth Lost:</span><span class="info-val" style="color:var(--red);">${res.lost_bandwidth.toFixed(0)} Mbps</span></div>
            <div class="info-row"><span class="info-label">Flows Disrupted:</span><span class="info-val">${res.disrupted_demands_count}</span></div>
          </div>
        `;
      } catch (err) {
        box.innerHTML = `<span style="color:var(--red);">What-if simulation failed: ${err.message}</span>`;
      }
    }

    // EVENT LOG
    async function loadEvents() {
      const container = document.getElementById('event-log-list');
      try {
        const data = await apiGet('/events?limit=30');
        const events = Array.isArray(data) ? data : (data.events || []);
        if (!events.length) {
          container.innerHTML = `<div style="color:var(--text-muted);">No events recorded yet.</div>`;
          return;
        }
        container.innerHTML = events.slice().reverse().map(e => `
          <div style="padding:0.3rem 0.5rem; background:var(--bg-card); border-left:3px solid var(--cyan); border-radius:0 4px 4px 0;">
            <span style="font-family:'Fira Code'; color:var(--text-dim);">[Step ${e.step}]</span>
            <strong>${e.event_type}:</strong> ${e.description}
          </div>
        `).join('');
      } catch (e) {}
    }

    // INITIALIZATION
    window.addEventListener('load', async () => {
      await syncState();
    });
  