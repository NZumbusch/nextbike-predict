/**
 * Minimaler D1-Ersatz über node:sqlite – nur für Tests (prepare/bind/all/first/run/batch).
 * Wendet alle Migrations an.
 */
import { DatabaseSync } from "node:sqlite";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

class Stmt {
  constructor(private db: DatabaseSync, private sql: string, private params: unknown[] = []) {}
  bind(...p: unknown[]) {
    return new Stmt(this.db, this.sql, p);
  }
  private conv() {
    return this.params.map((v) => (v === undefined ? null : v)) as (string | number | null)[];
  }
  async all<T>() {
    return { results: this.db.prepare(this.sql).all(...this.conv()) as T[], success: true, meta: {} };
  }
  async first<T>() {
    return (this.db.prepare(this.sql).get(...this.conv()) as T) ?? null;
  }
  async run() {
    const r = this.db.prepare(this.sql).run(...this.conv());
    return { success: true, meta: { changes: Number(r.changes) }, results: [] };
  }
  runSync() {
    return this.db.prepare(this.sql).run(...this.conv());
  }
}

export function createD1(): D1Database & { raw: DatabaseSync } {
  const db = new DatabaseSync(":memory:");
  const dir = join(import.meta.dirname, "..", "migrations");
  for (const f of readdirSync(dir).sort()) db.exec(readFileSync(join(dir, f), "utf8"));
  const d1 = {
    raw: db,
    prepare: (sql: string) => new Stmt(db, sql),
    async batch(stmts: Stmt[]) {
      db.exec("BEGIN");
      try {
        const out = stmts.map((s) => s.runSync());
        db.exec("COMMIT");
        return out;
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    },
  };
  return d1 as unknown as D1Database & { raw: DatabaseSync };
}
