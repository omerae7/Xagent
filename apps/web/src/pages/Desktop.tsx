import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, Hand, Maximize2, Monitor, MousePointerClick, RefreshCw, ShieldCheck, Undo2, WifiOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Badge, Button, Card, PageHeader } from "../components/ui";
import { Thinking } from "../components/viz/Orb";
import { get, post } from "../lib/api";
import { useLive } from "../lib/live";
import { toast, toastError } from "../lib/store";

export default function Desktop() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["desktop"], queryFn: () => get("/api/desktop/status"), refetchInterval: 15000 });
  const takeover = !!data?.takeover;
  const healthy = data?.health?.ok;
  const box = useRef<HTMLDivElement>(null);
  const rfbRef = useRef<any>(null);
  const [conn, setConn] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const [shot, setShot] = useState<string | null>(null);
  useLive((_d, t) => t === "desktop.takeover" && qc.invalidateQueries({ queryKey: ["desktop"] }));

  useEffect(() => {
    if (!box.current || data === undefined) return;
    let cancelled = false;
    (async () => {
      try {
        const { default: RFB } = await import("@novnc/novnc/lib/rfb");
        if (cancelled || !box.current) return;
        box.current.innerHTML = "";
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const rfb = new RFB(box.current, `${proto}://${location.host}/api/desktop/vnc?mode=${takeover ? "control" : "view"}`, { wsProtocols: ["binary"] });
        rfb.viewOnly = !takeover;
        rfb.scaleViewport = true;
        rfb.resizeSession = false;
        rfb.background = "transparent";
        rfb.addEventListener("connect", () => setConn("connected"));
        rfb.addEventListener("disconnect", () => setConn("disconnected"));
        rfbRef.current = rfb;
        setConn("connecting");
      } catch {
        setConn("disconnected");
      }
    })();
    return () => {
      cancelled = true;
      try { rfbRef.current?.disconnect(); } catch { /* اتصال بسته شده */ }
      rfbRef.current = null;
    };
  }, [takeover, data === undefined]);

  const toggle = async () => {
    try {
      await post("/api/desktop/takeover", { on: !takeover });
      toast(!takeover ? "کنترل در دست شماست؛ اقدامات خودکار ایجنت متوقف شد" : "کنترل به ایجنت بازگردانده شد", "success");
      qc.invalidateQueries({ queryKey: ["desktop"] });
    } catch (e) { toastError(e); }
  };
  const screenshot = async () => {
    try {
      const r = await fetch("/api/desktop/screenshot", { credentials: "same-origin" });
      if (!r.ok) throw new Error((await r.json()).detail);
      setShot(URL.createObjectURL(await r.blob()));
    } catch (e) { toastError(e); }
  };

  return (
    <div>
      <PageHeader title="دسکتاپ زنده" sub="کامپیوتر اختصاصی ایجنت (لینوکس XFCE با کروم و تلگرام دسکتاپ)" icon={<Monitor className="h-5 w-5" />} actions={
        <>
          <Button variant="ghost" icon={<Camera className="h-4 w-4" />} onClick={screenshot}>اسکرین‌شات</Button>
          <Button variant="ghost" icon={<Maximize2 className="h-4 w-4" />} onClick={() => box.current?.requestFullscreen?.()}>تمام‌صفحه</Button>
          <Button variant={takeover ? "soft" : "primary"} icon={takeover ? <Undo2 className="h-4 w-4" /> : <Hand className="h-4 w-4" />} onClick={toggle}>
            {takeover ? "بازگرداندن کنترل به ایجنت" : "در دست گرفتن کنترل"}
          </Button>
        </>
      } />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {takeover ? <Badge tone="warning" dot><MousePointerClick className="h-3 w-3" />کنترل در دست شما — کلیک‌های خودکار ایجنت متوقف است</Badge>
          : <Badge tone="primary" dot>حالت مشاهده (فقط نمایش)</Badge>}
        <Badge tone={healthy ? "success" : "danger"} dot>{healthy ? "کامپیوتر ایجنت در دسترس" : "کامپیوتر ایجنت در دسترس نیست"}</Badge>
        <Badge tone={conn === "connected" ? "success" : conn === "connecting" ? "neutral" : "danger"}>{conn === "connected" ? "تصویر زنده متصل" : conn === "connecting" ? "در حال اتصال…" : "اتصال تصویر قطع"}</Badge>
        <Badge tone="cyan"><ShieldCheck className="h-3 w-3" />فقط از طریق پنل احرازشده</Badge>
      </div>
      <Card className="relative overflow-hidden p-1.5">
        <div ref={box} className="relative aspect-video w-full overflow-hidden rounded-[18px] bg-black/80" />
        {conn !== "connected" && (
          <div className="absolute inset-1.5 grid place-items-center rounded-[18px] bg-bg/70 backdrop-blur">
            {conn === "connecting" ? <Thinking label="در حال اتصال به دسکتاپ…" /> : (
              <div className="space-y-3 text-center">
                <WifiOff className="mx-auto h-8 w-8 text-muted" />
                <p className="text-sm text-muted">اتصال به دسکتاپ برقرار نشد. ممکن است کانتینر دسکتاپ در حال راه‌اندازی یا در این نصب غیرفعال باشد.</p>
                <Button variant="soft" icon={<RefreshCw className="h-4 w-4" />} onClick={() => qc.invalidateQueries({ queryKey: ["desktop"] })}>تلاش دوباره</Button>
              </div>
            )}
          </div>
        )}
      </Card>
      {shot && (
        <Card className="mt-4 p-3">
          <div className="mb-2 flex items-center justify-between text-sm"><span>آخرین اسکرین‌شات</span><Button size="sm" variant="ghost" onClick={() => setShot(null)}>بستن</Button></div>
          <img src={shot} alt="اسکرین‌شات دسکتاپ" className="w-full rounded-xl" />
        </Card>
      )}
    </div>
  );
}
