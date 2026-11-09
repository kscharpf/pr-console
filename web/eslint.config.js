// ESLint flat config: type-aware typescript-eslint (strict and stylistic) and the React hooks rules.
// Lints web/src and web/tests (npm run lint); the build output and the config files are left out.
import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "public", "playwright-report", "test-results", "*.config.*"] },
  {
    files: ["src/**/*.{ts,tsx}", "tests/**/*.ts"],
    extends: [js.configs.recommended, tseslint.configs.strictTypeChecked, tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Numbers in template strings (`#${n}`, `${count} more`) are the common case here and always print as intended.
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
      // `() => setX(v)` handlers return the setter's void; the shorthand is idiomatic React and hides nothing.
      "@typescript-eslint/no-confusing-void-expression": ["error", { ignoreArrowShorthand: true }],
      // `s || "fallback"` on strings deliberately treats "" as missing; `??` would change that.
      "@typescript-eslint/prefer-nullish-coalescing": ["error", { ignorePrimitives: { string: true } }],
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    extends: [reactHooks.configs.flat.recommended],
  },
  {
    files: ["tests/**/*.ts"],
    rules: {
      // In a Playwright test a missing element or box is a failure: the `!` makes the test throw, which is the check.
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
);
