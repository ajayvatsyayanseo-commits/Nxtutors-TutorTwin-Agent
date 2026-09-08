import { redirect } from "next/navigation";

import { LoginForm } from "./login-form";
import { currentActor } from "@/lib/auth-actions";

export const metadata = { title: "Sign in · TutorTwin" };

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ changed?: string }>;
}) {
  // Already signed in? Send them on rather than showing a form that would
  // replace a working session with the same one.
  if (await currentActor()) redirect("/");

  const params = await searchParams;
  return (
    <div className="login-shell">
      <div className="login-card">
        <h1>TutorTwin control plane</h1>
        <p className="subtitle" style={{ marginBottom: 0 }}>
          Operational access. All actions are recorded against your account.
        </p>
        {params.changed ? (
          <div className="alert alert-success" role="status" style={{ marginTop: 16 }}>
            Password changed. Sign in with your new password.
          </div>
        ) : null}
        <LoginForm />
      </div>
    </div>
  );
}
