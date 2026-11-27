/* eslint-disable react/prop-types */

export default function SectionCard({ title, subtitle, status, children, className = '' }) {
  return (
    <section className={['section-card', className].filter(Boolean).join(' ')}>
      <div className="section-card-header">
        <div>
          <h3>{title}</h3>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {status && <span className="status-pill">{status}</span>}
      </div>
      <div className="section-card-body">{children}</div>
    </section>
  )
}
