import React from 'react';

// The team workflow lives on its own origin. The link exists only when the deployment copy of
// index.html declares <meta name="skellywix-workspace" content="paired">; a standalone page shows no such link.
const WORKFLOW_URL = 'http://127.0.0.1:48784/';
const paired = document.querySelector('meta[name="skellywix-workspace"]')?.getAttribute('content') === 'paired';

export default function WorkspaceHeader() {
  return <header className="site-head">
    <h1 className="brand"><img src="/skellywix-mark.svg" alt="" width="24" height="24" />Skellywix RE Kit</h1>
    <p className="purpose">Identify, compare, or check the hash of files without running them.</p>
    <nav aria-label="Workspace"><ul>
      {paired && <li><a href={WORKFLOW_URL}>Build</a></li>}
      <li><a href={paired ? '/index.html' : '#workspace'} aria-current="page">Inspect</a></li>
    </ul></nav>
  </header>;
}
