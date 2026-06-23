import { useCallback, useRef, useState } from 'react';

function formatBytes(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function Uploader({
  accept,
  file,
  onFile,
  label = 'Upload',
  required = false,
  hint,
}) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);

  const triggerInput = () => inputRef.current?.click();

  const handleFiles = useCallback(
    (files) => {
      if (!files || !files.length) return;
      onFile(files[0]);
    },
    [onFile],
  );

  const onDragOver = (e) => {
    e.preventDefault();
    setDragging(true);
  };
  const onDragLeave = (e) => {
    e.preventDefault();
    setDragging(false);
  };
  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    handleFiles(e.dataTransfer?.files);
  };

  return (
    <div className="field field--full">
      <div className="label-row">
        <label>
          {label}
          {required && <span className="required">*</span>}
        </label>
        {hint && <span className="hint">{hint}</span>}
      </div>

      <div
        className={`dropzone${dragging ? ' dragging' : ''}`}
        onClick={triggerInput}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            triggerInput();
          }
        }}
      >
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          hidden
          onChange={(e) => handleFiles(e.target.files)}
        />
        <strong>{dragging ? 'Drop to upload' : 'Drag & drop or click to browse'}</strong>
        <span>Accepted file type: {accept}</span>
        {file && (
          <div className="file-preview">
            <span>📦 <strong>{file.name}</strong></span>
            <span className="file-meta">· {formatBytes(file.size)}</span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onFile(null);
              }}
            >
              Clear
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
