import { useEffect, useRef, useState } from "react";
import { api, fileUrl } from "../api";
import { ErrorNote, Skeleton, Tag } from "./ui";

export type SourceRef = { passage_id: number; quote?: string; page?: number | null };

function Highlighted({ text, quote }: { text: string; quote?: string }) {
  if (!quote) return <>{text}</>;
  const flat = (s: string) => s.replace(/\s+/g, " ");
  const at = flat(text).toLowerCase().indexOf(flat(quote).toLowerCase().replace(/[."]+$/, ""));
  if (at < 0) return <>{flat(text)}</>;
  const body = flat(text);
  const len = flat(quote).replace(/[."]+$/, "").length;
  return <>{body.slice(0, at)}<mark>{body.slice(at, at + len)}</mark>{body.slice(at + len)}</>;
}

/** Read a cited passage in its context, in any format, with a link to the file at the page where one exists. */
export function SourceDialog({ source, onClose }: { source: SourceRef; onClose: () => void }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
    api(`/passages/${source.passage_id}`).then(setData).catch((e) => setError(e.message));
  }, [source.passage_id]);
  const current = data?.passages.find((p: any) => p.id === source.passage_id);
  const page = current?.page ?? source.page;
  return (
    <dialog ref={ref} className="dialog wide" onClose={onClose} aria-labelledby="source-title">
      <header className="dialog-head">
        <div>
          <h2 id="source-title">{data?.document.filename ?? "Source"}</h2>
          <div className="small muted">{current?.section ?? ""}{page ? ` · page ${page}` : ""}</div>
        </div>
        <button className="btn ghost sm" onClick={() => ref.current?.close()} aria-label="Close">Close</button>
      </header>
      <ErrorNote error={error} />
      {!data && !error && <Skeleton lines={4} />}
      {data && <div className="source-body">
        {data.passages.map((p: any) => (
          <section key={p.id} className={p.id === source.passage_id ? "source-passage cited" : "source-passage"}>
            {p.section && <div className="label">{p.section}{p.page ? ` · page ${p.page}` : ""}</div>}
            <p>{p.id === source.passage_id ? <Highlighted text={p.text} quote={source.quote} /> : p.text}</p>
          </section>
        ))}
      </div>}
      {data && <footer className="dialog-foot">
        {data.document.origin === "upload" && <Tag>Private to your workspace</Tag>}
        {data.document.has_file ? (
          <a className="btn" href={`${fileUrl(data.document.id)}${page ? `#page=${page}` : ""}`} target="_blank" rel="noreferrer">Open the file{page ? ` at page ${page}` : ""}</a>
        ) : <span className="small muted">Taken from the notice's own fields; there is no separate file.</span>}
      </footer>}
    </dialog>
  );
}
