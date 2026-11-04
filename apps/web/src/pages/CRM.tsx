import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { AtSign, Briefcase, FolderKanban, Mail, MessageCircle, Phone, Plus, Trash2, Users } from "lucide-react";
import { useState } from "react";
import { Badge, Button, Card, cn, Drawer, Empty, ErrorBox, Field, Input, Modal, PageHeader, Select, Skeleton, Tabs, Textarea } from "../components/ui";
import { del, get, patch, post } from "../lib/api";
import { faInt, fmtDate, relTime } from "../lib/format";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

const KIND: Record<string, { l: string; tone: any }> = {
  colleague: { l: "همکار", tone: "primary" },
  client: { l: "مشتری", tone: "success" },
  contact: { l: "مخاطب", tone: "neutral" },
  vendor: { l: "تأمین‌کننده", tone: "accent" },
};
const CHANNEL: Record<string, string> = { telegram: "تلگرام", call: "تماس", meeting: "جلسه", email: "ایمیل", note: "یادداشت", web: "وب" };

function ContactForm({ c, onClose }: { c?: any; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<any>(c || { kind: "colleague", name: "" });
  const set = (k: string, v: any) => setF((x: any) => ({ ...x, [k]: v }));
  const save = useMutation({
    mutationFn: () => {
      const body = { kind: f.kind, name: f.name, role_title: f.role_title, company: f.company, phone: f.phone, email: f.email, telegram_username: f.telegram_username, notes: f.notes };
      return c ? patch(`/api/crm/contacts/${c.id}`, body) : post("/api/crm/contacts", body);
    },
    onSuccess: () => { toast("ذخیره شد", "success"); qc.invalidateQueries({ queryKey: ["contacts"] }); onClose(); },
    onError: toastError,
  });
  return (
    <Modal open onClose={onClose} title={c ? "ویرایش مخاطب" : "مخاطب جدید"} footer={<><Button variant="ghost" onClick={onClose}>انصراف</Button><Button variant="primary" disabled={!f.name} loading={save.isPending} onClick={() => save.mutate()}>ذخیره</Button></>}>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="نام"><Input value={f.name || ""} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="نوع"><Select value={f.kind} onChange={(e) => set("kind", e.target.value)}>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v.l}</option>)}</Select></Field>
        <Field label="سمت"><Input value={f.role_title || ""} onChange={(e) => set("role_title", e.target.value)} /></Field>
        <Field label="شرکت"><Input value={f.company || ""} onChange={(e) => set("company", e.target.value)} /></Field>
        <Field label="تلفن"><Input value={f.phone || ""} onChange={(e) => set("phone", e.target.value)} className="ltr" /></Field>
        <Field label="ایمیل"><Input value={f.email || ""} onChange={(e) => set("email", e.target.value)} className="ltr" /></Field>
        <Field label="یوزرنیم تلگرام" hint="پیام‌های این فرد خودکار به سوابق CRM متصل می‌شود."><Input value={f.telegram_username || ""} onChange={(e) => set("telegram_username", e.target.value)} className="ltr" placeholder="@username" /></Field>
        <Field label="یادداشت" className="sm:col-span-2"><Textarea value={f.notes || ""} onChange={(e) => set("notes", e.target.value)} /></Field>
      </div>
    </Modal>
  );
}

function ContactDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["contact", id], queryFn: () => get(`/api/crm/contacts/${id}`) });
  const [note, setNote] = useState("");
  const [channel, setChannel] = useState("note");
  const [edit, setEdit] = useState(false);
  const add = async () => {
    if (!note.trim()) return;
    await post("/api/crm/interactions", { contact_id: id, summary: note, channel, direction: "out" }).catch(toastError);
    setNote("");
    qc.invalidateQueries({ queryKey: ["contact", id] });
  };
  const c = data?.contact;
  return (
    <Drawer open onClose={onClose} title={c?.name || "…"}>
      {isLoading || !c ? <Skeleton className="h-40" /> : (
        <div className="space-y-5">
          <div className="flex items-center gap-3">
            <div className="grid h-14 w-14 place-items-center rounded-[6px] bg-primary/12 text-primary ring-1 ring-inset ring-primary/20 text-xl font-bold">{c.name[0]}</div>
            <div className="min-w-0 flex-1">
              <div className="font-semibold">{c.name}</div>
              <div className="text-xs text-muted">{[c.role_title, c.company].filter(Boolean).join(" · ") || KIND[c.kind]?.l}</div>
            </div>
            <Button size="sm" variant="soft" onClick={() => setEdit(true)}>ویرایش</Button>
          </div>
          <div className="grid gap-2 text-sm">
            {c.phone && <div className="flex items-center gap-2"><Phone className="h-4 w-4 text-muted" /><span className="ltr">{c.phone}</span></div>}
            {c.email && <div className="flex items-center gap-2"><Mail className="h-4 w-4 text-muted" /><span className="ltr">{c.email}</span></div>}
            {c.telegram_username && <div className="flex items-center gap-2"><AtSign className="h-4 w-4 text-muted" /><span className="ltr">@{c.telegram_username}</span></div>}
            {c.notes && <p className="rounded-[5px] bg-line/5 p-3 text-xs leading-6 text-muted">{c.notes}</p>}
          </div>
          <div>
            <h4 className="mb-2 text-sm font-semibold">ثبت پیگیری</h4>
            <div className="flex gap-2">
              <Select value={channel} onChange={(e) => setChannel(e.target.value)} className="w-28">{Object.entries(CHANNEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>
              <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="خلاصه تماس یا پیگیری…" onKeyDown={(e) => e.key === "Enter" && add()} />
              <Button variant="primary" onClick={add}>ثبت</Button>
            </div>
          </div>
          <div>
            <h4 className="mb-2 text-sm font-semibold">سوابق ({faInt(data.interactions.length)})</h4>
            <ol className="space-y-3 border-s hairline ps-4">
              {data.interactions.length === 0 && <p className="text-xs text-muted">سابقه‌ای ثبت نشده است.</p>}
              {data.interactions.map((it: any) => (
                <li key={it.id} className="relative">
                  <span className="absolute -start-[21px] top-1.5 h-2.5 w-2.5 rounded-[2px] bg-primary ring-4 ring-bg" />
                  <div className="flex items-center gap-2 text-[11px] text-muted"><Badge tone={it.direction === "in" ? "cyan" : "accent"}>{CHANNEL[it.channel] || it.channel}</Badge>{relTime(it.created_at)}</div>
                  <p className="mt-1 text-sm leading-6">{it.summary}</p>
                  {it.follow_up_at && <p className="text-[11px] text-warning">پیگیری بعدی: {fmtDate(it.follow_up_at)}</p>}
                </li>
              ))}
            </ol>
          </div>
          {data.tasks.length > 0 && (
            <div>
              <h4 className="mb-2 text-sm font-semibold">تسک‌های محول‌شده</h4>
              {data.tasks.map((t: any) => <div key={t.id} className="flex justify-between border-b hairline py-2 text-sm"><span>{t.title}</span><Badge>{t.status}</Badge></div>)}
            </div>
          )}
        </div>
      )}
      {edit && c && <ContactForm c={c} onClose={() => { setEdit(false); qc.invalidateQueries({ queryKey: ["contact", id] }); }} />}
    </Drawer>
  );
}

function Projects() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["projects"], queryFn: () => get("/api/crm/projects") });
  const [name, setName] = useState("");
  const add = async () => {
    if (!name.trim()) return;
    await post("/api/crm/projects", { name }).catch(toastError);
    setName("");
    qc.invalidateQueries({ queryKey: ["projects"] });
  };
  return (
    <div className="space-y-4">
      <div className="flex max-w-md gap-2"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="نام پروژه جدید" onKeyDown={(e) => e.key === "Enter" && add()} /><Button variant="primary" onClick={add} icon={<Plus className="h-4 w-4" />}>افزودن</Button></div>
      {isLoading ? <Skeleton className="h-32" /> : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {(data?.items || []).map((p: any) => (
            <Card key={p.id} className="p-4">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 font-semibold"><span className="h-3 w-3 rounded-[2px]" style={{ background: p.color }} />{p.name}</span>
                <Select value={p.status} onChange={async (e) => { await patch(`/api/crm/projects/${p.id}`, { status: e.target.value }); qc.invalidateQueries({ queryKey: ["projects"] }); }} className="h-8 w-28 text-xs">
                  <option value="active">فعال</option><option value="on_hold">معلق</option><option value="done">تمام‌شده</option><option value="archived">بایگانی</option>
                </Select>
              </div>
              <p className="mt-2 text-xs text-muted">{faInt(p.task_count)} تسک</p>
            </Card>
          ))}
          {(data?.items || []).length === 0 && <p className="text-sm text-muted">پروژه‌ای ثبت نشده است.</p>}
        </div>
      )}
    </div>
  );
}

export default function CRM() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"contacts" | "projects">("contacts");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const { data, isLoading, error, refetch } = useQuery({ queryKey: ["contacts", q, kind], queryFn: () => get(`/api/crm/contacts?q=${encodeURIComponent(q)}${kind ? `&kind=${kind}` : ""}`) });
  useLive((_d, t) => t === "crm.changed" && qc.invalidateQueries({ queryKey: ["contacts"] }));
  return (
    <div>
      <PageHeader title="همکاران و CRM" sub="پروفایل همکاران، مخاطبان، پروژه‌ها و سوابق پیگیری" icon={<Users className="h-5 w-5" />}
        actions={tab === "contacts" && <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={() => setCreating(true)}>مخاطب جدید</Button>} />
      <Tabs value={tab} onChange={setTab} tabs={[{ value: "contacts", label: "مخاطبان", icon: <Users className="h-4 w-4" /> }, { value: "projects", label: "پروژه‌ها", icon: <FolderKanban className="h-4 w-4" /> }]} />
      <div className="mt-4">
        {tab === "projects" ? <Projects /> : (
          <>
            <div className="mb-4 flex flex-wrap gap-2">
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="جست‌وجو با نام، شرکت، تلفن یا یوزرنیم…" className="max-w-sm" />
              <Select value={kind} onChange={(e) => setKind(e.target.value)} className="w-36"><option value="">همه</option>{Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v.l}</option>)}</Select>
            </div>
            {error && <ErrorBox error={error} retry={refetch} />}
            {isLoading ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{[1, 2, 3].map((i) => <Skeleton key={i} className="h-28" />)}</div> : (
              (data?.items || []).length === 0 ? <Card><Empty icon={<Users className="h-6 w-6" />} title="مخاطبی ثبت نشده" text="همکاران را اضافه کنید تا ایجنت پیام‌ها و تسک‌ها را به آن‌ها متصل کند." /></Card> : (
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {(data?.items || []).map((c: any, i: number) => (
                    <motion.button key={c.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.03 }} onClick={() => setOpen(c.id)}
                      className="die group flex items-center gap-3 p-4 text-start transition">
                      <div className="grid h-12 w-12 shrink-0 place-items-center rounded-[6px] bg-primary/12 text-primary ring-1 ring-inset ring-primary/20 text-lg font-bold">{c.name[0]}</div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2"><span className="truncate font-medium">{c.name}</span><Badge tone={KIND[c.kind]?.tone}>{KIND[c.kind]?.l}</Badge></div>
                        <div className="mt-0.5 truncate text-xs text-muted">{[c.role_title, c.company].filter(Boolean).join(" · ") || "—"}</div>
                        <div className="mt-1 flex items-center gap-3 text-[11px] text-muted">
                          {c.telegram_username && <span className="flex items-center gap-1"><MessageCircle className="h-3 w-3" />@{c.telegram_username}</span>}
                          {c.last_contact_at && <span className="flex items-center gap-1"><Briefcase className="h-3 w-3" />{relTime(c.last_contact_at)}</span>}
                        </div>
                      </div>
                      <span role="button" tabIndex={0} className={cn("opacity-0 transition group-hover:opacity-100")} onClick={async (e) => { e.stopPropagation(); if (!confirm("حذف شود؟")) return; await del(`/api/crm/contacts/${c.id}`); qc.invalidateQueries({ queryKey: ["contacts"] }); }}>
                        <Trash2 className="h-4 w-4 text-muted hover:text-danger" />
                      </span>
                    </motion.button>
                  ))}
                </div>
              )
            )}
          </>
        )}
      </div>
      {open && <ContactDetail id={open} onClose={() => setOpen(null)} />}
      {creating && <ContactForm onClose={() => setCreating(false)} />}
    </div>
  );
}
