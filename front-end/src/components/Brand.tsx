export function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden="true"><span /><span /><span /></span>
      <div><span className="brand-name">meridian<span className="brand-dot">.</span></span>
        {!compact && <span className="brand-caption">MULTI-MODEL WORKSPACE</span>}
      </div>
    </div>
  )
}
