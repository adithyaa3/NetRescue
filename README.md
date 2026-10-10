# NetRescue — The Internet Goes Down

> **An Intelligent Network Resilience, Proactive Protection & Disaster Recovery System**  
> *Developed for CommitCon Hackathon 2026*

---

## 1. Team Name and Team Members

**Team Name:** The Syndicate

| Team Member | Roll Number |
|---|---|
| **Mikhul JSR** | 24PT15 |
| **Adarsh S** | 24PW01 |
| **Adithya B** | 24PW02 |
| **Vishwajith** | 24PW39 |

---

## 2. Problem Statement

### **Title: The Internet Goes Down**

Modern institutional and enterprise networks face critical availability challenges during catastrophic physical link cuts, regional disasters, switch power outages, and sudden congestion spikes. When infrastructure fails:
- Critical life-safety alarms and core authentication channels compete blindly against bulk bandwidth flows (e.g., streaming and file transfers).
- Standard shortest-path protocols (such as baseline Dijkstra) suffer from convergence delays, re-route traffic onto already saturated alternate corridors, or drop mission-critical traffic while attempting global recalculation after the failure has already occurred.
- Network operators lack precomputed guarantees and real-time visibility into the exact consequences of cascading multi-link failures and flapping links.

**The Objective:**  
Develop a network simulation, proactive protection, and intelligent rerouting system that immediately responds to physical link failures, maintains essential communication with verifiable guarantees, prioritizes traffic classes fairly, and efficiently utilizes remaining network capacity without inducing congestion collapses.

---

## 3. Brief Description of the Solution

**NetRescue** is an interactive, bandwidth-demand network resilience simulator and controller that combines **proactive mathematical guarantees** with **real-time heuristic routing**. 

Rather than reacting only after disaster strikes, NetRescue solves a proactive linear program (LP) to compute strict protection guarantees and bandwidth reservations for high-priority flows *before* any link fails, enabling instantaneous, solver-free reflex failover.

### Key Functional Capabilities

```
                  ┌────────────────────────────────────────────────────────┐
                  │                 NetRescue Architecture                  │
                  └────────────────────────────────────────────────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         ▼                                    ▼                                    ▼
┌──────────────────┐               ┌───────────────────────┐             ┌───────────────────┐
│ Topology & Flows │               │ Proactive Protection  │             │ Real-Time Routing │
├──────────────────┤               ├───────────────────────┤             ├───────────────────┤
│ • HTML5 Canvas   │               │ • LP Proactive Plan   │             │ • Reflex Recovery │
│ • 9 Presets      │               │ • Independent Checker │             │ • Reactive LP Opt │
│ • P1-P5 Demands  │               │ • Guaranteed Floors   │             │ • PACAR Heuristic │
│ • Drag & Edit    │               │ • Single-Failure Cov. │             │ • Baseline Dijk.  │
└──────────────────┘               └───────────────────────┘             └───────────────────┘
         │                                    │                                    │
         └────────────────────────────────────┼────────────────────────────────────┘
                                              ▼
                               ┌─────────────────────────────┐
                               │ Simulation & Observability  │
                               ├─────────────────────────────┤
                               │ • Independent Validator     │
                               │ • Flap Damping Guard        │
                               │ • What-If Sandbox Analyzer  │
                               │ • Comprehensive Audit Logs  │
                               └─────────────────────────────┘
```

1. **Network Topology Generation & Visualization:**
   - Interactive HTML5 Canvas graph rendering nodes (Gateways, Routers, Switches, Servers, Endpoints) and bidirectional physical links.
   - Live manipulation: drag-and-drop node placement, link creation/editing (capacity in Mbps, latency in ms), and inspection.
   - Built-in scenarios: Normal Campus Network, Congested Bottleneck Network, Earthquake/Disaster Ring, Data Center Mesh, and the verified Protected Demo preset.

2. **Traffic Demand Management & Prioritization:**
   - Granular multi-class traffic prioritization across 5 strict tiers:
     - **P1 — Emergency** (life safety, disaster alerts)
     - **P2 — Essential Infrastructure** (authentication, directory services, DNS)
     - **P3 — Academic / Enterprise Portals**
     - **P4 — General Internet / Media**
     - **P5 — Background Transfers / Backups**
   - Dynamic injection, modification, and emergency preemption of flows while the simulation runs.

3. **Routing Engines & Recovery Mechanics:**
   - **Protection Planner (Proactive LP):** Formulated with SciPy/HiGHS. Computes optimal bandwidth reservations across diverse candidate tunnels before failures occur, producing mathematically provable single-link failure guarantees for P1 and P2 traffic.
   - **Guarantee Checker:** An independent verification pass that stress-tests the declared protection plan against every single-link failure scenario, verifying capacity and non-violation before marking guarantees as verified.
   - **Reflex Recovery:** Instantaneous, solver-free failover. When a covered physical link fails, Reflex immediately activates the precomputed reservations, restoring critical services without waiting for an optimization solver to execute.
   - **Reactive LP Optimizer:** A multi-commodity lexicographic throughput optimizer that maximizes flow delivery from P1 through P5 while respecting verified recovery floors and minimizing latency.
   - **PACAR Heuristic (Priority-Aware Capacity-Adaptive Routing):** Our domain-specific heuristic evaluating multi-objective scoring:
     $$\text{Score} = 0.30 \times \text{Latency} + 0.35 \times \text{Congestion} + 0.10 \times \text{Hops} - 0.25 \times \text{Residual Capacity}$$
     Includes Yen's K-shortest simple paths, capacity-feasible fallback, and bottleneck progressive filling for equal-priority fairness.
   - **Baseline Router:** Classic capacity-constrained shortest-path Dijkstra algorithm used for rigorous algorithmic benchmarking.

4. **Failure Simulation & Flap Damping:**
   - Single-link cuts, batch multi-link cuts, and recovery restoration.
   - **Flap Damping Policy:** Detects unstable flapping links (e.g., 3 failures within 10 controller steps) and enforces a temporary hold-down quarantine to protect overall routing stability.

5. **Independent Allocation Validator:**
   - Reconstructs physical paths, validates flow endpoints, checks capacity constraints, ensures non-negativity, detects routing loops, and guarantees zero traffic is allocated across failed links.

6. **What-If Vulnerability Analyzer:**
   - Sandbox analysis tool allowing operators/judges to simulate hypothetical failures on private clones of the state, evaluating impact and loss without perturbing the operational live network.

7. **Explainable Recovery, Metrics & Event Log:**
   - Real-time tracking of delivered vs. dropped Mbps, link saturation ratios, and critical service availability.
   - Detailed chronological event logs showing the exact reasoning behind every route change, preemption, or capacity drop.

---

## 4. Technologies and Tools Used

### Backend
- **Python 3.12** — Core application runtime
- **FastAPI 0.104.1** — High-performance asynchronous web framework providing REST endpoints and interactive OpenAPI (Swagger) documentation
- **Uvicorn 0.24.0** — ASGI production server
- **Pydantic 2.5.2** — Strict data schema validation, serialization, and typing contracts
- **NetworkX 3.2.1** — Graph representation, path enumeration, and network topological operations

### Algorithms & Optimization
- **SciPy 1.18.1** (`scipy.optimize.linprog`) — Linear programming solver (HiGHS backend) for proactive multi-commodity protection planning and reactive lexicographic allocation
- **NumPy 2.5.3** — High-performance matrix operations and numerical vectorization

### Frontend & Visualization
- **HTML5 Canvas API** — Custom high-performance network visualizer supporting smooth panning, dragging, live link load meters, and failure status rendering
- **Vanilla JavaScript (ES2022)** — Fast, lightweight, zero-dependency dashboard logic with strict state synchronization guards
- **Modern CSS3** — Glassmorphism UI design system with responsive metrics bars and modal dialogs
- **Vite 8.3.0 & TypeScript** — Frontend toolchain located in `frontend/` (development scaffold)

### Development, QA & Testing Tools
- **pytest 7.4.3** — Automated testing framework (153 unit, numerical, and integration tests passed)
- **HTTPX 0.25.2** — Synchronous and asynchronous HTTP testing client for API acceptance verification
- **Playwright Core** — Headless browser end-to-end automation test suite (154 automated browser checks passed)

---

## 5. Instructions for Running or Demonstrating the Project

### Prerequisites
- **Python 3.12** installed (Python 3.12 is recommended for binary compatibility with pinned SciPy/Pydantic packages)
- **Git**
- A modern web browser (Google Chrome, Mozilla Firefox, or Microsoft Edge)

---

### Step 1: Clone the Repository
```bash
git clone https://github.com/your-org/The-Internet-Goes-Down.git
cd The-Internet-Goes-Down
```

---

### Step 2: Set Up Python Virtual Environment
On Linux / macOS:
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

On Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

---

### Step 3: Install Backend Dependencies
```bash
pip install --upgrade pip
pip install -r backend/requirements.txt
```

*(Optional) Run the complete test suite to verify system integrity:*
```bash
pytest backend/tests -v
```
*(Expected: 153 passed tests)*

---

### Step 4: Launch the NetRescue Server
Run Uvicorn from the project root:
```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

---

### Step 5: Open the Interactive Dashboard
Open your web browser and navigate to:
```
http://127.0.0.1:8000/
```
*(Interactive API documentation is also available at `http://127.0.0.1:8000/docs`)*

---

### Step 6: Step-by-Step Demonstration Walkthrough (Judge Runbook)

To experience the full resilience workflow in under 2 minutes:

1. **Load Protected Scenario:**
   - In the top right header, click **🛡️ Load Protected Demo**.
   - Notice the green status: **"All 2 P1/P2 flows are fully protected: 50 of 50 Mbps"**.
   - Inspect the **Protection Guarantees** tab: the LP solver has verified all single-link failure scenarios with the Guarantee Checker (`100% PASS`).

2. **Observe Healthy State:**
   - Switch to the **⚡ Traffic Demands** tab: `P1_DEMO` (10 Mbps) and `D1` (40 Mbps) are fully served on the primary path, while `D2` and `D3` utilize remaining capacity.
   - On the canvas, links `L1` and `L2` operate at 100% utilization.

3. **Simulate a Physical Link Cut:**
   - In the left panel, select link **L3 (A ↔ C)** and click **💥 Fail L3**.
   - **Result:** Link L3 turns dashed grey with a red ✖ marker.
   - Notice the toast notification: **"Reflex used. Validator violations: 0"**.
   - `P1_DEMO` (10 Mbps) and `D1` (40 Mbps) remain **100% delivered** via precomputed protection reservations.
   - Lower-priority demands (`D2`, `D3`) yield bandwidth to protect critical life-safety services.

4. **Verify What-If Sandbox:**
   - Open the **🔬 What-If Analysis** tab.
   - Select link **L1** and click **Evaluate Impact**.
   - Observe the projected impact without altering or breaking the live network.

5. **Compare Algorithms:**
   - Open the **⚖ Algorithm Comparison** tab and click **Run Comparison**.
   - Compare PACAR against the capacity-constrained Baseline Dijkstra under the current failure state.

6. **Restore the Network:**
   - Click **🔧 Restore L3** (or **🔄 Restore All**).
   - Observe the network automatically recover, restoring normal background and academic flows.

---

## 6. Significant External Resources Used

1. **FastAPI & Starlette:**  
   RESTful routing architecture, CORS middleware, and static asset streaming.  
   *Reference:* [FastAPI Documentation](https://fastapi.tiangolo.com/)

2. **NetworkX Library:**  
   Graph modeling, edge attribute manipulation, and implementation basis for Yen's K-Shortest Paths algorithm.  
   *Reference:* [NetworkX Algorithms](https://networkx.org/documentation/stable/reference/algorithms/index.html)

3. **SciPy Optimize (HiGHS Linear Programming Solver):**  
   Simplex and interior-point linear programming solver utilized for the multi-commodity proactive protection formulation.  
   *Reference:* Virtanen, P., et al., *SciPy 1.0: Fundamental Algorithms for Scientific Computing in Python*, Nature Methods (2020).

4. **Algorithmic Literature & References:**  
   - **Yen's Algorithm:** Yen, J. Y., *"Finding the K Shortest Loopless Paths in a Network"*, Management Science (1971).
   - **Max-Min Fairness Concepts:** Bertsekas, D., & Gallager, R., *Data Networks* (Prentice-Hall), progressive bottleneck allocation principles.

---

## 7. Significant Use of AI Tools

In accordance with CommitCon hackathon guidelines, our team transparently acknowledges the use of AI assistance during the development cycle:

- **Claude Code (Anthropic):**  
  Employed for test-suite scaffolding, edge-case vulnerability test design, and refactoring mathematical constraints within the Linear Programming formulation.
- **OpenAI ChatGPT & Codex:**  
  Assisted with initial boilerplate drafting for FastAPI REST schemas, exploratory JavaScript Canvas trigonometry routines, and syntax debugging.
- **Verification & Oversight:**  
  All AI-generated algorithmic routines and mathematical formulations were reviewed, tested, and validated against an automated regression suite of **153 backend tests** and **154 browser end-to-end tests** to confirm correctness, prevent hallucinated outputs, and ensure safety.
