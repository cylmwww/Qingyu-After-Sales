export function QingyuMark({ small = false }: { small?: boolean }) {
  return <span className={`brand-mark ${small ? "brand-mark-small" : ""}`} aria-hidden="true">
    <svg width={small ? 18 : 24} height={small ? 18 : 24} viewBox="0 0 32 32" fill="none">
      <path d="M24 22a11 11 0 1 0-6 5l8 2-2-7Z" stroke="currentColor" strokeWidth="2.4" strokeLinejoin="round" />
      <path d="M10 12h12M10 17h8" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  </span>;
}
