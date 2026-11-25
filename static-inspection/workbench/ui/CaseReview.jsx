import React, { useState } from 'react';

const statusText = {
  idle: '', not_requested: 'Pass logged. No model critique requested.',
  pending: 'Waiting for the critic. No model note yet.', reviewing: 'The critic is reading this ledger snapshot.',
  failed: 'No model note: the critic was unavailable or its reply was invalid.', expired: 'Critique request expired. No note is current.',
  cancelled: 'Critique cancelled.', stale: 'The ledger changed. The previous model note no longer applies.',
};

export default function CaseReview({ advisor, disabled, loaded, start, cancel, revision }) {
  const [reference, setReference] = useState(''), [withCritic, setWithCritic] = useState(false);
  if (!advisor) return null;
  const digest = reference.trim();
  const valid = !digest || /^[a-f\d]{64}$/i.test(digest);
  const note = advisor.critique;
  const pending = ['pending', 'reviewing'].includes(note.status);
  return <div className="case-review">
    <details><summary>Run the case with Skellywix</summary>
      <p>One pass, no reruns: identify each file, diff the pair, and check A’s hash if you supply a reference.</p>
      <label>Reference SHA-256 for A (optional)<input type="text" value={reference} onChange={e => setReference(e.target.value)} autoComplete="off" spellCheck={false} aria-invalid={!valid} /></label>
      {!valid && <p>Enter 64 hexadecimal characters or leave the reference blank.</p>}
      <label><input type="checkbox" checked={withCritic} onChange={e => setWithCritic(e.target.checked)} /> Request a model critique</label>
      {withCritic && <><p>Your OMP model receives check names, actors, slot letters, outcomes, and counts. No files, paths, hashes, or keys are sent.</p>
        <details className="critic-setup"><summary>Connect the critic</summary><p>From the kit directory in OMP, load the adapter in Python Eval:</p><pre>%load workbench/omp_critic.py</pre><p>Select <strong>Start case review</strong>. While the request is pending, run this in a second Eval cell:</p><pre>{`review_next(completion, port=${Number(location.port || 80)})`}</pre><p>The request expires in two minutes. Each call handles one note; start a new review before connecting again.</p></details></>}
      <button disabled={disabled || !loaded || !valid || pending} onClick={() => start(digest || null, withCritic)}>Start case review</button>
    </details>
    {note.status !== 'idle' && <div className="review-receipt" role="status">
      {advisor.skipped.length > 0 && <p>Last pass skipped: {advisor.skipped.map(k => k === 'compare_builds' ? 'comparison (no B)' : 'checksum (no reference)').join('; ')}.</p>}
      {note.status === 'ready' ? <><h3>Model critique</h3><p className="critic-note">{note.text}</p><p>Ledger citations: {note.cites.map((n, i) => <React.Fragment key={n}>{i > 0 && ', '}<a href={`#finding-${revision}-${n}`} onClick={() => { const entry = document.getElementById(`finding-${revision}-${n}`); if (entry) entry.open = true; }}>#{n}</a></React.Fragment>)}. Second opinion, not evidence.</p></> : <p>{statusText[note.status]}</p>}
      {pending && <><button disabled={disabled} onClick={() => cancel(note.end)}>Cancel critique</button>{note.status === 'reviewing' && <p>Cancellation discards the note. It can’t recall a request already sent.</p>}</>}
    </div>}
  </div>;
}
