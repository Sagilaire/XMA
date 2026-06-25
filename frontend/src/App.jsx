import { useCallback, useEffect, useRef, useState } from 'react';
import Uploader from './components/Uploader.jsx';

const API_BASE =
  (typeof __API_BASE__ !== 'undefined' && __API_BASE__) || 'http://localhost:4015';

const APP_NAME = 'XAPK Multi-Account Cloner';
const SUFFIX_PATTERN = /^[A-Za-z0-9_]{1,32}$/;

// Mirror of backend's package-suffix rules. The backend rejects any string
// not matching SUFFIX_PATTERN, but we sanitise upfront so the user can SEE
// what the resolved applicationId will be BEFORE uploading.
//
// Rules (must stay in sync with backend is_safe_suffix in utils.py):
//  * trim + lowercase
//  * any run of non-[a-z0-9_] becomes a single underscore
//  * collapse underscores, strip leading/trailing underscores
//  * prepend `_` so the result always reads as a token *tacked on* to the
//    original applicationId (e.g. com.ankama.dofustouch + _panda_touch)
//  * cap at 32 chars so is_safe_suffix is happy
//
// Trade-off (deliberate): the regex is ASCII-only. Labels that are entirely
// non-ASCII (e.g. "日本語") collapse to "" and the auto-derive effect
// leaves the suffix alone — the user can still type their intended token
// by hand. Widening this to /\p{L}\p{N}/u would also require the backend
// is_safe_suffix to widen; out of scope for this fix.
function sanitizeNewNameToSuffix(name) {
  if (!name) return '';
  let s = name.trim().toLowerCase();
  s = s.replace(/[^a-z0-9_]+/g, '_');
  s = s.replace(/_+/g, '_');
  s = s.replace(/^_+|_+$/g, '');
  if (!s) return '';
  if (!s.startsWith('_')) s = '_' + s;
  return s.slice(0, 32);
}

function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function validateIcon(file) {
  if (!file) return null;
  const isPng =
    file.type === 'image/png' ||
    (file.name && file.name.toLowerCase().endsWith('.png'));
  if (!isPng) return 'Icon must be a PNG image.';
  return null;
}

function freshRandomSuffix() {
  // 4 lowercase alphanumeric chars -> 36^4 ~ 1.7M combinations,
  // enough entropy that consecutive clones on this SPA almost never
  // collide on applicationId by accident.
  return `_${Math.random().toString(36).slice(2, 6)}`;
}

export default function App() {
  const [xapk, setXapk] = useState(null);
  const [icon, setIcon] = useState(null);
  const [suffix, setSuffix] = useState(freshRandomSuffix);
  // Becomes true the moment the user types directly into the suffix field.
  // Suppresses auto-derivation so an explicit edit is never clobbered by a
  // later change to newName.
  const [suffixManuallyEdited, setSuffixManuallyEdited] = useState(false);
  const [newName, setNewName] = useState('');
  const [progress, setProgress] = useState(0);
  const [busy, setBusy] = useState(false);
  const [indeterminate, setIndeterminate] = useState(false);
  const [status, setStatus] = useState({ kind: 'hidden', message: '', extra: null });

  const xhrRef = useRef(null);

  const resetForm = useCallback(() => {
    cancelInFlight();
    setXapk(null);
    setIcon(null);
    // Fresh random suffix per round — combined with auto-derivation from
    // newName this guarantees two consecutive uploads always get distinct
    // Android applicationIds, so the second install never shows the
    // "do you want to update the existing app?" prompt.
    setSuffix(freshRandomSuffix());
    setSuffixManuallyEdited(false);
    setNewName('');
    setProgress(0);
    setBusy(false);
    setStatus({ kind: 'hidden', message: '', extra: null });
  }, []);

  const onPickIcon = useCallback((file) => {
    const err = validateIcon(file);
    if (err) {
      setIcon(null);
      setStatus({ kind: 'error', message: err });
      return;
    }
    setIcon(file);
    setStatus({ kind: 'hidden', message: '', extra: null });
  }, []);

  const cancelInFlight = useCallback(() => {
    if (xhrRef.current) {
      try { xhrRef.current.abort(); } catch (_) { /* noop */ }
      xhrRef.current = null;
    }
  }, []);

  const handleSubmit = useCallback(
    (e) => {
      e?.preventDefault?.();
      if (busy) return;

      if (!xapk) {
        setStatus({ kind: 'error', message: 'Please pick a .xapk file first.' });
        return;
      }
      if (!SUFFIX_PATTERN.test(suffix)) {
        setStatus({
          kind: 'error',
          message: 'Suffix must be 1–32 chars using letters, digits, or underscores.',
        });
        return;
      }
      const iconErr = validateIcon(icon);
      if (iconErr) {
        setStatus({ kind: 'error', message: iconErr });
        return;
      }

      const form = new FormData();
      form.append('file', xapk);
      form.append('suffix', suffix);
      if (newName.trim()) form.append('new_name', newName.trim());
      if (icon) form.append('new_icon', icon);

      cancelInFlight();

      const xhr = new XMLHttpRequest();
      xhrRef.current = xhr;
      xhr.open('POST', `${API_BASE}/upload`, true);
      xhr.responseType = 'blob';

      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        setIndeterminate(false);
        setProgress(Math.round((ev.loaded / ev.total) * 100));
      };
      xhr.upload.onload = () => {
        setIndeterminate(true);
        setProgress(100);
      };

      xhr.onerror = () => {
        setBusy(false);
        setIndeterminate(false);
        setProgress(0);
        setStatus({ kind: 'error', message: 'Network error — could not reach the backend.' });
      };

      xhr.onload = () => {
        setBusy(false);
        setIndeterminate(false);
        const contentType = xhr.getResponseHeader('Content-Type') || '';
        const isJson = contentType.includes('application/json');

        if (xhr.status >= 200 && xhr.status < 300) {
          let filename =
            xapk.name.replace(/\.xapk$/i, '') +
            '-' +
            (suffix.replace(/^_+/, '') || 'clone') +
            '.xapk';
          const disp = xhr.getResponseHeader('Content-Disposition') || '';
          const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disp);
          if (match) filename = decodeURIComponent(match[1]);

          const blob = xhr.response;
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 800);

          setStatus({
            kind: 'success',
            message: `Cloned successfully. Your download (${formatBytes(blob.size)}) should start automatically.`,
            extra: {
              pkg: xhr.getResponseHeader('X-New-Package'),
              label: xhr.getResponseHeader('X-New-Label'),
            },
          });
          setProgress(0);
        } else if (isJson) {
          try {
            const data = JSON.parse(xhr.responseText);
            setStatus({
              kind: 'error',
              message: data.detail || data.error || `Server returned ${xhr.status}.`,
            });
          } catch {
            setStatus({ kind: 'error', message: `Server returned ${xhr.status}.` });
          }
        } else {
          setStatus({ kind: 'error', message: `Server returned ${xhr.status}.` });
        }
      };

      setBusy(true);
      setProgress(0);
      setIndeterminate(false);
      setStatus({ kind: 'info', message: 'Uploading and processing… this can take a while.' });

      xhr.send(form);
    },
    [busy, cancelInFlight, xapk, icon, suffix, newName],
  );

  // Die on unmount: cancel any in-flight upload so we don't leak the XHR.
  useEffect(() => () => cancelInFlight(), [cancelInFlight]);

  // Auto-derive the package suffix from newName unless the user has begun
  // typing directly into the suffix field. Without this, two clones with
  // different visible labels share the same applicationId and Android
  // treats the second install as an UPDATE (replacing the first).
  //
  // Recovery rule (review item 4): if the user typed into the suffix input
  // by accident but later returned it to a value that matches what the
  // auto-derive would currently produce, clear `suffixManuallyEdited` so
  // future newName edits re-engage the auto-derive. Avoids locking the
  // user out from one stray keystroke.
  useEffect(() => {
    const derived = sanitizeNewNameToSuffix(newName);
    if (suffixManuallyEdited) {
      if (derived && derived === suffix) {
        setSuffixManuallyEdited(false);
      }
      return;
    }
    if (derived) setSuffix(derived);
  }, [newName, suffix, suffixManuallyEdited]);

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>{APP_NAME}</h1>
        <p>
          Re-package an existing XAPK under a new package name so you can run multiple
          accounts of the same app side by side. Optionally rename the visible label
          and replace the launcher icon.
        </p>
      </header>

      <form className="card" onSubmit={handleSubmit}>
        <Uploader
          accept=".xapk"
          file={xapk}
          onFile={(file) => {
            setXapk(file);
            setStatus({ kind: 'hidden', message: '', extra: null });
          }}
          label="XAPK file"
          required
          hint="Drop your .xapk here or click to browse"
        />

        <div className="form-grid">
          <div className="field field--full">
            <div className="label-row">
              <label htmlFor="suffix">
                Package suffix<span className="required">*</span>
              </label>
              <span className="hint">appended to the original package name</span>
            </div>
            <input
              id="suffix"
              className="input"
              type="text"
              value={suffix}
              onChange={(e) => {
                setSuffix(e.target.value);
                // Once the user types into the suffix edit, treat it as
                // an authoritative override; stop the auto-derivation from
                // newName from clobbering it on later renders.
                setSuffixManuallyEdited(true);
              }}
              placeholder="e.g. _panda or _clone_a4f8"
              maxLength={32}
              required
            />
          </div>

          <div className="field">
            <div className="label-row">
              <label htmlFor="new_name">New app label</label>
              <span className="hint">optional</span>
            </div>
            <input
              id="new_name"
              className="input"
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Leave empty to use <original> <suffix>"
              maxLength={80}
            />
          </div>

          <div className="field">
            <div className="label-row">
              <label>New icon</label>
              <span className="hint">PNG only · optional</span>
            </div>
            <IconPicker icon={icon} onIcon={onPickIcon} />
          </div>
        </div>

        <p className="hint preview-line" aria-live="polite">
          Will install as Android package{' '}
          <code>
            {`<original-pkg>${suffix}`}
          </code>{' '}
          — the full resolved name appears in the green banner below after
          upload. Different labels (or two consecutive rounds) produce
          different applicationIds, so side-by-side installs no longer
          trigger the "update existing app" prompt.
        </p>

        <div className="actions">
          <div className={`progress${indeterminate ? ' indeterminate' : ''}`}>
            <div className="bar" style={{ width: busy ? `${progress}%` : '0%' }} />
          </div>
          <button type="submit" className="btn" disabled={busy || !xapk}>
            {busy ? 'Processing…' : 'Clone XAPK'}
          </button>
        </div>

        <div className={`status-area ${status.kind}`}>
          {status.message}
          {status.extra && (
            <ul className="summary-list">
              {status.extra.pkg && (
                <li>New package: <code>{status.extra.pkg}</code></li>
              )}
              {status.extra.label && (
                <li>New label: <code>{status.extra.label}</code></li>
              )}
            </ul>
          )}
        </div>

        {status.kind === 'success' && (
          <div className="actions" style={{ marginTop: 12 }}>
            <button type="button" className="btn secondary" onClick={resetForm}>
              Start over
            </button>
          </div>
        )}
      </form>

      <div className="config-note">
        Talking to backend at <code>{API_BASE}</code>{' '}
        (configurable via <code>VITE_API_BASE_URL</code> at build time).
      </div>
      <p className="footer-note">
        Tip: each user-account variant should pick a unique suffix to avoid
        Android install conflicts.
      </p>
    </div>
  );
}

function IconPicker({ icon, onIcon }) {
  const [preview, setPreview] = useState(null);

  useEffect(() => {
    if (!icon) {
      setPreview(null);
      return;
    }
    const url = URL.createObjectURL(icon);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [icon]);

  const trigger = useRef(null);
  const handlePick = () => trigger.current?.click();
  const onChange = (e) => {
    const file = e.target.files?.[0] || null;
    onIcon(file);
    e.target.value = '';
  };
  return (
    <div className="icon-input">
      <input
        ref={trigger}
        type="file"
        accept="image/png"
        onChange={onChange}
        hidden
      />
      {preview ? (
        <img src={preview} alt="icon preview" />
      ) : (
        <button type="button" className="icon-trigger" onClick={handlePick}>
          Choose PNG
        </button>
      )}
      <span className="icon-name">
        {icon ? icon.name : 'No icon chosen — original will remain.'}
      </span>
      {icon && (
        <button type="button" className="icon-clear" onClick={() => onIcon(null)}>
          Clear
        </button>
      )}
    </div>
  );
}
