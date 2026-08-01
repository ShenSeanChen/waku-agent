# Dashboard Provider Modal — Model Searchable Dropdown

## Context
The Waku dashboard has a per-provider edit modal (opened from the Models grid). For the currently active provider it exposes two free-text model fields:

- **Main model** — the model that runs the agent loop (needs tool calling).
- **Gate / summary model** — the cheap model used by the retrieval gate and consolidation summarizer.

Today these fields are plain `<input list="model-list">` with a browser-native `<datalist>`. Users expect a real dropdown that shows the live model catalog, but the native datalist only offers filtered suggestions as they type and does not present the full list on click. Some providers also fail to list models, leaving the dropdown empty-looking.

## Goal
Replace the datalist-based inputs with a **searchable custom dropdown** that:

1. Shows the full model list when the user clicks the ▾ button.
2. Lets the user type to filter the list.
3. Still allows manual entry of any model id.
4. Handles loading, errors, and empty states gracefully.

## Scope

### In scope
- Only the currently active provider’s modal shows Main / Gate model fields (existing behavior preserved).
- Reuse the existing `/api/models?provider=<name>` endpoint.
- Changes limited to the dashboard frontend:
  - `waku/ops/static/js/models.js`
  - `waku/ops/static/style.css`

### Out of scope
- New backend endpoints.
- Changing how `/api/models` fetches or falls back.
- Redesigning the Models grid, the chat switcher, or the “Your models” add-row.

## Design

### UI behavior

| State | Behavior |
|-------|----------|
| Collapsed | A text input shows the current model id; a ▾ button toggles the list. |
| Expanded | A dropdown panel appears below the input with a search box on top and a scrollable list of model ids. |
| Filter | Typing in the search box filters the list in real time. |
| Select | Clicking a list item (or pressing Enter) sets the input value and closes the panel. |
| Manual entry | The user can still type any id directly into the collapsed input; Save uses the input value regardless of whether it appears in the list. |
| Close | Click outside, press Esc, or click ▾ again to close. |

### Loading & error states

- **Loading**: show `Loading models…` at the top of the dropdown until the fetch completes.
- **Fetch failure**: display the backend-provided fallback models plus a subtle inline message such as `Could not load catalog — showing defaults only`.
- **Empty filter**: if the user types something that matches no model, show `No models match “<query>”`.

### Data flow

```
User opens provider modal
  └─ openProviderModal() renders two model pickers
       └─ loadModalModels(provider) fetches /api/models?provider=<name>
            └─ Backend catalog.list_models() returns
                 { models: [{id}, ...], listed: bool, error? }
       └─ Dropdown populated with models
  └─ User filters / selects / types
  └─ Save → saveProviderModal() reads input values
       └─ POST /api/providers with provider, model, small_model, key
```

### Frontend changes

#### `waku/ops/static/js/models.js`

1. **Remove `<datalist>` usage.**
   - Replace the two `<input list="model-list">` lines in `openProviderModal()` with calls to a new `renderModelPicker(id, label, value)`.

2. **Add `ModelPicker` helper.**
   - Store: `models`, `filteredModels`, `isOpen`, `query`.
   - Render collapsed input + dropdown panel.
   - Bind:
     - ▾ click → toggle
     - search input → filter
     - list item click → select
     - `Escape` / click outside → close
     - direct input typing → update value

3. **Reuse fetch logic.**
   - Keep `loadModalModels(provider)` but have it return/accept a callback instead of writing to `<datalist>`.
   - Cache the fetched model array for the lifetime of the modal so both pickers share one request.

4. **Edits lock.**
   - Keep the existing `markEditing()` call so the dashboard’s 5-second polling refresh does not wipe the open modal.

#### `waku/ops/static/style.css`

Add minimal new classes:

- `.model-picker` — wrapper for input + dropdown.
- `.model-picker-input` — text input + toggle button layout.
- `.model-picker-list` — dropdown panel (border, shadow, max-height, scroll).
- `.model-picker-search` — small search box at the top of the panel.
- `.model-picker-item` — list row with hover/focus states.
- `.model-picker-meta` — loading/error/empty helper text.

Styling should match the existing dashboard cards and inputs (rounded corners, subtle borders, `--good` / `--bad` color variables where appropriate).

### Error handling

- If `/api/models` fails or returns `listed: false`, the dropdown still shows the fallback models returned by the backend (provider defaults). Saving is never blocked.
- Direct input values are always honored, preserving support for model ids not yet in any catalog.
- Keyboard focus stays inside the modal; closing the dropdown does not close the modal.

### Testing

- **Manual verification**
  - Active provider = `anthropic` / `openai` / `openrouter`: long list is searchable.
  - Active provider = `deepseek` / `minimax` / `glm`: fallback models appear with an informative message.
  - Typing a custom model id and saving updates `.env` correctly.
  - Switching model via Save reflects on the next chat turn.

- **Automated coverage**
  - Update or extend `evals/deterministic/test_dashboard_routes.py` to assert that `/api/models` is used by the modal route and that the response shape includes `models`.
  - If a frontend unit-test harness exists, add a small test for the picker filter function; otherwise rely on manual checks.

## Trade-offs considered

| Approach | Pros | Cons | Decision |
|----------|------|------|----------|
| A. Native `<select>` | Minimal code, best accessibility | No search, poor for OpenRouter’s hundreds of models | Rejected |
| B. Searchable custom dropdown | Works for short and long lists, keeps manual entry | Requires custom JS / click-outside handling | **Selected** |
| C. Grouped with metadata | Helps users pick loop/gate models | Heavier; metadata not available from all providers | Rejected for this iteration |

## Open questions / future work

- Should the Gate model list exclude reasoning models by default? The user decided to show all models for now; filtering can be added later without changing the component shape.
- Should the picker persist the last query per provider? Not in this iteration.
