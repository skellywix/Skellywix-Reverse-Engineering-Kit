import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './desk.css';
import Skelly from './Skelly.jsx';
import CaseReview from './CaseReview.jsx';
import DenMasthead, { DenCredits } from './DenMasthead.jsx';

const modes = { identify_file: 'File ID', compare_builds: 'Build diff', verify_checksum: 'Hash check' };
const statuses = { off: 'No key issued', ready: 'Key issued · awaiting agent', connected: 'Agent paired', expired: 'Key expired', spent: 'Key spent' };
const hands = { operator: 'You', agent: 'Paired agent', skellywix: 'Skellywix' };
const limit = 16 * 1024 * 1024;
const endpoint = `http://127.0.0.1:${Number(location.port || (location.protocol === 'https:' ? 443 : 80)) + 1}/mcp`;
const config = `[mcp_servers.skellywix_re_kit]\nurl = "${endpoint}"\nbearer_token_env_var = "SKELLYWIX_CASE_TOKEN"`;
const bytes = n => `${n.toLocaleString()} ${n === 1 ? 'byte' : 'bytes'}`;
const icons = { identify_file: 'M9 3H4v16h8 M9 3v5h5L9 3z M20 20l-4-4 M18 13a4 4 0 1 1-8 0 4 4 0 0 1 8 0z', compare_builds: 'M3 5h7v14H3z M14 5h7v14h-7z M6 9h1 M17 9h1 M6 13h1 M17 15h1', verify_checksum: 'M12 3l8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6l8-3z M8 12l3 3 5-6' };
function Icon({ name }) { return <svg aria-hidden="true" viewBox="0 0 24 24" className="icon"><path d={icons[name]} /></svg>; }
function Meta({ data }) {
  return <dl className="meta"><dt>File format</dt><dd>{data.format}</dd><dt>Architecture</dt><dd>{data.architecture}</dd><dt>Size</dt><dd>{bytes(data.size)}</dd><dt>SHA-256</dt><dd className="digest">{data.sha256}</dd></dl>;
}
function Comparison({ result: r, slots: { a, b } }) {
  if (!a || !b) return null;
  const max = Math.max(a.size, b.size, 1);
  return <><h4>{r.identical ? 'Build hashes match' : 'Builds differ'}</h4><p>Size change (B − A): {r.size_delta > 0 ? '+' : ''}{bytes(r.size_delta)}. Format {r.format_changed ? 'changed' : 'unchanged'}; architecture {r.architecture_changed ? 'changed' : 'unchanged'}.</p>
    <table><caption>Builds, side by side</caption><thead><tr><th>Field</th><th scope="col">Specimen A</th><th scope="col">Specimen B</th></tr></thead><tbody>
      <tr><th scope="row">Size</th>{[a,b].map((s,i) => <td key={i}>{bytes(s.size)}<svg aria-hidden="true" className={'bar bar'+i} width="100%" height="7"><rect width={`${100*s.size/max}%`} height="7" /></svg></td>)}</tr>
      {['format','architecture','sha256'].map(k => <tr key={k} className={a[k] !== b[k] ? 'changed' : ''}><th scope="row">{k === 'sha256' ? 'SHA-256' : k}</th><td>{a[k]}</td><td>{b[k]}</td></tr>)}
    </tbody></table></>;
}
function Finding({ tool, result, slots }) {
  if (tool === 'identify_file' && Number.isInteger(result.size)) return <Meta data={result} />;
  if (tool === 'compare_builds' && Number.isInteger(result.size_delta)) return <Comparison result={result} slots={slots} />;
  if (tool === 'verify_checksum' && typeof result.matches === 'boolean') return <><h4 className={result.matches ? 'match' : 'mismatch'}>{result.matches ? 'Checksum matches' : 'Checksum mismatch'}</h4><dl className="meta"><dt>Reference SHA-256</dt><dd className="digest">{result.expected}</dd><dt>Specimen SHA-256</dt><dd className="digest">{result.actual}</dd></dl></>;
  return <pre>{JSON.stringify(result, null, 2)}</pre>;
}
function Sample({ slot, meta, revision, disabled, upload }) {
  const file = useRef(null);
  const [chosenAt, setChosenAt] = useState(null);
  const key = slot.toUpperCase();
  const changed = chosenAt !== null && revision !== undefined && revision !== chosenAt;
  async function apply() {
    if (changed) { setChosenAt(revision); return; }
    if (await upload(slot, file.current.files[0])) {
      file.current.value = '';
      setChosenAt(null);
    }
  }
  return <div className="sample"><div className="sample-title"><strong><i aria-hidden="true">{key}:</i>Specimen {key}</strong><span>{meta ? `${meta.format} · ${bytes(meta.size)}` : 'Drive empty'}</span></div>
    <div className="upload"><input type="file" ref={file} aria-label={`Choose specimen ${key}`} disabled={disabled} onChange={e => setChosenAt(e.target.files.length ? revision : null)} />
      <button disabled={disabled || chosenAt === null} onClick={apply}>{changed ? `Use ${key} in this folio` : `${meta ? 'Replace' : 'Load'} ${key}`}</button></div>
    {changed && <small role="status">The folio changed after you chose this file. Check the specimens before loading it.</small>}
    {meta && <details><summary>File ID record</summary><Meta data={meta} /></details>}</div>;
}
async function request(path, options) {
  const signal = AbortSignal.timeout(options?.method === 'POST' ? 30000 : 12000);
  const res = await fetch(`/api/${path}`, { cache: 'no-store', credentials: 'same-origin', redirect: 'error', signal, ...options });
  if (!res.ok) throw new Error(res.status === 409 ? 'This folio changed. Check the current entries, then try again.' : `Request declined (${res.status}). Check the folio and your input before retrying.`);
  return res;
}
function App() {
  const [state, setState] = useState(null), [online, setOnline] = useState(false);
  const [error, setError] = useState(''), [busy, setBusy] = useState('');
  const [mode, setMode] = useState('identify_file'), [slot, setSlot] = useState('a'), [hash, setHash] = useState('');
  const [answer, setAnswer] = useState(null), [credential, setCredential] = useState(null), [copied, setCopied] = useState(false), [reveal, setReveal] = useState(false);
  const [resetCount, setResetCount] = useState(0), [confirmReset, setConfirmReset] = useState(false);
  const epoch = useRef(0), locked = useRef(false), reading = useRef(false);
  function clearKey() { setCredential(null); setCopied(false); setReveal(false); }
  function accept(next) {
    setState(next); setOnline(true);
    setAnswer(prev => prev?.revision === next.revision ? prev : null);
    setCredential(prev => prev?.revision === next.revision && ['ready','connected'].includes(next.connection) ? prev : null);
  }
  async function refresh() {
    if (locked.current || reading.current) return;
    const id = epoch.current; reading.current = true;
    try { const next = await (await request('state')).json(); if (id === epoch.current) accept(next); }
    catch { if (id === epoch.current) { setOnline(false); clearKey(); setAnswer(null); } }
    finally { reading.current = false; }
  }
  useEffect(() => { refresh(); const timer = setInterval(refresh, 2200); return () => { ++epoch.current; clearInterval(timer); }; }, []);
  const current = online ? state : null, slots = current?.slots || {}, disabled = !!busy || !current;
  async function act(label, path, payload, then, binary = false) {
    if (locked.current || !current) return false;
    locked.current = true; ++epoch.current; setBusy(label); setError('');
    let ok = false;
    try {
      const response = await request(path, { method: 'POST', headers: { 'Content-Type': binary ? 'application/octet-stream' : 'application/json', 'X-Yuzu-Revision': String(current.revision) }, body: binary ? payload : JSON.stringify(payload) });
      const data = await response.json(); accept(data.state ?? data); then?.(data); ok = true;
    } catch (e) {
      setError(e instanceof TypeError || e.name === 'TimeoutError' ? 'The reading was interrupted. Check the refreshed ledger before retrying.' : e.message);
      setOnline(false); setAnswer(null); clearKey();
    }
    finally { locked.current = false; setBusy(''); if (!ok) refresh(); }
    return ok;
  }
  function upload(s, f) {
    if (!f || f.size > limit) { setError('Choose a file no larger than 16 MiB.'); return false; }
    return act(`Entering specimen ${s.toUpperCase()}…`, `sample/${s}`, f, () => { clearKey(); setAnswer(null); }, true);
  }
  function run() {
    const args = mode === 'compare_builds' ? {} : mode === 'verify_checksum' ? { slot, sha256: hash.trim().toLowerCase() } : { slot };
    act('Reading the file headers…', 'run', { tool: mode, arguments: args }, data => setAnswer({ mode, slot, hash: hash.trim().toLowerCase(), revision: data.state.revision, result: data.result }));
  }
  async function exportReport() {
    if (locked.current || !current) return;
    locked.current = true; ++epoch.current; setBusy('Exporting the ledger…'); setError('');
    try { const report = await (await request('report')).blob(); const url = URL.createObjectURL(report); const a = document.createElement('a'); a.href = url; a.download = 'skellywix-re-kit-evidence.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    catch { setError('The evidence ledger could not be exported. Refresh the folio and try again.'); }
    finally { locked.current = false; setBusy(''); refresh(); }
  }
  const connected = current && ['ready','connected'].includes(current.connection);
  const key = connected && credential?.revision === current.revision ? credential.token : null;
  const validHash = /^[a-f\d]{64}$/i.test(hash.trim());
  const canRun = mode === 'compare_builds' ? slots.a && slots.b : slots[slot] && (mode !== 'verify_checksum' || validHash);
  const visibleAnswer = !busy && current && answer?.revision === current.revision && answer.mode === mode && (mode === 'compare_builds' || answer.slot === slot) && (mode !== 'verify_checksum' || answer.hash === hash.trim().toLowerCase());
  const last = current?.events.at(-1);
  const advisor = current?.advisor, note = advisor?.critique;
  return <><a className="skip" href="#workspace">Skip to file tools</a><DenMasthead revision={current?.revision} />
    <main><div className="alerts" aria-live="polite">{!current && <p>{state ? 'Carrier lost. The local desk is offline; previous findings are hidden.' : 'Connecting to the local desk…'} <button onClick={refresh}>Refresh folio</button></p>}{busy && <p>{busy}</p>}</div>
    {error && <p role="alert" className="error">{error} <button onClick={() => { setError(''); refresh(); }}>Refresh folio</button></p>}
    <div className="layout"><section id="workspace" tabIndex={-1} className="workspace" aria-label="File tools">
      <div className="window-bar"><span>file_id.diz</span><i aria-hidden="true"/></div>
      <div className="modes" role="group" aria-label="Readings">{Object.entries(modes).map(([k, v]) => <button key={k} aria-pressed={mode === k} onClick={() => setMode(k)}><Icon name={k} />{v}</button>)}</div>
      <div className="workbody"><div className="section-head"><h2>File intake</h2><span className="limit">16 MiB per file</span></div><p>Load A first. Replacing A removes B. Loading either file clears the ledger and revokes the agent key.</p>
      <div key={resetCount}><Sample slot="a" meta={slots.a} revision={current?.revision} disabled={disabled} upload={upload} /><Sample slot="b" meta={slots.b} revision={current?.revision} disabled={disabled || !slots.a} upload={upload} /></div>
      <CaseReview key={current?.revision ?? 'offline'} revision={current?.revision} advisor={advisor} disabled={disabled} loaded={!!slots.a}
        start={(reference, critique) => act('Skellywix is checking the files…', 'review', { reference, critique })}
        cancel={end => act('Cancelling the critique…', 'critic/cancel', { end })} />
      <section className="run"><h2>{modes[mode]}</h2><p>{mode === 'identify_file' ? 'Read the header. Log the format, architecture, size, and SHA-256.' : mode === 'compare_builds' ? 'Put A and B head to head: hashes, sizes, formats, and architectures.' : 'Compare the specimen’s hash with a SHA-256 from a trusted source.'}</p>
        {mode !== 'compare_builds' && <label>Specimen<select value={slot} onChange={e => setSlot(e.target.value)}><option value="a">Specimen A</option><option value="b">Specimen B</option></select></label>}
        {mode === 'verify_checksum' && <label>Expected SHA-256<input type="text" value={hash} onChange={e => setHash(e.target.value)} autoComplete="off" spellCheck={false} placeholder="64 hexadecimal characters" /></label>}
        <button className="primary" disabled={disabled || !canRun} onClick={run}>{mode === 'identify_file' ? 'Inspect specimen' : mode === 'compare_builds' ? 'Compare specimens' : 'Verify checksum'}</button>
        {!canRun && <small>{mode === 'compare_builds' ? 'Load both specimens to compare builds.' : !slots[slot] ? `Load specimen ${slot.toUpperCase()} to start.` : 'Enter a 64-character hexadecimal digest.'}</small>}
        <div aria-live="polite">{visibleAnswer && <section className="finding"><h3>Check result</h3><Finding tool={mode} result={answer.result} slots={slots} /></section>}</div>
      </section></div></section>
      <aside><section className="agent" id="pact" tabIndex={-1}><div className="window-bar"><span>channel / #pact</span><i aria-hidden="true"/></div><h2>Agent pact</h2><p className="status" role="status"><i data-state={current?.connection} />{statuses[current?.connection] || 'Key status unavailable'}</p>
        <p>{connected ? `${current.remaining} tool calls left. ${current.connection === 'ready' ? 'Waiting for the client to pair.' : 'The client can read this folio.'}` : 'One key for one MCP agent. It can read case metadata, not files. The model critic gets no tools.'}</p>
        {connected ? <button disabled={disabled} onClick={() => act('Revoking the agent key…','disconnect',{},clearKey)}>Revoke agent key</button> : <><button disabled={disabled || !slots.a} onClick={() => act('Issuing a case key…','pair',{},data => { clearKey(); setCredential({ revision: data.state.revision, token: data.token }); })}>Pair agent</button><small>Key expires in 15 minutes or after 32 calls</small></>}
        {connected && <div className="pairing"><h3>Connect Codex</h3><p>Use a dedicated read-only client. Disable shell, browser, and other integrations. Set <code>SKELLYWIX_CASE_TOKEN</code> in its environment, never in chat. Add this configuration and start the client.</p><pre>{config}</pre>
        {key ? <><div className="actions"><button onClick={async () => { try { await navigator.clipboard.writeText(key); setCopied(true); } catch { setError('Clipboard unavailable. Reveal the case key and copy it manually.'); } }}>{copied ? 'Copy key again' : 'Copy case key'}</button><button aria-expanded={reveal} onClick={() => setReveal(!reveal)}>{reveal ? 'Hide case key' : 'Reveal case key'}</button></div>{reveal && <label>Private case key<input readOnly value={key} autoComplete="off" spellCheck={false} onFocus={e => e.target.select()} /></label>}{copied && <small role="status">Case key copied. Clear your clipboard after setup.</small>}</> : <p>This tab doesn’t hold the key. Revoke it and pair again if you need a new one.</p>}</div>}
      </section><section className="journal" id="ledger" tabIndex={-1}><div className="window-bar"><span>ledger.nfo</span><i aria-hidden="true"/></div><div className="section-head"><h2>Evidence ledger</h2>{advisor && <span className="count" aria-label={`${advisor.total} entries`}>{String(advisor.total).padStart(5, '0')}</span>}</div>
        <p>{!current ? 'Waiting for the local desk.' : !current.events.length ? 'No hits yet. Run a check to log entry #1.' : 'Last post first. Open an entry to inspect its evidence.'}</p>
        {advisor?.mismatches > 0 && <p className="ledger-warning">{advisor.mismatches} checksum {advisor.mismatches === 1 ? 'mismatch' : 'mismatches'} recorded in this folio.</p>}
        {current?.earlier_events_omitted > 0 && <p>{current.earlier_events_omitted} earlier entries counted; their details are no longer retained.</p>}
        {current && <ol>{[...current.events].reverse().map(e => <li key={`${current.revision}-${e.n}`}><details id={`finding-${current.revision}-${e.n}`}><summary><span>{modes[e.tool] || 'Export ledger'}{e.slot ? ` · ${e.slot.toUpperCase()}` : ''}</span><small>{hands[e.actor]} · #{e.n}</small></summary><Finding tool={e.tool} result={e.result} slots={slots} /></details></li>)}</ol>}
      </section></aside></div>
      <footer><span>Disk tools{current && <small>Revision {current.revision}</small>}</span>
        <div className="actions"><button disabled={disabled || !slots.a} onClick={exportReport}>Export evidence (JSON)</button>
          {current && confirmReset === current.revision ? <><span>Clear both specimens and the ledger?</span><button disabled={disabled} onClick={() => act('Clearing the folio…','reset',{},() => { clearKey(); setAnswer(null); setHash(''); setSlot('a'); setResetCount(n => n+1); setConfirmReset(false); })}>Confirm reset</button><button onClick={() => setConfirmReset(false)}>Keep folio</button></> : <button disabled={disabled || !slots.a} onClick={() => setConfirmReset(current.revision)}>Reset case</button>}
        </div>
      </footer>
      <DenCredits />
    </main>
    <Skelly revision={current?.revision} number={last?.n} tool={last?.tool} matches={last?.result.matches}
      identical={last?.result.identical} mode={mode} sampleCount={slots.b ? 2 : slots.a ? 1 : 0}
      total={advisor?.total} mismatches={advisor?.mismatches} omitted={current?.earlier_events_omitted} critic={note?.status} />
  </>;
}
createRoot(document.getElementById('root')).render(<App />);
