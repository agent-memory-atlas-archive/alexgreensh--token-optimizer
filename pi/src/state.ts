import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { constants, closeSync, fstatSync, lstatSync, mkdirSync, openSync, readFileSync, writeFileSync, renameSync } from "node:fs";

export type Settings = {
  enabled: boolean;
  retainDays: number;
  archiveToolOutput: boolean;
  continuity: boolean;
};
export const DEFAULT_SETTINGS: Settings = { enabled: false, retainDays: 7, archiveToolOutput: false, continuity: false };
function privateStat(path: string, file: boolean, stat = lstatSync(path)): void {
  if (stat.isSymbolicLink() || (file ? !stat.isFile() : !stat.isDirectory())) throw new Error("Local data path is not a regular file or directory");
  if (process.platform !== "win32" && ((stat.mode & 0o077) !== 0 || stat.uid !== process.getuid?.())) throw new Error("Local data path is not private");
}
export function ensurePrivateDir(path: string): void {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  assertPrivateDir(path);
}
export function assertPrivateDir(path: string): void { privateStat(path, false); }
export function readPrivateFile(path: string, maxBytes: number): string {
  privateStat(path, true);
  const fd = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const stat = fstatSync(fd);
    privateStat(path, true, stat);
    if (stat.size > maxBytes) throw new Error("Local data file exceeds size limit");
    return readFileSync(fd, "utf8");
  } finally { closeSync(fd); }
}
export function dataDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.TOKEN_OPTIMIZER_PI_HOME || join(homedir(), ".pi", "agent", "token-optimizer");
}
export function readSettings(root = dataDir()): Settings {
  try {
    const data: unknown = JSON.parse(readPrivateFile(join(root, "settings.json"), 16 * 1024));
    if (!data || typeof data !== "object") return { ...DEFAULT_SETTINGS };
    const v = data as Partial<Settings>;
    return {
      enabled: v.enabled === true,
      retainDays: Number.isSafeInteger(v.retainDays) && v.retainDays! >= 1 && v.retainDays! <= 365 ? v.retainDays! : 7,
      archiveToolOutput: v.archiveToolOutput === true,
      continuity: v.continuity === true,
    };
  } catch { return { ...DEFAULT_SETTINGS }; }
}
export function saveSettings(settings: Settings, root = dataDir()): void {
  ensurePrivateDir(root);
  const tmp = join(root, `settings-${randomUUID()}.tmp`);
  writeFileSync(tmp, JSON.stringify(settings, null, 2) + "\n", { mode: 0o600, flag: "wx" });
  renameSync(tmp, join(root, "settings.json"));
}
