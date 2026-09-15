import { useEffect } from "react";

/**
 * Minimal accessible modal (Escape-to-close + overlay click), matching the
 * behavior of the panel used across the operator tabs. Local copy so this app
 * stays independent of the main OpenSEO codebase.
 */
export function Modal({
  maxWidth = "max-w-sm",
  children,
  onClose,
  labelledBy,
}: {
  maxWidth?: string;
  children: React.ReactNode;
  onClose?: () => void;
  labelledBy?: string;
}) {
  useEffect(() => {
    if (!onClose) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      onClose();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className={`card bg-base-100 border border-base-300 w-full ${maxWidth} max-h-full shadow-xl`}
      >
        <div className="card-body gap-4 overflow-y-auto">{children}</div>
      </div>
    </div>
  );
}