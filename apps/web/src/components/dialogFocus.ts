import type { KeyboardEvent } from "react";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])"
].join(",");

export function focusFirstControl(container: HTMLElement | null): void {
  container?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)?.focus();
}

export function trapDialogFocus(event: KeyboardEvent<HTMLElement>, container: HTMLElement | null): void {
  if (event.key !== "Tab" || container === null) return;
  const controls = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
  const first = controls[0];
  const last = controls.at(-1);
  if (first === undefined || last === undefined) return;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
