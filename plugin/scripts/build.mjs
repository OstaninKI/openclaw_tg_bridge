import { mkdir, readFile, writeFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = resolve(root, "index.ts");
const outputPath = resolve(root, "dist/index.js");

const source = await readFile(sourcePath, "utf8");
const js = stripTypeScriptTypes(source);

await mkdir(dirname(outputPath), { recursive: true });
await writeFile(
  outputPath,
  `// Generated from index.ts by scripts/build.mjs. Do not edit by hand.\n${js}`,
  "utf8"
);
