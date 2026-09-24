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

COLLECTION_PUBLISH_MUTATION = """
mutation FixCollectionPublish($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) {
    publishable { id publishedOnPublication(publicationId: $publicationId) }
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

    Tolerates a null member on the multi-entity read (a collection miss
    with a product hit — or vice versa — returns the present entity's
    state; both null means the read failed outright).
    """
    data = read_result.get("data") or {}
    collection = data.get("collection") or {}
    product = data.get("product") or {}
    if collection.get("id"):
        return None, collection.get("publishedOnPublication")
    if product.get("id"):
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

    # Publish step (cached publication id — same path as collection_publish).
    publication_id = _publication_id_from_config(conn, config)
    if not publication_id:
        return {"ok": False, "outcome": "create_publish_failed",
                "detail": f"collection created ({created_gid}) but no "
                          "publication id cached — publish by hand or "
                          "retry; NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": created_gid},
                "adapter_response": {"create": _safe_response(write)}}
    try:
        publish = client.run(
            "publishablePublish", COLLECTION_PUBLISH_MUTATION,
            {"id": created_gid,
             "input": [{"publicationId": publication_id}],
             "publicationId": publication_id})
    except Exception as exc:
        return {"ok": False, "outcome": "create_publish_failed",
                "detail": f"collection created ({created_gid}) but "
                          f"publish raised: {exc} — NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": created_gid},
                "adapter_response": {"create": _safe_response(write)}}
    if not publish.get("ok"):
        return {"ok": False, "outcome": "create_publish_failed",
                "userErrors": publish.get("userErrors") or [],
                "errors": publish.get("errors") or [],
                "detail": f"collection created ({created_gid}) but "
                          "publish failed — NOT auto-undone",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "snapshot_patch": {"collection.created_gid": created_gid},
                "adapter_response": {"create": _safe_response(write),
                                     "publish": _safe_response(publish)}}

    verify = _collection_by_handle_read(client, handle)
    live = ((verify.get("data") or {}).get("collectionByHandle") or {})
    verified = (bool(verify.get("ok")) and live.get("id") == created_gid
                and live.get("title") == variables["title"])
    return {
        "ok": True,
        "outcome": "ok",
        "verified": verified,
        "verification_status": "verified" if verified else "verify_failed",
        "live_value": live.get("title"),
        "adapter": ADAPTER_COLLECTION_CREATE,
        "snapshot_patch": {"collection.created_gid": created_gid},
        "adapter_response": {"create": _safe_response(write),
                             "publish": _safe_response(publish),
                             "verify": _safe_response(verify)},
    }


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
        return {"ok": False, "outcome": "no_publication_id",
                "adapter": ADAPTER_COLLECTION_CREATE,
                "detail": "no cached publication id — cannot unpublish"}
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
