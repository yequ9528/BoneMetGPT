import { useEffect, useId, useRef, useState } from "react";

const columns = ["C11", "C12", "C13", "C22", "C23", "C33", "C44", "C55", "C66"];
const display = (item) => item === null || item === undefined || item === "" ? "—" : String(item);
const componentId = typeof props.componentId === "string" && props.componentId ? props.componentId : null;
const registry = globalThis.__boneCsvPickerComponentsV5 ||= new Map();
const host = (componentId && registry.get(componentId)) || { mounts: 0, cleanup: null };
host.scope = { props, callAction };
if (componentId) registry.set(componentId, host);

function CsvTargetPicker() {
  const { props: data, callAction } = host.scope;
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState("");
  const region = useRef(null);
  const errorsId = useId();
  const previousPage = useRef(data.page);
  const historical = data.state === "history";
  const creating = data.state === "creating";
  const uncertain = data.state === "uncertain";
  const locked = historical || creating || uncertain || !!busyId;
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const actions = Array.isArray(data.actions) ? data.actions : [];
  useEffect(() => {
    host.mounts += 1;
    clearTimeout(host.cleanup);
    return () => {
      host.mounts -= 1;
      host.cleanup = setTimeout(() => {
        if (!host.mounts && registry.get(componentId) === host) registry.delete(componentId);
      }, 0);
    };
  }, []);
  useEffect(() => {
    if (previousPage.current !== data.page) {
      region.current?.focus({ preventScroll: true });
      previousPage.current = data.page;
    }
  }, [data.page]);
  const act = async (action) => {
    if (!action || action.disabled || locked) return;
    setBusyId(action.id);
    setError("");
    try { await callAction(action); }
    catch (_error) { setError("The request could not be completed. Check the recorded task state before trying again."); }
    finally { setBusyId(null); }
  };

  return <section className={`bone-task bone-csv-picker ${historical ? "is-historical" : ""}`}
    aria-label="CSV target rows" aria-busy={creating || !!busyId}>
    <header className="bone-card-header"><p className="bone-eyebrow">CSV TARGETS</p>
      <h2>{data.title || "Choose a target row"}</h2>
      <p className="bone-csv-filename">{data.filename || "Uploaded CSV"} · Stiffness values in GPa</p></header>
    <p className="bone-csv-summary"><span><strong>{display(data.recordCount)}</strong> data rows</span>
      <span><strong>{display(data.validCount)}</strong> valid</span><span><strong>{display(data.invalidCount)}</strong> invalid</span></p>
    {historical && <p className="bone-history-label">Previous upload · Read only</p>}
    {data.selectedRecordIndex !== null && data.selectedRecordIndex !== undefined && <p className="bone-csv-current">
      {creating ? "Creating a design task from" : "Selected target:"} <strong>row {data.selectedRecordIndex}</strong>
      {data.filename ? ` · ${data.filename}` : ""}</p>}
    {creating ? <p className="bone-notice" role="status">Creating the design task… The new task will appear after creation is confirmed.</p> :
      uncertain ? <p className="bone-notice bone-notice-warning" role="status">Task creation is unconfirmed. Check the current task before choosing another row.</p> :
      !historical && <p className="bone-muted">Choose one valid row to review its target. Invalid rows include the reason they cannot be used.</p>}
    {error && <p className="bone-notice bone-notice-warning" role="alert">{error}</p>}
    <div ref={region} className="bone-table-scroll" tabIndex={0} role="region" aria-label="CSV target values in GPa; scroll horizontally for all nine components">
      <table className="bone-csv-table"><caption className="sr-only">CSV data rows · Nine stiffness components in GPa</caption>
        <thead><tr><th scope="col">Data row</th>{columns.map((name) => <th key={name} scope="col">{name}</th>)}</tr></thead>
        <tbody>{rows.map((row) => {
          const invalid = !!row.error;
          const selected = row.recordIndex === data.selectedRecordIndex;
          const selectable = !invalid && !!row.action && !row.action.disabled && !locked;
          return <tr key={row.recordIndex} className={`${selected ? "is-selected" : ""} ${invalid ? "is-invalid" : ""} ${selectable ? "is-selectable" : ""}`}
            aria-selected={selected} aria-describedby={invalid ? `${errorsId}-${row.recordIndex}` : undefined}
            onClick={selectable ? (event) => { if (!event.target.closest("button")) act(row.action); } : undefined}>
            <td>{row.action && !invalid ? <button type="button" className="bone-csv-row-button" disabled={!selectable}
              data-action-id={row.action.id} aria-label={`${row.action.label || "Select"} · data row ${row.recordIndex}`}
              onClick={() => act(row.action)}>{selected ? "Selected" : row.action.label || "Select"} · {row.recordIndex}</button> :
              <span>{invalid ? "Invalid" : selected ? "Selected" : "Row"} · {row.recordIndex}</span>}</td>
            {columns.map((name) => <td key={name}>{display(row.values?.[name])}</td>)}</tr>;
        })}</tbody></table>
    </div>
    {rows.some((row) => row.error) && <div className="bone-csv-errors" aria-label="Invalid row reasons">{rows.filter((row) => row.error).map((row) =>
      <p key={row.recordIndex} id={`${errorsId}-${row.recordIndex}`}><strong>Row {row.recordIndex}:</strong> {typeof row.error === "string" ? row.error : "This row does not contain a valid target."}</p>)}</div>}
    <nav className="bone-csv-pagination" aria-label="CSV target pages"><span>Page {display(data.page)} of {display(data.pageCount)} · Up to 20 rows per page</span>
      {actions.length > 0 && <div className="bone-actions">{actions.map((action) => <button key={action.id} type="button" className="bone-button"
        disabled={locked || action.disabled} data-action-id={action.id} onClick={() => act(action)}>{action.label || action.name}</button>)}</div>}</nav>
  </section>;
}

host.Component ||= CsvTargetPicker;
export default host.Component;
