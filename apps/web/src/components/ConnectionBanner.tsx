interface ConnectionBannerProps {
  error: string;
}

export function ConnectionBanner({ error }: ConnectionBannerProps) {
  return (
    <div className={`runtime-state ${error ? "runtime-state--warning" : ""}`} role="status">
      <span aria-hidden="true" />
      <strong>{error ? "Local · Needs attention" : "Local · Connected"}</strong>
      {error && <small>{error}</small>}
    </div>
  );
}
