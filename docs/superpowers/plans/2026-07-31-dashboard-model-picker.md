# Dashboard Model Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the native `<datalist>` model inputs in the dashboard provider modal with a searchable custom dropdown backed by `/api/models`.

**Architecture:** Keep the existing `/api/models` backend unchanged. Introduce a small vanilla-JS `ModelPicker` component in `waku/ops/static/js/models.js` and a matching CSS block in `waku/ops/static/style.css`. The modal still fetches models once when opened and renders one picker per field.

**Tech Stack:** Vanilla JS (dashboard uses classic script tags), CSS, Python/pytest for the backend contract test.

---

## File Structure

- `waku/ops/static/js/models.js` — add `renderModelPicker`, `setupModelPickers`, and event handlers; replace datalist inputs in `openProviderModal`.
- `waku/ops/static/style.css` — add `.model-picker*` styles after the `.provmodal` block.
- `evals/deterministic/test_dashboard_routes.py` — add a test that verifies `/api/models` returns the shape the picker depends on.
- `docs/superpowers/specs/2026-07-31-dashboard-model-picker-design.md` — spec (already written).

---

### Task 1: Add ModelPicker CSS

**Files:**
- Modify: `waku/ops/static/style.css:418`

- [ ] **Step 1: Insert CSS block after `.provmodal`**

```css
.model-picker{ position:relative; }
.model-picker-input{ display:flex; align-items:stretch; }
.model-picker-input input{ flex:1; border-radius:8px 0 0 8px; border-right:none; }
.model-picker-toggle{ width:34px; border:1px solid var(--line2); border-radius:0 8px 8px 0; background:var(--bg); color:var(--ink2); cursor:pointer; }
.model-picker-toggle:hover{ background:var(--panel); color:var(--ink); }
.model-picker-list{ display:none; position:absolute; top:100%; left:0; right:0; margin-top:4px; background:var(--panel); border:1px solid var(--line2); border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.15); z-index:10; max-height:240px; overflow:auto; }
.model-picker-list.open{ display:block; }
.model-picker-search{ position:sticky; top:0; padding:8px 10px; border:none; border-bottom:1px solid var(--line2); background:var(--panel); width:100%; outline:none; }
.model-picker-search:focus{ border-bottom-color:var(--accent); }
.model-picker-items{ padding:4px 0; }
.model-picker-item{ padding:7px 12px; cursor:pointer; font-size:12.5px; }
.model-picker-item:hover,.model-picker-item.active{ background:var(--accent-soft); color:var(--accent); }
.model-picker-meta{ padding:6px 12px; font-size:11px; color:var(--ink3); border-top:1px solid var(--line2); }
```

- [ ] **Step 2: Visual check**

Run the dashboard (`make dashboard` or `python -m waku`), open the provider modal, and use DevTools to confirm the `.model-picker` styles are loaded.

- [ ] **Step 3: Commit**

```bash
git add waku/ops/static/style.css
git commit -m "feat(dashboard): add model picker dropdown styles"
```

---

### Task 2: Add ModelPicker JS helpers

**Files:**
- Modify: `waku/ops/static/js/models.js` (insert after `loadModalModels` around line 314)

- [ ] **Step 1: Add helper functions**

```javascript
// Shared model list for the currently open modal.
let _modalModels = [];
let _activeModelPicker = null;
let _outsidePickerListener = false;

// Like esc() but also quotes, for strings that go inside HTML attributes.
function escAttr(s){
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderModelPicker(id, label, value){
  return `<label class="fld">${esc(label)}
    <div class="model-picker" id="${escAttr(id)}-picker">
      <div class="model-picker-input">
        <input type="text" id="${escAttr(id)}" value="${escAttr(value || "")}" autocomplete="off" onfocus="markEditing()" onclick="event.stopPropagation()">
        <button type="button" class="model-picker-toggle" onclick="toggleModelPicker('${escAttr(id)}'); event.stopPropagation();" aria-label="toggle models">▾</button>
      </div>
      <div class="model-picker-list" id="${escAttr(id)}-list">
        <input type="text" class="model-picker-search" id="${escAttr(id)}-search" placeholder="filter models..." autocomplete="off" oninput="filterModelPicker('${escAttr(id)}')" onfocus="markEditing()" onclick="event.stopPropagation()">
        <div class="model-picker-items" id="${escAttr(id)}-items"></div>
        <div class="model-picker-meta" id="${escAttr(id)}-meta"></div>
      </div>
    </div>
  </label>`;
}

function setupModelPickers(models, provider){
  _modalModels = models || [];
  ["pm-model", "pm-small-model"].forEach(id => {
    const input = document.getElementById(id);
    const itemsBox = document.getElementById(id + "-items");
    if (!input || !itemsBox) return;
    renderModelPickerItems(id, "");
    // Clicks on a model item select it; clicks inside the picker stop propagation
    // so the document listener does not close the list.
    itemsBox.addEventListener("click", e => {
      const item = e.target.closest(".model-picker-item");
      if (!item) return;
      e.stopPropagation();
      selectModelPicker(id, item.dataset.model || "");
    });
  });
  if (!_outsidePickerListener){
    _outsidePickerListener = true;
    document.addEventListener("click", () => closeAllModelPickers());
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeAllModelPickers(); });
  }
}

function toggleModelPicker(id){
  const list = document.getElementById(id + "-list");
  if (!list) return;
  const isOpen = list.classList.contains("open");
  closeAllModelPickers();
  if (!isOpen){
    list.classList.add("open");
    _activeModelPicker = id;
    const search = document.getElementById(id + "-search");
    if (search) search.focus();
  }
}

function closeAllModelPickers(){
  document.querySelectorAll(".model-picker-list.open").forEach(el => el.classList.remove("open"));
  _activeModelPicker = null;
}

function closeModelPicker(id){
  const list = document.getElementById(id + "-list");
  if (list) list.classList.remove("open");
  if (_activeModelPicker === id) _activeModelPicker = null;
}

function filterModelPicker(id){
  const query = (document.getElementById(id + "-search")?.value || "").toLowerCase();
  renderModelPickerItems(id, query);
}

function renderModelPickerItems(id, query){
  const itemsBox = document.getElementById(id + "-items");
  const metaBox = document.getElementById(id + "-meta");
  if (!itemsBox) return;
  const filtered = _modalModels.filter(m => (m.id || "").toLowerCase().includes(query));
  itemsBox.innerHTML = filtered.map(m => `<div class="model-picker-item" data-model="${escAttr(m.id)}">${esc(m.id)}</div>`).join("");
  if (metaBox){
    if (_modalModels.length === 0) metaBox.textContent = "No models loaded — you can still type any model id.";
    else if (filtered.length === 0) metaBox.textContent = `No models match "${esc(query)}".`;
    else metaBox.textContent = "";
  }
}

function selectModelPicker(id, value){
  const input = document.getElementById(id);
  if (input) input.value = value;
  closeModelPicker(id);
}
```

- [ ] **Step 2: Run lint / sanity**

Open the dashboard page and check the browser console for JS syntax errors. There should be none.

- [ ] **Step 3: Commit**

```bash
git add waku/ops/static/js/models.js
git commit -m "feat(dashboard): add model picker component helpers"
```

---

### Task 3: Wire the modal to use ModelPicker and fetch models

**Files:**
- Modify: `waku/ops/static/js/models.js:287-298`

- [ ] **Step 1: Replace the model input block**

Replace:

```javascript
      ${current ? `
      <label class="fld">Main model (runs the loop; needs tool calling) <input id="pm-model" list="model-list" value="${esc(st.model || "")}"></label>
      <label class="fld">Gate / summary model <input id="pm-small-model" list="model-list" value="${esc(st.small_model || "")}"></label>
      <datalist id="model-list"></datalist>` : ""}
```

with:

```javascript
      ${current ? `
      ${renderModelPicker("pm-model", "Main model (runs the loop; needs tool calling)", st.model || "")}
      ${renderModelPicker("pm-small-model", "Gate / summary model", st.small_model || "")}` : ""}
```

- [ ] **Step 2: Update `loadModalModels` to drive the picker**

Replace the existing `loadModalModels` function with:

```javascript
async function loadModalModels(provider){
  let data;
  try { data = await (await fetch("/api/models?provider=" + encodeURIComponent(provider))).json(); }
  catch(e){ data = {models: []}; }
  setupModelPickers(data.models || [], provider);
  const meta = document.getElementById("pm-model-meta");
  if (meta){
    if (!data.listed && data.error) meta.textContent = "Could not load catalog: " + data.error;
    else if (!data.listed) meta.textContent = "Live catalog unavailable — showing defaults.";
  }
}
```

- [ ] **Step 3: Manual verification**

1. Set `WAKU_PROVIDER=deepseek` (or any provider with a key).
2. Open dashboard → Models → click "edit" on the active provider.
3. Click the ▾ next to Main model: a dropdown with model ids appears.
4. Type in the search box: the list filters.
5. Click a model: the input updates.
6. Save and verify the `.env` file has `WAKU_MODEL=<chosen-id>`.

- [ ] **Step 4: Commit**

```bash
git add waku/ops/static/js/models.js
git commit -m "feat(dashboard): wire provider modal to searchable model picker"
```

---

### Task 4: Add backend contract test for `/api/models`

**Files:**
- Modify: `evals/deterministic/test_dashboard_routes.py`

- [ ] **Step 1: Add a shape test**

Insert after `test_the_handlers_behind_the_routes_exist_and_are_callable`:

```python
def test_api_models_returns_picker_contract(monkeypatch):
    """The model picker depends on /api/models returning {models: [{id}, ...], listed}."""
    import io
    import json
    import urllib.request

    from waku.ops import catalog

    monkeypatch.setenv("WAKU_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")
    monkeypatch.delenv("WAKU_MODEL", raising=False)
    monkeypatch.delenv("WAKU_SMALL_MODEL", raising=False)

    def fake_urlopen(req, timeout=10):
        body = io.BytesIO(json.dumps({"data": [{"id": "vendor/model:free"}]}).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: None
        return body

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    catalog._models_cache.clear()

    result = catalog.list_models("openrouter")
    assert result["listed"] is True
    assert isinstance(result["models"], list)
    assert result["models"][0]["id"] == "vendor/model:free"

    catalog._models_cache.clear()
```

- [ ] **Step 2: Run the test**

```bash
pytest evals/deterministic/test_dashboard_routes.py::test_api_models_returns_picker_contract -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add evals/deterministic/test_dashboard_routes.py
git commit -m "test(dashboard): assert /api/models contract for picker"
```

---

### Task 5: Polish and final verification

- [ ] **Step 1: Edge-case manual checks**

| Scenario | Expected |
|----------|----------|
| Provider with no catalog (e.g. `minimax`) | Dropdown shows fallback models; a note says catalog unavailable. |
| Provider with long catalog (e.g. `openrouter`) | Search filters quickly; scrolling works. |
| Click outside dropdown | Dropdown closes, modal stays open. |
| Press Escape | Dropdown closes. |
| Type a model id not in the list | Save still writes that id. |

- [ ] **Step 2: Run existing evals**

```bash
pytest evals/deterministic/test_dashboard_routes.py evals/deterministic/test_models.py evals/deterministic/test_pinned_models.py -v
```

Expected: all PASS.

- [ ] **Step 3: Commit any final tweaks**

```bash
git commit -m "feat(dashboard): searchable model picker in provider modal"
```

---

## Spec Coverage Check

| Spec requirement | Implementing task |
|---|---|
| Replace `<input list>` with searchable dropdown | Task 2 + Task 3 |
| Show full model list on click | Task 2 (`toggleModelPicker`) |
| Real-time filter | Task 2 (`filterModelPicker`) |
| Allow manual entry | Task 2 (`input` remains editable) |
| Loading / error / empty states | Task 2 + Task 3 |
| Only for active provider | Task 3 (keeps `current` guard) |
| Reuse `/api/models` | Task 3 |
| Backend contract test | Task 4 |

## Placeholder Scan

- No TBD / TODO / "implement later".
- No vague "add validation" steps.
- Every code block shows concrete code.
- Every test shows expected output.

## Type / Signature Consistency

- `_modalModels` is always an array of objects with `.id`.
- `renderModelPicker`/`setupModelPickers`/`loadModalModels` all use the same `id` strings: `pm-model`, `pm-small-model`.
- CSS classes match JS selectors: `#${id}-list`, `#${id}-items`, `#${id}-meta`, etc.
