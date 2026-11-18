import type { CodegenConfig } from "@graphql-codegen/cli";

// Tipos do front a partir do schema versionado (`php-map ui --print-schema`).
const config: CodegenConfig = {
  schema: "./schema.graphql",
  documents: ["src/**/*.{ts,tsx}", "!src/graphql/generated/**"],
  ignoreNoDocuments: true,
  generates: {
    "./src/graphql/generated/": {
      preset: "client",
      config: {
        useTypeImports: true,
        enumsAsTypes: true,
        scalars: { JSON: "unknown" },
      },
      presetConfig: { fragmentMasking: false },
    },
  },
};

export default config;
