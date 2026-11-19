import React from 'react';

// The logotype is cut from nine angular letterforms, not a font or a remote image.
function Wordmark() {
  return <svg className="wordmark" aria-hidden="true" viewBox="0 0 501 91">
    <defs>
      <linearGradient id="den-chrome" x2="0" y2="1" gradientUnits="objectBoundingBox">
        <stop stopColor="#fffce2"/><stop offset=".28" stopColor="#e6d9ff"/>
        <stop offset=".47" stopColor="#aba4c2"/><stop offset=".49" stopColor="#403952"/>
        <stop offset=".66" stopColor="#62597b"/><stop offset=".87" stopColor="#ece5fc"/><stop offset="1" stopColor="#9c90b5"/>
      </linearGradient>
      <g id="den-letters" transform="translate(25 17) skewX(-12)">
        <path d="M-18-12 47 0 40 12H14l-3 6 27 5 5 7-11 22H-2l-15 13L-6 40h28l3-7-27-5-6-8L0 4Z"/>
        <path d="M55 0h13l-3 19L84 0h20L78 26l18 26H77L65 33l-4 19H46Z"/>
        <path d="M110 0h43l-6 12h-25l-2 8h23l-5 11h-20l-2 9h29l-6 12H99Z"/>
        <path id="den-l" d="M161 0h15l-9 39h28l-8 13h-38Z"/>
        <use href="#den-l" x="48"/>
        <path d="M247 0h16l6 18 16-18h18l-30 33-4 19h-16l5-21Z"/>
        <path d="M298 0h15l-2 32 13-22h10l3 22 10-32h16l-19 52h-17l-2-22-15 22h-17Z"/>
        <path d="M372 0h17l-12 52h-17Z"/>
        <path d="M395 0h18l6 15L437 0l35-12-44 39 19 39-26-14-6-16-16 16h-21l31-29Z"/>
      </g>
    </defs>
    <use href="#den-letters" transform="translate(1 7)" fill="#3a1727" stroke="#f46632" strokeWidth="5" strokeLinejoin="bevel"/>
    <use href="#den-letters" transform="translate(0 3)" fill="#201925" stroke="#1b111e" strokeWidth="5" strokeLinejoin="bevel"/>
    <use href="#den-letters" fill="url(#den-chrome)" stroke="#fff2dc" strokeWidth="1.45" strokeLinejoin="bevel"/>
  </svg>;
}

function FlyerPrint() {
  return <svg className="flyer-print" aria-hidden="true" viewBox="0 0 1040 190" preserveAspectRatio="xMidYMid slice">
    <defs>
      <pattern id="den-dots" width="7" height="7" patternUnits="userSpaceOnUse"><path d="M0 0h2v2H0Z" fill="#b385ae"/></pattern>
      <pattern id="den-check" width="16" height="16" patternUnits="userSpaceOnUse" patternTransform="skewX(-33)"><path d="M0 0h8v8H0Zm8 8h8v8H8Z" fill="#d98a55"/></pattern>
    </defs>
    <path fill="#1e1428" d="M0 0h1040v190H0z"/>
    <path fill="#43243d" d="m0 174 214-81-86 42 218-77-137 77L568 24 327 142 731 20 497 116 821 46l-167 92 386-110v162H0z"/>
    <path fill="#6f3441" d="m385 190 242-103-72 47 192-84-58 44 142-55-63 42 200-70-38 34 110-31v103l-138 29 79-40-178 52 50-33-192 50 67-44-214 59z"/>
    <path fill="url(#den-dots)" opacity=".5" d="M0 0h1040v190H0z"/>
    <g stroke="#d89987" strokeWidth="1.3" fill="none" opacity=".7" transform="rotate(-13 890 117)">
      <ellipse cx="890" cy="125" rx="198" ry="66"/><ellipse cx="890" cy="125" rx="198" ry="43"/>
      <ellipse cx="890" cy="125" rx="198" ry="18"/><ellipse cx="890" cy="125" rx="122" ry="66"/>
      <ellipse cx="890" cy="125" rx="53" ry="66"/><path d="M692 125h396m-198-66v132"/>
    </g>
    <path fill="url(#den-check)" d="m0 163 178-49-32 26 81-9-101 34 30-22L0 185Zm693-163h241l-215 45 49-21-100 15 64-28Z"/>
  </svg>;
}

function ButtonWall() {
  return <div className="web-buttons" aria-hidden="true">
    <span className="badge badge-modem"><svg viewBox="0 0 20 17"><path d="M1 9h2V6h13v2h2v6H1zM6 3h2v3H6zm6-2h2v5h-2z"/><path className="cut" d="M4 10h2v2H4zm4 0h2v2H8z"/></svg><b>56K</b><i>FOREVER</i></span>
    <span className="badge badge-diz"><svg viewBox="0 0 20 17"><path d="M2 1h14l2 2v13H2z"/><path className="cut" d="M5 1h8v5H5zm0 8h10v6H5z"/></svg><b>.NFO</b><i>CLUB</i></span>
    <span className="badge badge-audio"><svg viewBox="0 0 20 17"><path d="M1 6h4l6-5v15l-6-5H1zm15-2 2 1-4 8-2-1z"/></svg><b>NO</b><i>AUTOPLAY</i></span>
  </div>;
}

export default function DenMasthead({ revision }) {
  return <header className="den-shell" id="top">
    <div className="mast"><span className="brand"><img src="/skellywix-mark.svg" alt="" width="26" height="26"/>~skellywix / localhost</span>
      <nav aria-label="Local webring"><span>LOCAL WEBRING</span><a href="#workspace">Intake</a><a href="#pact">Agent pact</a><a href="#ledger">Ledger</a></nav></div>
    <div className="flyer"><FlyerPrint/><div className="flyer-copy"><h1><span className="sr-only">Skellywix </span><Wordmark/><b>RE Kit</b></h1>
      <p>Welcome to the <strong>Bitrot Den.</strong> Read headers, compare builds, check hashes. Files never execute here.</p></div>
      <div className="sysop-print" aria-hidden="true"><div className="sysop-portrait"/><span>SYSOP <b>~skellywix</b></span></div></div>
    <div className="message-strip"><b>MOTD</b><samp>&lt;skellywix&gt;</samp><span>Save the .NFO. Keep the receipts.</span><small>{revision == null ? 'FOLIO OFFLINE' : `FOLIO ${String(revision).padStart(3, '0')}`}</small><ButtonWall/></div>
  </header>;
}

export function DenCredits() {
  return <div className="colophon"><span>Greetz from <b>#bitrot</b>. End of file.</span><a href="#top">Back to top</a></div>;
}
