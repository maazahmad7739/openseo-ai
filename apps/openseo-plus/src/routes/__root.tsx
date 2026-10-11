import { useEffect, useState } from "react";
import { createRootRoute, Outlet } from "@tanstack/react-router";
import {
  API_TOKEN_STORAGE_KEY,
  UNAUTHORIZED_EVENT,
} from "../features/operator/data/useOperatorData";

function AuthGate() {
  const [show, setShow] = useState(false);
  const [token, setToken] = useState("");

  useEffect(() => {
    const onUnauthorized = () => setShow(true);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (!show) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <form
        className="w-80 rounded-box bg-base-100 p-6 shadow-xl"
        onSubmit={(e) => {
          e.preventDefault();
          const value = token.trim();
          if (!value) return;
          localStorage.setItem(API_TOKEN_STORAGE_KEY, value);
          location.reload();
        }}
      >
        <h2 className="mb-1 text-base font-semibold">Session expired</h2>
        <p className="mb-4 text-xs text-base-content/60">
          Your API token was rejected (401). Paste a fresh token — no rebuild
          needed.
        </p>
        <input
          autoFocus
          type="password"
          className="input input-bordered mb-3 w-full text-sm"
          placeholder="API token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => setShow(false)}
          >
            Later
          </button>
          <button type="submit" className="btn btn-primary btn-sm">
            Save &amp; reload
          </button>
        </div>
      </form>
    </div>
  );
}

export const Route = createRootRoute({
  component: () => (
    <>
      <AuthGate />
      <Outlet />
    </>
  ),
  notFoundComponent: () => (
    <div className="flex min-h-screen items-center justify-center text-sm text-base-content/50">
      Not found
    </div>
  ),
});