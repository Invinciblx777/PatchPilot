// Compact settings menu: a real menu button with keyboard support.

import type { JSX } from "preact";
import { useEffect, useId, useRef, useState } from "preact/hooks";
import styles from "./Menu.module.css";

export type MenuItem =
  | { kind: "action"; label: string; onSelect: () => void }
  | { kind: "radio"; label: string; checked: boolean; onSelect: () => void }
  | { kind: "heading"; label: string };

export function Menu({ label, icon, items }: { label: string; icon: JSX.Element; items: MenuItem[] }) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const focusables = () => Array.from(listRef.current?.querySelectorAll<HTMLElement>("[role^='menuitem']") ?? []);

  useEffect(() => {
    if (!open) return;
    focusables()[0]?.focus();
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!listRef.current?.contains(target) && !buttonRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [open]);

  const close = (refocus = true) => {
    setOpen(false);
    if (refocus) buttonRef.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent) => {
    const items = focusables();
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      (event.key === "Home" ? items[0] : items.at(-1))?.focus();
    } else if (event.key === "Tab") {
      close(false);
    }
  };

  return (
    <div class={styles.wrap}>
      <button
        ref={buttonRef}
        type="button"
        class={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open ? "true" : "false"}
        aria-controls={menuId}
        aria-label={label}
        title={label}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setOpen(true);
          }
        }}
      >
        {icon}
      </button>
      {open && (
        <div ref={listRef} id={menuId} role="menu" aria-label={label} class={styles.menu} onKeyDown={onKeyDown}>
          {items.map((item) =>
            item.kind === "heading" ? (
              <div key={item.label} class={styles.heading} role="presentation">
                {item.label}
              </div>
            ) : item.kind === "radio" ? (
              <button
                key={item.label}
                type="button"
                role="menuitemradio"
                aria-checked={item.checked ? "true" : "false"}
                class={styles.item}
                tabIndex={-1}
                onClick={() => {
                  item.onSelect();
                  close();
                }}
              >
                <span class={`${styles.radio} ${item.checked ? styles.on : ""}`} aria-hidden="true" />
                {item.label}
              </button>
            ) : (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                class={styles.item}
                tabIndex={-1}
                onClick={() => {
                  item.onSelect();
                  close();
                }}
              >
                {item.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}
