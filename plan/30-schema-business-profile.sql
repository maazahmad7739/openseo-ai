-- ============================================================
-- 30 — Business profile fields for schema grounding (plan/30)
-- ============================================================
-- plan/27 §5 routed /services + /pricing JSON-LD through hardcoded
-- agency copy: serviceType 'Digital Marketing & AI Growth Services',
-- areaServed 'IN', and an Offer.name 'Digital marketing services'.
-- Any client got ANOTHER AGENCY's marketing language in their markup.
--
-- The deterministic schema builder must ground on the CLIENT's own
-- business profile. This migration adds the optional per-site columns;
-- the schema builder emits Service/Offer blocks ONLY when they are
-- populated (typed refusal otherwise) — never a placeholder, never an
-- agency default.
--
-- All objects are IF NOT EXISTS / idempotent-safe: re-running is a no-op.
--
-- What this creates:
--   1. site_config.business_service_types TEXT[] — what the client SELLS
--                             (Service.serviceType / Offer.itemOffered);
--                             NULL/empty = omit the Service/Offer block.
--   2. site_config.business_areas_served TEXT[] — where the client
--                             operates (Service.areaServed); NULL/empty =
--                             omit the field (schema.org: areaServed is
--                             optional on Service).
--   3. site_config.business_offer_name TEXT — Offer.name fallback
--                             ordering: page_title > business_offer_name
--                             > first service type.
-- ============================================================

ALTER TABLE site_config ADD COLUMN IF NOT EXISTS business_service_types TEXT[];
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS business_areas_served TEXT[];
ALTER TABLE site_config ADD COLUMN IF NOT EXISTS business_offer_name TEXT;