"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { CheckCircle2, AlertCircle, Loader2, ArrowRight } from "lucide-react";
import { api } from "../../../components/api";

function CallbackHandler() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [message, setMessage] = useState("Exchanging authorization code with Upstox...");

  useEffect(() => {
    const code = searchParams.get("code");
    const state = searchParams.get("state");
    const errorParam = searchParams.get("error");
    const errorDescription = searchParams.get("error_description");

    if (errorParam) {
      setStatus("error");
      setMessage(errorDescription || errorParam || "Upstox authorization was cancelled or failed");
      return;
    }

    if (!code || !state) {
      setStatus("error");
      setMessage("Missing authorization code or state in callback URL");
      return;
    }

    let active = true;

    async function handleExchange() {
      try {
        const result = await api.completeUpstoxOAuth(code!, state!);
        if (!active) return;
        setStatus("success");
        setMessage(`Upstox access token renewed successfully! Session active until ${new Date(result.expires_at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST.`);
      } catch (err) {
        if (!active) return;
        setStatus("error");
        setMessage(err instanceof Error ? err.message : "Failed to exchange Upstox authorization code");
      }
    }

    void handleExchange();

    return () => {
      active = false;
    };
  }, [searchParams]);

  return (
    <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-terminal-900 p-8 shadow-2xl shadow-black/40">
      <div className="flex items-center gap-3 text-sm font-semibold tracking-[.2em] text-bullish mb-6">
        <span className="h-2.5 w-2.5 rounded-full bg-bullish" /> SIDRA ALGO
      </div>

      {status === "loading" && (
        <div className="space-y-4 text-center py-6">
          <Loader2 className="h-10 w-10 animate-spin text-emerald-400 mx-auto" />
          <h2 className="text-xl font-semibold text-white">Renewing Upstox Session</h2>
          <p className="text-sm text-slate-400">{message}</p>
        </div>
      )}

      {status === "success" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 text-emerald-400">
            <CheckCircle2 className="h-8 w-8 flex-shrink-0" />
            <h2 className="text-xl font-semibold text-white">Authorization Successful</h2>
          </div>
          <p className="text-sm text-slate-300 leading-6">{message}</p>
          <button
            onClick={() => router.replace("/")}
            className="primary-button mt-6 w-full"
          >
            Return to Dashboard
            <ArrowRight className="h-4 w-4" />
          </button>
        </div>
      )}

      {status === "error" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 text-rose-400">
            <AlertCircle className="h-8 w-8 flex-shrink-0" />
            <h2 className="text-xl font-semibold text-white">Authorization Failed</h2>
          </div>
          <p className="rounded-md border border-rose-900 bg-rose-950/40 p-3 text-sm text-rose-300 leading-6">
            {message}
          </p>
          <button
            onClick={() => router.replace("/")}
            className="secondary-button mt-6 w-full justify-center"
          >
            Return to Dashboard
          </button>
        </div>
      )}
    </div>
  );
}

export default function UpstoxCallbackPage() {
  return (
    <main className="login-stage flex min-h-screen items-center justify-center p-5 sm:p-8">
      <Suspense
        fallback={
          <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-terminal-900 p-8 text-center">
            <Loader2 className="h-10 w-10 animate-spin text-emerald-400 mx-auto mb-4" />
            <p className="text-sm text-slate-400">Loading callback...</p>
          </div>
        }
      >
        <CallbackHandler />
      </Suspense>
    </main>
  );
}
