# OmniBase CRM

Single-file browser CRM (`lead_manager.html`, ~3600 lines). Salesforce Lightning-inspired, vanilla JS, zero dependencies, IndexedDB storage.

## Architecture

- **One HTML file**: all CSS, JS, and markup inline — no build step, no bundler
- **Additive patch pattern**: new feature code is appended as `<script>` blocks before `</body>`. Never modify the core JS block (lines ~700–2927)
- **`State` scoping**: `State` is a `const` in global lexical scope, not `window.State`. Always guard with `typeof State !== 'undefined'`
- **Virtual scroll rows**: use `data-id` attribute (not `data-rid`). Selector: `.trow[data-id]`, access: `row.dataset.id`
- **Capture-phase clicks**: intercept row clicks with `document.addEventListener('click', fn, true)` + `e.stopPropagation()`

## Key Data Shapes

- **Activity log**: `localStorage` key `ob_activities_v1`, array of `{id, accountId, type, subject, body, createdAt}`
- **Contact**: `{first_name, last_name, name, company_record_id, title, email, phone, status, record_id}`
- **Company linking**: `contact.company_record_id` matches `company.record_id` (11-digit string)
- **Record overlay modes**: `_recMode = 'company' | 'contact'`, shared `#recordOverlay` DOM

## Deployment

- **Vercel** (static, no build): `vercel.json` routes `/` → `/lead_manager.html`
- Python backend files excluded via `.vercelignore` — they would blow the 500 MB bundle limit
- GitHub → Vercel auto-deploys on every push to `main`
- Production URL: `https://omnibase-crm-baylitx-projects.vercel.app`

## Hard Rules

- **Never commit** `data/`, `*.csv`, `*.xlsx` — real company/contact data stays local only
- **Never touch** the core JS block (lines ~700–2927); append-only for new features
