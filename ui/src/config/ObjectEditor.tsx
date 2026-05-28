// Recursive editor that renders every field of the effective config so all core
// options are reachable. Primitives become inputs; nested objects become
// fieldsets; arrays are edited as JSON for fidelity with complex `sources` items.

interface ObjectEditorProps {
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  path?: string;
}

export function ObjectEditor({ value, onChange, path = "" }: ObjectEditorProps) {
  const setKey = (key: string, next: unknown) => onChange({ ...value, [key]: next });

  return (
    <div className="obj-editor">
      {Object.entries(value).map(([key, child]) => {
        const fieldId = path ? `${path}.${key}` : key;
        if (child !== null && typeof child === "object" && !Array.isArray(child)) {
          return (
            <fieldset className="obj-group" key={fieldId}>
              <legend>{key}</legend>
              <ObjectEditor
                value={child as Record<string, unknown>}
                onChange={(next) => setKey(key, next)}
                path={fieldId}
              />
            </fieldset>
          );
        }
        return (
          <FieldRow
            key={fieldId}
            name={key}
            value={child}
            onChange={(next) => setKey(key, next)}
          />
        );
      })}
    </div>
  );
}

function FieldRow({
  name,
  value,
  onChange,
}: {
  name: string;
  value: unknown;
  onChange: (next: unknown) => void;
}) {
  if (typeof value === "boolean") {
    return (
      <label className="field field--inline">
        <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
        <span>{name}</span>
      </label>
    );
  }
  if (typeof value === "number") {
    return (
      <label className="field field--inline">
        <span>{name}</span>
        <input
          className="text-input text-input--small"
          type="number"
          value={value}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        />
      </label>
    );
  }
  if (Array.isArray(value)) {
    return (
      <label className="field">
        <span>{name} (JSON array)</span>
        <textarea
          className="text-input code"
          rows={Math.min(10, Math.max(2, JSON.stringify(value, null, 2).split("\n").length))}
          defaultValue={JSON.stringify(value, null, 2)}
          onBlur={(e) => {
            try {
              onChange(JSON.parse(e.target.value));
            } catch {
              /* keep previous value on invalid JSON */
            }
          }}
        />
      </label>
    );
  }
  return (
    <label className="field field--inline">
      <span>{name}</span>
      <input
        className="text-input"
        value={value === null || value === undefined ? "" : String(value)}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}
