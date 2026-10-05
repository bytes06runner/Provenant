"use client";

// Official PayPal button (JS SDK v6, proven in spike S8). The order already exists on the
// server with its custom_id binding; the button only collects the buyer's approval, and the
// server authorizes after onApprove.
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui";
import { api } from "@/lib/api";

type Sdk = {
  createInstance(opts: { clientToken: string; components: string[]; pageType: string }): Promise<{
    findEligibleMethods(opts: { currencyCode: string }): Promise<{ isEligible(m: string): boolean }>;
    createPayPalOneTimePaymentSession(handlers: {
      onApprove(data: { orderId: string }): Promise<void>;
      onCancel(): void;
      onError(err: unknown): void;
    }): { start(opts: { presentationMode: string }, order: Promise<{ orderId: string }>): Promise<void> };
  }>;
};

declare global {
  interface Window {
    paypal?: Sdk;
  }
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace React.JSX {
    interface IntrinsicElements {
      "paypal-button": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & { type?: string };
    }
  }
}

function loadSdk(src: string): Promise<Sdk> {
  return new Promise((resolve, reject) => {
    if (window.paypal) return resolve(window.paypal);
    const s = document.createElement("script");
    s.src = src;
    s.async = true;
    s.onload = () => (window.paypal ? resolve(window.paypal) : reject(new Error("PayPal SDK did not load")));
    s.onerror = () => reject(new Error("PayPal SDK did not load"));
    document.head.appendChild(s);
  });
}

export function PayPalApprove({
  sessionId,
  orderId,
  merchant,
  approvalUrl,
  sdkUrl,
  presentationMode,
  onAuthorized,
}: {
  sessionId: string;
  orderId: string;
  merchant: string;
  approvalUrl: string | null;
  sdkUrl: string;
  presentationMode: string;
  onAuthorized: (r: { authorization_id: string; status: string }) => void;
}) {
  const button = useRef<HTMLElement | null>(null);
  const [status, setStatus] = useState("Loading the PayPal button...");
  const [ready, setReady] = useState(false);
  const [fallback, setFallback] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function init() {
      try {
        const [sdk, { clientToken }] = await Promise.all([loadSdk(sdkUrl), api.clientToken(merchant)]);
        const instance = await sdk.createInstance({ clientToken, components: ["paypal-payments"], pageType: "checkout" });
        const methods = await instance.findEligibleMethods({ currencyCode: "USD" });
        if (!methods.isEligible("paypal")) throw new Error("PayPal is not eligible for this session");
        const session = instance.createPayPalOneTimePaymentSession({
          async onApprove() {
            setStatus("Approved. Authorizing the payment...");
            try {
              onAuthorized(await api.authorize(sessionId));
            } catch (e) {
              setStatus(`Authorization failed: ${e instanceof Error ? e.message : e}`);
            }
          },
          onCancel() {
            setStatus("You closed the PayPal window. The order is still waiting for approval.");
          },
          onError(err) {
            setStatus(`PayPal reported an error: ${String(err)}`);
            setFallback(true);
          },
        });
        if (cancelled || !button.current) return;
        button.current.addEventListener("click", () => {
          // The order promise is passed at once so popup blockers allow the PayPal window.
          session.start({ presentationMode }, Promise.resolve({ orderId })).catch((e) => setStatus(String(e)));
        });
        setReady(true);
        setStatus("Pay with your PayPal sandbox buyer account.");
      } catch (e) {
        if (!cancelled) {
          setStatus(`${e instanceof Error ? e.message : e}. You can approve on PayPal's page instead.`);
          setFallback(true);
        }
      }
    }
    init();
    return () => {
      cancelled = true;
    };
  }, [sdkUrl, merchant, orderId, presentationMode, sessionId, onAuthorized]);

  return (
    <div className="space-y-3">
      <paypal-button ref={button} type="pay" hidden={!ready} />
      <p className="text-sm text-muted">{status}</p>
      {fallback && approvalUrl && (
        <div className="flex flex-wrap gap-2">
          <a href={approvalUrl} target="_blank" rel="noreferrer" className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-surface">
            Approve on PayPal
          </a>
          <Button
            variant="ghost"
            onClick={async () => {
              setStatus("Checking with PayPal...");
              try {
                onAuthorized(await api.authorize(sessionId));
              } catch (e) {
                setStatus(`Not approved yet: ${e instanceof Error ? e.message : e}`);
              }
            }}
          >
            I have approved it
          </Button>
        </div>
      )}
    </div>
  );
}
