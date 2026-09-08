import coreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

/**
 * Next's recommended rules plus its TypeScript set.
 *
 * Imported as flat configs directly. `FlatCompat` is for legacy `.eslintrc`
 * shareables; eslint-config-next 16 already ships flat, and wrapping it in the
 * compat layer makes the loader try to serialise a plugin object that contains
 * itself.
 *
 * `no-restricted-imports` is the one custom rule, and it protects the security
 * design: `lib/api` and `lib/session` read the session cookie and must never be
 * pulled into a client bundle. The `server-only` package already fails the build
 * if that happens; this turns the failure into a lint error that says why.
 */
const config = [
  { ignores: [".next/**", "node_modules/**", "e2e/**", "playwright-report/**", "test-results/**"] },
  ...coreWebVitals,
  ...nextTypescript,
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
      eqeqeq: ["error", "always"],
      "no-console": ["warn", { allow: ["error", "warn"] }],
    },
  },
  {
    files: ["src/components/**/*.tsx"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["@/lib/api", "@/lib/session"],
              message:
                "Components must not read the session or call the API directly. Pass data down from a server component.",
            },
          ],
        },
      ],
    },
  },
];

export default config;