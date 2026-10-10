import type { ComponentChildren } from "preact";
import { useEffect, useState } from "preact/hooks";

const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

if (typeof window !== "undefined") window.addEventListener("popstate", notify);

export function navigate(to: string): void {
  const url = new URL(to, window.location.href);
  if (url.pathname + url.hash !== window.location.pathname + window.location.hash) {
    window.history.pushState(null, "", url.pathname + url.hash);
  }
  notify();
}

export function usePath(): { path: string; hash: string } {
  const read = () => ({ path: window.location.pathname, hash: window.location.hash.slice(1) });
  const [location, setLocation] = useState(read);
  useEffect(() => {
    const listener = () => setLocation(read());
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return location;
}

type LinkProps = {
  href: string;
  children: ComponentChildren;
  class?: string;
  title?: string;
  "aria-label"?: string;
  "aria-current"?: "page";
  onClick?: (event: MouseEvent) => void;
};

export function Link({ href, children, onClick, ...rest }: LinkProps) {
  return (
    <a
      {...rest}
      href={href}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        navigate(href);
      }}
    >
      {children}
    </a>
  );
}
