# Ryze AI — SEO Platform Architecture Audit & Reverse Engineering

**Subject:** Ryze AI (`get-ryze.ai`), SEO Autopilot / Traffic Printer tiers
**Product surface audited:** `https://www.get-ryze.ai/seo`
**Primary evidence base:** `help.get-ryze.ai/llms.txt` (190+ documented pages), marketing site, `/ai-seo-tool`, `/geo`, `/geo-tool`, `/pricing`, `/llms.txt`
**Date of research:** 2026-09-30
**Method:** Full read of every SEO, GEO, integrations, reporting, agent-architecture, and plans page in the vendor's machine-readable documentation index (`llms.txt`), plus every public marketing/comparison page. No account access, no trial, no direct observation of the running product.

---

## How to read the confidence markers

Every claim in this document is tagged. This matters because the vendor's marketing pages and its help center **disagree with each other** in several material places, and a competitive audit that flattens those into one voice produces a worthless model.

| Marker | Meaning |
| --- | --- |
| **[D]** | **Documented.** Stated explicitly in the vendor's help center. Quoted or closely paraphrased. |
| **[M]** | **Marketing claim.** Appears on `get-ryze.ai` but has no corresponding help-center page. |
| **[I]** | **Inferred.** Reconstructed by this audit from adjacent documented facts + standard industry architecture. Reasoning is shown so it can be attacked. |
| **[X]** | **Unexplained / contradicted.** A claim with no documentation, or documentation that says something materially different. |

---

# PART 0 — SYSTEM ARCHITECTURE (PREREQUISITE TO EVERYTHING BELOW)

You cannot audit the parts without the spine. Ryze's spine is unusually well documented **[D]**, and almost every "how does it work" question resolves to it.

## 0.1 The three-layer architecture

**[D]** — `help.get-ryze.ai/how-ryze-works`:

> **Connected accounts.** Your store or CMS, Google Search Console, Google Analytics, and your ad platforms. Connected once in Workspace Settings → Integrations, per workspace.
> **The Ryze agent.** Reads those accounts, decides what to do, and executes. Everything it does is written to an activity log.
> **The MCP connector.** Exposes Ryze to Claude and ChatGPT as a set of tools. **The connector is the interface, not the engine — the agent keeps running whether or not a chat is open.**

**Audit significance:** this is a *retained, server-side, always-on agent with a chat-shaped control surface*, not a chatbot with SEO tools. The MCP server at `connector.get-ryze.ai/mcp` **[D]** is a transport. Deleting it would not stop the engine. This is the single most architecturally important fact in the whole platform and it is the thing a competitor most often gets wrong by building the inverse (an LLM tool-calling loop that only runs when prompted).

## 0.2 The four-stage loop

**[D]** — `audit → prioritise → execute → measure`, with results feeding back into the next cycle's audit.

| Stage | Concrete implementation |
| --- | --- |
| **Audit** | GSC queries/pages + DataForSEO rankings/competitors + store revenue-ranked products **[D]** |
| **Prioritise** | Brand Context → keyword seeds → DataForSEO expansion → AI selection **[D]** |
| **Execute** | Article generation + CMS publish; on-page fix application; off-page placement **[D]** |
| **Measure** | Daily rank pulls, GSC deltas, AI visibility runs → next cycle's prioritisation **[D]** |

## 0.3 The monthly cycle and deficit-only production

**[D]** — "Each cycle Ryze works toward a target volume of content, and only produces the **deficit** — what's still missing for the month — so it doesn't double up on work already done."

Publication pacing: **~5 articles/day**, auto-spread **[D]**. Lovable/Base44/GoDaddy Airo publishing runs in a **20:00 UTC daily window, batches of up to 5 per site** **[D]**.

**[I]** This is a *stateful scheduler with a persistent monthly quota ledger*, keyed to `billing_date`. It is not a cron that writes N articles. The billing-date keying is a business decision that leaks into the engineering: it means the engine cannot be evaluated as "pages this month" without knowing the cycle start, and it means a mid-cycle cancellation leaves a partially-spent quota.

## 0.4 The context layer ("Brand")

**[D]** — `help.get-ryze.ai/agent/context`:

> Everything Ryze produces — programmatic articles, on-page copy, off-page mentions, ad creatives, reports — is shaped by one place: your **Brand** context. It's the brief the agents read on every turn.

Three tabs: **Identity** (what you sell, industry, tone of voice), **Visual** (logo/product photos), **Context** (always-remember notes, writing-style rules, blog example URLs, external-reference rules).

**[I]** This is a **system-prompt-shaped structured document, injected on every agent turn, scoped per workspace.** "Tone of voice" with the literal instruction *"Avoids corporate enthusiasm, feature-list marketing, and hollow superlatives"* is not a UI label — it is a prompt fragment. The "blog examples" field is few-shot conditioning on client-authored URLs. The "external references" block is a link-graph constraint on the generator: it names both sources to cite and competitors to avoid sending link equity to.

This is the highest-leverage object in the entire product, and it is the part most competitors would build last. It is also the part with the weakest user-facing feedback loop — see [§5.7](#57-agent-context-has-no-eval-loop).

## 0.5 Control surfaces

| Surface | Documented behaviour |
| --- | --- |
| **Approvals** | Agent files proposals with summary / entity / before-after table / evidence. Approve applies + verifies; Reject suppresses re-filing. **Blocked** state for unfixable-from-their-side issues, re-checked on demand. **Opportunities** (a second proposal type) show a *draft* instead of a before/after. Opportunity research runs **weekly**, keeps a small backlog **[D]** |
| **Scheduled tasks** | Recurring (daily/weekly/monthly) or one-time. Each run = **fresh chat**, task authored as a direct imperative, result emailed. Read-only history **[D]** |
| **MCP connector** | Remote MCP at `connector.get-ryze.ai/mcp`, bearer `rk_live_…`. Read-only tools auto-run; write/delete default to **Needs approval**, configurable per-tool in Claude's connector settings **[D]** |
| **REST API** | `POST /api/v1/agent/signup` → email 8-digit code (1h TTL) → `POST /api/v1/agent/verify` → key. `GET /api/v1/tools` returns the live tool manifest incl. `requires_plan`. `POST /api/v1/tools/{name}` with `{args:{…}}` **[D]** |
| **Agent Activity / Logs** | Timestamped record of every action. "The authoritative record" **[D]** |

**[I]** The REST design (`GET /tools` returning a self-describing manifest with `requires_plan` flags, and tools returning **HTTP 200 with a `status` field** for business-level refusals like `subscription_required` / `out_of_credits` / `not_allowed`) is a *metered tool-broker architecture*. The 200-with-`status` pattern is a deliberate choice: it means a calling agent gets a machine-readable, recoverable error with a **payment link embedded in the message** — i.e. the API is designed to be driven by autonomous agents that can self-serve a human through a conversion funnel. `native__create_connection_link` / `native__get_connection_link` polling is a full OAuth orchestration loop exposed as a tool.

## 0.6 Metering

**[D]** — 0.5 credits per AI step. Fixed-cost tools: UGC video 10, ad creative 2.5, generated photo 0.5, SEO data lookup 0.5, competitor ad search 0.5, Reddit search 0.5, web fetch 0.1. **Tool calls are free; only reasoning steps and heavy tools cost.** Failed runs are never billed. Approvals background research is free. Managed Campaigns excluded.

**[I]** Metering the *reasoning step* rather than the *tool call* inverts the usual SaaS cost model. It makes Ryze's own margin scale with model inference cost, and it means **tool selection is the main cost lever** — a well-tuned agent doing fewer, larger-batched tool rounds is materially cheaper. Note the connection-level consequence: **AI work performed through the Claude/ChatGPT connector consumes no Ryze credits** **[D]**, because the inference happens in the user's assistant. That is a deliberate, and very clever, cost-absorption strategy: it makes the free tier's economics survivable and makes the connector the default path.

---

# PART 1 — SITE FIXES

## 1.1 Title tags

### Definition
The `<title>` element — the primary on-page relevance signal and the clickable headline in the SERP. Truncated around ~580px on desktop, ~60 characters on mobile. In an automated system it is a per-URL field with a character budget, a keyword target, and a brand suffix.

### Ryze's Execution (Reverse-Engineered)
**[D]** — "rewrites titles and metas"; the audit checks for "Missing, duplicated, or truncated title tags" (`/shopify/audit`). Site-fix execution model: `Run crawl` → issues grouped by type with **affected-URL counts** → per-issue **"Fix with agent"** → the agent applies across all affected URLs (`/seo/optimizations`). State machine: **To fix / Fixed / Passed / Blocked / Not applicable**. Scope: **top 300 GSC pages** **[D]**.

**[D]** Brand Context explicitly governs the copy: tone, writing-style rules, and *"what your competitor is"* all feed generation.

**[I]** Execution mechanics, reconstructed:
1. **URL set** = top 300 by impressions from GSC, joined to CMS resources. Non-GSC URLs are invisible to this pipeline — a real coverage limit.
2. **Detection** is template-comparison, not pure LLM: `title == null` ∥ `len(title) > budget` ∥ duplicate title across template families ∥ title ≡ product handle.
3. **Generation** is a constrained LLM call carrying: `primary_keyword`, Brand Context (identity + tone + style), a per-title character budget, and the existing title as a "what to preserve" hint (brand, model number, price-relevant tokens).
4. **Write-back** goes through the CMS adapter's metafield/resource update — *not* a template edit. This is deliberate: the docs are explicit that Ryze makes **"no theme edits"** **[D]**.
5. **Verification** by re-fetch and diff before marking **Fixed**.

**[I]** Why data-level not template-level: it works on 17+ CMS platforms with no theme access. The cost is a documented failure mode — theme/app code that regenerates the tag on render overwrites the fix. The troubleshooting page names this exactly: *"two systems writing the same tag will fight, and the last one to run wins"* **[D]**. A competent implementation must detect that and route to a **Blocked** proposal rather than reporting Fixed.

### Live Site Implementation
| Requirement | Detail |
| --- | --- |
| **Read** | GSC OAuth (Full user role; **Domain vs URL-prefix property choice is load-bearing** — prefix properties silently exclude the other host variant **[D]**) |
| **Write — Shopify** | OAuth on the `.myshopify.com` domain only, no app creation **[D]**; requires `write_products` or equivalent SEO-metafield scope on the custom-app path **[I]** |
| **Write — WordPress** | Application password (self-hosted, REST API) or WP.com/Jetpack JSON API module **[D]**; needs `edit_posts` capability **[I]** |
| **Write — headless** | GitHub App with contents:write on one repo → JSON commit **[D]**; or Custom Webhook `POST` with `Authorization: Bearer` **[D]** |
| **Write — no-API platforms** | Squarespace/Lovable/Base44/GoDaddy Airo: stored credentials + **headless browser session**, click Publish, then live-URL verification **[D]** |
| **Permissions** | Every write gated by Approvals **[D]**; write/delete MCP tools default to Needs approval **[D]** |
| **Not required** | Server access, SSH, theme code, deploy pipeline |

### Alternative / Improved Approaches
- **Snippet-CTR feedback loop.** Ryze optimises title tags without ever observing the CTR delta. Store Console gives position and CTR per URL; a title change is a natural experiment. **A competitor can win here by shipping an A/B title framework that measures realised CTR against predicted CTR**, which converts titles from a copy problem into a measurement problem.
- **Title variant pool + rotation.** Generate 3 candidates per URL, ship one, rotate on a 30-day cycle against GSC CTR. The thin-content risk is nil (titles are not body content) and the upside is compounding.
- **Template-aware write path.** Before writing, detect whether a theme/app owns the field (render twice, diff). Route to theme-config, app config, or metafield — instead of a documented conflict that requires human diagnosis.
- **SERP-pixel-aware budgets.** Truncation width depends on the SERP's actual font rendering, not a character count. A renderer-based pixel budget beats `len() <= 60`.

---

## 1.2 Meta descriptions

### Definition
The `<meta name="description">` element. Not a ranking factor, but the primary **click-through-rate** lever on the SERP. Google's rewriting behaviour means it is a *suggestion*, not a guarantee — so it is a CTR instrument, not a snippet guarantee.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Audited for "missing or duplicated meta descriptions" **[D]**. Shipped by the same Fix-with-agent path as titles, in the same crawl pass.

**[I]** Mechanically distinct from titles in three ways:
1. **No truncation budget to hit cleanly.** Google truncates on pixels, rewrites when it deems the description unhelpful, and rewrites more aggressively on mobile. So the generator's target is *a* plausible description, not *an* accurate one.
2. **Duplicate detection is the primary signal.** Template-driven CMSs produce the same description across hundreds of URLs. That is a **cheap, high-confidence, high-volume** detection — the single highest-leverage meta issue on an ecommerce site.
3. **The GEO layer changes the target.** **[D]** The AI-visibility page states the execution brief explicitly: *"**Title and meta optimization** — clear, extractable page-level statements."* That is not snippet copywriting. That is **passage engineering**: a self-contained factual sentence the model can lift, because a generated answer frequently quotes or paraphrases the description itself.

So Ryze appears to be optimising the meta description against **two objectives simultaneously** — human CTR and model extractability — and GEO plausibly takes precedence given where the product is now positioned.

### Live Site Implementation
Identical surface to titles: GSC read + one CMS write path. **[I]** The only platform-specific nuance is Shopify, where meta descriptions are a **theme field, not a resource field** — the write goes to `metafields.namespace = "custom", key = "seo.description"` or, on modern Online Store 2.0, to the `seo` metafield definition. Either way, if the theme reads a *different* source, the write is a no-op. This is the most common "my change vanished" cause in Shopify.

### Alternative / Improved Approaches
- **Split the objective explicitly.** A single LLM prompt cannot serve "convert a human" and "be extractable by a model" — they pull in opposite directions (persuasion vs. factual statement-first). **Two fields, two templates**: `meta_description` (CTR-optimised) and a GEO block rendered into a `data-*` attribute, `<meta name="ai-summary">`, or a leading definition sentence in the body. Right now they are fused.
- **Detect Google's rewrite rate** per template via GSC `CTR` vs `position` residuals, then rewrite only the templates Google is overwriting. Cheaper, higher signal.
- **Description-as-CRO asset.** Combine SERP description and above-the-fold H1 testing; treat them as one funnel rather than two fields.

---

## 1.3 Page speed / Core Web Vitals

### Definition
**LCP** (largest contentful paint, ≤2.5s), **INP** (interaction to next paint, ≤200ms), **CLS** (cumulative layout shift, ≤0.1), assessed at the **75th percentile of real-user field data** over a **28-day rolling window** **[D]**. INP replaced FID in March 2024 **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** — "Ryze monitors Core Web Vitals and ships performance fixes with approval — **image compression, lazy-loading, JavaScript reduction, and broken pagination fixes. No theme edits are required.**" *(from `/shopify/site-speed`)*

**[D]** Diagnostic posture: field data first (Search Console → CWV, CrUX), Lighthouse/PSI lab only for *cause*, never for *verdict* **[D]**. Honest weighting stated plainly: *"Core Web Vitals are a real but small ranking signal… The stronger argument for fixing speed is conversion."* **[D]**

**[I]** The four named fix categories tell you the entire implementation surface:
- **Image compression** → Shopify CDN transforms / `?width=` params; or a headless image pipeline on GitHub/Ryze Hosting. No server access needed.
- **Lazy-loading** → theme-level `loading="lazy"` attributes. On Shopify this is a **theme edit**, which Ryze says it does not do. **[I]** Therefore lazy-loading on Shopify is almost certainly delivered as **app-script injection** (a performance app in the install), not as a Ryze write. That is an important architectural boundary: Ryze's *performance* surface on Shopify is partly delegated to third-party app scripts — which is itself a common INP cause. This is a genuine tension in the product.
- **JS reduction** → requires *uninstalling apps* and editing theme `script_tags`. Outside Ryze's write surface on Shopify. Almost certainly surfaced as a **Blocked** proposal with a human instruction list, matching the documented Blocked mechanic **[D]**.
- **Broken pagination** → fully within reach (Shopify `/collections/…?page=N` crawl + relink).

**[I]** Honest conclusion: **Ryze's CWV module is strongest on headless/static stacks it fully controls (GitHub, Ryze Hosting, custom webhook receivers) and weakest on Shopify, where the dominant causes — app bloat and theme JS — are not in its write path.** The docs are refreshingly candid that apps are "the single highest-impact performance action" **[D]** while shipping "no theme edits" **[D]**. A competitor that ships **first-party theme control** (or a Playwright-driven theme editor with a human-in-the-loop diff) has a genuine, defensible wedge here.

**28-day window is a reporting liability, not just a technical detail.** Any fix shipped today is unverifiable for a month. Ryze's own reporting guidance says field data takes weeks to reflect **[D]** — so the CWV product is structurally a *reporting* product, and any honest UI must show CrUX as a 28-day series, not a pass/fail badge.

### Live Site Implementation
| Stack | Feasible | Mechanism |
| --- | --- | --- |
| **Shopify** | Partial | CDN image transforms (API); pagination (crawl+write); **apps/theme JS → Blocked** |
| **WordPress** | High | REST/media API + plugin (needs a Ryze plugin, or a capabilities grant) |
| **GitHub / static** | **Full** | Commit config/code; full control of the build |
| **Ryze Hosting** | **Full** | Vercel DNS (`cname.vercel-dns.com` / A `76.76.21.21` **[D]**) — the platform can ship perf changes at the edge |
| **Headless CMS** | High | Content API + delivery layer config |
| **Measurement** | — | CrUX API / PSI API / GSC CWV report **[I]**. **Never** a client-side PSI score as the source of truth |

### Alternative / Improved Approaches
- **Own the theme.** Offer a real theme-level performance agent that edits `theme.liquid` / headless config via PR. This is where the market's biggest performance wins actually live.
- **CrUX URL-prefix regression alerting**, not a periodic audit: alert when a template family's p75 crosses a threshold, with a per-template budget enforced in CI for headless.
- **Shift the pitch from "CWV" to "revenue per session."** A p75 LCP improvement has a defensible conversion number attached; a "CWV score" does not. Ryze's own docs make this argument and then sell the CWV label.
- **Diagnose the app graph programmatically.** Enumerate installed apps, attribute injected script bytes to each, rank by INP contribution, and produce an "uninstall for Xms" proposal. That is a genuinely differentiating, purely analytic product that needs no write access at all.

---

## 1.4 Internal links

### Definition
`<a href>` relationships within a domain. Two functions: **crawl path** (discoverability) and **authority distribution** (PageRank flow). On large catalogues they determine which pages get indexed at all **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** — "Ryze audits internal linking during the technical audit, identifies orphan pages, and ships internal linking adjustments with approval. Generated articles are published with internal links to your key pages rather than as isolated content."

**[D]** Audited signals: orphan pages, key pages with too few inbound internal links, related-product modules rendered by JS (crawler-invisible), products reachable only via filters, broken links to deleted products **[D]**.

**[I]** Two distinct mechanisms, and this split is the interesting part:

**(a) Pre-publication link injection.** At generation time, every article gets internal links to commercial pages. **[I]** This is the *high-leverage, low-risk* path: it happens before the page exists publicly, needs no crawl, and is fully deterministic. The target-selection is almost certainly **not** purely semantic — the docs say links are "drawn automatically from your store and Search Console" **[D]**, implying a **PageRank-proximity model**: rank candidate destinations by GSC impressions/clicks × crawl depth × topical similarity, then place links at points where the article's LLM-generated content already makes a natural reference. Because the generator produces the body, it can **co-construct the sentence and the link** — the link is woven into a claim the article is already making, which is exactly the editorial-quality standard Ryze enforces on the off-page exchange links **[D]**. Applying that same standard on-page is a strong signal of a shared link-insertion engine.

**(b) Post-publication graph repair.** Crawl → build the internal graph → compute inbound-link counts, orphan status, click depth from homepage → propose additions/removals. **[I]** Requires a full site crawl and an authority model; the write is a content-field edit (adding a link inside a product description / collection copy / related-product config) or a menu/navigation edit. **Menu edits are not available on Shopify without theme/app access** — so Ryze's link repair is confined to *content* it can write.

**[D]** Prioritisation: *"Articles should link to categories more often than to individual products. Categories are the commercial pages, they persist while products come and go, and they pass authority down to products anyway."* **[D]** That is a correct and non-obvious strategic default, and it implies a target-weighting model favouring durable nodes.

### Live Site Implementation
- **Read:** crawl (top 300 GSC pages for issues; full crawl implied for graph work) + GSC + catalogue.
- **Write:** CMS rich-text/HTML field update; Shopify `metafields` for collection metafields; related-product section settings via theme app extension **[I]**.
- **Hard limit:** no theme navigation edits on Shopify; no `<head>`/template access. Menu-driven internal linking is out of reach.
- **Headless:** full control via GitHub commit or webhook to a content API.

### Alternative / Improved Approaches
- **PageRank simulation as a first-class product.** Build the graph, run a personalised PageRank, diff current vs. optimal internal link structure, and output a *specific* "add this anchor from this page to that page" backlog. Most tools report orphan counts; almost none propose the optimal edge set.
- **Anchor-text portfolio control.** Treat on-page anchors as a distribution to be managed (brand / URL / generic / descriptive), not as free text. The same manipulation-detection logic Ryze applies to off-page anchors **[D]** should govern internal ones.
- **Freshness-weighted link decay.** Re-inject links into decaying pages on a schedule — a backlink-maintenance tactic applied to the internal graph. Nobody does this systematically.
- **JS-rendered link audit as a product.** Most sites have related-product carousels invisible to crawlers. A Playwright-render + diff against served HTML, reported as a crawl-budget leak, is a crisp, high-value, low-competition diagnostic.

---

## 1.5 Schema markup

### Definition
JSON-LD (or microdata) declaring page entities in Schema.org vocabulary so machines can interpret the page as data. Types that matter here: `Product`, `Offer`, `AggregateRating`, `BreadcrumbList`, `Organization`, `FAQPage`, `ItemList` **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** — The audit checks presence of `Product` on product pages, **completeness of the `offers` block (`price`, `priceCurrency`, `availability`)**, **markup that contradicts the visible page**, invalid/partial markup, `BreadcrumbList` + `Organization` presence, review markup validity **[D]**. Fix scope includes **"GTIN assignment and category corrections at feed level"** **[D]** — i.e. schema work extends into commerce-catalogue fields, not just JSON-LD.

**[D]** The failure catalogue is the most useful part of the audit technically, because it encodes Google's actual rules: incomplete `Product` markup is *"frequently ignored wholesale rather than partially honoured"* **[D]**; stale `InStock` is *"worse than no markup"* **[D]**; duplicate theme+app emission produces conflicting blocks **[D]**.

**[I]** The generator is a **template library with a data-binding step**, not free-form AI:
- Shopify themes emit `Product` markup from Liquid; Ryze's correct move is **theme config + metafield binding**, not injecting a second JSON-LD block (which would create the documented duplicate-markup conflict).
- The binding inputs are: `price` (from variant), `availability` (from inventory), `sku`/`gtin` (from metafield or variant barcode), `brand` (vendor), `aggregateRating` (review app data), `shippingDetails`/`hasMerchantReturnPolicy` (Shopify's own data where available **[I]**).
- **AI is used for judgement, not syntax**: deciding *which* types a URL warrants, and flagging contradictions, rather than authoring JSON. Generating JSON-LD by LLM would be malpractice; it is a deterministic template over deterministic fields.

**[I]** The genuinely hard sub-problem, and the one Ryze's docs reveal awareness of: **consistency enforcement**. "Markup must match the visible page" **[D]** requires comparing the JSON-LD `offers.price` against the *rendered* price — which means fetching the live page, parsing, and diffing. A pure-API implementation cannot do this on most themes. This is a real place where a headless-browser-based competitor could beat an API-only one.

### Live Site Implementation
- **Shopify:** product metafields, vendor, inventory level; theme `settings_schema.json`; merchant-center feed. **Never** inject a second `Product` block.
- **WordPress:** REST API + a plugin writing to the Yoast/RankMath meta field, or Yoast's REST API directly.
- **Headless:** full control — schema is generated at build time from your own data. Cleanest possible case.
- **Validation:** Google Rich Results Test, Schema.org validator, GSC enhancement reports **[D]**. **Validate the rendered page, not the template** **[D]** — which mandates JS rendering.
- **Feed sync:** GTIN/availability errors usually originate in the **Merchant Center feed**, not the page. Fixing the feed is often the higher-leverage action, and it needs the **Google Merchant Center API**, which is *not* in Ryze's documented integration list **[D]** — a gap.

### Alternative / Improved Approaches
- **Continuous schema drift detection.** Rather than audit-then-fix, run a cheap scheduled validator that catches: theme deploys that break markup, price changes that desync `offers`, reviews deleted from the review app, availability going stale. Drift is the steady-state failure; audits are episodic.
- **Render-and-diff as a paid differentiator.** Playwright render + extract both visible price and JSON-LD price + report divergence. This is the single check most tools cannot do, and it catches manual-action-level violations.
- **Ship the feed, not the page.** For ecommerce, a Merchant Center feed correctness service is higher-leverage and far less competitive than schema markup generation. Ryze appears to do GTINs at "feed level" **[D]** but does not document a Merchant Center integration **[D]**.
- **Speakable/`speakable` and `FAQPage` discipline.** Ryze's GEO page lists `Speakable` **[M]**, while its own help center warns `FAQPage` *"requires real question-and-answer content, not marketing copy reshaped into questions"* **[D]**. Enforcing that distinction programmatically — checking that every `FAQPage.mainEntity` is genuinely visible in rendered HTML — is a defensible compliance product.

---

## 1.6 Broken links

### Definition
Internal links returning 4xx/5xx, redirect chains (`A→B→C`), redirect loops, and canonical mismatches. On a large catalogue these accumulate silently and burn crawl budget while leaking link equity **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Audited: "Broken internal links and 404s that still receive traffic or links", "Redirect chains and loops", "Broken pagination", "Sitemap accuracy — non-canonical or non-indexable URLs included" **[D]**. Fix: on the marketing page, *"patches broken links and redirects"* **[M]**; on the docs, **`Re-index` a page** is a distinct user action **[D]**, and fixing is explicitly listed among the low-risk categories eligible for **auto-apply** **[D]** on the landing page FAQ.

**[I]** The pipeline is the most classically "crawler" of anything in the product, and the most likely to be competently built:
1. **Seed set** = sitemap ∪ internal link graph ∪ GSC top-300.
2. **Fetch** with a real HTTP client following redirects manually (`max_redirects=0`, then re-request per hop) so chains are observable.
3. **Classify** each failure: hard 404 / soft 404 (200 with no content) / 5xx / timeout / chain length / loop / canonical mismatch.
4. **Prioritise** by **(GSC clicks + impressions) to the dead URL** — a dead URL with traffic is categorically different from a dead URL with none, and Ryze's whole prioritisation philosophy is traffic-weighted **[D]**.
5. **Resolve** by finding the best surviving equivalent: exact product match → category match → semantic nearest neighbour via catalogue attributes → 410 if nothing.
6. **Write** — Shopify redirects via a Shopify API/proxy that posts to the redirects resource; elsewhere via content edits.
7. **Ping** Google Search Console **URL Inspection → Request Indexing** for the replacement. **[D]** Confirmed as a first-class action.

**[D]** The one place Ryze documents restraint, and it is the correct one: **do not mass-redirect to the homepage** — that is a soft 404 and does not pass value **[D]**. Redirect to the nearest genuinely relevant page, or 410 **[D]**.

### Live Site Implementation
- **Shopify redirect writes** need admin-level scope. This is one of the highest-privilege operations Ryze performs and one of the most consequential if it goes wrong at scale. **[I]** This is the strongest argument for Ryze's approval model, and an audit should treat Shopify redirect creation as a **high-severity, human-gated, rollback-required** operation class.
- **GSC URL Inspection API** — historically undocumented/unstable; many tools resort to manual request or sitemap resubmission. **[I]**
- **WordPress:** `redirection` plugin API or `.htaccess`/Nginx rules (no server access on shared hosting → plugin required).
- **Headless:** redirect rules are config in the repo. Clean.

### Alternative / Improved Approaches
- **Two-pass "propose, never write" for redirects.** Because redirect creation is high-risk and hard to reverse at scale (Shopify has no bulk-delete of a generated set), the safest high-value product is a **diff proposal** with export-to-CSV, letting the client apply. Ryze's own migration checklist requires manual per-URL mapping **[D]** — automating the *proposal* while keeping the *application* manual is the right split.
- **Sitemap-driven 404 indexation, inverted.** Most tools list your 404s. The valuable move is listing **the 404s Google actually indexed and clicked** — those are live traffic loss today, ranked and priced. Search Console Index Coverage/Inspections gives exactly this.
- **Prevention over cure.** A CI check on headless builds: validate every internal link target in the generated output; fail the deploy. A broken link never ships. Cheap to build, and structurally superior to a monthly cleanup.
- **Chain collapsing as a scheduled job.** Chains accumulate from years of small changes **[D]**. `A→B→C` should be rewritten to `A→C` on a cron, per-hop, with logs. Continuous maintenance; nobody does it.

---

# PART 2 — CONTENT

## 2.1 Scaling to 100s of blogs/month

### Definition
A production system that reliably generates, reviews, schedules, and publishes a hundred-plus long-form pages per month without quality collapse — the classic programmatic-SEO throughput problem.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Quotas and cadence:
- **90 pages/month** (SEO Autopilot, `/plans/usage-limits`) vs **"100+/mo"** (comparison table, `/plans/whats-included`) vs **"100s of blogs/month"** **[M]**. **[X] The vendor's own three numbers are 90, 100+, and "hundreds".** This is a live pricing-page contradiction, not a subtlety.
- Deficit-only production against a monthly target **[D]**.
- ~5/day auto-spread **[D]**; publish window 20:00 UTC, batches ≤5/site **[D]**.
- Articles: body 500+ words + meta title + meta description + **auto-selected and uploaded featured image** **[D]**.

**[I]** The throughput architecture:
- **Queue-based, not request-based.** Generation is a background job ("It runs in the background — the draft is ready in a few minutes") **[D]**, implying async workers with a durable job queue and a per-article state machine (`Planned → Drafted → Published | Failed`) **[D]**.
- **Idempotency by slug.** Republish overwrites the same file/slug, no duplicates **[D]** (GitHub: `<folder>/<slug>.json`; webhook: "your endpoint should **upsert by slug**" **[D]**). This is a correctly designed content-addressed publish path.
- **Retry with a skip rule.** "Ryze retries up to a few times before skipping" **[D]**.
- **Backpressure via quota ledger.** Deficit-only means the generator asks a ledger how many are already produced this cycle before generating. That ledger is the rate limiter.
- **The image step is a hidden cost centre.** "Selected automatically and uploaded" **[D]** to a public CDN, and the docs warn only the **host** is stable, not the path **[D]** — meaning images live in Ryze's storage (the example URL exposes `supabase.co/storage/v1/object/public/workspace-article-images-public/…`). **[I]** That is a **single-tenant, single-region, vendor-hosted image dependency on every article** — a real operational and SEO coupling (the `og:image` on 90 articles/month points at a third-party host). It also reveals the stack: Supabase for object storage, Vercel for edge/DNS, a custom crawler, LLM orchestration, MCP server. Good intel.

**[D]** Pacing rationale, stated and correct: *"A thousand pages appearing overnight is a pattern search engines treat with suspicion; steady publication is not."* **[D]**

### Live Site Implementation
- One write path per CMS (see §1.1 table), plus **Ryze Hosting** (CNAME to `cname.vercel-dns.com` or A to `76.76.21.21`, auto-HTTPS) **[D]** — a genuine zero-CMS option.
- **Override blog URL** field for headless: articles live at a different public address than the CMS; Ryze uses it for live links, indexing pings, and **liveness checks** **[D]**.
- **Liveness verification** is real: for Lovable, Ryze "opens each article on your live site and checks its title, meta description and text. This check waits up to 2 minutes" **[D]**. So the system has a **post-publish verification stage** — not just a fire-and-forget POST. That is a meaningful quality bar.

### Alternative / Improved Approaches
- **Impressions-per-batch gating.** **[D]** Ryze's own docs prescribe it ("measure impressions per batch in Search Console before scaling") but the product is structured around a fixed monthly quota, which structurally resists pausing. A competitor could make **"earned impressions" the release gate for the next batch** — self-limiting, self-correcting, and a genuinely better product. It is the single strongest idea available here.
- **Pruning automation.** **[D]** "Prune pages that never earn impressions" is prescribed and, per the docs, not productised. An automated *consolidation* engine (cluster thin/no-impression pages → 301 to the nearest earning page) is high-value, uncontested, and directly improves a site.
- **Human-in-the-loop sampling, not per-article review.** Ryze's default is approval-per-article, which does not survive 90/month. **Statistical QA** — review a random 5% + 100% of any batch whose quality metrics (length, keyword density, internal link count, duplicate-similarity vs. corpus) deviate — scales review to hundreds per month.
- **Near-duplicate detection at generation time.** Cosine/embedding similarity against the entire published corpus, with a hard reject above threshold. This is the direct technical defence against the thin-content failure mode Ryze's own docs warn about **[D]**, and it is not described anywhere in their docs.
- **Decouple image hosting.** Serve `og:image` and article imagery from the client's own asset host. Removing a third-party image dependency at 90 articles/month is table stakes for enterprise.

---

## 2.2 Programmatic content generation

### Definition
Generating large numbers of pages from a structured data source and a shared template, each targeting a distinct long-tail query. The failure mode is **thin content at scale** — near-identical pages differing by a swapped variable **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** The generation contract: body (500+ words) + meta title + meta description + featured image + internal links + `primary_keyword`, published as **both `body_markdown` and `body_html`** **[D]**, with a `blocks` array carrying "Ryze's structured rich blocks (TL;DR, stats, FAQ)" **[D]**.

**[I]** The `blocks` array is a significant architectural tell. It means Ryze's article model is **not prose-with-markdown** — it is a **typed document model** (TL;DR block, stats block, FAQ block, body). That is the correct design for GEO, because it forces the generator to emit **extractable units** by construction rather than hoping prose contains them. It also explains the FAQPage caution **[D]**: the FAQ block is a first-class object, and the guidance about not reshaping marketing copy as Q&A **[D]** becomes a validation rule on a specific block type.

**[D]** The quality doctrine, which is genuinely good and largely unenforced by product:
- "One page per distinct intent, never several per intent"
- "Real data per page, not templated prose with variables"
- "Skip topics with insufficient source material rather than generating padding"
- "Internal links specific to the page, not a shared template footer"
- "Set a minimum data-completeness bar — do not generate a page when the underlying record is sparse" **[D]**

**[D]** Two pools, and this is the smartest structural decision in the product:
- **Pool A (priority-anchored):** built around priority keywords + highest-revenue products; drives internal links to commercial pages **[D]**.
- **Pool B (open):** adjacent demand mined from **GSC impressions, competitor gaps, and AI brainstorming across the wider catalogue** **[D]**.

**[I]** Pool B's "competitor gaps" is a **SERP-diff against DataForSEO competitors** — keywords competitors rank for where the client has no page. That is a real, well-defined computation. "AI brainstorming" is a weaker, LLM-generated long-tail expansion — the least rigorous step in the chain and the most likely source of zero-volume topics. Given **[D]** "Ryze never invents metrics" but also **[D]** "very low-volume terms are dropped", the funnel is: LLM proposes → DataForSEO validates volume/difficulty → low volume dropped. Defensible, but the validation is the only real filter.

**[I]** The **one-page-per-intent** rule — the single most important programmatic-SEO control — is the hardest to enforce without clustering, and clustering requires DataForSEO SERP overlap analysis, which is expensive. The docs assert the rule; the evidence suggests the product approximates it via LLM selection at step 4 of keyword selection **[D]**, which is a weaker guarantee. **This is the most exploitable weakness in Ryze's content engine: intent collision across a 90-page batch is only weakly prevented.**

### Live Site Implementation
Same as §2.1. Add: **LLM provider access** (BYO Anthropic key supported **[D]**; model selection is *not* user-facing — "Ryze chooses the right model for each reply" **[D]**), and **image generation/selection**.

### Alternative / Improved Approaches
- **SERP-overlap clustering as a hard gate.** Cluster the whole planned batch by SERP similarity (DataForSEO returns SERP data), then forbid two pages in one cluster. It converts a policy into an invariant. This is the top technical recommendation in this audit.
- **Data-completeness as a typed precondition.** A schema on the source record (catalogue product, GSC query) with required fields; generation is refused below threshold. Docs prescribe it; a schema enforces it.
- **Typed block model as a competitive moat, not just an internal detail.** Expose the block model publicly — a headless client renders TL;DR/stats/FAQ blocks into semantically meaningful components. That is a deliverable artefact other tools cannot match, and it makes the GEO benefit legible to the client.
- **Publish to a "staging" namespace, promote on evidence.** Serve unpublished-from-index drafts, promote only after impressions. The inverse of today's flow and structurally self-protective.

---

## 2.3 Real-time keyword tracking

### Definition
Continuous SERP position measurement for a defined keyword set, with movement fed back into planning. "Real time" is a marketing word; the industry reality is daily, and Ryze says daily **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Rank tracking cadence: **daily** (`/reports/scheduling`, `/ai-visibility/monitoring-interval`, `/plans/usage-limits` — three independent pages agree). Source: **DataForSEO** **[D]**. Credit cost: **0.5 per SEO data lookup** **[D]**.

**[D]** The important and under-advertised detail: **the Keywords page is read-only.** "The Keywords page lists both without a status marker and has **no switch or delete button**: activation is decided by Ryze on the next keyword pass, not from the page." Deleting requires asking the chat; keywords in the current plan require support. **[D]**

**[I]** Two products are being conflated under one marketing label:
1. **Rank tracking** (DataForSEO, daily, real) — position of tracked terms.
2. **Keyword-set control** (real-time, and explicitly *not* real-time — it's a monthly autonomous pass).

**[D]** The read-only design is a real product weakness, honestly documented: users cannot override the agent's keyword choices through the UI. That is a clean, obvious differentiator for any competitor — **a human-editable keyword board with intent clustering, cannibalisation warnings, and "park" semantics** — because the current design forces the only lever to be Brand Context prose **[D]**.

**[D]** Also documented and correct: the highest-ROI targets are **"queries where you already receive impressions but rank poorly"** — demand proven, page exists **[D]**. This is the "strike distance" segment and the one Ryze's audit surfaces as *"Pages with impressions but poor rankings… If you read one section, read that one"* **[D]**. Strong product thinking, correctly prioritised.

### Live Site Implementation
- **DataForSEO** — Ryze's proprietary dependency for volume, difficulty, intent, CPC, rankings, competitors, DR, backlinks **[D]**. No direct DataForSEO integration is offered to users; the user may optionally add **Ahrefs** (API v3, own key, own unit balance) or **Semrush** (own key, own unit balance) for backlink/keyword data **[D]**.
- **GSC** for the impression/click/strike-distance data **[D]**.
- No crawl is needed for rank tracking itself.

### Alternative / Improved Approaches
- **A human-editable keyword board.** Trivial to build, removes the single most-complained documented limitation, and creates a natural place to surface cannibalisation and intent overlap. High value, low cost.
- **Click-through / SERP-feature aware position.** Position 3 with a featured snippet is not position 3. Parsing SERP features from DataForSEO and reporting *"effective position"* is a genuine analytic improvement.
- **Velocity alerting, not level reporting.** Alert on rank *velocity* changes with a significance floor, not on level crossings. A daily position feed without statistical discipline is noise generation.
- **Cannibalisation detection as a paid audit.** Cluster the client's own ranking URLs by query overlap and report pages competing with each other. Directly actionable, purely analytic, and it maps exactly to the "one page per intent" rule Ryze states but does not appear to enforce.

---

## 2.4 Converting Reddit posts into traffic engines

### Definition
Discovering high-signal Reddit threads where a brand is genuinely relevant, and converting that attention into owned or trackable traffic — legitimately, through participation rather than spam.

### Ryze's Execution (Reverse-Engineered)
**[D]** — The **only** documented Reddit capability is a search tool: **"Reddit search — 0.5 credits"** (`/agent/credits`). Reddit Ads exists as a *paid-media* integration **[D]**, entirely separate from SEO. The `Reddit` item in the "Content" feature list on `/seo` **[M]**, alongside "Quora & Medium posts" **[M]** and "Wikipedia mentions" **[M]**, has **no corresponding help-center page** **[X]**.

**[D]** The closest adjacent documented feature is the Approvals **Opportunity** type: *"a thread where competitors are named and you aren't"* **[D]**. Opportunities render a **draft body** with a copy button and are classified **"Can be applied by you"** — *"those offer `I applied it` and `Not for us` instead of Approve/Reject"* **[D]**.

**[I]** This is the answer, and it is a good one: **Ryze does not post to Reddit. It surfaces Reddit opportunities and hands the human a draft.** The architecture is:
1. Reddit search (0.5 credits/call) **[D]** across the tracked prompt/keyword space.
2. Thread scoring: relevance to a priority keyword + recency + engagement + whether competitors are named **[D]**.
3. Draft generation **conditioned on Brand Context** — including the "claims you must not make (legal, medical, compliance)" rules **[D]**, which is exactly the control a Reddit-reply generator needs.
4. Delivery as a **human-applied Opportunity** with copy-paste text and a disposition button **[D]**.
5. The human posts under their own account; Ryze records `I applied it`.

**[I]** This is the **only defensible design**. Reddit's ToS prohibits automated posting; account bans are the failure mode and the account is the client's brand. Automating the *research and drafting* while a human performs the *impounding action* is correct risk architecture — and it is the same pattern as Ryze's manual mode for Squarespace, Lovable, and Base44 publishing **[D]**. It also means the marketing line "Converting Reddit posts" **[M]** overstates: the product converts Reddit *research* into human-executed posts. **A competitor should not "beat" this by automating the posting; they should beat it on discovery quality and thread-level analytics.**

### Live Site Implementation
- Reddit OAuth for API access, **or** unauthenticated search + public JSON endpoints. **[I]** Aggressive automated posting → account destruction, ToS violation, and the loss of the client's brand account. Unacceptable risk.
- UTM-tagged landing pages for attribution.
- Manual human action, copy-button UX, disposition tracking **[D]**.

### Alternative / Improved Approaches
- **Thread-level ROI attribution.** UTM + a "posted on" date per thread, so the *client* can see which threads produced traffic. Ryze records `I applied it` **[D]** but the docs do not describe linking that to a measured outcome. This is a clean, honest, closed-loop product.
- **Comment-to-page matching.** Score not just thread relevance but whether the client's site has a page that *answers the exact question asked in the thread*. This is a retrieval problem, and Ryze already has a corpus of generated pages — pairing is cheap and would materially raise reply quality.
- **Community-memory value.** Reddit threads are indexed by Perplexity and ChatGPT **[M — and supported by Ryze's own docs that Perplexity cites Reddit-style sources heavily]**. A tracked thread that later gets cited is a compounding asset. Track citation of threads, not just clicks.
- **Be explicit in positioning.** Ryze's own docs are more careful than its landing page. Match the docs, not the landing page.

---

## 2.5 Quora & Medium syndication / posting

### Definition
Q&A-platform participation (Quora) and long-form publishing platforms (Medium) used as third-party presence and citation sources.

### Ryze's Execution (Reverse-Engineered)
**[X]** — **Not documented anywhere in 190+ help-center pages.** "Quora & Medium posts" appears only in the marketing feature list on `/seo` **[M]**. The nearest documented behaviours:
- **Medium appears once**, in the `/seo` page's DR-30+ backlink showcase as a *prospect* — `medium.com/@author  DA 76  Outreach` **[M]** — i.e. a **link-acquisition target**, not a publishing destination.
- Off-page outputs documented in full are only three: **guest posts, press releases, backlink exchange** **[D]**.
- All documented publishing targets are CMS/platform destinations the client owns **[D]** — Medium and Quora are not among them.

**[I]** Two possible implementations, and neither is knowable from public material:
- **(a) Opacity** — there is a real Quora/Medium surface that is not documented because it is manually operated by the "senior strategists" attached to Traffic Printer/Ecom tiers (human-run, hence absent from self-serve docs). This is the *most likely* explanation and it is consistent with "Traffic Printer = full service" **[D]**.
- **(b) Marketing drift** — the feature list is a category claim ("third-party presence") that outran the product.

**[I]** Important architectural note either way: **Medium and Quora are hostile to automated posting.** Medium requires a Google account; Quora aggressively enforces anti-automation. Any implementation must be human-in-the-loop at the posting step, exactly as Reddit. The structural constraint is identical.

### Live Site Implementation
- Quora: no official public posting API; OAuth for business pages only. **[I]** Human-in-the-loop required; high ban risk on automation.
- Medium: no general publishing API; integration via account credentials. **[I]** Human-in-the-loop required.
- Both: **UCP / attribution** and brand-account credential management.

### Alternative / Improved Approaches
- **Separate the claim from the product.** "Third-party presence" is a real strategy; automated Quora/Medium posting is not a defensible product. A competitor should sell *measured third-party placements* (guest posts, PR, editorial) and not claim automated Q&A posting.
- **If building it: citation-first framing.** Ryze's own docs establish the hierarchy: third-party mentions are *"the only real lever on brand mentions in AI answers"* **[D]**, and *"[D]o not claim a specific citation lift"* for anything less rigorous. A Quora/Medium module should be positioned as *brand-mention surface area* with an explicit, long timeline, exactly as Ryze positions share-of-voice **[D]**.
- **Answer-engine share as the only real KPI.** A Quora answer that gets cited by Perplexity is worth 100 answers that get read. Instrument for citations, not traffic.

---

## 2.6 Wikipedia mentions

### Definition
Getting a brand or its key facts referenced in Wikipedia or its derivatives. Extremely high authority, extremely difficult to achieve, governed by **notability** policy rather than marketing.

### Ryze's Execution (Reverse-Engineered)
**[X]** — **Not documented.** "Wikipedia mentions" appears only in the `/seo` marketing feature list **[M]**. Zero help-center coverage. No Wikipedia or wiki integration in the documented 50+ integration list **[D]**. The documented integrations are: Analytics (GA4, GSC, Ahrefs, Semrush, PostHog, AppsFlyer); Commerce & CMS (17 platforms); CRM; Ads (12); Messaging (3); Files; AI **[D]**. No wiki.

**[I]** Wikipedia is the **least plausible** item in the entire feature list, and its presence is a useful diagnostic. A tool that genuinely placed Wikipedia citations would:
1. Have a documented, high-touch process — Wikipedia is the one domain where automation is *actively harmful*.
2. Document the **notability** prerequisite, since that is 100% of the difficulty and 0% of the tactic.
3. Never frame it as a volume feature alongside "300+ niche directories".
4. Require identity verification, COI disclosure, and a human editor relationship.

None of those apply. **[I] The most likely explanation: the "Wikipedia mentions" line reflects Wikipedia *replication* — Ryze's own site and its client base are documented as being scraped and republished by Wikipedia-derived systems, plus the general fact that a high-volume brand-mention footprint is what eventually gets reflected in encyclopedic sources. The marketing list has compressed a months-long, non-causal, partly-passive outcome into a feature bullet.**

### Live Site Implementation
- **No API exists for creating Wikipedia articles.** Editing is via MediaWiki's API, requires an account with a history, is subject to **conflict-of-interest disclosure**, and every claim needs a **reliable independent source** — i.e. the press-release and guest-post output of the same product **[D]**.
- Strictly: **notoriability policy** (WP:NOTABILITY) governs whether a topic may have an article at all.
- **Wikipedia:NOADS, no advertorial, no COI-undeclared PR** — all long-standing policy.
- **[I]** A defensible product would expose this as a **read-only readiness diagnostic**: assess notability evidence coverage, identify the independent third-party sources that exist today, and report gaps. Zero automation, zero policy risk, genuine value.

### Alternative / Improved Approaches
- **Sell notability readiness, not mentions.** A "Wikipedia readiness" audit — how many independent sources exist, their independence (not PR-distributed), coverage of the claims, and the gap list — is a credible, high-value, zero-risk product.
- **Sequence it correctly.** Press releases and guest posts are the *inputs* to notability. Ryze documents these **[D]** and Ryze's own docs are the right framing: *"Brand mentions come largely from training data, and training data is other people's content"* **[D]**. The honest architecture is: build third-party mentions for months → they compound into encyclopedic and model-prior presence. That is a **narrative**, not a feature.
- **Competitor advantage: credibility.** A competitor that *excludes* Wikipedia-automation claims and instead publishes a notability methodology has a trust advantage with the exact buyers (SEO directors, agency owners) who can tell the difference. Ryze's docs already have the honest version; only the landing page overreaches.

---

# PART 3 — BACKLINKS

## 3.1 Acquiring up to 500+ backlinks per month

### Definition
The volume and velocity of inbound links from third-party domains to a client's pages. Volume alone is inert or harmful; relevance, editorial context, and anchor naturalness determine value **[D]**.

### Ryze's Execution (Reverse-Engineered)
**[X]** — **The headline number does not survive contact with the documentation.**

| Source | Claim |
| --- | --- |
| `/seo` landing page **[M]** | "Up to **500+ backlinks per month**" |
| `/pricing` **[M]** | "**50-100**" backlinks/month (Traffic Printer) |
| `/plans/whats-included` **[D]** | "**100+/mo**" (Traffic Printer) |
| `/plans/usage-limits` **[D]** | "**50+** high quality backlinks per month" |
| `/seo/backlinks` **[D]** | "around **100** backlinks a month" (partner network, "the main driver") |
| `/seo/faq` **[D]** | "around **100** backlinks a month" |

**Every documented number is 50–100. The 500+ claim appears only on the landing page.** **[X]** This is a **5× discrepancy on the flagship off-page metric**, between the page a prospect reads first and every page the vendor's own documentation maintains. Any serious competitive analysis must treat 500+ as unverified.

**[D]** The actual mechanism, per `/seo/backlinks`, is three channels:
1. **Partner network** — *"Ryze runs a large network of partner sites, which places links back to your pages at volume. This is the main driver"* **[D]**.
2. **Guest post placements** — each placed post links back **[D]**.
3. **Press syndication** — each newsroom pickup is a link **[D]**.

**[I]** Critical structural insight: **the dominant channel (partner network) is reciprocal, and Ryze's documentation is unusually candid about exactly how that works** (`/seo/backlink-exchange`):
- **At most one exchange link per ~40 articles** **[D]**
- Placed in the **second half** of the article, woven into a sentence that already makes a point; *"never a 'useful resource' footnote, and never in the intro"* **[D]**
- **Only when a relevant partner exists** — *"Most articles get no exchange link at all"* **[D]**
- Never matched with: direct competitors (permanently, including same brand on another domain), the client's own other workspaces, sites sharing only geography/industry, or prospects **[D]**
- **Eligibility:** connected store + active plan (both directions) **[D]**
- **Ryze-hosted blogs on `*.byryze.com` are excluded** — only custom-domain targets participate **[D]**
- Distribution fairness: sites with fewer received links surface first **[D]**
- Opt-out: Content settings → Backlink exchange toggle **[D]**

**[I]** **This is the most technically interesting thing in Ryze's entire product, and it is invisible in the marketing.** A reciprocal network requires:
- A **bipartite matching** over (article, partner-site) with a hard relevance predicate — solved as an assignment problem with constraints, not a random rotation.
- A **relevance classifier** that must satisfy the "a reader of *that specific article* would click through" standard **[D]** — i.e. a per-article semantic match, not a site-level category match.
- **A rate limiter with a global stateful counter** (1 per 40 articles) and a **placement-position constraint** (second half, inline, not a footer).
- **A suppression list** (competitors, own network, prospects) that is permanent.
- **Fairness scheduling** (least-received-first) and **eligibility gating** (store connected + active plan + custom domain).

That is a real, non-trivial distributed system. It is also the mechanism by which 50–100 links/month is achieved without the site reading like a link farm — which is precisely why the ceiling is ~100 and not ~500. **The 1-in-40 rate limit is the binding constraint on the headline number.** At ~90 articles/month, 1-per-40 yields ~2 exchange links/month of that type; the rest must come from guest posts and press syndication. A vendor claiming 500+/month is claiming something its own documented rate limit forbids.

**[D]** Honesty that is genuinely creditable and rare: *"A handful of relevant, editorially-placed links outperforms hundreds of low-quality ones. Volume without relevance is at best inert and at worst a liability."* **[D]** And: *"Directory and profile links"* and *"Sitewide footer links"* are explicitly listed under **"does not matter"** **[D]**. And: *"Ryze cannot guarantee placement on any specific site. Editorial decisions belong to publishers."* **[D]**

### Live Site Implementation
- **Guest posts / PR:** Ryze-side only. No client permission required beyond approving drafts. Placement is performed by Ryze **[D]**.
- **Backlink exchange:** requires a **connected store on the client's own domain** + active plan **[D]**. On Shopify this is the OAuth connection; on headless, the GitHub App or custom webhook.
- **Verification:** every link recorded with anchor text, referring URL, target page, and target keyword **[D]**; authority tracked from **DataForSEO** (DR, total backlinks, referring domains over time) **[D]**.
- **Not required:** link outreach, guest-post networks, directory submission lists.

### Alternative / Improved Approaches
- **Reframe the product around link quality, and let the count be whatever it is.** Ryze's own docs argue for this **[D]**. A competitor selling "20 DR-40+ editorial links/month, each reviewed" at $599 is a *stronger* offer than "500 backlinks" at $599, because the 500 number is not credible and the quality number is.
- **Publish the exchange algorithm.** Ryze's 1-in-40, second-half, relevance-gated, fairness-scheduled placement is a defensible engineering artefact. Documenting it openly converts a hidden mechanism into a trust asset — and makes it much harder to attack.
- **Make the reciprocal network the moat.** A network that requires connected stores on real domains is a **supply-side moat** with real switching costs, exactly like an ad exchange. That is a much bigger business than link brokerage, and it is under-sold.
- **Detect and report paid-link risk continuously.** If a client's profile drifts toward what Ryze's own docs call "not worth having" **[D]**, flag it. Nobody does this as a service.

---

## 3.2 Securing DR 30+ backlinks

### Definition
Acquiring links from domains with a third-party authority metric (Ahrefs DR, Moz DA) of 30 or above — a proxy for a site with real, independent editorial authority.

### Ryze's Execution (Reverse-Engineered)
**[D/M]** — On `/seo` **[M]**, a visual panel lists prospects with their authority: `forbes.com DA 95 Live`, `wsj.com DA 94 Live`, `techcrunch.com DA 93 Live`, `businessinsider.com DA 92 Live`, `axios.com DA 89 Pending`, `fortune.com DA 88 Pending`, `medium.com/@author DA 76 Outreach`. The section header reads **"DR 30+ backlinks"** and the bullets are *"Blog posts, guest articles, Reddit, PR"*.

**[I]** **Read this panel as a pipeline UI, not a results chart.** The status vocabulary is revealing:
- `Live` — content is published on the domain
- `Pending` — submitted, awaiting editorial decision
- `Outreach` — contact attempted

This is a **prospect/status board**, not a proof of acquisition. `forbes.com` at DA 95 does not run programmatic guest-post placements at volume; "Live" almost certainly means *a Forbes contributor post exists* — a one-off, human-mediated, brand-specific outcome. Meanwhile, in their own `/seo/backlinks` docs, the *actual* volume driver is described as **"Ryze runs a large network of partner sites"** **[D]** — i.e. Ryze-owned/controlled properties, whose DR is a function of Ryze's own domain equity, not of independent editorial authority.

**[D]** And here is the sharpest internal contradiction in the entire product: Ryze's own `/ecommerce/backlinks-mentions` **[D]** explicitly lists as **"does not matter"**:

> Directory and profile links · Sitewide footer links · Exact-match anchors repeated at scale · Nofollow-only placements

…and states **"A handful of relevant, editorially-placed links outperforms hundreds of low-quality ones."** Meanwhile the `/seo` landing page markets a **DR/DA leaderboard** as the headline proof. The docs are right and the landing page is optimising the wrong metric — and a buyer who reads both will notice.

**[D]** The genuine and correct mechanism underneath: **the anchor-text portfolio.** *"Natural profiles are mixed: brand names, URLs, generic phrases, and descriptive text."* **[D]** Maintaining a mixed anchor distribution across ~100 links/month requires real per-placement control — a constraint solver over (target domain, target page, anchor text, placement context) that keeps exact-match share below some threshold. **[I]** That constraint solver is the actual engineering asset behind "DR 30+ backlinks"; the DR number is a display of it.

### Live Site Implementation
- No client-side requirement for the *placement* itself.
- The **client's authority is the input**: DataForSEO DR, referring domains, and topical relevance of the client's own site determine which partners will accept. **[I]** This is why the docs say *"Links cannot fix a weak site. Authority pointed at thin content still ranks thin content"* **[D]** — the network's placement quality is gated on client authority, so a new domain gets a worse mix. **Expect link quality to be strongly correlated with starting DR**, and to say so.
- Requires the client's site on its own domain (eligible for exchange) **[D]**.

### Alternative / Improved Approaches
- **Sell the anchor distribution, not the DR number.** Publish the client's actual anchor mix, link velocity, referring-domain growth, and toxicity score. That is a defensible, verifiable claim in a category full of unverifiable DR boasts.
- **Origami of the moat: the network's own DR.** A reciprocal network's aggregate authority is a compounding asset. Report "median partner DR of your network" and how it grows — that is a real, differentiating metric.
- **Vertical relevance beats raw DR.** A DR 35 niche-forum link with 100% topical relevance is worth more than a DR 90 general-news link. Build the product around *relevance-weighted* authority — a real gap in every DR-selling tool.
- **Third-party verification of placements.** Independent verification that a claimed link is still live, still dofollow, on the claimed domain. Cheap to build, and it is the exact failure mode ("a host site later drops" **[D]**) that Ryze's own FAQ admits it can no longer replace after cancellation.

---

## 3.3 Generating 100+ PR press releases

### Definition
Producing newsroom-format announcements distributed through a syndication wire, generating pickup on news and press sites with links back to the client's domain.

### Ryze's Execution (Reverse-Engineered)
**[X]** — "**100+ PR press releases**" **[M]** appears **only** on the `/seo` landing page. The documentation **[D]** describes press releases as a per-cycle output in the same `planned → drafted → published` state machine as articles **[D]**, with **no volume figure at all**, and `/plans/whats-included` **does not list press releases under any plan** — the Traffic Printer row says only "Backlink building 100+/mo" and "Guest posts". **[D]**

**[D]** The documented pipeline (`/seo/press`):
1. **Drafted** — title, summary, body, around the company and a key page
2. **Review & approve** — client edits and approves
3. **Publish** — "Ryze submits the release to the newswire for distribution"
4. **Syndicated** — "The newswire syndicates it to news outlets. Ryze tracks the submission and updates the status once it's live."
5. Company details (name, contact, country) from workspace settings are used to build the release **[D]**

**[D]** **The restricted-industry logic is the most revealing engineering detail in the whole document set.** Ryze's newswire editors apply stricter rules to certain verticals. For *designer-styled physical goods, supplements and wellness, cosmetic or surgical practices, and crypto/gambling/cannabis* **[D]**:
- A **ranking-style** release ("Top 7…", "Best … of 2026") is **rejected on format alone** **[D]**
- A plain **company announcement** about the same business **passes** **[D]**
- Ryze **auto-rewrites bounced releases as announcements and resubmits** **[D]**
- "The draft you review will simply be shaped as an announcement" **[D]**

**[I]** This is an **industry-conditional generator mode with a classify→rewrite→resubmit loop**. Concretely: an industry classifier; a per-industry release *format policy*; a bounce-classification step on the wire's rejection; and a rewrite that changes the release's rhetorical type (ranking → announcement) while preserving the underlying news. That is a real, specific, hard-won integration — and it is the kind of detail you only get from a real newswire relationship. It is also the strongest evidence in the entire audit that the PR channel is **real and operational**, not vaporware.

**[I]** But the wire's economics make 100+/month implausible on its own terms. A syndicated wire charges per release and gates on editorial quality; a ranking-style release from a supplements brand is rejected on sight **[D]**. 100+/month of *accepted* releases would (a) require wire volume pricing, (b) risk the account, and (c) produce a profile of hundreds of thin PR pages — precisely the pattern Google has discounted since 2016. **The honest read: the volume exists as a target (Ryze writes N drafts) and the accept rate is the real constraint.**

### Live Site Implementation
- **Client side:** approve/edit drafts (no integration required), plus company name/contact/country in workspace settings **[D]**.
- **Ryze side:** a newswire account relationship with per-industry editorial policy. This is a **contractual, non-replicable asset** — a competitor cannot buy their way to the same wire account, review history, and industry classification.
- **Tracking:** submission status polling; each pickup recorded as a backlink with anchor, referring URL, target page, keyword **[D]**.

### Alternative / Improved Approaches
- **Sell pickups, not submissions.** The only number that matters is *accepted, live, linking* placements. Publishing the accept-rate and live-pickup count is credible precisely because it is a small number; "100+ releases" is neither verifiable nor impressive.
- **The newswire relationship is the moat — act like it.** The industry-conditional format policy is a real proprietary asset. Documented publicly, it becomes a differentiator against every other AI PR tool.
- **Digital PR as the actual product.** Real editorial placements (a journalist's newsletter, a niche publication's contributed piece, a podcast) outperform wire syndication and cannot be scaled by anyone — deliberately. That is a $3,000–10,000/month service line, not a $599 line item.

---

## 3.4 Automating / scaling guest posts

### Definition
Writing articles for publication on third-party sites, in exchange for a link back to the client's domain. A link-building tactic that produces editorial-quality placements and side-loads brand content onto partner domains.

### Ryze's Execution (Reverse-Engineered)
**[D]** — `/seo/guest-posts` documents the entire flow:
1. **Drafted** — "Ryze writes a guest-post draft — body, meta title, and meta description — built around your priority keywords and pages."
2. **Review & approve** — "You review the draft and edit it as needed, then approve it for placement."
3. **Published** — "The post is placed on the target publication, and **Ryze records the placement URL**."

Status flow shared with the rest of the engine: `planned → drafted → published | failed` **[D]**.

**[I]** This is the one off-page channel with a **human in the critical path by design** — the client approves every draft before placement. The pipeline is:

```
keyword/page target → draft (LLM, Brand Context conditioned)
  → CLIENT APPROVAL  ← mandatory gate
  → editorial placement (Ryze-side, relationship/manual)
  → placement URL recorded
  → backlink row: anchor, referring URL, target page, target keyword
```

**[D]** The client approval gate on guest posts (but *not* on articles) is a meaningful signal: guest posts carry **brand-reputation risk** on a domain the client does not control, so they are gated harder than content on the client's own site. That is correct risk design and worth calling out.

**[D]** **[I]** The **target-publication selection** is the un-documented part and the hardest. Documented constraints: the `/seo` page's panel shows targets with statuses `Live` / `Pending` / `Outreach` **[M]**, and Medium is a target with status `Outreach` **[M]**. So there is a prospect pipeline with a state machine. **[I]** Realistic implementation: a prospect database (sector, DR, traffic, topical fit, prior acceptance, contact channel), a match score against the client's priority keywords, outreach generation, and a manual/Ryze-side placement step. **This is a relationship-and-database business, not a code business** — which is why it is the slowest part of the engine to scale and why it caps at tens, not hundreds, per month.

### Live Site Implementation
| Requirement | Detail |
| --- | --- |
| **Client approval** | Mandatory per draft **[D]**. No write access to client systems required |
| **Brand Context** | Governs voice, claims, and references **[D]** |
| **Placement** | Ryze-side only. No client integration, no credentials, no guest-post network access required |
| **Recording** | Placement URL captured as a backlink row **[D]** |
| **Tracking** | Share the same calendar as programmatic articles **[D]** |

### Alternative / Improved Approaches
- **Replace the prospect database with a qualification model.** Rather than "find publications," model the inverse: given a target domain, score it for (topical fit, prior guest-post acceptance, editorial quality, indexation of contributed pages, spam history) — and refuse low-quality placements. A tool that *declines* placements is trusted; one that maximises volume is not.
- **Make acceptance the KPI.** Guest-post outreach conversion (drafted → placed) is a real, measurable, honest metric that nobody publishes.
- **Editorial differentiation as the product.** A guest post that reads as AI filler on a real publication is worse than no post — it burns the relationship. The differentiator is voice quality against the *publication's* style, not the client's. Ryze has blog examples in Brand Context **[D]**; extending that to per-publication style conditioning would measurably raise acceptance.
- **Volume is capped; quality is not.** Guest posts will never be 500/month. Sell 20 well-matched placements, not 500 guest posts.

---

## 3.5 Broken link detection (for link building)

### Definition
Finding broken or redirected links on *other* websites that could point at a client's relevant page — classic broken-link building, a legitimate and effective outreach tactic. **Note the distinction from §1.6: that is fixing your own broken links; this is harvesting other sites' broken links.**

### Ryze's Execution (Reverse-Engineered)
**[X]** — "Broken link detection" appears in the **Backlinks** section of the `/seo` feature list **[M]**. The help center's `/seo/backlinks` **[D]** lists three channels only — **partner network, guest posts, press syndication** — and does not mention broken-link building. `/ecommerce/backlinks-mentions` **[D]** lists the same three. **No broken-link-building documentation exists.**

**[I]** The likely mapping is that "broken link detection" on the landing page is a **compression of the *technical* broken-link detection documented under Site Fixes** (§1.6: "Broken internal links and 404s that still receive traffic or links" **[D]**, "Broken link detection" appearing in the audit list). A marketing page compressing "we detect broken links" into an off-page feature list is a plausible category error. Alternatively, it may be a real, undocumented outreach module. Unknowable from public material.

**[I]** If it *is* real, the architecture is: crawl a prospect universe → identify outbound links returning 404/410 or redirected → extract the anchor text and target context → match against the client's catalogue (does a live, relevant client page exist?) → score by (prospect authority × topical fit × brokenness) → generate a personalised outreach email from Brand Context → deliver as a human-applied **Opportunity** **[D]**, not an automated send.

### Live Site Implementation
- **Crawl at scale** — the entire cost driver. A serious broken-link index is a continuous crawling operation (like a Common Crawl derivative), not a per-client job.
- **Reddit/Quora/Medium/Wikipedia**: for Wikipedia, the relevant signal is **dead links in existing articles** — a genuinely powerful, legitimate, and non-promotional tactic. But see §2.6: requires manual, disclosed, careful editing.
- **Deliver:** email/Slack with a one-click "I applied it" — matching the documented Opportunity model **[D]**.
- **SMTP / sending infrastructure** — the hard part, and the reason human-in-the-loop matters.

### Alternative / Improved Approaches
- **Sell it as a *relevance* product, not a *brokenness* product.** "We found 4,000 dead links to your competitors' products" is interesting. "Here are 300 pages ranking for commercial queries that link to a dead product in your category, and you have the exact replacement" is a **closing tool**. The insight is that broken-link building is really *gap analysis on the live web*, and the broken link is just the delivery mechanism.
- **Build the crawl index as the moat.** A continuously-crawled corpus of commercial web pages is an enormous, expensive, defensible asset. This is a business, not a feature.
- **Close the loop with the Agency opportunity model.** Ryze already has "a thread where competitors are named and you aren't" **[D]** — the same delivery primitive, one prompt broadened. Low marginal cost, genuine incremental value.

---

## 3.6 Submissions to 300+ niche directories

### Definition
Submitting a business listing to hundreds of small, topically-relevant web directories, historically for citation building and local/entity SEO.

### Ryze's Execution (Reverse-Engineered)
**[X]** — "**300+ niche directories**" appears **only** in the `/seo` landing page **[M]**. No documentation. And more importantly, the documentation **actively contradicts the value proposition**:

**[D]** — `/ecommerce/backlinks-mentions`, under the heading **"What makes a link worth having"**, column **"does not"**:

> **Directory and profile links**

**[D]** — and: *"Links cannot fix a weak site"*; *"Volume without relevance is at best inert and at worst a liability."*

**[D]** Google's own posture is well known and Ryze's docs reference it: thin content at scale is *"worse than publishing nothing"* **[D]**.

**[I]** This is the clearest example on the page of a marketing claim contradicted by the vendor's own documentation. The landing page's Backlinks section contains four bullets — "Up to 500+ backlinks per month", "DR 30+ backlinks", "100+ PR press releases", "300+ niche directories" — of which **two are contradicted outright by the docs, one (500+) is 5× the documented figure, and one (100+ PR) is undocumented.** The Backlinks list on the landing page is substantially aspirational.

**[I]** **Is there a legitimate version?** Yes, and it's worth being precise about it, because it is a defensible product if framed correctly:
1. **Not for link equity.** Their docs are right that directory links don't pass authority in any meaningful way.
2. **For entity disambiguation and data consistency.** Consistent NAP (name/address/phone), category, and business description across hundreds of citations genuinely helps machines disambiguate which entity a brand refers to. That matters **more** in the AI-answer era than it did in 2015, precisely because retrieval and entity resolution are the bottleneck for being named correctly in generated answers.
3. **For knowledge-graph ingestion.** Wikidata, Apple/Google business knowledge panels, and the LLM training corpus all draw from structured directory data.

So the honest positioning is: **entity-data consistency at scale, not link acquisition** — and the KPI is **accuracy and coverage of entity data**, not link count. Ryze's docs would agree. Its landing page does not.

### Live Site Implementation
- **Per-directory automation** — each directory has a different form, often captcha-gated, often requiring an account. Realistically: a mix of structured submission APIs, semi-automated form-filling (headless browser), and manual submission.
- **Massive account/credential surface** — hundreds of directory accounts under one brand. A single compromised credential exposes all.
- **Uptime/citation verification**: directories are frequently spammy, and a wrong listing is a real reputational cost. Continuous NAP-consistency monitoring is arguably the more valuable product.

### Alternative / Improved Approaches
- **Reframe to entity management, and win on it.** "Entity consistency across 300+ data sources, continuously monitored for accuracy" is credible, valuable, and defensible. "300+ directory submissions" is neither. Same execution, honest framing, and it survives scrutiny.
- **Prioritise the 20 that matter.** Wikidata, Google Business Profile, Apple Business Connect, Bing Places, the major industry associations, and the handful of directories that actually get scraped by LLMs. 20 sources done correctly beat 300 done carelessly — and Ryze's own docs say so **[D]**.
- **Sell monitoring, not submission.** A continuously-verified NAP/entity audit is recurring-value software; a one-off submission is a commodity service. Ryze's own docs argue that a handful of relevant links beats hundreds **[D]** — apply the same logic to citations.
- **The competitive opening is explicit.** Ryze's own docs say directory links don't matter. A competitor that says this **and can be trusted** because its landing page matches its documentation is winning on credibility, not on features.

---

# PART 4 — REPORTING

## 4.1 Daily rank tracking

### Definition
Systematic measurement of SERP positions for a keyword set on a fixed cadence, with movement surfaced to the user. The oldest and most trusted SEO measurement product.

### Ryze's Execution (Reverse-Engineered)
**[D]** — **daily**, stated in three separate documentation pages **[D]**. Source: **DataForSEO** **[D]**. Cost: 0.5 credits per SEO data lookup **[D]**.

**[D]** The **reporting discipline** around it is more interesting than the feature, and it is genuinely excellent:
- *"Organic rankings — **weekly at most**. Daily rank data exists, but daily reporting on it invites reacting to noise."* **[D]**
- *"Reporting more often than a metric meaningfully changes does not give you more information — it gives you more noise to react to."* **[D]**
- *"Average position is the most misleading number in SEO."* A falling average alongside rising impressions and clicks is *good* **[D]**.
- GSC's own 2–3 day reporting delay makes the last 48 hours useless **[D]**.
- Seasonality dominates short windows: compare year over year, not week over week **[D]**.

**[I]** **Collect daily, report weekly, alert on velocity.** The data collection is a scheduled DataForSEO pull per workspace (SERP endpoint, device/location parameters matched to workspace `Location` **[D]**) writing positions to a time-series table. Reporting is a separate, opinionated layer. This three-way separation — *collect / report / alert* — is the correct architecture and most competitors conflate it.

**[I]** The **location/device dimension** is load-bearing and easy to miss: keywords are country-specific **[D]** ("Volume and difficulty are pulled live from DataForSEO and reflect your workspace's country"). So a multi-market client needs per-market rank series. DataForSEO charges per SERP request, so this is a real cost driver at scale — and a place where sampling strategies (e.g. daily for tier-1 keywords, weekly for the long tail) materially change the unit economics. **[I]** Nothing in the docs suggests Ryze samples. **Opportunity.**

### Live Site Implementation
- **DataForSEO SERP** — Ryze's dependency. Users can substitute **Ahrefs** or **Semrush** with their own API keys for organic keyword data **[D]**, which is documented as consuming *their* unit balance, not Ryze credits **[D]** — a sensible pass-through.
- **GSC** for impressions/clicks/CTR/position cross-validation.
- **No crawl required** for rank tracking itself.
- Alerts to Slack and email **[D]**.

### Alternative / Improved Approaches
- **Cross-validate position against GSC.** Show DataForSEO's tracked position *and* actual GSC average position for the same queries, and flag divergence. Divergence means either a device/location mismatch or a genuine tracking gap. Nobody surfaces this; it is a trust-building differentiator.
- **Sample intelligently.** Tier-1 keywords daily, long-tail weekly, re-tiered by velocity. Same information, materially lower cost — and it lets a competitor undercut on price.
- **Velocity and significance, not level.** A daily *change* alert with a confidence floor, not a daily "you moved from 8 to 9" notification. Ryze's own noise argument **[D]** applies to its own product.
- **Rank distribution, not average.** Median, p25/p75, and share-of-voice by position bucket. Ryze correctly calls average position misleading **[D]** and then reports average position. Internal inconsistency worth exploiting.

---

## 4.2 AI search citations (brand presence in AI Overviews)

### Definition
Measuring how often and how prominently a brand and its URLs appear in generated answers from ChatGPT, Perplexity, Google AI Overviews, Claude, Gemini, Bing and Shopify search. Ryze's term for this discipline is **GEO — generative engine optimization** **[D]**.

### Ryze's Execution (Reverse-Engineered)
This is Ryze's most distinctive and most thoroughly documented capability **[D]** — and, on the evidence, the part where its docs and marketing are most closely aligned.

**[D]** **The unit of measurement is a prompt, not a keyword.** *"There is no ranked list in a generated answer — a page is either drawn in or is not."* **Up to 50 prompts per workspace** **[D]**, grouped into **topics**, with **one branded topic** ("Your brand vs competitors") doubling as competitor discovery and the rest category topics **[D]**. **25 prompts seeded across 5 topics** at setup; **Suggest** adds more server-side without duplicating **[D]**.

**[D]** Per-run capture: brand named y/n; every cited URL + domain; competitor brands; where the brand fell in the answer; **the full answer text** for later re-examination **[D]**.

**[D]** The six metrics, with formulas **[D]**:

| Metric | Formula | Meaning |
| --- | --- | --- |
| **Mention rate** | `answers naming your brand / total answers` | Brand establishment (from priors), *not* content quality |
| **Citation share** | `citations to your domain / total citations across all domains` | Counted **by domain**, so one answer citing 4 pages doesn't inflate. **Most responsive to content work** |
| **Share of voice** | your mentions / all brand mentions | All tracked brands sum to 100% |
| **Answer coverage** | prompts where you appeared at all | Broadest, too coarse for diagnosis |
| **Competitor overlap** | co-occurrence frequency | Reveals the *real* competitive set |
| **Position in answer** | first recommendation / in list / passing mention | First-position carries disproportionate weight |
| **Visibility by assistant** | that assistant's answers naming the brand / its answers | Brands × assistants matrix |

**[D]** **Cadence — a major, and defensible, divergence between Ryze and typical competitors:**

| Data | Cadence | Source |
| --- | --- | --- |
| Rank tracking | **daily** | **[D]** 3 pages agree |
| AI visibility runs | **monthly**, once per billing cycle (**Refresh** on demand) | **[D]** 3 pages agree |
| **Prompts** | **once per day** (re-runs same day only for new/failed prompts) | `/ai-visibility/prompt-monitoring` **[D]** |

**[X]** **Internal inconsistency worth noting:** the marketing pages say **"Weekly checks of whether ChatGPT, Claude, Gemini and Perplexity mention your brand"** and **"Delivered weekly, every Wednesday"** **[M]**, while the documentation says runs are **monthly** **[D]** and that *"AI visibility runs — monthly, once per billing cycle"* **[D]**. The `prompt-monitoring` page's "prompts run once per day" **[D]** may describe a lighter-weight pass than a full citation run. **The docs are internally consistent; the marketing is not.** The monthly cadence is *better engineering*, with an explicit rationale:

> "Daily AI visibility numbers would be actively misleading… A daily figure therefore measures engine randomness far more than it measures your site. Reacting to it produces false alarms." **[D]**

**[I]** This is the right call and most competitors get it wrong. The measurement problem is severe: *"Only a minority of cited URLs repeat between two runs of the same prompt"* **[D]**. The variance is dominated by engine stochasticity, so any daily signal is ~90% noise. Monthly runs maximise the signal-to-noise ratio per unit of API cost. **A competitor copying Ryze's cadence but not its reasoning will over-promise; one that adopts the reasoning will beat it on trust.**

**[D]** **Prompt-set design is treated as a scientific instrument**: unbranded ("Ryze pricing" only confirms you rank for your name); buying intent; phrased as speech; deliberately varied framing; large enough to survive noise. **Never edit prompts mid-measurement — changing wording resets comparability** **[D]**.

**[D]** **Five prompt categories with different behaviour** — this is the best taxonomy in the whole product **[D]**:

| Category | Example | Behaviour |
| --- | --- | --- |
| Category discovery | "what is X software" | definitional pages win |
| Comparison | "X vs Y" | review/comparison sites dominate |
| Recommendation | "best X for Y" | **brand priors dominate; hardest to move** |
| Problem-led | "how do I fix X" | **documentation wins; most winnable** |
| Platform-worded | "best X app for [platform]" | that platform's own properties dominate |

*"Problem-led prompts are the tractable ones."* **[D]** And: *"Treat any claim that share of voice can be moved quickly with suspicion."* **[D]** — exceptionally candid.

**[D]** **Engines differ and must not be averaged** **[D]**: Perplexity cites heavily and stably (often the first to move); AIO leans on conventional Google ranking (treat as an SEO output); ChatGPT is the most volatile; Claude retrieves selectively.

**[D]** **The honest limits section is the most credible writing in Ryze's entire documentation** **[D]**:
- Citations are volatile; never judge by a single result.
- "Ryze cannot make a model say something."
- Platform-worded prompts are structurally hard.
- **GEO influences retrieval, not prior knowledge.** *"Shifting those priors requires sustained third-party presence over much longer periods."*
- Tracking *"does not observe answers inside individual users' private conversations… Traffic attribution — a citation is not a confirmed visit."*

### Live Site Implementation
| Requirement | Detail |
| --- | --- |
| **Answer engines** | ChatGPT, Perplexity, Google AIO, Claude, Gemini, Bing, Shopify search **[D]** — each via its own API, or via a web/UI surface |
| **Execution** | Scheduled job per workspace: iterate the prompt set → query each engine → parse answer text + citations → persist runs |
| **Storage** | Time-series of (prompt, engine, date, mentioned, cited URLs, competitors, position, full text) **[D]** |
| **Client permission** | **None.** Fully read-only, no CMS access needed |
| **Cost** | LLM/API calls; "Reddit search 0.5 credits" and "web fetch 0.1 credits" are the metered primitives **[D]** |
| **Limitation** | Interface-level observation only; no per-user or personalised visibility **[D]** |

**[I]** A note on engine access: Google AI Overviews has **no public API**. Any AIO measurement is necessarily a SERP scrape or a SERP-API-derived inference, which is legally and technically fragile and can break silently on Google's markup changes. **AIO coverage is the least reliable cell in the matrix and should be labelled as such.** Ryze's docs do not distinguish API-sourced from scrape-sourced engines — a competitor that does, and shows a per-engine confidence indicator, is more honest and more useful.

### Alternative / Improved Approaches
- **Repeat-run variance as a first-class metric.** For each prompt, run *N* times and report the **citation probability** and **answer entropy** — "we ran this prompt 5 times; your page appeared in 2 (40%), the leader in 4 (80%)". This converts a noisy binary into a distribution and is the single biggest methodological improvement available. No documented tool does this.
- **Compute-normalised monitoring.** Prompt variance ≠ site variance. A split-plot design (fixed prompts, varied phrasing) separates engine noise from site movement.
- **Likelihood-of-citation as the score.** Instead of a share, model `P(cited | prompt)` per page type, and report which page archetype wins each cluster. Actionable and honest about uncertainty.
- **AI referral traffic as the real ROI.** Ryze reports this for itself (**6,400+ AI-assistant referral sessions, May–Aug 2026** **[M]**) — do it for clients via GA4 referral-source segmentation (`chatgpt.com`, `perplexity.ai`, `claude.ai`). That is the number a CFO cares about, and it connects GEO to revenue.
- **Ship the weekly/marketing cadence only with the monthly caveat visible.** Ryze's docs are right; the landing page is not. Whoever fixes that inconsistency gains trust.

---

## 4.3 Traffic forecasts

### Definition
Projecting future organic sessions, clicks and revenue from current rankings, CTR curves, and published content — with stated confidence intervals.

### Ryze's Execution (Reverse-Engineered)
**[X]** — "Traffic forecasts" appears in the Reporting section of the `/seo` feature list **[M]**. **Zero help-center documentation.** No forecast page, no methodology, no confidence-interval language anywhere in 190+ pages.

**[I]** What the documentation *does* contain is the **opposite** of forecasting — repeated, explicit warnings against projection:
- *"Week one produces published work, not results."* **[D]**
- *"The ordering is consistent: paid moves fastest, technical fixes next, organic ranking after that, AI citations later still, and brand priors slowest of all."* **[D]**
- *"Anyone promising fast share-of-voice gains is overclaiming."* **[D]**
- *"Treat any claim that share of voice can be moved quickly with suspicion."* **[D]**
- *"A page published today is not a candidate for citation today."* **[D]**
- Expectation tables giving first-visible timelines for every work type **[D]**.

**[I]** Ryze's documentation is arguably the most disciplined anti-forecasting corpus in the category. The landing page nonetheless lists "Traffic forecasts" as a feature. Three readings:
1. **Real but undocumented** — a projection model surfaced inside the custom dashboards or a scheduled task, with no dedicated page because it isn't a standalone product.
2. **Deliverable-as-a-service** — a strategist-produced projection in the Wednesday Loom recap (Traffic Printer/Ecom) **[M]**. The docs describe a weekly human report **[M]** but never call it a forecast.
3. **Category claim** — a standard SaaS-SEO feature bullet, to make the reporting list look complete against Semrush/Surfer.

**[I]** **Unknowable from public material, and it should be treated as a question to ask in a sales conversation rather than a capability to assume.** Note the asymmetry with the *reporting* claims: "Slack & email alerts", "Custom dashboards" and "Weekly AI summaries" are all fully documented **[D]**. "Traffic forecasts" being the sole undocumented item in an otherwise exhaustively documented list is a meaningful signal.

**[I]** If it does exist, the *only* defensible method is **bottom-up from measured CTR curves, not top-down from growth rates**:
```
forecast(URL) = position(URL) × CTR(position, device, SERP-features) × impressions(URL)
```
with position projected from a decaying-OBS model (new page → low position → improving → plateau), and **explicit intervals**. Confidence bands are mandatory: Ryze's own docs note 3→5 citations "is not a 67% improvement" **[D]** — that discipline must extend to forecasting or the number is theatre.

### Live Site Implementation
| Requirement | Detail |
| --- | --- |
| **Historical series** | GSC (16 months) **[D]** + DataForSEO daily ranks **[D]** + GA4 sessions/revenue **[D]** |
| **Projection** | Position decay model + CTR-by-position curve + seasonality (year-over-year) **[D — Ryze explicitly warns seasonality dominates short windows]** |
| **Revenue** | GA4 revenue or Shopify order attribution **[D]** |
| **Client permission** | Read-only. No CMS access needed |
| **Required honesty** | Confidence intervals, stated methodology, and a stated failure mode |

### Alternative / Improved Approaches
- **Sell *ranges*, not numbers.** "This content portfolio is likely to produce 40–90k organic sessions over 6–12 months, based on your measured CTR curve and a decay model" is credible and sellable. A point estimate is neither.
- **Publish the model.** Show the decay assumptions, the CTR curve source, and backtest against 12 months of the client's own history. A backtested forecast is a genuine trust instrument — and Ryze's docs' own emphasis on baselines **[D]** makes this a natural fit for the category.
- **Sensitivity analysis.** "If average position improves 2 places: +X. If it does not: +Y." Decision-relevant, unlike a point estimate.
- **The cheapest defensible alternative: a *scenarios* model, not a forecast.** "Conservative / expected / aggressive" with the assumptions for each. No false precision, fully decision-useful.

---

## 4.4 Slack & email alerts

### Definition
Proactive push notifications of significant changes or scheduled outputs, delivered to where the user already works, without requiring a dashboard check.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Fully documented and one of the better-designed sections.

| Element | Documented behaviour |
| --- | --- |
| **Trigger model** | *"Alerts are for things that need a decision"* — on significant change, **not** on a timetable **[D]** |
| **Event classes** | "significant traffic drops, spend anomalies, indexation failures, connection losses" **[D]** |
| **Alerting philosophy** | *"Keep them few. An alert channel that fires constantly gets muted, and then the one that mattered is missed too."* **[D]** |
| **Delivery** | Slack (a channel **per workspace**) and email (individuals or a distribution list) **[D]** |
| **Scoping** | Per workspace, so agency client data does not mix **[D]** |
| **Included on all plans** | **[D]** `/plans/whats-included` |

**[D]** Alerting is correctly **decoupled from reporting**: *"Report scheduling is separate from when the agent works."* **[D]**

**[D]** Slack has a **second, distinct role**: *"Connect Ryze to your Slack workspace and ask the agent about your marketing… without leaving Slack"* **[D]**. So Slack is both an outbound notification channel and an inbound conversational surface.

**[I]** Architecture: a **per-workspace subscription registry** (channel/email/webhook targets); an **event bus** emitting normalised domain events from every subsystem (publish, fix shipped, GSC anomaly, indexation change, connection loss, rank delta); and a **rule layer** deciding what crosses the alerting bar. The hard, high-value part is the **rule layer**: significance thresholds that suppress noise. A naive implementation (§3 of §4.1's own argument applied to itself) fires constantly and gets muted — which Ryze explicitly names as the failure mode **[D]**.

### Live Site Implementation
- **Slack:** Slack app with `chat:write` + channel scope. Ryze holds workspace-admin-level installation consent — a real enterprise-security consideration.
- **Email:** an sending domain with SPF/DKIM/DMARC configured, plus per-workspace distribution lists.
- **Connection-loss detection:** requires a **health-check poller** per integration per workspace. **[I]** This is the highest-value, most-neglected alert class: a silently-dropped OAuth token is indistinguishable from a traffic collapse if you only look at traffic. Ryze names connection loss as an alert class **[D]** — that requires active token-validity probing, not reactive error handling.
- **No CMS access required.**

### Alternative / Improved Approaches
- **Anomaly detection over thresholds.** Rather than "traffic down 20%", model each metric's normal volatility and alert on statistically abnormal deviation, with a per-workspace learned baseline. A generic threshold produces the same alert for every client and is wrong for all of them.
- **Alert on *pending decisions*, not on data.** "3 approved fixes are waiting for you" and "a fix correlated with a 7-day rank drop" are actionable; "rankings changed" is not. The marketing page's rollback promise **[M]** — *"rolls back automatically if a fix correlates with a drop over 7 days"* **[D? no — marketing only]** — would be an excellent alert **if implemented**, and its absence from the docs is notable. **A competitor that ships change→rank correlation monitoring with automatic rollback has a real, defensible, safety-first differentiator.** Ryze's docs claim reversibility in the marketing FAQ **[M]** and never mention rollback mechanics in 190+ pages. **Verify this in a pilot.**
- **Weekly digest consolidation.** Group low-priority events into one Wednesday digest, matching the documented weekly cadence **[D]**, and reserve real-time alerts for the four genuine decision classes **[D]**.
- **Per-client escalation routing for agencies.** Route by workspace to the right human, with escalation if unacknowledged. Ryze names "Mixing clients into one channel is the usual cause of a report reaching the wrong recipient" **[D]** — the obvious next step, and a real agency need.

---

## 4.5 Custom dashboards

### Definition
User-composed live views of metrics, charts and tables, assembled without manual widget configuration — either from templates, by natural-language request, or both.

### Ryze's Execution (Reverse-Engineered)
**[D]** — Documented in `/reports/overview`:
- **Templates:** Organic Traffic Overview, AI Traffic Overview, Product Winners & Losers, Paid Ads Overview, All Accounts Overview **[D]**
- **Ask for one:** *"click **Design a dashboard** in the chat shelf, or just describe it ('a dashboard with spend, ROAS and conversions per platform for the last 30 days'). The agent pulls the data, lays out the charts and tables, and hands you the page."* **[D]**
- **"Dashboards are live views the agent builds from your connected accounts — you don't configure widgets by hand."* **[D]**
- **Platform coverage** of the paid dashboard: Google Ads, Meta, Microsoft Ads, TikTok, Snapchat, LinkedIn, Reddit, OpenAI Ads **[D]**
- **Starred-connection preference** when multiple accounts exist per platform **[D]**
- **Export** the view, filtered to period and segment **[D]**
- **Unified attribution** across ads, SEO and CRO — *"rather than treating them separately… CRO improvements raise the return on both ads and organic, and catalogue work improves Shopping and organic together"* **[D]**
- **Per-workspace**, giving agencies per-client reporting without manual separation **[D]**

**[D]** The **"don't configure widgets by hand"** design decision is a strong one and rarely executed well. **[I]** Most tools ship a BI-style builder (drag, drop, configure, save) and call it done. Ryze's approach — **the agent produces a finished dashboard object as an output of a natural-language request** — removes the entire builder UI and puts the work in the LLM's hands. The cost is reproducibility: regenerating a dashboard is non-deterministic unless the underlying query plan is persisted as a declarative spec. **[I]** The correct implementation is therefore: **natural language → a persisted declarative query spec → rendered dashboard**, with the spec saved and re-renderable. That gives the ergonomic win *and* reproducibility, and it is what a serious implementation must do.

**[D]** The honesty about attribution is notable: *"Platform-reported numbers will not match each other… Ryze reports each rather than forcing a false reconciliation."* **[D]** Refusing to manufacture a reconciled number that no platform would produce is correct and rare.

### Live Site Implementation
| Requirement | Detail |
| --- | --- |
| **Data** | Every connected account via its API; **no** client-side change |
| **Storage** | Persisted query spec + cached result set + layout definition per dashboard, scoped to a workspace **[I]** |
| **White-label** | Colours, logo, custom domain, own email domain **[D]** — genuine agency functionality |
| **Export** | Server-side render → PDF/image; CSV for raw data **[D]** |
| **Permissions** | Read-only source accounts; dashboards inherit workspace scoping **[D]** |

### Alternative / Improved Approaches
- **Ship the declarative spec publicly.** A JSON dashboard spec that a client can fork, version, and deploy to their own BI tool would be a genuinely differentiating, trust-building artefact. It converts "ask an LLM for a chart" into "here is the reproducible artefact."
- **Baseline-relative everything.** Ryze's own guidance is emphatic: *"Record a baseline. Every report is only meaningful against where you started."* **[D]** **A dashboard that shows only current numbers is not a dashboard, it is a monitor.** Every chart should default to baseline-delta. This is Ryze's own stated principle not reflected in its own product description, and it is the highest-value single improvement available in this category.
- **Anomaly overlays on every chart.** Statistical bands (expected range) on time series turn a dashboard into a diagnostic instrument. Almost nobody does this.
- **Latency annotation on the data.** Mark publication and fix dates on the timeline. A spike becomes explicable. This is a small UI decision with a large effect on report credibility, and it directly implements Ryze's own latency guidance **[D]**.

---

## 4.6 Weekly AI summaries

### Definition
A periodic, human-readable synthesis of what changed, what it means, and what happens next — the executive layer over the underlying data.

### Ryze's Execution (Reverse-Engineered)
**[D]** — **Wednesdays** **[D]**, in three forms, and the distinction matters:

| Layer | Cadence | Nature |
| --- | --- | --- |
| **AI visibility report** | Weekly, Wednesday **[D]** | Citations, mentions, share of voice, per-engine, per-prompt, competitor domains **[D]** |
| **Agent Activity** | **Continuous**, as actions occur **[D]** | Timestamped log of every action **[D]** |
| **Scheduled agent tasks** | User-defined recurring or one-time **[D]** | Fresh chat per run, result emailed, read-only history **[D]** |

**[D]** The human element: on Traffic Printer / Ecom, *"your strategist sends a Loom recap, the week's wins, and next week's plan"* every Wednesday **[M]**, and *"Same human, same Thursday. Agents work — humans review."* **[M]**

**[D]** Content of the AI visibility report **[D]**: citation share + which pages earned them; mention rate; share of voice against every competitor; **per-engine breakdown** (ChatGPT, Perplexity, AIO, Claude reported **separately**); **per-prompt detail** including what was cited instead; **competitor domains ranked**.

**[D]** Reading guidance is unusually good **[D]**: *"Read engines separately, never blended."* *"Start with the prompts where you are absent — what was cited instead is the actionable finding."* *"3 to 5 citations is not a 67% improvement."* *"The reliable read is direction across a quarter, on a stable prompt set, against a recorded baseline."* And the key asymmetry **[D]**: **"Rising citations with flat mentions is the normal and expected early pattern: your content is being found before your brand is established. Content work moves citations. Only sustained third-party presence over many months moves mentions."**

**[I]** Architecture: the summary is an **LLM generation over a structured weekly delta** (metrics computed deterministically, narrative generated). The critical engineering is in the *inputs*, not the prose:
1. Deterministic metric deltas (never LLM-computed arithmetic).
2. Per-prompt presence/absence matrix → "where you are absent and X was cited instead."
3. Change events from Agent Activity (fixes shipped, articles published) so narrative and events agree.
4. Latency-aware framing using Ryze's own timeline table **[D]**.
5. Baseline anchoring.

**[D]** The cadence rationale is consistent with everything else: *"AI visibility — weekly, read monthly. Volatility makes anything faster misleading."* **[D]**

### Live Site Implementation
- **Data:** GSC, GA4, DataForSEO, AI visibility runs, Agent Activity log. **No CMS write access.**
- **Delivery:** Slack message and/or email with a link to the full record; scheduled-task runs produce a fresh chat + emailed recap **[D]**.
- **Export:** PDF/CSV from dashboards and Agent Activity **[D]**.
- **Rendering:** server-side report generation (PDF) plus a live link.

### Alternative / Improved Approaches
- **Separate the two audiences properly.** The client owner wants *what happened and what's next*. The technical SEO wants *evidence*. Ryze merges them. Two views from one dataset — an executive summary and an evidence appendix — is strictly better and cheap to build.
- **State what has not moved.** **[D]** Ryze's own guidance: *"state what has not moved yet alongside what has. A report that only shows improving numbers stops being believed the first time something goes wrong."* **[D]** **This is the most valuable reporting principle in Ryze's documentation and it is not a productised feature.** A competitor that automatically renders a "not yet moved, and here's why, and here's when it should" section for every metric would win client trust outright. It is a small feature with an outsized effect.
- **Variance-aware framing in the copy.** "Citations 3→5" is noise. "Citations 3→5, within the observed run-to-run range of ±2." Pairing the number with its own measured variance is a genuine methodological upgrade.
- **Action-linked narrative.** Every claim in the summary should link to the specific agent action taken or queued, so the report and the activity log are one object. Currently they are two.

---

# PART 5 — CROSS-CUTTING AUDIT FINDINGS

## 5.1 Claim-to-evidence matrix

| # | Claim (source) | Documented position | Verdict |
| --- | --- | --- | --- |
| 1 | "Up to **500+** backlinks/month" **[M]** `/seo` | 50+ **[D]** `/plans/usage-limits`; 50–100 **[M]** `/pricing`; 100 **[D]** `/seo/backlinks` | **Contradicted, 5×** |
| 2 | "**100+** PR press releases" **[M]** `/seo` | No volume documented **[D]**; press absent from plan tables **[D]** | **Undocumented** |
| 3 | "**300+** niche directories" **[M]** `/seo` | Docs list directory/profile links under **"does not matter"** **[D]** | **Contradicted by own docs** |
| 4 | "**DR 30+** backlinks" **[M]** `/seo` | A prospect **status board** (Live/Pending/Outreach) **[M]**; main driver is "Ryze runs a large network of partner sites" **[D]** | **Misleading framing** |
| 5 | "Converting **Reddit** posts" **[M]** `/seo` | Only a **search tool** (0.5 credits) **[D]**; Reddit delivered as human-applied Opportunities **[D]** | **Overstated** |
| 6 | "**Quora & Medium** posts" **[M]** `/seo` | Zero documentation **[X]**. Medium appears only as a *prospect* **[M]** | **Undocumented** |
| 7 | "**Wikipedia** mentions" **[M]** `/seo` | Zero documentation **[X]**; no wiki integration **[D]** | **Contradicted by plausibility** |
| 8 | "**100s** of blogs/month" **[M]** `/seo` | 90 **[D]** `/plans/usage-limits`; 100+ **[D]** `/plans/whats-included` | **Contradicted (90 vs 100+ vs 100s)** |
| 9 | "**Real-time** keyword tracking" **[M]** `/seo` | Daily rank tracking **[D]**; **Keywords page read-only** **[D]** | **Overstated ×2** |
| 10 | "**Traffic** forecasts" **[M]** `/seo` | Zero documentation **[X]**; docs repeatedly warn against projection **[D]** | **Undocumented** |
| 11 | "**AI citations** tracked weekly" **[M]** `/geo`, `/geo-tool` | **Monthly**, once per billing cycle **[D]** (3 pages) | **Contradicted** |
| 12 | "**Custom** dashboards" **[M]** `/seo` | Fully documented, agent-built from templates or NL **[D]** | **Verified** |
| 13 | "**Slack & email** alerts" **[M]** `/seo` | Fully documented, per-workspace, on-significant-change **[D]** | **Verified** |
| 14 | "**Weekly** AI summaries" **[M]** `/seo` | Wednesday **[D]**; AI visibility report **[D]** | **Verified** (cadence: report weekly, data monthly) |
| 15 | "Rolls back automatically if a fix correlates with a drop over 7 days" **[M]** `/seo` FAQ | Zero rollback mechanics in 190+ docs **[X]** | **Unverified — test in pilot** |
| 16 | Site fixes (titles/metas/speed/links/schema/broken links) | Audit + Fix-with-agent, 5-state machine, top-300 GSC pages **[D]** | **Verified** |
| 17 | Programmatic content, keyword selection | Two pools, deficit-only, ~5/day, real DataForSEO metrics **[D]** | **Verified** |
| 18 | Backlink exchange / partner network | Fully documented with rate limits and exclusions **[D]** | **Verified** (and under-sold) |
| 19 | GEO execution (crawler access, llms.txt, schema, answer pages) | Documented across `/ai-visibility/*`, `/concepts/*` **[D]** | **Verified** |
| 20 | Stack identification | Supabase storage **[D]**, Vercel DNS **[D]**, Next.js marketing site, Mintlify docs, remote MCP **[D]** | **Verified by artifacts** |

**Score: 7 of 20 marketing claims fully verified; 4 contradicted by the vendor's own documentation; 5 undocumented; 4 overstated.**

## 5.2 The documentation/marketing gap as a competitive weapon

**[I]** This is the single most exploitable finding in the audit. Ryze's help center is **better than its marketing**, and that gap is *measurably* large — 4 outright contradictions, 5 undocumented claims.

Why this matters competitively:
1. **The docs are the durable artefact.** They are machine-readable (`llms.txt`, 190+ pages, `.md` on every URL — a deliberate LLM-retrieval strategy **[D]**). They will be scraped, summarised and cited by AI assistants for years. The landing page is ephemeral.
2. **A sophisticated buyer reads both** — and the SEO-agency buyer Ryze explicitly targets with agency/multi-workspace/white-label features **[D]** is *by definition* sophisticated.
3. **The contradictions are on the two most commercially important numbers**: backlink volume (5×) and content volume (90 vs 100+ vs 100s). These are the numbers used to evaluate the vendor.
4. **The correct competitive response is not to attack.** It is to **publish a page whose marketing and documentation say the same thing**, and to say explicitly which of Ryze's claims are verifiable. A competitor that does this wins on trust with exactly the buyer Ryze wants — and does so credibly, from public sources.

## 5.3 The genuine technical strengths (do not attack these)

1. **The four-stage closed loop** with results feeding prioritisation **[D]**. Competitors with better one-shot features still lose here.
2. **The Backlink Exchange constraint system** **[D]** — 1-in-40 rate limit, second-half placement, relevance-gated matching, permanent competitor suppression, own-network exclusion, least-received-first fairness, store+plan eligibility, `*.byryze.com` exclusion, opt-out toggle. This is a real distributed system and it is *under-sold*.
3. **The newswire industry-conditional generator** with bounce-rewrite-resubmit **[D]**. A genuine, hard-won integration.
4. **The deficit-only monthly cycle tied to billing** **[D]**. Correct pacing, correct idempotency.
5. **The GEO metrics with correct formulas** — especially citation share counted **by domain** rather than by answer, and the six-metric separation of mentions from citations **[D]**. Methodologically superior to most of the category.
6. **The prompt-category taxonomy** and the "problem-led prompts are the tractable ones" insight **[D]**. Best-in-class analytical framing.
7. **The honest-limits writing** **[D]** — "Ryze cannot make a model say something", "GEO influences retrieval, not priors", "platform-worded prompts have a structural ceiling", "anyone promising fast share-of-voice gains is overclaiming". This is a credibility asset a competitor should match, not exploit.
8. **The reporting-latency doctrine** **[D]** — "reporting more often than a metric meaningfully changes does not give you more information — it gives you more noise to react to" **[D]**. Correct, and most competitors violate it.
9. **The publish-verification stage** — post-publish live-URL verification with content checks **[D]**. Most tools fire-and-forget.
10. **Upsert-by-slug idempotency** with no duplicate artefacts **[D]**.

## 5.4 Structural weaknesses a competitor can attack

| # | Weakness | Evidence | Attack |
| --- | --- | --- | --- |
| 1 | **Keywords page read-only** | **[D]** "no switch or delete button" | Editable keyword board with intent clustering + cannibalisation warnings |
| 2 | **Content volume hard quota, not evidence-gated** | **[D]** 90/mo deficit-only | Gate the next batch on **earned impressions** — Ryze's own advice **[D]**, productised as a gate |
| 3 | **No automated pruning/consolidation** | **[D]** prescribed, not productised | Automated 301 consolidation of no-impression pages |
| 4 | **Third-party image CDN on all articles** | **[D]** `supabase.co` URL, "only the host is stable" | Serve images from client infrastructure |
| 5 | **Shopify write surface is data-level only** | **[D]** "No theme edits are required" | Theme-level agents (PR-based) unlock menu links, JS reduction, template titles |
| 6 | **No Merchant Center integration** | Absent from 50+ integrations **[D]** | Feed-correctness product — the highest-leverage ecommerce SEO surface |
| 7 | **Intent-collision prevention is LLM-approximated, not SERP-verified** | **[D]** stated as policy | DataForSEO SERP-overlap clustering as a hard generation gate |
| 8 | **Third-party image dependency + no near-duplicate corpus check** | Absent from docs | Embedding-similarity rejection at generation time |
| 9 | **Rollback/correlation-monitoring unverified** | Marketing **[M]** only | Ship change→rank correlation with automatic rollback |
| 10 | **Crawl budget is wall-clock, not evidence** | **[D]** daily, everywhere | Per-keyword velocity tiering (daily tier-1, weekly long tail) |
| 11 | **Daily AI visibility not run; monthly only** | **[D]** | **Repeat-run probability** (N runs → `P(cited)`) — the real methodological upgrade |
| 12 | **Single answer-engine interface observation** | **[D]** acknowledged | Add AI referral traffic from GA4 — the CFO metric Ryze reports for itself **[M]** but not for clients |
| 13 | **Unattributed link outcomes** | **[D]** `I applied it` with no measurement **[D]** | Close the loop: thread/placement → UTM → measured outcome |
| 14 | **Weekly reports don't state non-movement** | Prescribed **[D]**, not productised | Auto-generated "not yet moved, here's why, here's when" section |

## 5.5 Technical-stack fingerprint

**[D]/[I]** from observable artifacts:

| Component | Evidence | Confidence |
| --- | --- | --- |
| **Frontend** | Next.js (`/_next/image`, `main-logo-sun-2.webp`) | **[D]** |
| **Edge/DNS** | Vercel (`cname.vercel-dns.com`, A `76.76.21.21`) **[D]** | **[D]** |
| **Object storage** | Supabase (`supabase.co/storage/v1/object/public/workspace-article-images-public/`) **[D]** | **[D]** |
| **Docs platform** | Mintlify (`mintcdn.com/ryzeai-5f9aafd5/…`) **[D]** | **[D]** |
| **Marketing subdomain** | `reports.get-ryze.ai` (API + signup + payments) **[D]** | **[D]** |
| **MCP server** | `connector.get-ryze.ai/mcp` — remote MCP, bearer auth, no SSE **[D]** | **[D]** |
| **API style** | `POST /api/v1/tools/{name}` with `{args}`, self-describing tool manifest with `requires_plan`, HTTP 200 + `{status}` business errors **[D]** | **[D]** |
| **Browser automation** | Lovable/Base44/GoDaddy Airo publishing: stored credentials, prompt injection, click Publish, live-URL verification with 2-min timeout **[D]** | **[D]** |
| **LLM** | Model not user-selectable; BYO Anthropic key supported **[D]** | **[D]** |
| **Crawl** | "Run crawl… takes a few minutes" on top-300 GSC pages **[D]** | **[D]** |
| **Liveness checks** | GitHub/webhook "Override blog URL… used for live links, indexing pings, and liveness checks" **[D]** | **[D]** |
| **Automation path** | Open **PR** for sites with no write API (landing FAQ: "We push fixes via plugin, API, or **PR**") **[M]** | **[M]** |

**[I] Notable:** the documentation set is itself an LLM-retrieval asset — every page available as `.md`, plus a complete `llms.txt` index and an `/llms.txt` on the marketing domain. **[D]** This is deliberate GEO/GEO-adjacent distribution, and it means Ryze's own claims are now permanently in the training and retrieval corpus that its product measures. A competitor should do the same immediately; it is the cheapest distribution available in 2026.

## 5.6 The one thing to verify before any competitive decision

**Ryze's marketing FAQ claims every change is "logged and reversible in one click" and that the agent "rolls back automatically if a fix correlates with a drop over 7 days."** **[M]** Neither appears anywhere in 190+ documentation pages **[X]**.

This matters more than any other single item, because:
- It is the **risk-transfer mechanism**. If true, it is the strongest argument for buying an autonomous SEO agent. If false, Ryze is what every buyer fears: an agent that silently rewrites 90 product titles and 300 internal links a month with only a per-item approval click as a safeguard.
- **Shopify redirect creation is a high-severity, high-privilege, hard-to-bulk-reverse operation.** Combined with an unverified rollback claim, that is the specific risk a buyer needs to price.
- **It is empirically testable in a 7-day trial** — the marketing page offers a free scan (`reports.get-ryze.ai/scan` **[M]**) and the pricing page a 7-day free trial **[D]**.

**Recommendation: any competitive evaluation of Ryze should include a controlled pilot on a low-traffic subdomain measuring (a) approval friction at volume, (b) time-to-revert for a batch of 100+ changes, (c) whether any correlation-based rollback fires, (d) actual backlink volume against the 500+ claim, and (e) GSC movement at day 90 against Ryze's own stated timelines.**

## 5.7 Agent Context has no eval loop

**[D]** The docs are emphatic that Brand Context is *"the highest-leverage place in Ryze… a few minutes here changes everything downstream"* **[D]** — and that when topics look wrong, *"the cause is almost always agent context"* **[D]**.

**[I]** But there is no documented mechanism to know whether a context change *helped*. Context is free-form prose affecting keyword seeds, topic selection, article generation, off-page copy, and reports — and there is no A/B, no holdout, no before/after attribution, no quality score. The only feedback is qualitative ("topics look off-target").

This is a real architectural gap and a genuine competitive opening. A competitor could ship **per-field context attribution**: change one context field, hold the rest, and report the measurable delta in topic specificity, duplicate rate, internal-link quality, and downstream GSC impressions. In an LLM-driven content system, **prompt-level evaluation is the difference between a product that improves and one that plateaus** — and it is the hard thing nobody has built.

---

# PART 6 — STRATEGIC SUMMARY

## 6.1 What Ryze actually is, architecturally

Not an SEO tool. **A retained, server-side, always-on marketing agent with a chat-shaped control surface (MCP), a per-workspace context layer, a billing-date-keyed monthly production ledger, a self-serve path and a human-strategist path, and a proprietary reciprocal link network as the off-page supply side.**

The SEO product is the surface. The **Backlink Exchange network** and the **newswire relationship** are the moat. The **MCP/REST agent-broker** is the distribution.

## 6.2 Competitive positioning

| Axis | Ryze's position | Opportunity |
| --- | --- | --- |
| **Honesty** | Docs excellent; marketing contradicts them in 4 places | **Publish a page where marketing and docs match.** Attack the 4 contradictions factually and move on. |
| **On-page depth** | Data-level writes; no theme edits | **Own the theme.** Menu links, template titles, JS reduction — the real Shopify wins. |
| **Content quality** | Fixed quota; LLM-approximated intent control | **Evidence-gated release + SERP-overlap clustering + embedding duplicate rejection + automated pruning.** |
| **Off-page** | Network + wire + exchange. Marketing overclaims 5×. | **Sell 20 verified links, not 500 claimed.** Publish the exchange algorithm. Verify placements independently. |
| **Measurement** | Excellent methodology, monthly cadence | **Repeat-run `P(cited)` distributions.** The single best methodological upgrade available. |
| **Platforms** | Wide breadth (50+), shallow depth | **Depth on 3 stacks > breadth on 50.** Shopify + Next.js + WordPress done properly beats 50 shallow adapters. |
| **Agency** | White-label, multi-workspace, per-client routing **[D]** | **A real white-label that survives client scrutiny** — documented APIs, spec files, exportable artefacts. |

## 6.3 The three highest-leverage moves

1. **Publish a competitive teardown that cites only public, checkable evidence** — the 5× backlink discrepancy, the 90/100+/100s content discrepancy, the directory-links contradiction, the weekly-vs-monthly AI visibility discrepancy, the unverified rollback claim. Every one is documented from Ryze's own pages. This is a credibility asset that costs nothing and cannot be retracted.

2. **Build the measurement layer Ryze's own docs prove is missing.** Repeat-run citation probability, intent-overlap cannibalisation detection, prompt-change attribution, "not yet moved" reporting, and AI referral traffic from GA4. Every one of these is prescribed somewhere in Ryze's documentation and productised nowhere. **Following the vendor's own advice better than the vendor follows it is the cleanest possible differentiation.**

3. **Go deep on three stacks instead of wide on fifty.** Ryze's documented weakness is precise and exploitable: on Shopify it does data-level writes with no theme access, so the two largest Shopify performance causes — app bloat and theme JS — are explicitly out of reach, and its own docs say so. **[D]** A competitor that can actually ship a theme change via PR, with human review, takes the highest-value ecommerce SEO surface available.

---

## Appendix A — Full source inventory

**Primary (machine-readable, authoritative):**
`help.get-ryze.ai/llms.txt` + 60+ individual `.md` pages covering: how-it-works, optimizations, keywords, programmatic, guest-posts, press, backlinks, backlink-exchange, seo-faq, shopify/{overview,audit,product-pages,collection-pages,product-schema,merchant-listings,product-variants,duplicate-content,redirects,sitemaps,site-speed,faceted-navigation,large-catalogs,international}, ecommerce/{site-structure,category-pages,product-descriptions-at-scale,internal-linking,out-of-stock-products,technical-audit,keyword-research,content-programmatic,backlinks-mentions,seo-reporting}, ai-visibility/{overview,metrics,chatgpt-tracking,other-engines,prompt-monitoring,monitoring-interval,citations-report,share-of-voice,not-cited,ai-readable-pages,llms-txt,structured-data-for-ai}, concepts/{generative-engine-optimization,answer-engine-optimization,ai-visibility,llm-seo,agentic-commerce,share-of-voice-ai,citation-share,technical-seo,programmatic-seo,core-web-vitals,crawl-budget}, reports/{overview,seo-audit,ai-visibility-report,cro-report,competitors,scheduling,exporting}, agent/{context,approvals,reports,schedules,credits}, integrations/{overview,shopify,shopify-custom-app,wordpress,framer,wix,webflow,github,ghost,squarespace,lovable,godaddy-airo,base44,ryze-hosting,custom-webhook,directus,magento,hubspot,highlevel,lightspeed,ahrefs,semrush,google-search-console,google-analytics,mcp-server,slack,anthropic,custom-mcp}, plans/{overview,whats-included,usage-limits,agency-multi-workspace}, security/{overview,data-storage,data-processing}, troubleshooting/{shopify-not-connecting,audit-not-completing,changes-not-applied,gsc-data-missing,orphan-pages-canonicals,crawl-errors,ai-visibility-no-data}, how-ryze-works, first-week, quickstart, what-is-ryze, agent-api, claude/{connect,permissions,faq}

**Secondary (marketing/comparison):** `/seo`, `/ai-seo-tool`, `/geo`, `/geo-tool`, `/pricing`, `/llms.txt`, `/compare/*`, `/case-studies/*`, `/blog/*`

**Not accessed (account-gated):** the running product, `reports.get-ryze.ai/scan`, trial environments, `/agency`, `/muse`, `/instinct-for-ads-and-seo`, `/shopify-claude-ai-for-paid-ads-geo-cro`, free-tool implementations (`/schema-markup-generator`, `/llms-txt-generator`, `/keyword-clustering-tool`, `/backlink-checker`, `/ai-search-visibility-checker`, `/alt-text-generator`, `/serp-snippet-preview`) — **these are a valuable untapped source of reverse-engineering signal and were out of scope here.**

## Appendix B — Questions to answer in any pilot

1. Does the auto-rollback-on-7-day-drop claim hold? What is the correlation method and the false-positive rate? **[M] — unverified**
2. What is the **actual** backlink volume in month 1, 2, 3? Against 50+ **[D]**, 100 **[D]**, or 500+ **[M]**?
3. Is Reddit/Quora/Medium/Wikipedia **actually done**, by the product or by a human strategist? At which plan tier?
4. Do "traffic forecasts" exist? What method, what confidence interval, what backtest?
5. Is the keyword set **editable** through any surface, or is Brand Context the only lever? **[D] says read-only**
6. How many of the 90 pages/month earn impressions? What is the pruning policy?
7. What exactly is `GET /api/v1/tools`? The full tool manifest is the single richest architectural artefact available — **request it**.
8. Can a WordPress client grant capabilities granularly, or is a Ryze plugin required?
9. What happens to articles and exchange links after cancellation? **[D]** "Nothing we build for you is removed" — verify.
10. Is there any first-party theme control on Shopify, now or planned? **[D]** says "no theme edits are required" — which is an odd thing for a product to say unless it is a limitation.
