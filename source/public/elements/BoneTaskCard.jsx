import { useEffect, useId, useRef, useState } from "react";

const steps = ["Set a target", "Select a candidate", "Confirm the mesh", "Generate and view"];
const value = (item) => item === null || item === undefined || item === "" ? "Not recorded" : String(item);

// Chainlit re-evaluates the module for each props update. Keep its React type
// stable, while reading fresh props and action bridges from the current scope.
const componentId = typeof props.componentId === "string" && props.componentId ? props.componentId : null;
const componentRegistry = globalThis.__boneTaskCardComponentsV4 ||= new Map();
const componentHost = (componentId && componentRegistry.get(componentId)) || { mounts: 0, cleanup: null };
componentHost.scope = { props, callAction, submitElement, cancelElement };
if (componentId) componentRegistry.set(componentId, componentHost);

function Preview({ url, label, state, message, wide = false, onUnavailable }) {
  const dialog = useRef(null);
  const opener = useRef(null);
  const titleId = useId();
  const [broken, setBroken] = useState(false);
  useEffect(() => { setBroken(false); }, [url]);
  const close = () => {
    dialog.current?.close();
    opener.current?.focus();
  };
  return <div className={`bone-preview ${wide ? "bone-preview-wide" : ""}`}>
    {url && !broken ? <button className="bone-image-button" type="button" ref={opener}
      aria-label={`Enlarge ${label}`} onClick={() => dialog.current?.showModal()}>
      <img src={url} alt={label} onError={() => { setBroken(true); onUnavailable?.(url); }} />
      <span className="bone-zoom-hint">View larger</span>
    </button> : <div className="bone-preview-status" role="status">
      <strong>{state === "failed" || state === "unavailable" || broken ? "Preview unavailable" : "Preparing preview"}</strong>
      <span>{message || (broken ? "The image could not be displayed." : "The recorded task data remains available.")}</span>
    </div>}
    <dialog className="bone-lightbox" ref={dialog} aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); close(); }}
      onClick={(event) => { if (event.target === dialog.current) close(); }}>
      <div className="bone-lightbox-heading"><h2 id={titleId}>{label}</h2>
        <button type="button" className="bone-button" onClick={close}>Close preview</button></div>
      {url && <img src={url} alt={label} />}
    </dialog>
  </div>;
}

function ActionButton({ action, onAction, disabled, primary = false, label }) {
  if (!action) return null;
  return <button className={`bone-button ${primary ? "bone-button-primary" : ""}`} type="button"
    disabled={disabled || action.disabled} data-action-id={action.id} onClick={() => onAction(action)}>
    {label || action.label || action.name}
  </button>;
}

function ActionBar({ actions = [], onAction, disabled, primaryFirst = false }) {
  return actions.length > 0 && <div className="bone-actions">{actions.map((action, index) =>
    <ActionButton key={action.id} action={action} onAction={onAction} disabled={disabled}
      primary={primaryFirst && index === 0} />)}</div>;
}

function Comparison({ comparison, candidates }) {
  const [open, setOpen] = useState(false);
  const tableId = useId();
  if (!comparison?.rows?.length) return null;
  return <section className="bone-comparison" aria-label="Target and candidate comparison">
    <button type="button" className="bone-button bone-comparison-toggle" aria-expanded={open} aria-controls={tableId}
      onClick={() => setOpen(!open)}>{open ? "Hide" : "View"} all 9 stiffness components <span>({comparison.unit || "GPa"})</span>
      <span aria-hidden="true">{open ? "−" : "+"}</span></button>
    {open && <div id={tableId} className="bone-table-scroll" tabIndex={0} role="region" aria-label="Scrollable nine-component comparison">
      <table><thead><tr><th scope="col">Component</th><th scope="col">Target</th>
        {candidates.map((candidate) => <th key={candidate.id} scope="col">{candidate.name || `Rank ${candidate.rank}`} prediction</th>)}
      </tr></thead><tbody>{comparison.rows.map((row) => <tr key={row.name}>
        <th scope="row">{row.name}</th><td>{value(row.target)}</td>
        {candidates.map((candidate, index) => <td key={candidate.id}>{value(row.predictions?.[index])}</td>)}
      </tr>)}</tbody></table>
    </div>}
  </section>;
}

function Candidate({ candidate, historical, locked, onAction, selected = false }) {
  return <article className={`bone-candidate ${selected ? "is-selected" : ""}`} data-candidate-id={candidate.id}>
    <div className="bone-candidate-heading"><h3>{candidate.name || `Rank ${candidate.rank}`}</h3>
      <div className="bone-badges">
        {candidate.recommended && <span className="bone-badge">Recommended</span>}
        {selected && <span className="bone-badge bone-badge-solid">Confirmed</span>}
      </div>
    </div>
    <p className={`bone-eligibility ${candidate.eligible ? "" : "is-excluded"}`}>
      {candidate.eligible ? "Eligible candidate" : "Not eligible for selection"}</p>
    <Preview url={candidate.previewUrl} state={candidate.previewState} message={candidate.previewMessage}
      label={`${candidate.name || `Rank ${candidate.rank}`} unit cell`} />
    <dl className="bone-candidate-data"><div><dt>Normalized diameter</dt><dd>{value(candidate.diameter)} <span>(dimensionless)</span></dd></div>
      {candidate.rankingMetric !== "nmse" && <div><dt>P-10 ranking score</dt><dd>{value(candidate.score)}</dd></div>}
      <div><dt>Relative density</dt><dd>{value(candidate.density)} <span>(ratio)</span></dd></div>
      <div><dt>NMSE</dt><dd>{value(candidate.nmse)} <span>(squared relative norm, ratio)</span></dd></div></dl>
    {!!candidate.exclusionReasons?.length && <p className="bone-eligibility is-excluded">{candidate.exclusionReasons.join(", ")}</p>}
    {candidate.action ? <ActionButton action={candidate.action} onAction={onAction}
      disabled={historical || locked || !candidate.eligible} primary={selected}
      label={locked && !historical ? "Selection unavailable" : undefined} /> :
      <p className="bone-readonly-label">{historical ? "Historical preview" : candidate.eligible ? "Read-only preview" : "Selection unavailable"}</p>}
  </article>;
}

function BoneTaskCard() {
  const { props, callAction, submitElement, cancelElement } = componentHost.scope;
  useEffect(() => {
    componentHost.mounts += 1;
    clearTimeout(componentHost.cleanup);
    return () => {
      componentHost.mounts -= 1;
      // A deferred cleanup also permits React StrictMode's immediate remount.
      componentHost.cleanup = setTimeout(() => {
        if (!componentHost.mounts && componentRegistry.get(componentId) === componentHost) componentRegistry.delete(componentId);
      }, 0);
    };
  }, []);
  const nested = props.kind !== "candidates" && props[props.kind] && typeof props[props.kind] === "object" ? props[props.kind] : {};
  const data = { ...props, ...nested };
  const candidates = Array.isArray(data.candidates) ? data.candidates : [];
  const confirmed = data.confirmationState === "confirmed" && !!data.selectedId;
  const [comparisonOpen, setComparisonOpen] = useState(false);
  const [meshView, setMeshView] = useState("selected");
  const [busyId, setBusyId] = useState(null);
  const [localError, setLocalError] = useState("");
  const [failedResultUrl, setFailedResultUrl] = useState(null);
  const meshTabsId = useId();
  useEffect(() => { setFailedResultUrl(null); }, [data.previewUrl]);
  const lastSelected = useRef(confirmed ? data.selectedId : null);
  useEffect(() => {
    if (confirmed && lastSelected.current !== data.selectedId) setComparisonOpen(false);
    lastSelected.current = confirmed ? data.selectedId : null;
  }, [confirmed, data.selectedId]);
  const historical = !!data.historical;
  const locked = historical || !!data.locked || data.confirmationState === "pending" || !!busyId;
  const actions = Array.isArray(data.actions) ? data.actions : [];
  const resultState = data.verificationState === "verified" ? "verified" : data.verificationState === "review" ? "review" : "checking";
  const act = async (action) => {
    if (!action || action.disabled || historical || busyId) return;
    setBusyId(action.id);
    setLocalError("");
    try { await callAction(action); }
    catch (_error) { setLocalError("The action could not be completed. Refresh the task to check its recorded state."); }
    finally { setBusyId(null); }
  };
  const notices = <>
    {data.replayLabel && <p className="bone-replay-label">{data.replayLabel}</p>}
    {historical && <p className="bone-history-label">Historical task · Read only · Previous confirmations have ended</p>}
    {data.message && <p className={`bone-notice ${data.confirmationState === "uncertain" ? "bone-notice-warning" : ""}`} role="status">{data.message}</p>}
    {localError && <p className="bone-notice bone-notice-warning" role="alert">{localError}</p>}
  </>;

  if (data.kind === "summary") return <section className={`bone-task bone-task-summary ${historical ? "is-historical" : ""}`}
    aria-label={historical ? "Historical task summary" : "Current task summary"} data-job-id={data.jobId}>
    <div className="bone-summary-heading"><h2>{data.jobLabel || "Design task"}</h2>
      <span className="bone-summary-id" title={data.jobId}>{data.jobId ? data.jobId.slice(-8) : ""}</span></div>
    {notices}<p className="bone-summary-status" role="status">{data.statusText}</p>
    <ol className="bone-steps">{steps.map((step, index) => <li key={step}
      className={index + 1 === Number(data.phase) ? "is-current" : index + 1 < Number(data.phase) ? "is-complete" : ""}
      aria-current={index + 1 === Number(data.phase) ? "step" : undefined}>
      <span className="bone-step-number">{index + 1}</span><span>{step}</span></li>)}</ol>
  </section>;

  if (data.kind === "candidates") {
    const selected = candidates.find((candidate) => candidate.id === data.selectedId);
    return <section className={`bone-task bone-task-candidates ${historical ? "is-historical" : ""}`} data-job-id={data.jobId} aria-label="Candidate unit cells">
      <header className="bone-card-header"><p className="bone-eyebrow">CANDIDATE PREVIEWS</p>
        <h2>{confirmed ? "Your selected unit cell" : "Review the available unit cells"}</h2>
        <p>{confirmed ? "The selection is recorded. Continue to the mesh step or review the comparison." : "Compare the structures and predictions before confirming your selection."}</p></header>
      {notices}
      {data.confirmationState === "pending" && <p className="bone-notice" role="status">Confirming your selection…</p>}
      {confirmed && selected && <div className="bone-selected-summary"><div className="bone-selected-visual" data-candidate-id={selected.id}>
        <Preview url={selected.previewUrl} state={selected.previewState} message={selected.previewMessage}
          label={`${selected.name || `Rank ${selected.rank}`} unit cell`} />
        </div><div className="bone-selected-copy">
        <span className="bone-badge bone-badge-solid">Selection confirmed</span><h3>{selected.name || `Rank ${selected.rank}`}</h3>
        <p>{selected.recommended ? "You confirmed the recommended candidate." : "Your choice is recorded; the recommendation remains unchanged."}</p>
        <p className="bone-muted">Normalized strut diameter: {value(selected.diameter)} (dimensionless)</p>
        <button className="bone-button" type="button" aria-expanded={comparisonOpen}
          onClick={() => setComparisonOpen(!comparisonOpen)}>{comparisonOpen ? "Hide candidate comparison" : "View candidate comparison"}</button>
      </div></div>}
      {(!confirmed || comparisonOpen) && <>
        <div className="bone-candidate-grid">{candidates.map((candidate) => <Candidate key={candidate.id} candidate={candidate}
          historical={historical} locked={locked} selected={confirmed && candidate.id === data.selectedId} onAction={act} />)}</div>
        <Comparison comparison={data.comparison} candidates={candidates} />
      </>}
      <ActionBar actions={actions} onAction={act} disabled={historical || !!busyId} />
    </section>;
  }

  if (data.kind === "mesh") {
    const views = data.views || {};
    const viewLabels = { original: "Original mesh", selected: "Selected domain", excluded: "Excluded regions" };
    const awaiting = data.mode === "await_domain";
    const submit = (confirm) => {
      if (historical || busyId || data.askActive === false) return;
      setBusyId(confirm ? "domain-confirm" : "domain-cancel");
      if (confirm) submitElement({ payload: { ...data.askPayload, confirm: true } });
      else cancelElement();
    };
    return <section className={`bone-task bone-task-mesh ${historical ? "is-historical" : ""}`} data-job-id={data.jobId} aria-label="Mesh computational domain">
      <header className="bone-card-header"><p className="bone-eyebrow">MESH DOMAIN PREVIEW</p>
        <h2>{awaiting ? "Confirm the computational domain" : data.mode === "preparing" ? "Preparing the mesh preview" : "Review the computational domain"}</h2>
        <p>{awaiting ? "Review the selected and excluded regions before confirming the quality check." : "The uploaded mesh is shown in its original coordinate system."}</p></header>
      {notices}<div className="bone-two-column"><div className="bone-visual-column">
        <div className="bone-view-tabs" role="tablist" aria-label="Mesh views">{Object.keys(viewLabels).map((key) =>
          <button key={key} id={`${meshTabsId}-${key}`} type="button" role="tab" aria-selected={meshView === key}
            aria-controls={`${meshTabsId}-panel`} tabIndex={meshView === key ? 0 : -1}
            className={meshView === key ? "is-active" : ""} onClick={() => setMeshView(key)}
            onKeyDown={(event) => {
              const keys = Object.keys(viewLabels);
              const next = event.key === "ArrowRight" ? (keys.indexOf(key) + 1) % keys.length :
                event.key === "ArrowLeft" ? (keys.indexOf(key) + keys.length - 1) % keys.length :
                event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : -1;
              if (next < 0) return;
              event.preventDefault();
              setMeshView(keys[next]);
              event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next]?.focus();
            }}>{viewLabels[key]}</button>)}</div>
        <div id={`${meshTabsId}-panel`} role="tabpanel" aria-labelledby={`${meshTabsId}-${meshView}`}><Preview wide url={views[meshView]}
          label={viewLabels[meshView]} state={data.previewState} message={data.previewMessage} /></div>
        <div className="bone-legend"><span className="is-blue">Blue: selected hexahedra</span><span className="is-red">Red: excluded solids</span><span className="is-grey">Grey: shells / other</span></div>
      </div><div className="bone-detail-column"><h3>{awaiting ? "Selected domain" : "Uploaded mesh"}</h3>
        <dl className="bone-details"><div><dt>Input</dt><dd>{value(data.fileName)}</dd></div>
          {data.originalNodeCount !== undefined || data.originalElementCount !== undefined ?
            <div><dt>Original mesh</dt><dd>{value(data.originalNodeCount)} nodes · {value(data.originalElementCount)} elements</dd></div> : <>
            <div><dt>Recorded nodes</dt><dd>{value(data.nodeCount)}</dd></div>
            <div><dt>Recorded elements</dt><dd>{value(data.elementCount)}</dd></div></>}
          <div><dt>Selected domain</dt><dd>{data.selectedNodeCount !== null && data.selectedNodeCount !== undefined ? `${value(data.selectedNodeCount)} nodes · ` : ""}{value(data.selectedCount)} hexahedral elements</dd></div>
          <div><dt>Excluded</dt><dd>{value(data.excludedCount)} elements · {value(data.excludedSolids)} solids</dd></div>
          {data.excludedTypes && <div><dt>Excluded types</dt><dd>{typeof data.excludedTypes === "string" ? data.excludedTypes : Object.entries(data.excludedTypes).map(([key, item]) => `${key}: ${typeof item === "object" ? item.count : item}`).join(" · ") || "None"}</dd></div>}
          <div><dt>Quality</dt><dd className={awaiting ? "bone-quality-pending" : ""}>{awaiting ? "Not checked yet" : value(data.quality)}</dd></div>
          <div><dt>Coordinates</dt><dd>mm · Original coordinate system</dd></div></dl>
        <p className="bone-muted">{awaiting ? "Confirming this domain starts the quality check only. Conformal generation requires a separate confirmation." : "Only the confirmed hexahedral domain is used for lattice generation. Excluded regions are not computed."}</p>
        {awaiting && <div className="bone-actions"><button type="button" className="bone-button bone-button-primary"
          disabled={historical || !!busyId || data.askActive === false} onClick={() => submit(true)}>Confirm domain and check quality</button>
          <button type="button" className="bone-button" disabled={historical || !!busyId || data.askActive === false}
            onClick={() => submit(false)}>Cancel</button></div>}
        <ActionBar actions={actions} onAction={act} disabled={historical || !!busyId} primaryFirst />
      </div></div>
    </section>;
  }

  if (data.kind === "result") return <section className={`bone-task bone-task-result ${historical ? "is-historical" : ""}`} data-job-id={data.jobId} aria-label="Conformal result">
    <header className="bone-card-header"><p className="bone-eyebrow">CONFORMAL RESULT</p>
      <h2>{resultState === "verified" ? "View the generated structure" : resultState === "review" ? "Review the recorded result" : "Checking recorded result files"}</h2>
      <p>{resultState === "verified" ? "The recorded result files were verified and are ready for inspection." : resultState === "review" ?
        "The recorded files need review before they can be opened as a verified result." : "Result verification is in progress. The recorded geometry has not been confirmed yet."}</p></header>{notices}
    <div className="bone-two-column"><div className="bone-visual-column"><Preview wide url={resultState === "verified" ? data.previewUrl : null}
      label="Conformal lattice structure" state={resultState === "review" ? "unavailable" : data.previewState}
      message={resultState === "checking" ? "Checking the recorded result files." : data.previewMessage} onUnavailable={setFailedResultUrl} /></div>
      <div className="bone-detail-column"><h3>Conformal lattice preview</h3>
        <dl className="bone-details"><div><dt>Input</dt><dd>{value(data.fileName)}</dd></div>
          <div><dt>Selected unit cell</dt><dd>{value(data.selectedName)}</dd></div>
          <div><dt>Result</dt><dd>{resultState === "verified" ? "Verified geometry available" : resultState === "review" ? "Verification needs review" : "Checking recorded result files"}</dd></div>
          <div><dt>Image preview</dt><dd>{resultState !== "verified" ? "Not confirmed" : data.previewUrl && failedResultUrl === data.previewUrl ? "Unavailable" : data.previewUrl ? "Available" : data.previewState === "failed" || data.previewState === "unavailable" ? "Unavailable" : "Preparing"}</dd></div></dl>
        <p className="bone-muted">{resultState === "verified" ? "Inspect the structure and the selected unit cell in 3D, or access the existing result files." : "Use Refresh Job status to check the recorded result."}</p>
        {resultState === "verified" && data.viewerUrl && <a className="bone-button bone-button-primary bone-viewer-link" href={data.viewerUrl} target="_blank" rel="noreferrer">Open 3D results <span aria-hidden="true">↗</span></a>}
        {resultState === "verified" && !!data.files?.length && <div className="bone-supporting-files"><h4>Supporting files</h4>{data.files.map((file) =>
          <a key={file.url} href={file.url} target="_blank" rel="noreferrer">{file.label}</a>)}</div>}
      </div></div><ActionBar actions={actions} onAction={act} disabled={historical || !!busyId} />
  </section>;

  return <section className={`bone-task bone-task-compact ${historical ? "is-historical" : ""}`} data-job-id={data.jobId} aria-label={data.title || "Task action"}>
    <header className="bone-card-header"><h2>{data.title || (data.kind === "entry" ? "Continue your design" : "Task update")}</h2>
      {data.description && <p>{data.description}</p>}</header>{notices}
    <ActionBar actions={actions} onAction={act} disabled={historical || !!data.locked || !!busyId} primaryFirst />
  </section>;
}

componentHost.Component ||= BoneTaskCard;
export default componentHost.Component;
