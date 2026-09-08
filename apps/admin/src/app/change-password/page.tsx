import { redirect } from "next/navigation";

import { ChangePasswordForm } from "./form";
import { currentActor } from "@/lib/auth-actions";

export const metadata = { title: "Change password · TutorTwin" };

export default async function ChangePasswordPage() {
  const actor = await currentActor();
  if (!actor) redirect("/login");

  return (
    <div className="login-shell">
      <div className="login-card">
        <h1>Change password</h1>
        {actor.must_change_password ? (
          <div className="alert alert-warning" role="status" style={{ marginTop: 16 }}>
            Your account still uses the password it was created with. That value was
            printed to a terminal, so it must be replaced before you can use the
            control plane.
          </div>
        ) : (
          <p className="subtitle">
            Changing your password signs you out of every session, including this one.
          </p>
        )}
        <ChangePasswordForm />
      </div>
    </div>
  );
}
