/** Caminhos do Windows e do Linux na interface. */
import { describe, expect, test } from "vitest";
import { vscodeHref } from "../components/CodeViewer";
import { samePath } from "./Layout";

describe("caminhos nos dois sistemas", () => {
  test("link do VS Code", () => {
    expect(vscodeHref("C:\\caminho\\e-cidade\\", "forms/a.php", 12)).toBe("vscode://file/C:/caminho/e-cidade/forms/a.php:12");
    expect(vscodeHref("/caminho/do/e-cidade/", "forms/a.php", 12)).toBe("vscode://file/caminho/do/e-cidade/forms/a.php:12");
    expect(vscodeHref("/caminho/do/e-cidade", "a.php")).toBe("vscode://file/caminho/do/e-cidade/a.php");
  });

  test("mesma pasta: maiúsculas só são ignoradas no Windows", () => {
    expect(samePath("C:\\Caminho\\", "c:/caminho")).toBe(true);
    expect(samePath("/caminho/do/e-cidade/", "/caminho/do/e-cidade")).toBe(true);
    expect(samePath("/caminho/do/E-cidade", "/caminho/do/e-cidade")).toBe(false);
    expect(samePath("D:/a", "D:/b")).toBe(false);
  });
});
