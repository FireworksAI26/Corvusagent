interface BrandMarkProps {
  className?: string;
  decorative?: boolean;
  label?: string;
}

function classNames(...values: Array<string | undefined>): string {
  return values.filter((value): value is string => Boolean(value)).join(" ");
}

export function BrandMark({ className, decorative = false, label = "Corvus" }: BrandMarkProps) {
  return (
    <svg
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : label}
      className={classNames("brand-mark", className)}
      fill="none"
      role={decorative ? undefined : "img"}
      viewBox="0 0 84 84"
    >
      <path d="M61 14A30 30 0 1 0 61 70" stroke="currentColor" strokeLinecap="square" strokeWidth="8" />
      <path d="M54 27A17 17 0 1 0 54 57" stroke="currentColor" strokeLinecap="square" strokeWidth="8" />
      <rect data-brand-signal="true" fill="var(--brand-cyan, #66d9ee)" height="9" width="9" x="37.5" y="37.5" />
    </svg>
  );
}

export function BrandLockup({ className }: { className?: string }) {
  return (
    <div aria-label="Corvus" className={classNames("brand-lockup", className)} role="img">
      <BrandMark className="brand-lockup__mark" decorative />
      <span aria-hidden="true" className="brand-lockup__word">CORVUS</span>
    </div>
  );
}
