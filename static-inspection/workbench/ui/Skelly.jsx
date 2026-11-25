import React, { useEffect, useRef, useState } from 'react';
import voice from './skelly-dialogue.json';
import './skelly.css';

const corners = ['bottom-right', 'bottom-left', 'top-left', 'top-right'];
const bubbleDuration = 8000;

function findingLine(tool, matches, identical) {
  if (tool === 'identify_file') return voice.case.inspect;
  if (tool === 'export_report') return voice.case.export;
  if (tool === 'verify_checksum' && typeof matches === 'boolean') return matches ? voice.case.match : voice.case.mismatch;
  if (tool === 'compare_builds' && typeof identical === 'boolean') return identical ? voice.case.identical : voice.case.different;
  return null;
}

// The companion gets counts and fixed categories, never a sample, digest, key, or model instruction.
export default function Skelly({ revision, number, tool, matches, identical, mode, sampleCount, total, mismatches, omitted, critic }) {
  const root = useRef(null), sprite = useRef(null), options = useRef(null);
  const previous = useRef(null), nextLine = useRef({ pet: 0, feed: 0 });
  const drag = useRef(null), suppressClick = useRef(false);
  const [corner, setCorner] = useState(corners[0]);
  const [menu, setMenu] = useState(false), [small, setSmall] = useState(false);
  const [asleep, setAsleep] = useState(false), [speech, setSpeech] = useState(null);
  const pose = asleep ? 'nap' : speech?.pose ?? 'tablet';

  useEffect(() => {
    const prev = previous.current;
    previous.current = { revision, number, mode, critic };
    if (!prev || asleep || small) return;
    if (prev.revision == null) {
      if (revision != null) setSpeech(current => current?.text === voice.unavailable ? null : current);
      return; // Initial connection and recovery are not fresh findings.
    }
    let text;
    if (revision == null) text = voice.unavailable;
    else if (critic === 'ready' && prev.critic !== critic) text = voice.case.critique;
    else if (number !== prev.number && mismatches && tool === 'verify_checksum' && matches) text = voice.case.mixed;
    else if (number != null && (revision !== prev.revision || number !== prev.number)) text = findingLine(tool, matches, identical);
    else if (revision !== prev.revision) text = voice.samples[sampleCount];
    else if (mode !== prev.mode) text = voice.workflow[mode];
    else return; // Polls and restored visibility do not replay comments.
    setSpeech(text ? { text, pose: 'tablet' } : null);
  }, [revision, number, tool, matches, identical, mode, sampleCount, mismatches, critic, asleep, small]);

  useEffect(() => {
    if (!speech) return;
    const timer = setTimeout(() => setSpeech(null), bubbleDuration);
    return () => clearTimeout(timer);
  }, [speech]);

  useEffect(() => {
    if (!menu && !speech) return;
    function escape(event) {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) return;
      if (menu && root.current?.contains(document.activeElement)) options.current?.focus({ preventScroll: true });
      setMenu(false); setSpeech(null);
    }
    function outside(event) { if (menu && !root.current?.contains(event.target)) setMenu(false); }
    window.addEventListener('keydown', escape);
    window.addEventListener('pointerdown', outside);
    window.addEventListener('focusin', outside);
    return () => {
      window.removeEventListener('keydown', escape);
      window.removeEventListener('pointerdown', outside);
      window.removeEventListener('focusin', outside);
    };
  }, [menu, speech]);

  function care(action) {
    let text = voice[action];
    if (Array.isArray(text)) {
      const index = nextLine.current[action];
      nextLine.current[action] = (index + 1) % text.length;
      text = text[index];
    }
    setMenu(false);
    setAsleep(action === 'nap');
    setSpeech({ text, pose: action === 'pet' ? 'delighted' : action === 'feed' ? 'treat' : 'tablet' });
    sprite.current?.focus({ preventScroll: true });
  }

  function recap() {
    const text = revision == null ? voice.unavailable : !total ? (sampleCount ? 'Files loaded. No check logged yet.' : 'No files yet. Load a file in A and I’ll keep count.') :
      `${total} ${total === 1 ? 'entry' : 'entries'} in this case. ${mismatches ? `${mismatches} checksum ${mismatches === 1 ? 'mismatch' : 'mismatches'} on record.` : 'No checksum mismatch recorded.'}${omitted ? ` ${omitted} earlier entries are count-only.` : ''}`;
    setMenu(false);
    setSpeech({ text, pose: 'tablet' });
    sprite.current?.focus({ preventScroll: true });
  }

  function grab(event) {
    if (small || !event.isPrimary || event.button !== 0) return;
    suppressClick.current = false;
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, box: root.current.getBoundingClientRect(), moved: false };
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function move(event) {
    const d = drag.current;
    if (d?.id !== event.pointerId) return;
    const dx = event.clientX - d.x, dy = event.clientY - d.y;
    if (!d.moved) {
      if (Math.hypot(dx, dy) < 6) return;
      d.moved = true; setMenu(false); setSpeech(null);
    }
    const x = Math.max(-d.box.left, Math.min(dx, window.innerWidth - d.box.right));
    const y = Math.max(-d.box.top, Math.min(dy, window.innerHeight - d.box.bottom));
    root.current.style.translate = `${x}px ${y}px`;
  }
  function release(event) {
    const d = drag.current;
    if (d?.id !== event.pointerId) return;
    drag.current = null;
    suppressClick.current = d.moved;
    if (d.moved && event.type === 'pointerup') {
      setCorner(`${event.clientY < window.innerHeight / 2 ? 'top' : 'bottom'}-${event.clientX < window.innerWidth / 2 ? 'left' : 'right'}`);
    }
    root.current.style.removeProperty('translate');
  }
  function tap(event) {
    if (event.detail && suppressClick.current) { suppressClick.current = false; return; }
    if (small) { setSmall(false); return; }
    care(asleep ? 'wake' : 'pet');
  }

  return <div ref={root} className="skelly-pet" data-corner={corner} data-small={small} data-pose={pose} role="group" aria-label="Skellywix">
    <button type="button" ref={sprite} className="skelly-sprite" aria-label={small ? 'Show Skellywix' : asleep ? 'Wake Skellywix' : 'Pet Skellywix'}
      aria-describedby={small ? undefined : 'skelly-description'} title={small ? 'Show Skellywix' : 'Skellywix · Click to interact, drag to move'}
      onPointerDown={grab} onPointerMove={move} onPointerUp={release} onPointerCancel={release} onLostPointerCapture={release} onClick={tap}>
      {small ? 'Skellywix' : <span key={speech?.text ?? pose} className={`skelly-art pose-${pose}`} aria-hidden="true" />}
    </button>
    {!small && <>
      <span id="skelly-description" className="skelly-description">{voice.poses[pose]}</span>
      <button type="button" ref={options} className="skelly-options" aria-label="Skellywix options" title="Skellywix options" aria-expanded={menu}
        aria-controls="skelly-actions" onClick={() => { setMenu(!menu); setSpeech(null); }}>
        <svg width="18" height="6" viewBox="0 0 18 6" fill="currentColor" aria-hidden="true"><circle cx="3" cy="3" r="1.5"/><circle cx="9" cy="3" r="1.5"/><circle cx="15" cy="3" r="1.5"/></svg>
      </button>
      <div id="skelly-actions" role="group" aria-label="Skellywix actions" className="skelly-actions" hidden={!menu}>
        <span className="skelly-handle" aria-hidden="true">Skellywix</span>
        <button type="button" disabled={asleep} onClick={recap}>Recap this case</button>
        <button type="button" disabled={asleep} onClick={() => care('feed')}>Give a treat</button>
        <button type="button" onClick={() => care(asleep ? 'wake' : 'nap')}>{asleep ? 'Wake her up' : 'Let her nap'}</button>
        <button type="button" onClick={() => { setCorner(corners[(corners.indexOf(corner) + 1) % corners.length]); setMenu(false); options.current?.focus({ preventScroll: true }); }}>Move to next corner</button>
        <button type="button" onClick={() => { setSmall(true); setMenu(false); setSpeech(null); sprite.current?.focus({ preventScroll: true }); }}>Minimize Skellywix</button>
      </div>
    </>}
    <div className="skelly-voice" role="status" aria-label="Skellywix says" aria-atomic="true">
      {speech && !menu && !small && <span className="skelly-bubble"><b aria-hidden="true">Skellywix</b>{speech.text}</span>}
    </div>
  </div>;
}
