import { Prism } from "prism-react-renderer";

/**
 * O `prism-react-renderer` já traz SQL, JavaScript, HTML e CSS; o PHP vem do `prismjs`,
 * carregado sob demanda (só quando um arquivo `.php` é aberto).
 */
const LANGUAGES: Record<string, string> = {
  php: "php",
  inc: "php",
  sql: "sql",
  js: "javascript",
  mjs: "javascript",
  html: "markup",
  htm: "markup",
  xml: "markup",
  css: "css",
  json: "json",
  md: "markdown",
  yml: "yaml",
  yaml: "yaml",
};

export function languageOf(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return LANGUAGES[extension] ?? "plain";
}

let phpLoading: Promise<void> | null = null;

export function loadLanguage(language: string): Promise<void> {
  if (language !== "php" || Prism.languages.php) return Promise.resolve();
  phpLoading ??= (async () => {
    // Os componentes do prismjs registram a linguagem no `Prism` global.
    (globalThis as { Prism?: unknown }).Prism = Prism;
    await import("prismjs/components/prism-markup-templating");
    await import("prismjs/components/prism-php");
  })();
  return phpLoading;
}
