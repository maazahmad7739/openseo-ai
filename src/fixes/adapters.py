"""Write adapters (plan/21 §4): one per write target, never-raise contract.

Mirrors the connector contract (supports()/execute()/restore()): every call
returns a typed dict and never raises on provider failure. All writes are
GraphQL-only, pinned to the version resolved from system_config
('shopify.api_version', default 2026-01), routed through the single
ShopifyGraphQLClient with the mutation registry.

Each adapter validates its payload BEFORE any network call (executor
contract §5.4) and returns {ok, detail, ...} shapes the executor persists
into adapter_response verbatim (never credentials).
"""

import json
import re

import connectors.shopify as shopify_connector

# Version pin comes from system_config at adapter construction (executor);
# doc URLs live in MUTATION_REGISTRY (build rule: re-verified at build time).


class WriteAdapterError(Exception):
    pass


def _client_from_config(config):
    """Build the GraphQL client from a config dict (executor/test injectable)."""
    return shopify_connector.ShopifyGraphQLClient(
        shop_domain=config.get("shop_domain"),
        access_token=config.get("access_token"),
        api_version=config.get("api_version"),
    )


def _client_from_framer_config(config, client=None):
    """Build the Framer client from a Phase 0 credential dict.

    client: TEST seam (injected fake/mock client) — same pattern as the
    redirect adapter's client seam. When omitted, the real FramerClient is
    built from the resolved config (mode resolved by the connector).
    """
    if client is not None:
        return client
    from connectors.framer import FramerClient
    return FramerClient(
        framer_api_key=config.get("framer_api_key"),
        framer_site_ref=config.get("framer_site_ref"),
        api_base=config.get("api_base"),
    )


# ------------------------------------------------------------
# product_seo_title (sub_type 'seo.title') — PILOT write path
# ------------------------------------------------------------

PRODUCT_SEO_READ_QUERY = """
query FixSnapshotProduct($id: ID!) {
  product(id: $id) { id title status seo { title description } }
}
"""

PRODUCT_SEO_UPDATE_MUTATION = """
mutation FixProductSeoUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id seo { title } }
    userErrors { field message }
  }
}
"""


def _payload_for(payload_json):
    """payload_json = {mutation, variables, scope_required, doc_url}."""
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "productUpdate":
        raise WriteAdapterError(f"unsupported mutation for seo.title adapter: {mutation!r}")
    variables = (payload_json.get("variables") or {}).get("product") or {}
    seo = variables.get("seo") or {}
    if not variables.get("id") or not isinstance(seo.get("title"), str):
        raise WriteAdapterError(
            "productUpdate payload must carry product.id and product.seo.title")
    if not variables.get("seo"):
        raise WriteAdapterError("seo.title adapter writes ONLY the seo field "
                                "(product title is customer-visible; out of pilot scope)")
    return mutation, payload_json.get("variables") or {}


def _sibling_seo_field(fix_row, field):
    """The sibling seo field from the fresh pickup snapshot (LIVE-VERIFIED).

    Shopify's ProductUpdateInput.seo REPLACES the whole seo object (live-
    verified 2026-09-23 on action-seo-test: writing seo:{title} wipes
    seo.description and vice versa). A single-field fix must therefore
    carry the sibling field's CURRENT value in the same write or it would
    silently destroy it. The executor's fresh-read snapshot is the source
    of truth; a missing snapshot refuses rather than writes a guessed
    sibling (the caller then fails the fix — no data loss, no guessing).
    """
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get(field)
    return value if isinstance(value, str) else None


def product_seo_title_execute(conn, fix_row, config, dry_run=False):
    """Apply one seo.title fix: write -> read-back verify.

    fix_row: dict with fix_id, target_entity_ref (GID), payload_json,
    diff_json, snapshot_json. Returns typed result; never raises on
    provider errors (network/http failures become ok=False).
    """
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    try:
        mutation, variables = _payload_for(fix_row["payload_json"])
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": "product_seo_title"}

    if dry_run:
        return {"ok": True, "outcome": "dry_run", "adapter": "product_seo_title",
                "detail": {"mutation": mutation, "gid": gid,
                           "variables": variables}}

    # Shopify replaces the WHOLE seo object (live-verified): carry the
    # sibling description from the fresh pickup snapshot so a title fix
    # never wipes it.
    sibling = _sibling_seo_field(fix_row, "seo.description")
    if sibling is None:
        return {"ok": False, "outcome": "no_snapshot",
                "detail": "seo.title write needs the pickup snapshot's "
                          "seo.description (productUpdate.seo replaces the "
                          "whole object) — refusing a data-destroying write",
                "adapter": "product_seo_title"}
    write_variables = {
        "product": {"id": gid,
                    "seo": {"title": variables["product"]["seo"]["title"],
                            "description": sibling}},
    }
    write = client.run("productUpdate", PRODUCT_SEO_UPDATE_MUTATION, write_variables)
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"), "adapter": "product_seo_title"}

    # Read-back verification (plan/21 §4): trust only a fresh read.
    verify = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    live_title = None
    if verify.get("ok"):
        product = (verify.get("data") or {}).get("product") or {}
        live_title = (product.get("seo") or {}).get("title")
    new_title = (((variables.get("product") or {}).get("seo") or {}).get("title")
                 if isinstance(variables, dict) else None)
    verified = bool(verify.get("ok")) and live_title == new_title
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_title,
        "adapter": "product_seo_title",
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def product_seo_title_restore(conn, fix_row, config):
    """Restore from the FRESH-READ snapshot captured at executor pickup.

    Restores BOTH seo fields from the snapshot (productUpdate.seo replaces
    the whole object — restoring title alone would leave the description
    the fix wrote, or vice versa). Returns the same typed shape as execute.
    """
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    old_title = snapshot.get("seo.title")
    if old_title is None:
        return {"ok": False, "outcome": "no_snapshot", "adapter": "product_seo_title",
                "detail": "snapshot_json missing seo.title — refusing to guess a restore value"}
    # The snapshot carries the full pre-state seo surface; None is a legit
    # pre-state (field was empty) and must be preserved as None.
    old_description = snapshot.get("seo.description")

    variables = {"product": {"id": gid,
                             "seo": {"title": old_title,
                                     "description": old_description}}}
    write = client.run("productUpdate", PRODUCT_SEO_UPDATE_MUTATION, variables)
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"), "adapter": "product_seo_title"}
    verify = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    live_title = None
    if verify.get("ok"):
        product = (verify.get("data") or {}).get("product") or {}
        live_title = (product.get("seo") or {}).get("title")
    restored = bool(verify.get("ok")) and live_title == old_title
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_title, "adapter": "product_seo_title",
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


def product_seo_title_snapshot(conn, fix_row, config):
    """FRESH READ pre-state at executor pickup (plan/21 §5.3).

    Never trusted from generation: the executor compares the live value to
    diff old_value BEFORE writing; a mismatch means the diff is stale.
    """
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    read = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": "product_seo_title"}
    product = (read.get("data") or {}).get("product") or {}
    seo = product.get("seo") or {}
    return {
        "ok": True,
        "snapshot": {
            "seo.title": seo.get("title"),
            "seo.description": seo.get("description"),
            "product.title": product.get("title"),
            "status": product.get("status"),
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": "product_seo_title",
    }


def _safe_response(result):
    """Strip to persistable fields (adapter_response column contract)."""
    if not isinstance(result, dict):
        return {"raw": str(result)[:500]}
    keep = {}
    for key in ("ok", "outcome", "userErrors", "errors", "http_status",
                "retry_after", "api_version", "adapter"):
        if key in result:
            keep[key] = result[key]
    detail = result.get("detail")
    if detail:
        keep["detail"] = str(detail)[:500]
    return keep


# ------------------------------------------------------------
# product_seo_description (sub_type 'seo.description') — Phase 2 write path
# (plan/21 §2.2/§4 verified: productUpdate(seo: {description}),
# write_products; metafieldsSet only as fallback, not needed for products)
# ------------------------------------------------------------

PRODUCT_SEO_DESCRIPTION_UPDATE_MUTATION = """
mutation FixProductSeoDescriptionUpdate($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id seo { description } }
    userErrors { field message }
  }
}
"""

ADAPTER_SEO_DESCRIPTION = "product_seo_description"


def _meta_payload_for(payload_json):
    """payload_json = {mutation, variables, scope_required, doc_url} for the
    seo.description path: product.id + product.seo.description required."""
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "productUpdate":
        raise WriteAdapterError(
            f"unsupported mutation for seo.description adapter: {mutation!r}")
    variables = (payload_json.get("variables") or {}).get("product") or {}
    seo = variables.get("seo") or {}
    if not variables.get("id") or not isinstance(seo.get("description"), str):
        raise WriteAdapterError(
            "productUpdate payload must carry product.id and "
            "product.seo.description")
    if set((variables.get("seo") or {}).keys()) != {"description"}:
        raise WriteAdapterError(
            "seo.description adapter writes ONLY seo.description (no title/"
            "status drift through the meta path)")
    return mutation, payload_json.get("variables") or {}


def product_seo_description_execute(conn, fix_row, config, dry_run=False):
    """Apply one seo.description fix: write -> read-back verify."""
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    try:
        mutation, variables = _meta_payload_for(fix_row["payload_json"])
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_SEO_DESCRIPTION}

    if dry_run:
        return {"ok": True, "outcome": "dry_run", "adapter": ADAPTER_SEO_DESCRIPTION,
                "detail": {"mutation": mutation, "gid": gid,
                           "variables": variables}}

    # Shopify replaces the WHOLE seo object (live-verified): carry the
    # sibling title from the fresh pickup snapshot so a description fix
    # never wipes it.
    sibling = _sibling_seo_field(fix_row, "seo.title")
    if sibling is None:
        return {"ok": False, "outcome": "no_snapshot",
                "detail": "seo.description write needs the pickup snapshot's "
                          "seo.title (productUpdate.seo replaces the whole "
                          "object) — refusing a data-destroying write",
                "adapter": ADAPTER_SEO_DESCRIPTION}
    write_variables = {
        "product": {"id": gid,
                    "seo": {"title": sibling,
                            "description": variables["product"]["seo"]["description"]}},
    }
    write = client.run("productUpdate", PRODUCT_SEO_DESCRIPTION_UPDATE_MUTATION,
                       write_variables)
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"), "adapter": ADAPTER_SEO_DESCRIPTION}

    verify = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    live_meta = None
    if verify.get("ok"):
        product = (verify.get("data") or {}).get("product") or {}
        live_meta = (product.get("seo") or {}).get("description")
    new_meta = (((variables.get("product") or {}).get("seo") or {}).get("description")
                if isinstance(variables, dict) else None)
    verified = bool(verify.get("ok")) and live_meta == new_meta
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_meta,
        "adapter": ADAPTER_SEO_DESCRIPTION,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def product_seo_description_restore(conn, fix_row, config):
    """Restore BOTH seo fields from the fresh-read snapshot at pickup
    (productUpdate.seo replaces the whole object)."""
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    old_meta = snapshot.get("seo.description")
    if old_meta is None:
        return {"ok": False, "outcome": "no_snapshot", "adapter": ADAPTER_SEO_DESCRIPTION,
                "detail": "snapshot_json missing seo.description — refusing to "
                          "guess a restore value"}
    old_title = snapshot.get("seo.title")

    variables = {"product": {"id": gid,
                             "seo": {"title": old_title,
                                     "description": old_meta}}}
    write = client.run("productUpdate", PRODUCT_SEO_DESCRIPTION_UPDATE_MUTATION, variables)
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"), "adapter": ADAPTER_SEO_DESCRIPTION}
    verify = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    live_meta = None
    if verify.get("ok"):
        product = (verify.get("data") or {}).get("product") or {}
        live_meta = (product.get("seo") or {}).get("description")
    restored = bool(verify.get("ok")) and live_meta == old_meta
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_meta, "adapter": ADAPTER_SEO_DESCRIPTION,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


def product_seo_description_snapshot(conn, fix_row, config):
    """FRESH READ pre-state at pickup (full seo surface — a stale guard may
    compare any field the payload touches, and the snapshot doubles as the
    revert source for BOTH seo fields)."""
    client = _client_from_config(config)
    gid = fix_row["target_entity_ref"]
    read = client.run("product", PRODUCT_SEO_READ_QUERY, {"id": gid})
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": ADAPTER_SEO_DESCRIPTION}
    product = (read.get("data") or {}).get("product") or {}
    seo = product.get("seo") or {}
    return {
        "ok": True,
        "snapshot": {
            "seo.title": seo.get("title"),
            "seo.description": seo.get("description"),
            "product.title": product.get("title"),
            "status": product.get("status"),
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": ADAPTER_SEO_DESCRIPTION,
    }


# ------------------------------------------------------------
# redirect (sub_type 'redirect') — Phase 3 write path (plan/21 §2.5/§4)
# urlRedirectCreate(urlRedirect:{path,target}) verified against pinned
# 2026-01 docs; scope write_online_store_navigation (MUTATION_REGISTRY).
# ------------------------------------------------------------

URL_REDIRECT_CREATE_MUTATION = """
mutation FixRedirectCreate($urlRedirect: UrlRedirectInput!) {
  urlRedirectCreate(urlRedirect: $urlRedirect) {
    urlRedirect { id path target }
    userErrors { field message }
  }
}
"""

URL_REDIRECT_READ_QUERY = """
query FixRedirectRead($id: ID!) {
  urlRedirect(id: $id) { id path target }
}
"""

URL_REDIRECT_DELETE_MUTATION = """
mutation FixRedirectDelete($id: ID!) {
  urlRedirectDelete(id: $id) {
    deletedUrlRedirectId
    userErrors { field message }
  }
}
"""

ADAPTER_REDIRECT = "redirect"


# ------------------------------------------------------------
# publish-state (plan/21 §2.3 #1/#3, Phase 3): the only OTHER auto-write.
# Products: productUpdate(status: ACTIVE) — scope write_products.
# Collections: publishablePublish(id, input:{publicationId}) — scope
#   write_publications; the publication id is cached in system_config
#   ('shopify.publication_id', provisioned NULL, cached at first use via
#   the publications query). Snapshot = full publish pre-state so a revert
#   can restore EXACTLY what pickup found (product status / publication
#   membership), never an assumption.
# ------------------------------------------------------------

PRODUCT_PUBLISH_UPDATE_MUTATION = """
mutation FixProductPublish($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id status }
    userErrors { field message }
  }
}
"""

# LIVE-VERIFIED (2026-09-25, action-seo-test 2026-01): Publishable has NO
# `id` field on the read-back (undefinedfield) and the read-back is only
# meaningful when $publicationId is DECLARED — hence the explicit
# $publicationId variable in the mutation signature.
COLLECTION_PUBLISH_MUTATION = """
mutation FixCollectionPublish($id: ID!, $input: [PublicationInput!]!, $publicationId: ID!) {
  publishablePublish(id: $id, input: $input) {
    publishable { publishedOnPublication(publicationId: $publicationId) }
    userErrors { field message }
  }
}
"""

COLLECTION_UNPUBLISH_MUTATION = """
mutation FixCollectionUnpublish($id: ID!, $input: [PublicationInput!]!) {
  publishableUnpublish(id: $id, input: $input) {
    publishable { id publishedOnPublication(publicationId: $publicationId) }
    userErrors { field message }
  }
}
"""

PUBLISHABLE_READ_QUERY = """
query FixPublishableRead($id: ID!, $publicationId: ID!) {
  collection(id: $id) {
    id
    publishedOnPublication(publicationId: $publicationId)
  }
  product(id: $id) {
    id
    status
    publishedOnPublication(publicationId: $publicationId)
  }
}
"""

# LIVE-VERIFIED (2026-09-24, action-seo-test): stores with NO sales-channel
# publications reject ANY publicationId on publishedOnPublication with
# NOT_FOUND — which nulls the WHOLE product object (one bad field poisons
# the read). Product publish/unpublish only ever need status, so they read
# through this publication-free query; the publication-scoped read stays on
# the collection path (a None publication id there refuses upstream).
PRODUCT_STATUS_READ_QUERY = """
query FixProductStatusRead($id: ID!) {
  product(id: $id) { id status }
}
"""


def _product_status_read(client, gid):
    """Publication-free product status read -> (status, read_envelope)."""
    read = client.run("product", PRODUCT_STATUS_READ_QUERY, {"id": gid})
    status = ((read.get("data") or {}).get("product") or {}).get("status")
    return status, read

PUBLICATIONS_QUERY = """
query FixPublications {
  publications(first: 10) {
    nodes { id title }
  }
}
"""

ADAPTER_PUBLISH_PRODUCT = "product_publish_product"
ADAPTER_PUBLISH_COLLECTION = "collection_publish"


def _publication_id_from_config(conn, config):
    """Cached publication id from system_config; None when absent.

    The GraphQL caller resolves it itself when None (single source: the
    client caches the freshly-read id back into system_config).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_value FROM system_config "
                "WHERE config_key = 'shopify.publication_id'")
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _publication_read_variables(gid, publication_id):
    return {"id": gid,
            "publicationId": publication_id or "gid://shopify/Publication/unknown"}


def _extract_publishable_state(read_result):
    """Read-back envelope -> (status, published_on_publication).

    LIVE-VERIFIED (2026-09-25, 2026-01): Publishable carries NO `id`
    field — presence is detected via the publishedOnPublication value
    itself (present = the entity resolved; both-absent = read failed or
    entity missed).
    """
    data = read_result.get("data") or {}
    collection = data.get("collection")
    product = data.get("product")
    if collection is not None:
        return None, collection.get("publishedOnPublication")
    if product is not None:
        return product.get("status"), product.get("publishedOnPublication")
    return None, None


def _redirect_payload_for(payload_json):
    """Validate the redirect payload BEFORE any network call.

    payload_json = {mutation, variables: {urlRedirect: {path, target}},
    scope_required, doc_url}. Only urlRedirectCreate is executable here;
    Update/Delete exist in the registry for scope mapping + rollback.
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "urlRedirectCreate":
        raise WriteAdapterError(
            f"unsupported mutation for redirect adapter: {mutation!r}")
    redirect = ((payload_json.get("variables") or {}).get("urlRedirect") or {})
    path = (redirect.get("path") or "").strip()
    target = (redirect.get("target") or "").strip()
    if not path or not target:
        raise WriteAdapterError(
            "urlRedirectCreate payload must carry urlRedirect.path and "
            "urlRedirect.target")
    if path == target:
        raise WriteAdapterError("redirect path == target (no-op loop)")
    return redirect


def _extract_redirect_id(write_result):
    """urlRedirectCreate envelope -> created redirect GID (or None)."""
    data = write_result.get("data") or {}
    created = (data.get("urlRedirectCreate") or {}).get("urlRedirect") or {}
    return created.get("id")


def redirect_execute(conn, fix_row, config, dry_run=False, client=None):
    """Apply one redirect fix: create -> read-back verify (id + path).

    fix_row needs target_entity_ref ONLY as a pre-read hint — a redirect
    creates a NEW Shopify object, so the created id comes from the
    mutation envelope, not from generation. Snapshot semantics: the
    pre-state of a redirect is "redirect absent" (deleting it restores).

    client: TEST seam (FakeGraphQLClient) — when omitted, the client is
    built from config exactly like the other adapters.
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:  # never-raise adapter contract
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_REDIRECT}
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        redirect = _redirect_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_REDIRECT}

    if dry_run:
        return {"ok": True, "outcome": "dry_run", "adapter": ADAPTER_REDIRECT,
                "detail": {"mutation": "urlRedirectCreate",
                           "urlRedirect": redirect}}

    try:
        write = client.run("urlRedirectCreate", URL_REDIRECT_CREATE_MUTATION,
                           {"urlRedirect": redirect})
    except Exception as exc:  # never-raise adapter contract (call-time failures)
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_REDIRECT}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"), "adapter": ADAPTER_REDIRECT}

    # Read-back verification (trust only a fresh read of the created id).
    redirect_id = _extract_redirect_id(write)
    verified = False
    live_path = live_target = None
    verify = {"ok": False, "outcome": "no_redirect_id"}
    if redirect_id:
        verify = client.run("urlRedirect", URL_REDIRECT_READ_QUERY,
                            {"id": redirect_id})
        if verify.get("ok"):
            live = (verify.get("data") or {}).get("urlRedirect") or {}
            live_path = live.get("path")
            live_target = live.get("target")
            verified = (live_path == redirect.get("path")
                        and live_target == redirect.get("target"))
    return {
        "ok": True,
        "outcome": "ok",
        "redirect_id": redirect_id,
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": f"{live_path} -> {live_target}" if live_path else None,
        "adapter": ADAPTER_REDIRECT,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
        # Rollback source: the executor persists this merged snapshot when
        # wiring measurement, and revert reads redirect.created_id from it.
        "snapshot_patch": {"redirect.created_id": redirect_id},
    }


def redirect_snapshot(conn, fix_row, config):
    """Pre-state at pickup: does a redirect for this path already exist?

    A redirect is CREATE-only (no pre-existing value is mutated), so the
    "snapshot" records the pre-state fact the rollback needs: whether a
    redirect already maps this path. An existing redirect is NOT overwritten
    (the executor's stale-diff/overwrite risk is removed at the source):
    the snapshot carries redirect_exists so execute can no-op safely, and
    a revert deletes only the redirect THIS fix created.
    """
    client = _client_from_config(config)
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        redirect = _redirect_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_REDIRECT}
    return {
        "ok": True,
        "snapshot": {
            "redirect.path": redirect.get("path"),
            "redirect.target": redirect.get("target"),
            "redirect.pre_existing": None,   # resolved at execute time
            "read_at_api_version": client.api_version,
        },
        "adapter": ADAPTER_REDIRECT,
    }


def redirect_restore(conn, fix_row, config, client=None):
    """Rollback: delete the redirect THIS fix created.

    The created id is persisted in snapshot_json (redirect.created_id) at
    execute time. A missing created_id means the original write never
    completed — deleting by path would be a guess, so refuse (typed).

    client: TEST seam, same as redirect_execute.
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_REDIRECT}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    created_id = snapshot.get("redirect.created_id") if isinstance(snapshot, dict) else None
    if not created_id:
        return {"ok": False, "outcome": "no_snapshot", "adapter": ADAPTER_REDIRECT,
                "detail": "snapshot_json missing redirect.created_id — refusing "
                          "to delete a redirect this fix did not create"}
    delete = client.run("urlRedirectDelete", URL_REDIRECT_DELETE_MUTATION,
                        {"id": created_id})
    if not delete.get("ok"):
        return {"ok": False, "outcome": delete.get("outcome", "provider_error"),
                "userErrors": delete.get("userErrors") or [],
                "errors": delete.get("errors") or [],
                "detail": delete.get("detail"), "adapter": ADAPTER_REDIRECT}
    deleted = ((delete.get("data") or {}).get("urlRedirectDelete") or {}
               ).get("deletedUrlRedirectId")
    return {"ok": True, "outcome": "ok", "restored": deleted,
            "live_value": None, "adapter": ADAPTER_REDIRECT,
            "adapter_response": {"delete": _safe_response(delete)}}


ADAPTERS = {
    "seo.title": {
        "execute": product_seo_title_execute,
        "restore": product_seo_title_restore,
        "snapshot": product_seo_title_snapshot,
    },
    "seo.description": {
        "execute": product_seo_description_execute,
        "restore": product_seo_description_restore,
        "snapshot": product_seo_description_snapshot,
    },
    "redirect": {
        "execute": redirect_execute,
        "restore": redirect_restore,
        "snapshot": redirect_snapshot,
    },
}


def get_adapter(sub_type):
    adapter = ADAPTERS.get(sub_type)
    if adapter is None:
        raise WriteAdapterError(f"no write adapter for sub_type {sub_type!r}")
    return adapter


# ------------------------------------------------------------
# product_publish_product — productUpdate(status: ACTIVE) write path
# (plan/21 §2.3 #1: publish restore; VERIFIED 2026-01 ProductUpdateInput.status)
# ------------------------------------------------------------

def _publish_product_payload_for(payload_json):
    """payload_json = {mutation, variables: {product: {id, status}}}.

    Only status writes route here: this adapter must never carry seo/title
    drift (that is the seo.* adapters' exclusive surface).
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "productUpdate":
        raise WriteAdapterError(
            f"unsupported mutation for product_publish_product adapter: {mutation!r}")
    variables = (payload_json.get("variables") or {}).get("product") or {}
    if not variables.get("id") or not variables.get("status"):
        raise WriteAdapterError(
            "productUpdate payload must carry product.id and product.status")
    if set(variables.keys()) - {"id", "status"}:
        raise WriteAdapterError(
            "product_publish_product adapter writes ONLY id+status "
            "(no seo/title drift through the publish path)")
    return variables


def product_publish_execute(conn, fix_row, config, dry_run=False, client=None):
    """Apply one product publish-state fix: write -> read-back verify."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:  # never-raise adapter contract
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_PRODUCT}
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        variables = _publish_product_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_PRODUCT}

    gid = fix_row["target_entity_ref"]
    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_PUBLISH_PRODUCT,
                "detail": {"mutation": "productUpdate", "gid": gid,
                           "variables": variables}}

    try:
        write = client.run("productUpdate", PRODUCT_PUBLISH_UPDATE_MUTATION,
                           {"product": variables})
    except Exception as exc:  # never-raise adapter contract
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_PRODUCT}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_PUBLISH_PRODUCT}

    publication_id = _publication_id_from_config(conn, config)
    # Status verify via the publication-free read (LIVE-VERIFIED: stores
    # with no publications reject the publication-scoped field outright).
    live_status, verify = _product_status_read(client, gid)
    verified = (bool(verify.get("ok"))
                and live_status == variables.get("status"))
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_status,
        "adapter": ADAPTER_PUBLISH_PRODUCT,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def product_publish_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: product status + publication state."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_PRODUCT}
    gid = fix_row["target_entity_ref"]
    publication_id = _publication_id_from_config(conn, config)
    try:
        status, read = _product_status_read(client, gid)
        published = None
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_PRODUCT}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": ADAPTER_PUBLISH_PRODUCT}
    return {
        "ok": True,
        "snapshot": {
            "product.status": status,
            "publishable.published_on_publication": published,
            "publication_id": publication_id,
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": ADAPTER_PUBLISH_PRODUCT,
    }


def product_publish_restore(conn, fix_row, config, client=None):
    """Rollback: restore the product status read at pickup.

    A snapshot without product.status refuses to guess (typed no_snapshot).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_PRODUCT}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    old_status = snapshot.get("product.status") if isinstance(snapshot, dict) else None
    if not old_status:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_PUBLISH_PRODUCT,
                "detail": "snapshot_json missing product.status — refusing "
                          "to guess a restore value"}
    gid = fix_row["target_entity_ref"]
    variables = {"id": gid, "status": old_status}
    try:
        write = client.run("productUpdate", PRODUCT_PUBLISH_UPDATE_MUTATION,
                           {"product": variables})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_PRODUCT}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_PUBLISH_PRODUCT}
    live_status, verify = _product_status_read(client, gid)
    restored = bool(verify.get("ok")) and live_status == old_status
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_status, "adapter": ADAPTER_PUBLISH_PRODUCT,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


# ------------------------------------------------------------
# collection_publish — publishablePublish/publishableUnpublish write path
# (plan/21 §2.3 #1: collection publish restore; VERIFIED 2026-01:
#  args id + input:[PublicationInput!]!, scope write_publications)
# ------------------------------------------------------------

def _collection_publish_payload_for(payload_json):
    """payload_json = {mutation, variables: {id, publicationId}}.

    Only publishablePublish routes here (unpublish is the rollback path,
    executed via restore with the snapshot publication id).
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "publishablePublish":
        raise WriteAdapterError(
            f"unsupported mutation for collection_publish adapter: {mutation!r}")
    variables = payload_json.get("variables") or {}
    if not variables.get("id") or not variables.get("publicationId"):
        raise WriteAdapterError(
            "publishablePublish payload must carry variables.id and "
            "variables.publicationId")
    return variables


def collection_publish_execute(conn, fix_row, config, dry_run=False, client=None):
    """Apply one collection publish fix: publish -> read-back verify.

    The publication id comes from the payload (generation resolved it from
    system_config; a None there means generation failed to resolve — the
    executor expires the row instead of guessing a publication).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:  # never-raise adapter contract
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_COLLECTION}
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        variables = _collection_publish_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_COLLECTION}

    gid = fix_row["target_entity_ref"]
    publication_id = variables.get("publicationId")
    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_PUBLISH_COLLECTION,
                "detail": {"mutation": "publishablePublish", "gid": gid,
                           "publicationId": publication_id}}

    try:
        write = client.run("publishablePublish", COLLECTION_PUBLISH_MUTATION,
                           {"id": gid,
                            "input": [{"publicationId": publication_id}],
                            "publicationId": publication_id})
    except Exception as exc:  # never-raise adapter contract
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_COLLECTION}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_PUBLISH_COLLECTION}

    verify = client.run("collection", PUBLISHABLE_READ_QUERY,
                        _publication_read_variables(gid, publication_id))
    _, live_published = _extract_publishable_state(verify)
    verified = bool(verify.get("ok")) and live_published is True
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_published,
        "adapter": ADAPTER_PUBLISH_COLLECTION,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def collection_publish_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: collection publication membership.

    Snapshot captures published_on_publication so a revert can unpublish
    ONLY when pickup found the collection already published (an absent
    membership restores 'absent' by doing nothing — recorded as null).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_COLLECTION}
    gid = fix_row["target_entity_ref"]
    payload_json = fix_row.get("payload_json") or {}
    if isinstance(payload_json, str):
        try:
            payload_json = json.loads(payload_json)
        except (TypeError, ValueError):
            payload_json = {}
    variables = (payload_json.get("variables") or {}) if isinstance(payload_json, dict) else {}
    publication_id = variables.get("publicationId") or _publication_id_from_config(conn, config)
    if not publication_id:
        return {"ok": False, "outcome": "no_publication_id",
                "detail": "publishablePublish fix carries no publicationId and "
                          "system_config has none cached — refusing to guess",
                "adapter": ADAPTER_PUBLISH_COLLECTION}
    try:
        read = client.run("collection", PUBLISHABLE_READ_QUERY,
                          _publication_read_variables(gid, publication_id))
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_COLLECTION}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": ADAPTER_PUBLISH_COLLECTION}
    _, published = _extract_publishable_state(read)
    return {
        "ok": True,
        "snapshot": {
            "publishable.published_on_publication": published,
            "publication_id": publication_id,
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": ADAPTER_PUBLISH_COLLECTION,
    }


def collection_publish_restore(conn, fix_row, config, client=None):
    """Rollback: restore the publication membership read at pickup.

    Snapshot says the collection WAS published -> re-publish. Snapshot says
    it was NOT -> unpublish (the fix published it). A missing publication id
    refuses (typed no_snapshot) — never a guessed channel.
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_PUBLISH_COLLECTION}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    old_published = (snapshot.get("publishable.published_on_publication")
                     if isinstance(snapshot, dict) else None)
    publication_id = (snapshot.get("publication_id")
                      if isinstance(snapshot, dict) else None)
    if publication_id is None or old_published is None:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_PUBLISH_COLLECTION,
                "detail": "snapshot_json missing publication state — refusing "
                          "to guess a restore value"}
    gid = fix_row["target_entity_ref"]
    mutation_name = ("publishablePublish" if old_published is True
                     else "publishableUnpublish")
    mutation_sql = (COLLECTION_PUBLISH_MUTATION if old_published is True
                    else COLLECTION_UNPUBLISH_MUTATION)
    try:
        write = client.run(mutation_name, mutation_sql,
                           {"id": gid,
                            "input": [{"publicationId": publication_id}],
                            "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_PUBLISH_COLLECTION}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_PUBLISH_COLLECTION}
    verify = client.run("collection", PUBLISHABLE_READ_QUERY,
                        _publication_read_variables(gid, publication_id))
    _, live_published = _extract_publishable_state(verify)
    restored = bool(verify.get("ok")) and live_published == old_published
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_published, "adapter": ADAPTER_PUBLISH_COLLECTION,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}

ADAPTERS.update({
    "product_publish_product": {
        "execute": product_publish_execute,
        "restore": product_publish_restore,
        "snapshot": product_publish_snapshot,
    },
    "collection_publish": {
        "execute": collection_publish_execute,
        "restore": collection_publish_restore,
        "snapshot": collection_publish_snapshot,
    },
})


# ------------------------------------------------------------
# Unpublish-after-redirect (Phase 4 item 3, plan/21 §2.5): retirement of
# the redirect SOURCE page AFTER its 301 is verified live. One adapter per
# entity kind, mirroring the publish adapters:
#   product_unpublish    — productUpdate(status: ARCHIVED)  (write_products)
#   collection_unpublish — publishableUnpublish(publicationId) (write_publications)
# Sequencing is STRUCTURAL: these rows are minted by the executor only in
# the redirect-verified branch (see jobs/fix_executor.py), never alongside
# the redirect mutation itself.
# ------------------------------------------------------------

ADAPTER_UNPUBLISH_PRODUCT = "product_unpublish"
ADAPTER_UNPUBLISH_COLLECTION = "collection_unpublish"

PRODUCT_ARCHIVE_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/productUpdate"
)
COLLECTION_UNPUBLISH_DOC_URL = (
    "https://shopify.dev/docs/api/admin-graphql/2026-01/mutations/publishableUnpublish"
)


def build_product_unpublish_payload(gid):
    """productUpdate(status: ARCHIVED) payload (VERIFIED 2026-01).

    ARCHIVED (not DRAFT): the product is retired — it keeps its data, drops
    out of every sales channel, and stays revertable to its pickup status.
    """
    return {
        "mutation": "productUpdate",
        "variables": {"product": {"id": gid, "status": "ARCHIVED"}},
        "scope_required": "write_products",
        "doc_url": PRODUCT_ARCHIVE_DOC_URL,
    }


def build_collection_unpublish_payload(gid, publication_id):
    """publishableUnpublish payload (VERIFIED 2026-01; write_publications)."""
    return {
        "mutation": "publishableUnpublish",
        "variables": {"id": gid, "publicationId": publication_id},
        "scope_required": "write_publications",
        "doc_url": COLLECTION_UNPUBLISH_DOC_URL,
    }


def _unpublish_product_payload_for(payload_json):
    """Only status=ARCHIVED writes route through the product unpublish
    adapter (the executor's typed guard against payload drift)."""
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "productUpdate":
        raise WriteAdapterError(
            f"unsupported mutation for product_unpublish adapter: {mutation!r}")
    variables = (payload_json.get("variables") or {}).get("product") or {}
    if not variables.get("id") or variables.get("status") != "ARCHIVED":
        raise WriteAdapterError(
            "product_unpublish payload must carry product.id and "
            "product.status == 'ARCHIVED'")
    if set(variables.keys()) - {"id", "status"}:
        raise WriteAdapterError(
            "product_unpublish adapter writes ONLY id+status (no seo drift)")
    return variables


def product_unpublish_execute(conn, fix_row, config, dry_run=False, client=None):
    """Retire one product: status -> ARCHIVED, read-back verify."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:  # never-raise adapter contract
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        variables = _unpublish_product_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}

    gid = fix_row["target_entity_ref"]
    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_UNPUBLISH_PRODUCT,
                "detail": {"mutation": "productUpdate", "gid": gid,
                           "variables": variables}}

    try:
        write = client.run("productUpdate", PRODUCT_PUBLISH_UPDATE_MUTATION,
                           {"product": variables})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}

    publication_id = _publication_id_from_config(conn, config)
    # Status verify via the publication-free read (LIVE-VERIFIED: stores
    # with no publications reject the publication-scoped field outright).
    live_status, verify = _product_status_read(client, gid)
    verified = (bool(verify.get("ok"))
                and live_status == variables.get("status"))
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_status,
        "adapter": ADAPTER_UNPUBLISH_PRODUCT,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def product_unpublish_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: product status + publication state."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    gid = fix_row["target_entity_ref"]
    publication_id = _publication_id_from_config(conn, config)
    try:
        status, read = _product_status_read(client, gid)
        published = None
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    return {
        "ok": True,
        "snapshot": {
            "product.status": status,
            "publishable.published_on_publication": published,
            "publication_id": publication_id,
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": ADAPTER_UNPUBLISH_PRODUCT,
    }


def product_unpublish_restore(conn, fix_row, config, client=None):
    """Rollback: restore the product status read at pickup (ACTIVE etc.).

    A snapshot without product.status refuses to guess (typed no_snapshot).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    old_status = (snapshot.get("product.status")
                  if isinstance(snapshot, dict) else None)
    if not old_status:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_UNPUBLISH_PRODUCT,
                "detail": "snapshot_json missing product.status — refusing "
                          "to guess a restore value"}
    gid = fix_row["target_entity_ref"]
    variables = {"id": gid, "status": old_status}
    try:
        write = client.run("productUpdate", PRODUCT_PUBLISH_UPDATE_MUTATION,
                           {"product": variables})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_PRODUCT}
    live_status, verify = _product_status_read(client, gid)
    restored = bool(verify.get("ok")) and live_status == old_status
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_status, "adapter": ADAPTER_UNPUBLISH_PRODUCT,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


def _unpublish_collection_payload_for(payload_json):
    """Only publishableUnpublish routes through the collection unpublish
    adapter (publish is the rollback direction, handled by its own
    adapter's restore)."""
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "publishableUnpublish":
        raise WriteAdapterError(
            f"unsupported mutation for collection_unpublish adapter: {mutation!r}")
    variables = payload_json.get("variables") or {}
    if not variables.get("id") or not variables.get("publicationId"):
        raise WriteAdapterError(
            "publishableUnpublish payload must carry variables.id and "
            "variables.publicationId")
    return variables


def collection_unpublish_execute(conn, fix_row, config, dry_run=False, client=None):
    """Retire one collection from the online store: publishableUnpublish
    against the payload's publication id, read-back verify."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:  # never-raise adapter contract
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    try:
        payload_json = fix_row["payload_json"]
        if isinstance(payload_json, str):
            payload_json = json.loads(payload_json)
        variables = _unpublish_collection_payload_for(payload_json)
    except (WriteAdapterError, ValueError) as exc:
        return {"ok": False, "outcome": "payload_invalid", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}

    gid = fix_row["target_entity_ref"]
    publication_id = variables.get("publicationId")
    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_UNPUBLISH_COLLECTION,
                "detail": {"mutation": "publishableUnpublish", "gid": gid,
                           "publicationId": publication_id}}

    try:
        write = client.run("publishableUnpublish", COLLECTION_UNPUBLISH_MUTATION,
                           {"id": gid,
                            "input": [{"publicationId": publication_id}],
                            "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}

    verify = client.run("collection", PUBLISHABLE_READ_QUERY,
                        _publication_read_variables(gid, publication_id))
    _, live_published = _extract_publishable_state(verify)
    verified = bool(verify.get("ok")) and live_published is False
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_published,
        "adapter": ADAPTER_UNPUBLISH_COLLECTION,
        "adapter_response": {"write": _safe_response(write),
                             "verify": _safe_response(verify)},
    }


def collection_unpublish_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: collection publication membership.

    Snapshot captures published_on_publication so a revert can re-publish
    ONLY when pickup found the collection published; an absent membership
    restores 'absent' (revert becomes a no-op, recorded as null).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    gid = fix_row["target_entity_ref"]
    payload_json = fix_row.get("payload_json") or {}
    if isinstance(payload_json, str):
        try:
            payload_json = json.loads(payload_json)
        except (TypeError, ValueError):
            payload_json = {}
    variables = (payload_json.get("variables") or {}) if isinstance(payload_json, dict) else {}
    publication_id = variables.get("publicationId") or _publication_id_from_config(conn, config)
    if not publication_id:
        return {"ok": False, "outcome": "no_publication_id",
                "detail": "collection_unpublish fix carries no publicationId "
                          "and system_config has none cached — refusing to "
                          "guess a channel",
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    try:
        read = client.run("collection", PUBLISHABLE_READ_QUERY,
                          _publication_read_variables(gid, publication_id))
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    _, published = _extract_publishable_state(read)
    return {
        "ok": True,
        "snapshot": {
            "publishable.published_on_publication": published,
            "publication_id": publication_id,
            "read_at_api_version": read.get("api_version"),
        },
        "adapter": ADAPTER_UNPUBLISH_COLLECTION,
    }


def collection_unpublish_restore(conn, fix_row, config, client=None):
    """Rollback: restore the publication membership read at pickup.

    Snapshot says the collection WAS published -> re-publish. Snapshot says
    it was NOT -> nothing to restore (the fix changed nothing). A missing
    publication id refuses (typed no_snapshot) — never a guessed channel.
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    old_published = (snapshot.get("publishable.published_on_publication")
                     if isinstance(snapshot, dict) else None)
    publication_id = (snapshot.get("publication_id")
                      if isinstance(snapshot, dict) else None)
    if publication_id is None or old_published is None:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_UNPUBLISH_COLLECTION,
                "detail": "snapshot_json missing publication state — refusing "
                          "to guess a restore value"}
    gid = fix_row["target_entity_ref"]
    if old_published is False:
        # Pickup found the collection ALREADY unpublished — the fix was a
        # no-op on this surface; restore = affirm the unchanged state.
        return {"ok": True, "outcome": "ok", "restored": True,
                "live_value": False, "adapter": ADAPTER_UNPUBLISH_COLLECTION,
                "adapter_response": {"restore": {"ok": True,
                                                 "no_op": True}}}
    mutation_name = "publishablePublish"
    mutation_sql = COLLECTION_PUBLISH_MUTATION
    try:
        write = client.run(mutation_name, mutation_sql,
                           {"id": gid,
                            "input": [{"publicationId": publication_id}],
                            "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_UNPUBLISH_COLLECTION}
    verify = client.run("collection", PUBLISHABLE_READ_QUERY,
                        _publication_read_variables(gid, publication_id))
    _, live_published = _extract_publishable_state(verify)
    restored = bool(verify.get("ok")) and live_published == old_published
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_published, "adapter": ADAPTER_UNPUBLISH_COLLECTION,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


ADAPTERS.update({
    "product_unpublish": {
        "execute": product_unpublish_execute,
        "restore": product_unpublish_restore,
        "snapshot": product_unpublish_snapshot,
    },
    "collection_unpublish": {
        "execute": collection_unpublish_execute,
        "restore": collection_unpublish_restore,
        "snapshot": collection_unpublish_snapshot,
    },
})


# ------------------------------------------------------------
# collection_description (sub_type 'collection_description') —
# collectionUpdate(input: {id, descriptionHtml}) write path (plan/24 §2;
# VERIFIED 2026-01: CollectionInput.descriptionHtml; scope write_products;
# registry row exists in MUTATION_REGISTRY). Smart (rule-set) collections
# answer the mutation with an async job — the verify path treats an
# eventually-consistent read as verify_pending, NEVER as a failed write.
# ------------------------------------------------------------

COLLECTION_DESCRIPTION_READ_QUERY = """
query FixCollectionDescriptionRead($id: ID!) {
  collection(id: $id) { id descriptionHtml }
}
"""

COLLECTION_DESCRIPTION_UPDATE_MUTATION = """
mutation FixCollectionDescriptionUpdate($collection: CollectionInput!) {
  collectionUpdate(collection: $collection) {
    collection { id descriptionHtml }
    job { id done }
    userErrors { field message }
  }
}
"""

ADAPTER_COLLECTION_DESCRIPTION = "collection_description"

# Smart-collection writes settle asynchronously: bounded read-back backoff
# (plan/24 §2.1) before declaring the verify outcome. A still-stale read
# after the window is verify_pending (no auto-revert on an eventually-
# consistent read) — only a DEFINITIVE value mismatch after the window
# is verify_failed (which the executor auto-reverts per plan/21 §5.3).
_DESC_VERIFY_ATTEMPTS = 3
_DESC_VERIFY_BACKOFF_SECONDS = 2.0


def _collection_description_payload_for(payload_json):
    """payload_json = {mutation, variables, scope_required, doc_url}.

    Strict single-field discipline (plan/24 §2.2): ONLY {id,
    descriptionHtml} may ride the CollectionInput — any title/ruleSet/
    published key is a drift vector into surfaces this fix must never
    touch (rule-set edits are an async job surface with different risk).
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "collectionUpdate":
        raise WriteAdapterError(
            f"unsupported mutation for collection_description adapter: "
            f"{mutation!r}")
    variables = (payload_json.get("variables") or {}).get("collection") or {}
    description = variables.get("descriptionHtml")
    if not variables.get("id") or not isinstance(description, str):
        raise WriteAdapterError(
            "collectionUpdate payload must carry collection.id and "
            "collection.descriptionHtml")
    extra = set(variables.keys()) - {"id", "descriptionHtml"}
    if extra:
        raise WriteAdapterError(
            "collection_description adapter writes ONLY descriptionHtml "
            f"(no title/ruleSet drift through this path): {sorted(extra)}")
    return variables


def _collection_description_read(client, gid):
    """Publication-free collection description read -> read envelope."""
    return client.run("collection", COLLECTION_DESCRIPTION_READ_QUERY,
                      {"id": gid})


def _live_description(read):
    collection = ((read.get("data") or {}).get("collection") or {})
    return collection.get("descriptionHtml")


def collection_description_execute(conn, fix_row, config, dry_run=False,
                                   client=None):
    """Apply one collection_description fix: write -> read-back verify.

    Smart collections settle via an async job: a bounded backoff re-reads
    the description before declaring verify_pending (still-stale) vs
    verified (settled) vs verify_failed (definitive mismatch).
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    gid = fix_row["target_entity_ref"]
    try:
        variables = _collection_description_payload_for(
            fix_row["payload_json"])
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}

    new_html = variables["descriptionHtml"]
    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_COLLECTION_DESCRIPTION,
                "detail": {"mutation": "collectionUpdate", "gid": gid,
                           "variables": variables}}

    try:
        write = client.run("collectionUpdate",
                           COLLECTION_DESCRIPTION_UPDATE_MUTATION,
                           {"collection": {"id": gid,
                                           "descriptionHtml": new_html}})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    if not write.get("ok"):
        return {"ok": False,
                "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}

    import time
    live_html = None
    verified = False
    settled = False
    # Smart (rule-set) collections answer with an async job: while the job
    # is unsettled (job.done=false, plan/21:407), a stale read is
    # verify_pending — NEVER verify_failed — so the executor does not
    # auto-revert a write that likely landed. Only a mismatch on a
    # synchronous write (no job / job.done=true) after the window is a
    # definitive verify_failed.
    job = ((write.get("data") or {}).get("collectionUpdate") or {}).get("job")
    job_done = bool(job.get("done")) if isinstance(job, dict) else None
    for attempt in range(_DESC_VERIFY_ATTEMPTS):
        if attempt:
            time.sleep(_DESC_VERIFY_BACKOFF_SECONDS)
        verify = _collection_description_read(client, gid)
        if not verify.get("ok"):
            continue
        live_html = _live_description(verify)
        if live_html == new_html:
            verified = True
            settled = True
            break
        if isinstance(live_html, str) and live_html != new_html:
            # A DIFFERENT value: definitive only on a synchronous write —
            # an unsettled async job stays verify_pending (plan/24 §2.1).
            settled = (job_done is not False
                       and attempt == _DESC_VERIFY_ATTEMPTS - 1)
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": ("verified" if verified
                                else "verify_pending" if not settled
                                else "verify_failed"),
        "live_value": live_html,
        "adapter": ADAPTER_COLLECTION_DESCRIPTION,
        "adapter_response": {"write": _safe_response(write)},
    }


def collection_description_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: the collection's live
    descriptionHtml (plan/24 §2.4). A failed read returns ok=False and
    the executor fails the fix (snapshot_failed) — generation-time
    old_values are never trusted for the restore path."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    gid = fix_row["target_entity_ref"]
    read = _collection_description_read(client, gid)
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "userErrors": read.get("userErrors") or [],
                "errors": read.get("errors") or [],
                "detail": read.get("detail"),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    collection = (read.get("data") or {}).get("collection") or {}
    if not collection.get("id"):
        return {"ok": False, "outcome": "not_found",
                "detail": "collection read returned no collection object",
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    return {"ok": True, "outcome": "ok",
            "snapshot": {
                "description_html": collection.get("descriptionHtml"),
                "title": collection.get("title"),
                "read_at_api_version": read.get("api_version"),
            },
            "adapter": ADAPTER_COLLECTION_DESCRIPTION}


def collection_description_restore(conn, fix_row, config, client=None):
    """Rollback: write the pickup snapshot's descriptionHtml back.

    A missing snapshot refuses (typed no_snapshot) — never a guessed
    restore value (plan/24 §2.4)."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    old_html = (snapshot.get("description_html")
                if isinstance(snapshot, dict) else None)
    if not isinstance(old_html, str):
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_COLLECTION_DESCRIPTION,
                "detail": "snapshot_json missing description_html — "
                          "refusing to guess a restore value"}
    gid = fix_row["target_entity_ref"]
    try:
        write = client.run(
            "collectionUpdate", COLLECTION_DESCRIPTION_UPDATE_MUTATION,
            {"collection": {"id": gid, "descriptionHtml": old_html}})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_COLLECTION_DESCRIPTION}
    verify = _collection_description_read(client, gid)
    live_html = _live_description(verify) if verify.get("ok") else None
    restored = bool(verify.get("ok")) and live_html == old_html
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_html,
            "adapter": ADAPTER_COLLECTION_DESCRIPTION,
            "adapter_response": {"write": _safe_response(write),
                                 "verify": _safe_response(verify)}}


ADAPTERS.update({
    "collection_description": {
        "execute": collection_description_execute,
        "restore": collection_description_restore,
        "snapshot": collection_description_snapshot,
    },
})


# ------------------------------------------------------------
# collection_create (sub_type 'collection_create') — the FIRST write path
# with NO pre-existing entity (plan/25 §3). Sequence: collectionCreate ->
# snapshot_patch(created_gid) -> publishablePublish -> read-back verify.
# Revert is STRICTLY publishableUnpublish — never collectionDelete (the
# operator may have enriched the collection between apply and revert;
# unpublish restores the exact pre-fix public state: no public page).
# ------------------------------------------------------------

COLLECTION_CREATE_MUTATION = """
mutation FixCollectionCreate($input: CollectionInput!) {
  collectionCreate(input: $input) {
    collection { id title handle descriptionHtml seo { title description } }
    userErrors { field message }
  }
}
"""

ADAPTER_COLLECTION_CREATE = "collection_create"

_CREATE_ALLOWED_FIELDS = frozenset(
    {"title", "handle", "descriptionHtml", "seo", "products"})
_CREATE_ALLOWED_SEO = frozenset({"title", "description"})
# Real stores can carry non-numeric Shopify ids (hex/legacy forms seen in
# fixtures and older shops) — require the GID SHAPE, not decimal digits.
_PRODUCT_GID_RE = re.compile(r"^gid://shopify/Product/[0-9a-zA-Z]+$")


def _collection_create_payload_for(payload_json):
    """Strict allowlist (plan/25 §3.2): exactly {title, handle,
    descriptionHtml, seo, products}; seo limited to {title, description};
    product ids must be real GIDs. Anything else (ruleSet, templateSuffix,
    redirectNewHandle, image, metafields, publications) is a drift vector
    -> payload_invalid BEFORE any network call."""
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    mutation = payload_json.get("mutation")
    if mutation != "collectionCreate":
        raise WriteAdapterError(
            f"unsupported mutation for collection_create adapter: "
            f"{mutation!r}")
    variables = (payload_json.get("variables") or {}).get("collection") or {}
    extra = set(variables.keys()) - _CREATE_ALLOWED_FIELDS
    if extra:
        raise WriteAdapterError(
            "collection_create payload carries disallowed fields "
            f"(no ruleSet/templateSuffix drift): {sorted(extra)}")
    missing = {"title", "handle"} - set(variables.keys())
    if missing:
        raise WriteAdapterError(
            f"collectionCreate payload missing required fields: "
            f"{sorted(missing)}")
    seo = variables.get("seo") or {}
    if seo and set(seo.keys()) - _CREATE_ALLOWED_SEO:
        raise WriteAdapterError(
            "collectionCreate seo limited to {title, description}")
    products = variables.get("products") or []
    if not isinstance(products, list) or not products:
        raise WriteAdapterError(
            "collectionCreate must attach at least one member product")
    for gid in products:
        if not isinstance(gid, str) or not _PRODUCT_GID_RE.match(gid):
            raise WriteAdapterError(
                f"collectionCreate products carry a non-GID member: "
                f"{gid!r}")
    return variables


def _collection_by_handle_read(client, handle):
    query = """
query FixCollectionByHandle($handle: String!) {
  collectionByHandle(handle: $handle) {
    id
    title
    handle
    descriptionHtml
    seo { title description }
  }
}
"""
    return client.run("collectionByHandle", query, {"handle": handle})


def collection_create_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup — the ONE special case where
    "entity does not exist" is a VALID pre-state (plan/25 §3.4):
      * collectionByHandle -> null  => {"exists": False} (proceed)
      * collectionByHandle -> found => {"exists": True} (the executor's
        stale-diff guard fails the fix BEFORE the create fires — the
        only sub_type where the stale guard runs in the positive
        direction)."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_CREATE}
    payload_json = fix_row.get("payload_json") or {}
    if isinstance(payload_json, str):
        try:
            payload_json = json.loads(payload_json)
        except (TypeError, ValueError):
            payload_json = {}
    variables = ((payload_json.get("variables") or {})
                 .get("collection") or {}) if isinstance(payload_json, dict) else {}
    handle = variables.get("handle")
    if not handle:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": "collection_create payload carries no handle",
                "adapter": ADAPTER_COLLECTION_CREATE}
    read = _collection_by_handle_read(client, handle)
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "userErrors": read.get("userErrors") or [],
                "errors": read.get("errors") or [],
                "detail": read.get("detail"),
                "adapter": ADAPTER_COLLECTION_CREATE}
    existing = (read.get("data") or {}).get("collectionByHandle")
    return {"ok": True, "outcome": "ok",
            "snapshot": {
                "exists": bool(existing),
                "handle": handle,
                "existing_gid": (existing or {}).get("id"),
                "read_at_api_version": read.get("api_version"),
            },
            "adapter": ADAPTER_COLLECTION_CREATE}


def collection_create_execute(conn, fix_row, config, dry_run=False,
                              client=None):
    """Create -> snapshot_patch(created_gid) -> publish -> verify.

    Typed outcomes:
      ok/verified            — created + published + read-back matched
      create_publish_failed  — collection EXISTS (created_gid in detail)
                               but publish failed; NOT auto-undone: the
                               entity is operator-visible, a human
                               decides (publish by hand or delete).
      provider_error         — create itself failed; nothing to revert.
    """
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_CREATE}
    try:
        variables = _collection_create_payload_for(
            fix_row["payload_json"])
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_CREATE}
    handle = variables["handle"]

    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "detail": {"mutation": "collectionCreate",
                           "handle": handle,
                           "variables": variables}}

    # IDEMPOTENT ROUTING (plan/25 §3.3 self-healing): a previous run may
    # have created the collection but failed the publish step (the
    # typed create_publish_failed path records created_gid but never
    # auto-undoes). Resolve the handle FIRST — if it exists, skip the
    # create entirely and route straight to publish-only with the
    # resolved GID.
    existing_gid = None
    try:
        pre = _collection_by_handle_read(client, handle)
        if pre.get("ok"):
            existing_gid = ((pre.get("data") or {})
                            .get("collectionByHandle") or {}).get("id")
    except Exception:
        existing_gid = None  # network hiccup: fall through to create

    if existing_gid:
        # Already exists — the create half of this fix is a NO-OP.
        # Publish the existing entity (self-heal the interrupted run).
        return _collection_create_publish_only(
            conn, client, config, gid=existing_gid, handle=handle,
            expected_title=None,  # don't fail on a title the operator edited
            existing=True)

    try:
        # LIVE-VERIFIED (2026-09-24, action-seo-test 2026-01): the pinned
        # schema requires `input:` — the `collection:` arg seen in newer
        # docs is NOT accepted by the 2026-01 endpoint (argumentnotaccepted).
        write = client.run("collectionCreate", COLLECTION_CREATE_MUTATION,
                           {"input": variables})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_CREATE}
    if not write.get("ok"):
        return {"ok": False,
                "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_COLLECTION_CREATE}
    created = ((write.get("data") or {}).get("collectionCreate") or {})
    user_errors = created.get("userErrors") or []
    collection = created.get("collection") or {}
    if user_errors or not collection.get("id"):
        return {"ok": False, "outcome": "provider_error",
                "userErrors": user_errors,
                "errors": [],
                "detail": f"collectionCreate userErrors: {user_errors}",
                "adapter": ADAPTER_COLLECTION_CREATE}
    created_gid = collection["id"]

    return _collection_create_publish_only(
        conn, client, config, gid=created_gid, handle=handle,
        expected_title=variables["title"], existing=False,
        create_response=write)


def _collection_create_publish_only(conn, client, config, gid, handle,
                                    expected_title, existing,
                                    create_response=None):
    """Shared publish->verify tail of collection_create_execute: resolves
    the publication id (cache -> resolver), publishes, reads back.
    Idempotent: called with the EXISTING gid on self-heal runs, or the
    freshly created GID on first runs."""
    publication_id = _publication_id_from_config(conn, config)
    if not publication_id:
        from connectors.shopify import resolve_publication_id
        publication_id = resolve_publication_id(conn, client)
    if not publication_id:
        return {"ok": False, "outcome": "create_publish_failed",
                "detail": f"collection {'found' if existing else 'created'} "
                          f"({gid}) but no publication id resolved — "
                          "publish by hand or retry; NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": gid},
                "adapter_response": {"create": _safe_response(create_response)
                                     if create_response else {}}}
    try:
        publish = client.run(
            "publishablePublish", COLLECTION_PUBLISH_MUTATION,
            {"id": gid,
             "input": [{"publicationId": publication_id}],
             "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "create_publish_failed",
                "detail": f"collection {'found' if existing else 'created'} "
                          f"({gid}) but publish raised: {exc} — "
                          "NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": gid},
                "adapter_response": {"create": _safe_response(create_response)
                                     if create_response else {}}}
    if not publish.get("ok"):
        return {"ok": False, "outcome": "create_publish_failed",
                "userErrors": publish.get("userErrors") or [],
                "errors": publish.get("errors") or [],
                "detail": f"collection {'found' if existing else 'created'} "
                          f"({gid}) but publish failed — NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": gid},
                "adapter_response": {"create": _safe_response(create_response)
                                     if create_response else {},
                                     "publish": _safe_response(publish)}}

    verify = _collection_by_handle_read(client, handle)
    live = ((verify.get("data") or {}).get("collectionByHandle") or {})
    verified = (bool(verify.get("ok")) and live.get("id") == gid
                and (expected_title is None
                     or live.get("title") == expected_title))
    response = {"ok": True,
                "outcome": "ok",
                "verified": verified,
                "verification_status": "verified" if verified
                else "verify_failed",
                "live_value": live.get("title"),
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": gid},
                "adapter_response": {"publish": _safe_response(publish),
                                     "verify": _safe_response(verify)}}
    if create_response is not None:
        response["adapter_response"]["create"] = _safe_response(
            create_response)
    if existing:
        response["detail"] = (f"collection already existed ({gid}) — "
                              "skipped create, published existing entity "
                              "(self-heal)")
    return response


def collection_create_restore(conn, fix_row, config, client=None):
    """Rollback: STRICTLY publishableUnpublish of the created collection
    (plan/25 §3.5). NEVER collectionDelete — the operator may have
    enriched the entity between apply and revert; unpublish returns the
    storefront to the exact pre-fix public state (no public page)."""
    if client is None:
        try:
            client = _client_from_config(config)
        except Exception as exc:
            return {"ok": False, "outcome": "no_credentials",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "adapter": ADAPTER_COLLECTION_CREATE}
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    created_gid = (snapshot.get("collection.created_gid")
                   if isinstance(snapshot, dict) else None)
    if not created_gid:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "detail": "snapshot_json missing collection.created_gid — "
                          "refusing to guess what to unpublish"}
    publication_id = _publication_id_from_config(conn, config)
    if not publication_id:
        from connectors.shopify import resolve_publication_id
        publication_id = resolve_publication_id(conn, client)
    if not publication_id:
        return {"ok": False, "outcome": "no_publication_id",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "detail": "no publication id resolved — cannot unpublish"}
    try:
        write = client.run(
            "publishableUnpublish", COLLECTION_UNPUBLISH_MUTATION,
            {"id": created_gid,
             "input": [{"publicationId": publication_id}],
             "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "no_credentials", "detail": str(exc),
                "adapter": ADAPTER_COLLECTION_CREATE}
    if not write.get("ok"):
        return {"ok": False, "outcome": write.get("outcome", "provider_error"),
                "userErrors": write.get("userErrors") or [],
                "errors": write.get("errors") or [],
                "detail": write.get("detail"),
                "adapter": ADAPTER_COLLECTION_CREATE}
    verify = client.run("collection", PUBLISHABLE_READ_QUERY,
                        _publication_read_variables(created_gid,
                                                    publication_id))
    _, live_published = _extract_publishable_state(verify)
    restored = bool(verify.get("ok")) and live_published is False
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live_published,
            "adapter": ADAPTER_COLLECTION_CREATE,
            "adapter_response": {"unpublish": _safe_response(write),
                                 "verify": _safe_response(verify)}}


ADAPTERS.update({
    "collection_create": {
        "execute": collection_create_execute,
        "restore": collection_create_restore,
        "snapshot": collection_create_snapshot,
    },
})


# ------------------------------------------------------------
# framer.title / framer.description (plan/26 Phase 3) — the FIRST
# non-Shopify write path. Same safety bar as the Shopify adapters:
#   * snapshot() fresh-reads the page meta at executor pickup (the
#     executor compares live values against diff old_values — the
#     stale-diff guard) and doubles as the revert source;
#   * the meta surface is WHOLE-OBJECT (a write replaces both fields —
#     the same hazard class as productUpdate.seo, verified live on
#     Shopify 2026-09-23): every write carries BOTH fields, the value
#     being fixed plus the SIBLING from the fresh snapshot. A missing
#     sibling refuses (typed no_snapshot) — never a data-destroying
#     partial write;
#   * restore() writes back BOTH fields from the snapshot; None is a
#     legitimate pre-state (empty meta) and is preserved as None;
#   * payload validated BEFORE any network call; dry_run writes nothing;
#   * never raises — typed dicts everywhere.
# target_entity_ref = the canonical URL (generation contract, plan/26
# Phase 2); the client resolves the live page by that ref.
# ------------------------------------------------------------

ADAPTER_FRAMER_TITLE = "framer.title"
ADAPTER_FRAMER_DESCRIPTION = "framer.description"

FRAMER_SNAPSHOT_TITLE_KEY = "meta.title"
FRAMER_SNAPSHOT_DESCRIPTION_KEY = "meta.description"


def _framer_payload_for(payload_json, field):
    """Validate the framer payload BEFORE any network call.

    payload_json (from plan/26 Phase 2 generation):
      {mutation: 'framer.updatePageMeta',
       variables: {page_ref, field: 'title'|'description', <field>: value},
       surface: 'framer'}
    Strict allowlist: no drift fields ride the write.
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    if payload_json.get("mutation") != "framer.updatePageMeta":
        raise WriteAdapterError(
            f"unsupported mutation for framer {field} adapter: "
            f"{payload_json.get('mutation')!r}")
    if payload_json.get("surface") != "framer":
        raise WriteAdapterError(
            "framer payload must carry surface='framer'")
    variables = payload_json.get("variables") or {}
    page_ref = (variables.get("page_ref") or "").strip()
    if not page_ref:
        raise WriteAdapterError("framer payload must carry variables.page_ref")
    payload_field = variables.get("field")
    if payload_field != field:
        raise WriteAdapterError(
            f"framer {field} adapter refuses payload field {payload_field!r}")
    value = variables.get(field)
    if not isinstance(value, str):
        raise WriteAdapterError(
            f"framer payload must carry variables.{field} as a string")
    extra = set(variables.keys()) - {"page_ref", "field", field}
    if extra:
        raise WriteAdapterError(
            f"framer adapter writes ONLY page_ref+field+{field} "
            f"(drift fields: {sorted(extra)})")
    return page_ref, value


def _framer_sibling(fix_row, snapshot_key):
    """The sibling meta field from the fresh pickup snapshot (WHOLE-OBJECT
    write rule — mirror of _sibling_seo_field).

    Key-precision discipline: a MISSING snapshot key returns None (the
    caller refuses — no snapshot, no write); a PRESENT key with None/''
    returns '' (an empty meta pre-state is legitimate and preserved)."""
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict) or snapshot_key not in snapshot:
        return None
    value = snapshot.get(snapshot_key)
    return value if isinstance(value, str) else ""


def _framer_write_and_verify(client, page_ref, title, description,
                             adapter_name, dry_run=False):
    """Shared write -> read-back verify tail for both framer adapters.

    Returns the typed adapter result. Write carries BOTH fields
    (sibling discipline); verify re-READS and compares the written field.
    """
    if dry_run:
        return {"ok": True, "outcome": "dry_run", "adapter": adapter_name,
                "detail": {"mutation": "framer.updatePageMeta",
                           "page_ref": page_ref,
                           "title": title, "description": description}}
    write = client.update_page_meta(page_ref, title=title,
                                    description=description)
    if not write.get("ok"):
        return {"ok": False,
                "outcome": write.get("outcome", "provider_error"),
                "errors": [],
                "detail": write.get("detail"), "adapter": adapter_name}
    verify = client.read_page_meta(page_ref)
    live = (verify.get("data") or {}) if verify.get("ok") else {}
    return {
        "ok": True,
        "outcome": "ok",
        "verified": False,
        "live_value": None,
        "adapter": adapter_name,
        "_verify_state": {"live": live, "title": title,
                          "description": description},
    }


def _framer_verify_result(verify_state, field, adapter_name):
    """Compare the fresh read to the written state -> verification_status."""
    live = verify_state.get("live") or {}
    live_title = live.get("title")
    live_description = live.get("description")
    if field == "title":
        verified = (live_title == verify_state["title"]
                    and live_description == verify_state["description"])
        live_value = live_title
    else:
        live_description = live.get("description")
        verified = (live_description == verify_state["description"]
                    and live_title == verify_state["title"])
        live_value = live_description
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live_value,
        "adapter": adapter_name,
    }


def _framer_title_variables(fix_row):
    page_ref, new_title = _framer_payload_for(
        fix_row["payload_json"], "title")
    return page_ref, new_title


def _framer_description_variables(fix_row):
    page_ref, new_description = _framer_payload_for(
        fix_row["payload_json"], "description")
    return page_ref, new_description


def framer_title_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: BOTH meta fields (whole-object
    surface — the snapshot is the sibling source AND the revert source)."""
    client = _client_from_framer_config(config, client)
    try:
        page_ref, _new_title = _framer_payload_for(
            fix_row["payload_json"], "title")
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_TITLE}
    try:
        read = client.read_page_meta(page_ref)
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_TITLE}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": ADAPTER_FRAMER_TITLE}
    data = read.get("data") or {}
    return {"ok": True, "outcome": "ok",
            "snapshot": {
                FRAMER_SNAPSHOT_TITLE_KEY: data.get("title"),
                FRAMER_SNAPSHOT_DESCRIPTION_KEY: data.get("description"),
                "page_ref": page_ref,
            },
            "adapter": ADAPTER_FRAMER_TITLE}


def framer_title_execute(conn, fix_row, config, dry_run=False, client=None):
    """Apply one framer.title fix: whole-object write -> read-back verify."""
    client = _client_from_framer_config(config, client)
    try:
        page_ref, new_title = _framer_payload_for(
            fix_row["payload_json"], "title")
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_TITLE}

    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_FRAMER_TITLE,
                "detail": {"mutation": "framer.updatePageMeta",
                           "page_ref": page_ref, "field": "title"}}

    # Whole-object rule: carry the sibling description from the fresh
    # pickup snapshot (None pre-state = empty meta, preserved as None).
    sibling = _framer_sibling(fix_row, FRAMER_SNAPSHOT_DESCRIPTION_KEY)
    if sibling is None:
        return {"ok": False, "outcome": "no_snapshot",
                "detail": "framer.title write needs the pickup snapshot's "
                          "meta.description (Framer meta write replaces "
                          "the whole object) — refusing a data-destroying "
                          "write",
                "adapter": ADAPTER_FRAMER_TITLE}
    result = _framer_write_and_verify(
        client, page_ref, new_title, sibling, ADAPTER_FRAMER_TITLE,
        dry_run=dry_run)
    if not result.get("ok") or result.get("outcome") == "dry_run":
        return result
    state = result.pop("_verify_state")
    out = _framer_verify_result(state, "title", ADAPTER_FRAMER_TITLE)
    out["adapter_response"] = {"write": {"ok": True, "outcome": "ok"}}
    return out


def framer_title_restore(conn, fix_row, config, client=None):
    """Rollback: write back BOTH meta fields from the pickup snapshot.

    A snapshot missing meta.title refuses to guess (typed no_snapshot);
    None pre-states are preserved as None (empty meta was the real state).
    """
    client = _client_from_framer_config(config, client)
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    page_ref = snapshot.get("page_ref") or fix_row.get("target_entity_ref")
    if FRAMER_SNAPSHOT_TITLE_KEY not in snapshot:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_FRAMER_TITLE,
                "detail": "snapshot_json missing meta.title — refusing "
                          "to guess a restore value"}
    # None pre-state (field was empty) is legitimate and preserved as None.
    old_title = snapshot.get(FRAMER_SNAPSHOT_TITLE_KEY)
    old_description = snapshot.get(FRAMER_SNAPSHOT_DESCRIPTION_KEY)
    try:
        write = client.update_page_meta(page_ref, title=old_title,
                                        description=old_description)
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_TITLE}
    if not write.get("ok"):
        return {"ok": False,
                "outcome": write.get("outcome", "provider_error"),
                "errors": [], "detail": write.get("detail"),
                "adapter": ADAPTER_FRAMER_TITLE}
    try:
        verify = client.read_page_meta(page_ref)
    except Exception as exc:
        verify = {"ok": False, "detail": str(exc)}
    live = (verify.get("data") or {}) if verify.get("ok") else {}
    restored = (live.get("title") == old_title
                and live.get("description") == old_description)
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live.get("title"),
            "adapter": ADAPTER_FRAMER_TITLE,
            "adapter_response": {"write": {"ok": True, "outcome": "ok"}}}


def framer_description_snapshot(conn, fix_row, config, client=None):
    """FRESH READ pre-state at pickup: BOTH meta fields (whole-object)."""
    client = _client_from_framer_config(config, client)
    try:
        page_ref, _new = _framer_payload_for(
            fix_row["payload_json"], "description")
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_DESCRIPTION}
    try:
        read = client.read_page_meta(page_ref)
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_DESCRIPTION}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"),
                "adapter": ADAPTER_FRAMER_DESCRIPTION}
    data = read.get("data") or {}
    return {"ok": True, "outcome": "ok",
            "snapshot": {
                FRAMER_SNAPSHOT_TITLE_KEY: data.get("title"),
                FRAMER_SNAPSHOT_DESCRIPTION_KEY: data.get("description"),
                "page_ref": page_ref,
            },
            "adapter": ADAPTER_FRAMER_DESCRIPTION}


def framer_description_execute(conn, fix_row, config, dry_run=False,
                               client=None):
    """Apply one framer.description fix: whole-object write -> verify."""
    client = _client_from_framer_config(config, client)
    try:
        page_ref, new_description = _framer_payload_for(
            fix_row["payload_json"], "description")
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_DESCRIPTION}

    if dry_run:
        return {"ok": True, "outcome": "dry_run",
                "adapter": ADAPTER_FRAMER_DESCRIPTION,
                "detail": {"mutation": "framer.updatePageMeta",
                           "page_ref": page_ref, "field": "description"}}

    sibling = _framer_sibling(fix_row, FRAMER_SNAPSHOT_TITLE_KEY)
    if sibling is None:
        return {"ok": False, "outcome": "no_snapshot",
                "detail": "framer.description write needs the pickup "
                          "snapshot's meta.title (Framer meta write "
                          "replaces the whole object) — refusing a "
                          "data-destroying write",
                "adapter": ADAPTER_FRAMER_DESCRIPTION}
    result = _framer_write_and_verify(
        client, page_ref, sibling, new_description,
        ADAPTER_FRAMER_DESCRIPTION, dry_run=dry_run)
    if not result.get("ok") or result.get("outcome") == "dry_run":
        return result
    state = result.pop("_verify_state")
    out = _framer_verify_result(state, "description",
                                ADAPTER_FRAMER_DESCRIPTION)
    out["adapter_response"] = {"write": {"ok": True, "outcome": "ok"}}
    return out


def framer_description_restore(conn, fix_row, config, client=None):
    """Rollback: write back BOTH meta fields from the pickup snapshot."""
    client = _client_from_framer_config(config, client)
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    page_ref = snapshot.get("page_ref") or fix_row.get("target_entity_ref")
    if FRAMER_SNAPSHOT_DESCRIPTION_KEY not in snapshot:
        return {"ok": False, "outcome": "no_snapshot",
                "adapter": ADAPTER_FRAMER_DESCRIPTION,
                "detail": "snapshot_json missing meta.description — "
                          "refusing to guess a restore value"}
    old_title = snapshot.get(FRAMER_SNAPSHOT_TITLE_KEY)
    old_description = snapshot.get(FRAMER_SNAPSHOT_DESCRIPTION_KEY)
    try:
        write = client.update_page_meta(page_ref, title=old_title,
                                        description=old_description)
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": ADAPTER_FRAMER_DESCRIPTION}
    if not write.get("ok"):
        return {"ok": False,
                "outcome": write.get("outcome", "provider_error"),
                "errors": [], "detail": write.get("detail"),
                "adapter": ADAPTER_FRAMER_DESCRIPTION}
    try:
        verify = client.read_page_meta(page_ref)
    except Exception as exc:
        verify = {"ok": False, "detail": str(exc)}
    live = (verify.get("data") or {}) if verify.get("ok") else {}
    restored = (live.get("title") == old_title
                and live.get("description") == old_description)
    return {"ok": True, "outcome": "ok", "restored": restored,
            "live_value": live.get("description"),
            "adapter": ADAPTER_FRAMER_DESCRIPTION,
            "adapter_response": {"write": {"ok": True, "outcome": "ok"}}}


ADAPTERS.update({
    "framer.title": {
        "execute": framer_title_execute,
        "restore": framer_title_restore,
        "snapshot": framer_title_snapshot,
    },
    "framer.description": {
        "execute": framer_description_execute,
        "restore": framer_description_restore,
        "snapshot": framer_description_snapshot,
    },
})


# ------------------------------------------------------------
# netlify.* (plan/27) — the FIRST Git-PR write path. Static-hosted sites
# (Netlify/GitHub Pages/any git-backed host) never take live API
# overwrites: every fix is delivered as ONE pull request against the
# site's content repo, titled 'fix(seo): [action] for [page-path]'. A
# human merges; the platform's verification is the committed diff.
#
# Same safety bar as the Shopify/Framer adapters:
#   * snapshot() fresh-reads the file from the repo's base branch at
#     executor pickup (the stale-diff guard's live-value source AND the
#     revert's old_content source);
#   * execute() opens a branch off the base branch, commits the exact
#     file diff, opens the PR, and records the PR coordinates into
#     snapshot_patch (the rollback source — reverts CLOSE the PR, never
#     a force-push);
#   * restore() closes the recorded PR (typed no_pr refusal when the PR
#     number was never recorded — never a guessed rollback);
#   * payload validated BEFORE any network call; dry_run writes nothing;
#   * never raises — typed dicts everywhere.
# target_entity_ref = the repo-relative file path (generation contract,
# plan/27); the client resolves the file by that path at snapshot time.
# ------------------------------------------------------------

ADAPTER_NETLIFY_TITLE_META = "netlify.title_meta_rewrite"
ADAPTER_NETLIFY_STRUCTURED_DATA = "netlify.structured_data"
ADAPTER_NETLIFY_NOINDEX = "netlify.noindex"
ADAPTER_NETLIFY_CANONICAL = "netlify.canonical_tag"

NETLIFY_SNAPSHOT_PATH_KEY = "file.path"
NETLIFY_SNAPSHOT_URL_KEY = "page.url"

ACTION_LABELS = {
    ADAPTER_NETLIFY_TITLE_META: "title-meta-rewrite",
    ADAPTER_NETLIFY_STRUCTURED_DATA: "structured-data",
    ADAPTER_NETLIFY_NOINDEX: "noindex-tag",
    ADAPTER_NETLIFY_CANONICAL: "canonical-tag",
}


def _client_from_netlify_config(config, client=None):
    """Build the GitHubPRClient from a plan/27 credential dict.

    client: TEST seam (injected fake/mock client) — same pattern as the
    framer adapters' client seam. When omitted, the real client is built
    from the resolved config (mode resolved by the connector)."""
    if client is not None:
        return client
    from connectors.github_static import GitHubPRClient
    return GitHubPRClient(
        github_token=config.get("github_token"),
        github_repo=config.get("github_repo"),
        github_branch=config.get("github_branch"),
        github_root_dir=config.get("github_root_dir"),
        api_base=config.get("api_base"),
    )


def _netlify_payload_for(payload_json, sub_type):
    """Validate the netlify payload BEFORE any network call.

    payload_json (from plan/27 generation):
      {mutation: 'netlify.gitPr',
       variables: {page_url, file_path, action: 'title_meta_rewrite'|
                   'structured_data_inject'|'noindex_tag',
                   title?, description?, jsonld?, noindex?},
       surface: 'netlify'}
    Strict allowlist per action: no drift fields ride the write.
    """
    if not isinstance(payload_json, dict):
        raise WriteAdapterError("payload_json is not an object")
    if payload_json.get("mutation") != "netlify.gitPr":
        raise WriteAdapterError(
            f"unsupported mutation for netlify adapter: "
            f"{payload_json.get('mutation')!r}")
    if payload_json.get("surface") != "netlify":
        raise WriteAdapterError("netlify payload must carry surface='netlify'")
    variables = payload_json.get("variables") or {}
    page_url = (variables.get("page_url") or "").strip()
    file_path = (variables.get("file_path") or "").strip()
    if not page_url or not file_path:
        raise WriteAdapterError(
            "netlify payload must carry variables.page_url + file_path")
    action = (variables.get("action") or "").strip()
    expected = {
        ADAPTER_NETLIFY_TITLE_META: "title_meta_rewrite",
        ADAPTER_NETLIFY_STRUCTURED_DATA: "structured_data_inject",
        ADAPTER_NETLIFY_NOINDEX: "noindex_tag",
        ADAPTER_NETLIFY_CANONICAL: "canonical_tag",
    }.get(sub_type)
    if action != expected:
        raise WriteAdapterError(
            f"{sub_type} adapter refuses payload action {action!r} "
            f"(expected {expected!r})")
    # per-action field allowlist
    allowed = {"page_url", "file_path", "action"}
    if action == "canonical_tag":
        # plan/28: the canonical href = the page's own canonical URL —
        # always a string (generation mints it from page_url)
        allowed |= {"canonical_url"}
        if not isinstance(variables.get("canonical_url"), str) or \
                not variables["canonical_url"].startswith("https://"):
            raise WriteAdapterError(
                "netlify canonical_tag payload must carry an absolute "
                "https variables.canonical_url")
    elif action == "title_meta_rewrite":
        allowed |= {"title", "description"}
        # title OR description may be a None PLACEHOLDER (generation
        # contract, plan/27): the executor fills the placeholder from the
        # pickup snapshot (sibling discipline — the HTML pair is ONE edit
        # unit). A payload with BOTH None still refuses (nothing to write).
        title_val = variables.get("title")
        desc_val = variables.get("description")
        if not (isinstance(title_val, str) or isinstance(desc_val, str)):
            raise WriteAdapterError(
                "netlify title_meta_rewrite payload must carry a string "
                "variables.title or variables.description (the other may "
                "be a None placeholder the snapshot fills)")
        if title_val is not None and not isinstance(title_val, str):
            raise WriteAdapterError(
                "netlify title_meta_rewrite variables.title must be a "
                "string or a None placeholder")
        if desc_val is not None and not isinstance(desc_val, str):
            raise WriteAdapterError(
                "netlify title_meta_rewrite variables.description must be "
                "a string or a None placeholder")
    elif action == "structured_data_inject":
        allowed |= {"jsonld"}
        if not isinstance(variables.get("jsonld"), str):
            raise WriteAdapterError(
                "netlify structured_data_inject payload must carry a "
                "string variables.jsonld (serialized JSON-LD)")
        try:
            parsed = json.loads(variables["jsonld"])
        except (TypeError, ValueError):
            raise WriteAdapterError(
                "netlify structured_data_inject variables.jsonld is not "
                "valid JSON")
        if not isinstance(parsed, (dict, list)):
            raise WriteAdapterError(
                "netlify structured_data_inject jsonld must be a JSON "
                "object or array")
    elif action == "noindex_tag":
        if "noindex" in variables and variables["noindex"] is not True:
            raise WriteAdapterError(
                "netlify noindex_tag variables.noindex must be true")
    extra = set(variables.keys()) - allowed
    if extra:
        raise WriteAdapterError(
            f"netlify adapter writes ONLY {sorted(allowed)} "
            f"(drift fields: {sorted(extra)})")
    return variables


def _jsonld_script(value):
    """One serialized JSON-LD script block (validated at payload level)."""
    return f'<script type="application/ld+json">\n{value}\n</script>'


def apply_netlify_html_edit(content, action, variables):
    """Pure HTML-text transformer: (content, action, variables) ->
    (new_content, diff_summary) or (None, reason).

    Rules (plan/27 §2 + plan/28, deterministic, no regex-over-the-void):
      * title_meta_rewrite — replace/insert <title> and
        <meta name="description"> inside <head>; an absent <head> refuses
        (never a guessed insertion point);
      * structured_data_inject — insert the JSON-LD script before
        </head>; refuse when an identical @type+name block already exists
        (idempotency guard);
      * canonical_tag — inject a self-referencing <link rel="canonical">
        before </head>; refuses when the page ALREADY carries a canonical
        (never a second canonical — no-op loop guard); the href is the
        payload's page_url (verified absolute https);
      * noindex_tag — insert/replace <meta name="robots"> with
        content containing noindex; refuses when a robots meta ALREADY
        says noindex (no-op loop guard).
    """
    if content is None:
        return None, "file_not_found"
    text = content if isinstance(content, str) else content.decode("utf-8")
    head_lower = text.lower()
    if "<head" not in head_lower or "</head>" not in head_lower:
        return None, "no_head_section"
    head_start = head_lower.index("<head")
    head_end = head_lower.index("</head>")

    if action == "title_meta_rewrite":
        new = text
        changed = []
        title_value = variables.get("title")
        import re as _re
        title_match = _re.search(r"<title[^>]*>(.*?)</title>", text[head_start:head_end],
                                 _re.IGNORECASE | _re.DOTALL)
        if title_match:
            span_start = head_start + title_match.start(1)
            span_end = head_start + title_match.end(1)
            new = new[:span_start] + title_value + new[span_end:]
            changed.append("title")
            head_end = head_end + (len(title_value)
                                   - (title_match.end(1) - title_match.start(1)))
        else:
            insert_at = new.lower().index("</head>")
            new = new[:insert_at] + f"    <title>{title_value}</title>\n" \
                  + new[insert_at:]
            changed.append("title")
        desc_value = variables.get("description")
        import re as _re2
        head_lower2 = new.lower()
        h2s = head_lower2.index("<head")
        h2e = head_lower2.index("</head>")
        desc_match = _re2.search(
            r'<meta\s+name=["\']description["\'][^>]*>',
            new[h2s:h2e], _re2.IGNORECASE)
        if desc_match:
            tag = desc_match.group(0)
            if desc_value and desc_value in tag:
                # plan/27 no-op guard: the new description EQUALS the live
                # value (e.g. the title fix carries an unchanged sibling) —
                # skip the desc edit and keep the title change; refusing
                # the whole transform here would kill valid title fixes.
                pass
            else:
                new_tag = _re2.sub(r'content=["\'].*?["\']',
                                   lambda m: f'content="{desc_value}"'
                                   if not m.group(0).startswith("content='")
                                   else f"content='{desc_value}'", tag,
                                   flags=_re2.IGNORECASE | _re2.DOTALL)
                if new_tag == tag:
                    return None, "meta_description_transform_failed"
                span = (h2s + desc_match.start(), h2s + desc_match.end())
                new = new[:span[0]] + new_tag + new[span[1]:]
                changed.append("meta_description")
        else:
            insert_at = h2e
            new = new[:insert_at] + \
                f'    <meta name="description" content="{desc_value}">\n' \
                + new[insert_at:]
            changed.append("meta_description")
        if not changed:
            return None, "no_changes"
        return new, {"fields": changed}

    if action == "structured_data_inject":
        script = _jsonld_script(variables.get("jsonld"))
        if script in text:
            return None, "already_present"
        # idempotency guard: an existing JSON-LD block with the same @type
        # + name is treated as already-present
        import re as _re3
        try:
            new_obj = json.loads(variables["jsonld"])
        except (TypeError, ValueError):
            return None, "jsonld_invalid"
        for match in _re3.finditer(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>'
                r'(.*?)</script>', text, _re3.IGNORECASE | _re3.DOTALL):
            try:
                existing = json.loads(match.group(1))
            except (TypeError, ValueError):
                continue
            if (isinstance(existing, dict) and isinstance(new_obj, dict)
                    and existing.get("@type") == new_obj.get("@type")
                    and existing.get("name") == new_obj.get("name")):
                return None, "already_present"
        insert_at = text.lower().index("</head>")
        return text[:insert_at] + f"    {script}\n" + text[insert_at:], \
            {"fields": ["jsonld"]}

    if action == "noindex_tag":
        import re as _re4
        h2s = head_lower.index("<head")
        h2e = head_lower.index("</head>")
        robots_match = _re4.search(
            r'<meta\s+name=["\']robots["\'][^>]*>',
            text[h2s:h2e], _re4.IGNORECASE)
        if robots_match:
            tag = robots_match.group(0)
            if "noindex" in tag.lower():
                return None, "already_noindex"
            new_tag = _re4.sub(r'content=["\'].*?["\']',
                               'content="noindex, follow"', tag,
                               flags=_re4.IGNORECASE | _re4.DOTALL)
            span = (h2s + robots_match.start(), h2s + robots_match.end())
            return text[:span[0]] + new_tag + text[span[1]:], \
                {"fields": ["robots_meta"]}
        insert_at = h2e
        return text[:insert_at] + \
            '    <meta name="robots" content="noindex, follow">\n' \
            + text[insert_at:], {"fields": ["robots_meta"]}

    if action == "canonical_tag":
        import re as _re5
        h2s = head_lower.index("<head")
        h2e = head_lower.index("</head>")
        # no-op loop guard: a page with ANY canonical must never get a
        # second one (a duplicate canonical is worse than none)
        existing_match = _re5.search(
            r'<link[^>]*rel=["\']canonical["\'][^>]*>',
            text[h2s:h2e], _re5.IGNORECASE)
        if existing_match:
            return None, "already_has_canonical"
        href = (variables.get("canonical_url") or "").strip()
        if not href.startswith("https://"):
            return None, "canonical_url_not_https"
        insert_at = h2e
        return text[:insert_at] + \
            f'    <link rel="canonical" href="{href}">\n' \
            + text[insert_at:], {"fields": ["canonical_link"]}

    return None, f"unknown action {action!r}"


def _pr_branch_name(fix_row):
    fix_id = str(fix_row.get("fix_id") or "unknown")
    return f"openseo/fix-{fix_id[:8]}"


def _pr_title(fix_row, sub_type):
    from connectors.github_static import PR_TITLE_FORMAT
    action_label = ACTION_LABELS.get(
        sub_type, (fix_row.get("sub_type") or "fix").replace(".", "-"))
    page_label = (fix_row.get("target_url") or "").split("://", 1)[-1] \
        .rstrip("/")
    return PR_TITLE_FORMAT.format(action_label=action_label,
                                  page_label=page_label)


def _pr_body(fix_row):
    from connectors.github_static import (PR_BODY_RECOMMENDATION_LINE,
                                          PR_BODY_FIX_LINE)
    rec_id = str(fix_row.get("recommendation_id") or "")
    fix_id = str(fix_row.get("fix_id") or "")
    return (
        "Automated SEO fix generated by OpenSEO.\n\n"
        f"{PR_BODY_RECOMMENDATION_LINE.format(recommendation_id=rec_id)}\n"
        f"{PR_BODY_FIX_LINE.format(fix_id=fix_id)}\n\n"
        "Revert path: close this PR (the platform's /fixes/{id}/revert "
        "does exactly that; rollback links back to the recommendation "
        "in Supabase)."
    )


def _netlify_changes_for(fix_row, sub_type, new_content, old_content):
    variables = (fix_row.get("payload_json") or {}).get("variables") or {}
    return [{
        "path": variables.get("file_path"),
        "old_content": old_content,
        "new_content": new_content,
    }]


def _netlify_common_snapshot(conn, fix_row, config, client, sub_type):
    """Shared fresh read: the CURRENT file content from the base branch.

    The snapshot stores the full file (the revert source) + the parsed
    live values the stale-diff guard reads (head/meta.title,
    meta/meta.description).
    """
    try:
        payload_vars = _netlify_payload_for(fix_row.get("payload_json"),
                                            sub_type)
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": sub_type}
    try:
        read = client.read_file(payload_vars["file_path"])
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": sub_type}
    if not read.get("ok"):
        return {"ok": False, "outcome": read.get("outcome", "provider_error"),
                "detail": read.get("detail"), "adapter": sub_type}
    content = (read.get("data") or {}).get("content")
    live = _parse_live_meta(content)
    snapshot = {
        NETLIFY_SNAPSHOT_PATH_KEY: payload_vars["file_path"],
        NETLIFY_SNAPSHOT_URL_KEY: payload_vars["page_url"],
        "file.content": content,
        "meta.title": live.get("title"),
        "meta.description": live.get("description"),
        "meta.robots": live.get("robots"),
    }
    return {"ok": True, "outcome": "ok", "snapshot": snapshot,
            "adapter": sub_type}


def _parse_live_meta(content):
    """(title, description, robots) from raw HTML; None-tolerant."""
    out = {"title": None, "description": None, "robots": None}
    if not isinstance(content, str):
        return out
    import re
    m = re.search(r"<title[^>]*>(.*?)</title>", content,
                  re.IGNORECASE | re.DOTALL)
    if m:
        out["title"] = m.group(1).strip()
    m = re.search(r'<meta\s+name=["\']description["\'][^>]*>', content,
                  re.IGNORECASE)
    if m:
        cm = re.search(r'content=["\'](.*?)["\']', m.group(0),
                       re.IGNORECASE | re.DOTALL)
        if cm:
            out["description"] = cm.group(1)
    m = re.search(r'<meta\s+name=["\']robots["\'][^>]*>', content,
                  re.IGNORECASE)
    if m:
        cm = re.search(r'content=["\'](.*?)["\']', m.group(0),
                       re.IGNORECASE)
        if cm:
            out["robots"] = cm.group(1)
    return out


def _netlify_common_execute(conn, fix_row, config, dry_run, client,
                            sub_type):
    """Shared execute: transform -> open ONE PR -> record PR coordinates."""
    client = _client_from_netlify_config(config, client)
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict) or \
            NETLIFY_SNAPSHOT_PATH_KEY not in snapshot or \
            "file.content" not in snapshot:
        return {"ok": False, "outcome": "no_snapshot",
                "detail": "netlify execute needs the pickup snapshot's "
                          "file.content + file.path — refusing to write "
                          "from a guessed base state",
                "adapter": sub_type}
    try:
        variables = _netlify_payload_for(fix_row.get("payload_json"),
                                         sub_type)
    except WriteAdapterError as exc:
        return {"ok": False, "outcome": "payload_invalid",
                "detail": str(exc), "adapter": sub_type}

    # plan/27 sibling discipline: a None placeholder field is filled from
    # the fresh pickup snapshot (the live HTML pair is ONE edit unit — the
    # placeholder must never ride a write as None, which would blank the
    # sibling field). A snapshot missing the sibling key refuses (typed).
    if variables.get("action") == "title_meta_rewrite":
        live_meta = _parse_live_meta(snapshot.get("file.content"))
        if variables.get("title") is None:
            filled = live_meta.get("title")
            if not isinstance(filled, str):
                return {"ok": False, "outcome": "no_snapshot",
                        "detail": "payload carries a None title placeholder "
                                  "but the pickup snapshot has no live "
                                  "<title> — refusing a guessed write",
                        "adapter": sub_type}
            variables["title"] = filled
        if variables.get("description") is None:
            filled = live_meta.get("description")
            if not isinstance(filled, str):
                return {"ok": False, "outcome": "no_snapshot",
                        "detail": "payload carries a None description "
                                  "placeholder but the pickup snapshot has "
                                  "no live meta description — refusing a "
                                  "blanking write",
                        "adapter": sub_type}
            variables["description"] = filled

    old_content = snapshot.get("file.content")
    action = variables["action"]
    new_content, summary = apply_netlify_html_edit(old_content, action,
                                                   variables)
    if new_content is None:
        return {"ok": False, "outcome": "transform_refused",
                "detail": f"HTML transform refused: {summary}",
                "adapter": sub_type}
    if new_content == old_content:
        return {"ok": False, "outcome": "no_change",
                "detail": "transform produced an identical file — refusing "
                          "an empty PR",
                "adapter": sub_type}

    if dry_run:
        return {"ok": True, "outcome": "dry_run", "adapter": sub_type,
                "detail": {"mutation": "netlify.gitPr",
                           "file_path": variables["file_path"],
                           "action": action,
                           "diff_summary": summary,
                           "pr_title": _pr_title(fix_row, sub_type)}}

    head = _pr_branch_name(fix_row)
    pr = client.open_pr(
        _pr_title(fix_row, sub_type), _pr_body(fix_row), head,
        config.get("github_branch") or "main",
        _netlify_changes_for(fix_row, sub_type, new_content, old_content))
    if not pr.get("ok"):
        return {"ok": False, "outcome": pr.get("outcome", "provider_error"),
                "errors": [], "detail": pr.get("detail"), "adapter": sub_type}
    data = pr.get("data") or {}
    out = {
        "ok": True, "outcome": "ok",
        "verified": True,  # the PR diff IS the verification (plan/27 §3)
        "verification_status": "verified",
        "live_value": data.get("pr_url"),
        "adapter": sub_type,
        "adapter_response": {
            "pr_number": data.get("pr_number"),
            "pr_url": data.get("pr_url"),
            "branch": data.get("branch"),
            "files": data.get("files"),
            "diff_summary": summary,
        },
        # the rollback source: reverts CLOSE this PR; pr.repo feeds the
        # merge-sync endpoint (polls GitHub for the merged state)
        "snapshot_patch": {
            "pr.number": data.get("pr_number"),
            "pr.url": data.get("pr_url"),
            "pr.branch": data.get("branch"),
            "pr.repo": config.get("github_repo"),
        },
    }
    return out


def _netlify_common_restore(conn, fix_row, config, client, sub_type):
    """Rollback = CLOSE the recorded PR (never a force-push, never a
    guessed branch). A snapshot without pr.number refuses (typed)."""
    client = _client_from_netlify_config(config, client)
    snapshot = fix_row.get("snapshot_json") or {}
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (TypeError, ValueError):
            snapshot = {}
    if not isinstance(snapshot, dict) or snapshot.get("pr.number") is None:
        return {"ok": False, "outcome": "no_pr_recorded",
                "detail": "snapshot_json carries no pr.number — refusing "
                          "to guess a rollback target (close the PR "
                          "manually or re-derive from change_log)",
                "adapter": sub_type}
    try:
        result = client.close_pr(snapshot["pr.number"])
    except Exception as exc:
        return {"ok": False, "outcome": "provider_error",
                "detail": str(exc), "adapter": sub_type}
    if not result.get("ok"):
        return {"ok": False, "outcome": result.get("outcome", "provider_error"),
                "detail": result.get("detail"), "adapter": sub_type}
    return {"ok": True, "outcome": "ok", "restored": True,
            "live_value": None, "adapter": sub_type,
            "adapter_response": {"closed_pr": snapshot.get("pr.number")}}


def _netlify_adapter(sub_type):
    def snapshot(conn, fix_row, config, client=None):
        client = _client_from_netlify_config(config, client)
        return _netlify_common_snapshot(conn, fix_row, config, client,
                                        sub_type)

    def execute(conn, fix_row, config, dry_run=False, client=None):
        return _netlify_common_execute(conn, fix_row, config, dry_run,
                                       client, sub_type)

    def restore(conn, fix_row, config, client=None):
        return _netlify_common_restore(conn, fix_row, config, client,
                                       sub_type)

    return {"execute": execute, "restore": restore, "snapshot": snapshot}


def netlify_title_meta_rewrite_snapshot(conn, fix_row, config, client=None):
    """Named export (tests/console): fresh repo read for the title/meta PR."""
    return _netlify_common_snapshot(conn, fix_row, config,
                                    _client_from_netlify_config(config,
                                                                client),
                                    ADAPTER_NETLIFY_TITLE_META)


def netlify_title_meta_rewrite_execute(conn, fix_row, config, dry_run=False,
                                       client=None):
    """Named export: transform -> ONE PR -> verify (plan/27 §3)."""
    return _netlify_common_execute(conn, fix_row, config, dry_run,
                                   _client_from_netlify_config(config,
                                                               client),
                                   ADAPTER_NETLIFY_TITLE_META)


def netlify_title_meta_rewrite_restore(conn, fix_row, config, client=None):
    """Named export: rollback = close the recorded PR."""
    return _netlify_common_restore(conn, fix_row, config,
                                   _client_from_netlify_config(config,
                                                               client),
                                   ADAPTER_NETLIFY_TITLE_META)


ADAPTERS.update({
    ADAPTER_NETLIFY_TITLE_META: _netlify_adapter(ADAPTER_NETLIFY_TITLE_META),
    ADAPTER_NETLIFY_STRUCTURED_DATA: _netlify_adapter(
        ADAPTER_NETLIFY_STRUCTURED_DATA),
    ADAPTER_NETLIFY_NOINDEX: _netlify_adapter(ADAPTER_NETLIFY_NOINDEX),
    ADAPTER_NETLIFY_CANONICAL: _netlify_adapter(ADAPTER_NETLIFY_CANONICAL),
})
